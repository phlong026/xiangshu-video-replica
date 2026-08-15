from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import logging
import socket
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, Protocol, cast
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import uuid4

from fastapi import HTTPException

from app.analysis import insert_version
from app.auth import CurrentUser
from app.character_reference_matching import (
    current_character_reference_selection_for_generation,
)
from app.characters import character_is_available, get_project_main_character, read_character
from app.permissions import (
    require_asset_access,
    require_not_auditor,
    require_project_access,
    write_audit,
)
from app.source_frames import (
    SOURCE_FRAME_CANDIDATES_KIND,
    SOURCE_FRAME_SELECTION_KIND,
    latest_version,
)
from app.storage import (
    StorageAdapter,
    StorageBackendUnavailable,
    require_storage_match,
    storage_object_ref_from_uri,
)

logger = logging.getLogger(__name__)

FIRST_FRAME_CANDIDATES_KIND = "first_frame_candidates"
FIRST_FRAME_SELECTION_KIND = "first_frame_selection"
FIRST_FRAME_SCHEMA_VERSION = "b5.first-frame.v1"
FIRST_FRAME_MODELS = ("gpt-image-2", "nano-banana-pro-2k")
FIRST_FRAME_IMAGE_CONTENT_TYPES = {"image/jpeg", "image/png", "image/webp"}
MAX_FIRST_FRAME_CANDIDATES = 3
APILIO_DEFAULT_BASE_URL = "https://api.apilio.ai"
APILIO_IMAGE_EDIT_PATH = "/v1/images/edits"
MAX_PROVIDER_IMAGE_BYTES = 20 * 1024 * 1024
APILIO_OUTPUT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0 Safari/537.36"
)

FirstFrameModel = Literal["gpt-image-2", "nano-banana-pro-2k"]


@dataclass(frozen=True)
class GeneratedImage:
    content: bytes
    content_type: str


@dataclass(frozen=True)
class ImageInput:
    content: bytes
    content_type: str
    filename: str


@dataclass(frozen=True)
class FirstFrameCharacterInputs:
    main_character_version_id: str
    character_snapshot: dict[str, object]
    reference_asset_ids: list[str]
    character_name: str
    authorized_project_ids: list[str]
    character_reference_selection_id: str | None = None
    character_version_id: str | None = None


class ImageProvider(Protocol):
    provider_name: str

    def edit(
        self,
        *,
        model: FirstFrameModel,
        prompt: str,
        source_image: ImageInput,
        character_reference_images: list[ImageInput],
        output_count: int,
    ) -> list[GeneratedImage]: ...


class ImageProviderFailed(RuntimeError):
    pass


class RetryableImageProviderFailed(ImageProviderFailed):
    pass


class FakeImageProvider:
    provider_name = "fake"

    def edit(
        self,
        *,
        model: FirstFrameModel,
        prompt: str,
        source_image: ImageInput,
        character_reference_images: list[ImageInput],
        output_count: int,
    ) -> list[GeneratedImage]:
        del model, prompt, character_reference_images
        return [
            GeneratedImage(content=source_image.content, content_type=source_image.content_type)
            for _ in range(output_count)
        ]


class ApilioTransport(Protocol):
    def post(
        self, url: str, *, headers: Mapping[str, str], body: bytes
    ) -> tuple[bytes, Mapping[str, str]]: ...

    def get(self, url: str) -> tuple[bytes, Mapping[str, str]]: ...


class UrllibApilioTransport:
    """Small stdlib transport so provider secrets never enter the client process."""

    def __init__(self, *, timeout_seconds: float = 45.0) -> None:
        self.timeout_seconds = timeout_seconds

    def post(
        self, url: str, *, headers: Mapping[str, str], body: bytes
    ) -> tuple[bytes, Mapping[str, str]]:
        return self._open(Request(url, data=body, headers=dict(headers), method="POST"))

    def get(self, url: str) -> tuple[bytes, Mapping[str, str]]:
        require_safe_provider_download_url(url)
        # Apilio's CDN rejects the default urllib user agent even for a valid signed URL.
        return self._open(
            Request(url, headers={"User-Agent": APILIO_OUTPUT_USER_AGENT}, method="GET")
        )

    def _open(self, request: Request) -> tuple[bytes, Mapping[str, str]]:
        try:
            opener = build_opener(NoRedirectHandler())
            with opener.open(request, timeout=self.timeout_seconds) as response:
                content_length = response.headers.get("Content-Length")
                if content_length and int(content_length) > MAX_PROVIDER_IMAGE_BYTES:
                    raise ImageProviderFailed("Apilio response exceeds the image size limit")
                body = response.read(MAX_PROVIDER_IMAGE_BYTES + 1)
                if len(body) > MAX_PROVIDER_IMAGE_BYTES:
                    raise ImageProviderFailed("Apilio response exceeds the image size limit")
                return body, dict(response.headers.items())
        except HTTPError as exc:
            logger.warning("Apilio image request failed with HTTP status %s", exc.code)
            failure_type = (
                RetryableImageProviderFailed
                if exc.code == 429 or exc.code >= 500
                else ImageProviderFailed
            )
            raise failure_type(f"Apilio returned HTTP {exc.code}") from exc
        except (TimeoutError, URLError, OSError) as exc:
            logger.warning("Apilio image request failed: %s", type(exc).__name__)
            raise RetryableImageProviderFailed("Apilio image request failed") from exc


class ApilioImageProvider:
    provider_name = "apilio"

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = APILIO_DEFAULT_BASE_URL,
        transport: ApilioTransport | None = None,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.transport = transport or UrllibApilioTransport()

    def edit(
        self,
        *,
        model: FirstFrameModel,
        prompt: str,
        source_image: ImageInput,
        character_reference_images: list[ImageInput],
        output_count: int,
    ) -> list[GeneratedImage]:
        body, content_type = build_apilio_edit_multipart(
            model=model,
            prompt=prompt,
            source_image=source_image,
            character_reference_images=character_reference_images,
            output_count=output_count,
        )
        raw_body, _ = self.transport.post(
            f"{self.base_url}{APILIO_IMAGE_EDIT_PATH}",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": content_type,
                "Accept": "application/json",
            },
            body=body,
        )
        return self._parse_response(raw_body, output_count=output_count)

    def _parse_response(self, raw_body: bytes, *, output_count: int) -> list[GeneratedImage]:
        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ImageProviderFailed("Apilio returned invalid JSON") from exc
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, list) or not data:
            raise ImageProviderFailed("Apilio response is missing image output")
        if len(data) != output_count:
            raise ImageProviderFailed("Apilio returned an unexpected number of image outputs")
        return [self._parse_image(item) for item in data]

    def _parse_image(self, item: object) -> GeneratedImage:
        if not isinstance(item, dict):
            raise ImageProviderFailed("Apilio response is missing image output")
        encoded = item.get("b64_json")
        if isinstance(encoded, str) and encoded:
            try:
                content = base64.b64decode(encoded, validate=True)
            except ValueError as exc:
                raise ImageProviderFailed("Apilio returned invalid base64 image data") from exc
            content_type = normalized_image_content_type(item.get("mime_type"))
            validate_provider_image_bytes(content, content_type)
            return GeneratedImage(content=content, content_type=content_type)
        url = item.get("url")
        if not isinstance(url, str) or not valid_provider_output_url(url):
            raise ImageProviderFailed("Apilio response is missing image output")
        content, headers = self.transport.get(url)
        if not content:
            raise ImageProviderFailed("Apilio returned an empty image output")
        content_type = normalized_image_content_type(header_value(headers, "content-type"))
        validate_provider_image_bytes(content, content_type)
        return GeneratedImage(
            content=content,
            content_type=content_type,
        )


def build_apilio_edit_multipart(
    *,
    model: FirstFrameModel,
    prompt: str,
    source_image: ImageInput,
    character_reference_images: list[ImageInput],
    output_count: int,
) -> tuple[bytes, str]:
    boundary = f"----video-replica-{uuid4().hex}"
    body = bytearray()

    def add_field(name: str, value: str) -> None:
        body.extend(f"--{boundary}\r\n".encode())
        body.extend(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
        body.extend(value.encode())
        body.extend(b"\r\n")

    def add_image(image: ImageInput) -> None:
        body.extend(f"--{boundary}\r\n".encode())
        body.extend(
            (
                'Content-Disposition: form-data; name="image"; '
                f'filename="{safe_filename(image.filename)}"\r\n'
            ).encode()
        )
        body.extend(f"Content-Type: {image.content_type}\r\n\r\n".encode())
        body.extend(image.content)
        body.extend(b"\r\n")

    add_field("model", model)
    add_field("prompt", prompt)
    add_image(source_image)
    for image in character_reference_images:
        add_image(image)
    add_field("response_format", "url")
    add_field("n", str(output_count))
    if model == "gpt-image-2":
        add_field("size", "auto")
    else:
        add_field("aspect_ratio", image_aspect_ratio(source_image))
        add_field("image_size", "2K")
    body.extend(f"--{boundary}--\r\n".encode())
    return bytes(body), f"multipart/form-data; boundary={boundary}"


def image_aspect_ratio(image: ImageInput) -> str:
    dimensions = png_dimensions(image.content) or jpeg_dimensions(image.content)
    if dimensions is None:
        return "9:16"
    width, height = dimensions
    target_ratio = width / height
    supported_ratios = {
        "1:1": 1.0,
        "2:3": 2 / 3,
        "3:2": 3 / 2,
        "3:4": 3 / 4,
        "4:3": 4 / 3,
        "4:5": 4 / 5,
        "5:4": 5 / 4,
        "9:16": 9 / 16,
        "16:9": 16 / 9,
        "21:9": 21 / 9,
    }
    return min(supported_ratios, key=lambda ratio: abs(supported_ratios[ratio] - target_ratio))


def png_dimensions(content: bytes) -> tuple[int, int] | None:
    if len(content) < 24 or content[:8] != b"\x89PNG\r\n\x1a\n" or content[12:16] != b"IHDR":
        return None
    return int.from_bytes(content[16:20], "big"), int.from_bytes(content[20:24], "big")


def jpeg_dimensions(content: bytes) -> tuple[int, int] | None:
    if len(content) < 4 or content[:2] != b"\xff\xd8":
        return None
    offset = 2
    while offset + 9 <= len(content):
        if content[offset] != 0xFF:
            offset += 1
            continue
        marker = content[offset + 1]
        offset += 2
        if marker in {0xD8, 0xD9}:
            continue
        if offset + 2 > len(content):
            return None
        segment_length = int.from_bytes(content[offset : offset + 2], "big")
        if segment_length < 7 or offset + segment_length > len(content):
            return None
        if marker in {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}:
            height = int.from_bytes(content[offset + 3 : offset + 5], "big")
            width = int.from_bytes(content[offset + 5 : offset + 7], "big")
            return width, height
        offset += segment_length
    return None


def safe_filename(filename: str) -> str:
    return "".join(char if char.isalnum() or char in {".", "-", "_"} else "_" for char in filename)


def valid_provider_output_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme == "https" and bool(parsed.hostname)


def require_safe_provider_download_url(value: str) -> None:
    if not valid_provider_output_url(value):
        raise ImageProviderFailed("Apilio output URL must use HTTPS")
    hostname = urlparse(value).hostname
    if hostname is None:
        raise ImageProviderFailed("Apilio output URL is invalid")
    try:
        addresses = socket.getaddrinfo(hostname, 443, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ImageProviderFailed("Apilio output URL hostname could not be resolved") from exc
    if not addresses:
        raise ImageProviderFailed("Apilio output URL hostname could not be resolved")
    for address in addresses:
        ip = ipaddress.ip_address(address[4][0])
        if not ip.is_global:
            raise ImageProviderFailed("Apilio output URL must resolve to a public address")


class NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(
        self, req: Request, fp: object, code: int, msg: str, headers: object, newurl: str
    ) -> None:
        del req, fp, code, msg, headers, newurl
        return None


def header_value(headers: Mapping[str, str], name: str) -> str | None:
    name_lower = name.lower()
    return next((value for key, value in headers.items() if key.lower() == name_lower), None)


def normalized_image_content_type(value: object) -> str:
    content_type = str(value or "image/png").split(";", 1)[0].lower().strip()
    if content_type not in FIRST_FRAME_IMAGE_CONTENT_TYPES:
        raise ImageProviderFailed("Apilio returned an unsupported image type")
    return content_type


def validate_provider_image_bytes(content: bytes, content_type: str) -> None:
    signatures = {
        "image/jpeg": content.startswith(b"\xff\xd8\xff"),
        "image/png": content.startswith(b"\x89PNG\r\n\x1a\n"),
        "image/webp": len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WEBP",
    }
    if not signatures[content_type]:
        raise ImageProviderFailed("Apilio returned image bytes that do not match its content type")


def generate_first_frame_candidates(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    actor: CurrentUser,
    storage: StorageAdapter,
    provider: ImageProvider,
    model: FirstFrameModel,
    prompt: str | None,
    quantity: int,
) -> sqlite3.Row:
    require_not_auditor(
        conn,
        actor=actor,
        action="first_frame.generate",
        entity_type="project",
        entity_id=project_id,
    )
    require_project_access(conn, actor=actor, project_id=project_id, action="first_frame.generate")
    if model not in FIRST_FRAME_MODELS:
        raise first_frame_error(
            422, "FIRST_FRAME_MODEL_UNSUPPORTED", "The requested image model is unavailable."
        )
    if quantity < 1 or quantity > MAX_FIRST_FRAME_CANDIDATES:
        raise first_frame_error(
            422,
            "FIRST_FRAME_QUANTITY_INVALID",
            f"Generate between 1 and {MAX_FIRST_FRAME_CANDIDATES} candidates.",
        )

    source_selection = current_source_frame_selection(conn, project_id=project_id)
    source_frame_asset_id = str(source_selection["source_frame_asset_id"])
    source_frame = require_asset_access(
        conn,
        actor=actor,
        asset_id=source_frame_asset_id,
        action="first_frame.generate",
    )
    if str(source_frame["project_id"]) != project_id or str(source_frame["kind"]) != "source_frame":
        raise first_frame_error(
            422, "SOURCE_FRAME_INVALID", "The confirmed source frame is invalid."
        )

    character_inputs = resolve_first_frame_character_inputs(
        conn,
        project_id=project_id,
        source_frame_selection_version_id=str(source_selection["id"]),
    )
    source_image = read_asset_image(storage, source_frame)
    reference_assets = [
        read_character_reference_asset(
            conn,
            actor=actor,
            asset_id=asset_id,
            authorized_project_ids=character_inputs.authorized_project_ids,
        )
        for asset_id in character_inputs.reference_asset_ids
    ]
    reference_images = [read_asset_image(storage, asset) for asset in reference_assets]
    effective_prompt = normalize_prompt(prompt, character_name=character_inputs.character_name)

    generated = edit_once_with_retry(
        provider,
        model=model,
        prompt=effective_prompt,
        source_image=source_image,
        character_reference_images=reference_images,
        quantity=quantity,
    )
    if len(generated) != quantity or any(
        not item.content or item.content_type not in FIRST_FRAME_IMAGE_CONTENT_TYPES
        for item in generated
    ):
        raise first_frame_error(
            502,
            "FIRST_FRAME_PROVIDER_RESPONSE_INVALID",
            "The image provider did not return the requested candidates.",
        )
    require_current_first_frame_inputs(
        conn,
        project_id=project_id,
        source_frame_selection_version_id=str(source_selection["id"]),
        main_character_version_id=character_inputs.main_character_version_id,
        character_reference_selection_id=(character_inputs.character_reference_selection_id),
        character_version_id=character_inputs.character_version_id,
    )

    created_assets: list[tuple[str, str]] = []
    try:
        candidates: list[dict[str, object]] = []
        for image in generated:
            extension = image_extension(image.content_type)
            asset_id = str(uuid4())
            storage_key = f"projects/{project_id}/first-frames/{asset_id}.{extension}"
            created_assets.append((asset_id, storage_key))
            stored = storage.put_object(storage_key, image.content, content_type=image.content_type)
            candidates.append(
                {
                    "asset_id": asset_id,
                    "storage_key": storage_key,
                    "storage_uri": stored.uri,
                    "sha256": stored.sha256 or hashlib.sha256(image.content).hexdigest(),
                    "size_bytes": stored.size,
                    "content_type": image.content_type,
                }
            )

        with conn:
            for candidate in candidates:
                conn.execute(
                    """
                    INSERT INTO assets (
                        id, project_id, kind, storage_uri, sha256, size_bytes, content_type,
                        created_by_user_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        candidate["asset_id"],
                        project_id,
                        "first_frame",
                        candidate["storage_uri"],
                        candidate["sha256"],
                        candidate["size_bytes"],
                        candidate["content_type"],
                        actor.id,
                    ),
                )
            version_payload: dict[str, object] = {
                "schema_version": FIRST_FRAME_SCHEMA_VERSION,
                "source_frame_selection_version_id": str(source_selection["id"]),
                "source_frame_asset_id": source_frame_asset_id,
                "main_character_version_id": character_inputs.main_character_version_id,
                "character_snapshot": character_inputs.character_snapshot,
                "character_reference_asset_ids": character_inputs.reference_asset_ids,
                "provider": provider.provider_name,
                "model": model,
                "prompt": effective_prompt,
                "candidates": candidates,
            }
            if character_inputs.character_reference_selection_id is not None:
                version_payload["character_reference_selection_id"] = (
                    character_inputs.character_reference_selection_id
                )
                version_payload["character_version_id"] = character_inputs.character_version_id
            row = insert_version(
                conn,
                project_id=project_id,
                asset_id=source_frame_asset_id,
                kind=FIRST_FRAME_CANDIDATES_KIND,
                created_by_user_id=actor.id,
                payload=version_payload,
            )
    except sqlite3.Error as exc:
        delete_created_first_frames(storage, created_assets, actor_id=actor.id)
        raise first_frame_error(
            500,
            "FIRST_FRAME_PERSIST_FAILED",
            "First-frame candidates could not be saved. Generate them again.",
        ) from exc
    except (OSError, StorageBackendUnavailable, ValueError) as exc:
        delete_created_first_frames(storage, created_assets, actor_id=actor.id)
        raise first_frame_error(
            503,
            "FIRST_FRAME_STORAGE_UNAVAILABLE",
            "First-frame storage is temporarily unavailable.",
        ) from exc

    write_audit(
        conn,
        actor=actor,
        action="first_frame.generate",
        entity_type="version",
        entity_id=str(row["id"]),
        metadata={"project_id": project_id, "model": model, "quantity": quantity},
    )
    return row


def confirm_first_frame(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    first_frame_asset_id: str,
    actor: CurrentUser,
) -> sqlite3.Row:
    require_not_auditor(
        conn,
        actor=actor,
        action="first_frame.confirm",
        entity_type="project",
        entity_id=project_id,
    )
    require_project_access(conn, actor=actor, project_id=project_id, action="first_frame.confirm")
    candidate_version = current_first_frame_candidates(conn, project_id=project_id)
    payload = json.loads(str(candidate_version["payload_json"]))
    candidates = payload.get("candidates")
    if not isinstance(candidates, list):
        raise first_frame_error(
            409, "FIRST_FRAME_CANDIDATES_INVALID", "Generate first-frame candidates again."
        )
    candidate = next(
        (
            value
            for value in candidates
            if isinstance(value, dict) and value.get("asset_id") == first_frame_asset_id
        ),
        None,
    )
    if candidate is None:
        raise first_frame_error(
            422, "FIRST_FRAME_CANDIDATE_NOT_FOUND", "Select a candidate from the latest set."
        )
    asset = require_asset_access(
        conn,
        actor=actor,
        asset_id=first_frame_asset_id,
        action="first_frame.confirm",
    )
    if str(asset["project_id"]) != project_id or str(asset["kind"]) != "first_frame":
        raise first_frame_error(
            422, "FIRST_FRAME_CANDIDATE_NOT_FOUND", "The selected first frame is invalid."
        )

    row = insert_version(
        conn,
        project_id=project_id,
        asset_id=first_frame_asset_id,
        kind=FIRST_FRAME_SELECTION_KIND,
        created_by_user_id=actor.id,
        payload={
            "schema_version": FIRST_FRAME_SCHEMA_VERSION,
            "first_frame_candidates_version_id": str(candidate_version["id"]),
            "first_frame_asset_id": first_frame_asset_id,
        },
    )
    write_audit(
        conn,
        actor=actor,
        action="first_frame.confirm",
        entity_type="version",
        entity_id=str(row["id"]),
        metadata={"project_id": project_id, "first_frame_asset_id": first_frame_asset_id},
    )
    return row


def current_source_frame_selection(
    conn: sqlite3.Connection, *, project_id: str
) -> dict[str, object]:
    selection = latest_version(conn, project_id, SOURCE_FRAME_SELECTION_KIND)
    candidates = latest_version(conn, project_id, SOURCE_FRAME_CANDIDATES_KIND)
    if selection is None or candidates is None:
        raise first_frame_error(
            409, "SOURCE_FRAME_SELECTION_REQUIRED", "Confirm a source frame first."
        )
    payload = json.loads(str(selection["payload_json"]))
    if payload.get("source_frame_candidates_version_id") != str(candidates["id"]):
        raise first_frame_error(
            409,
            "SOURCE_FRAME_SELECTION_STALE",
            "Select a source frame from the latest candidate set.",
        )
    return cast(dict[str, object], payload | {"id": str(selection["id"])})


def resolve_first_frame_character_inputs(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    source_frame_selection_version_id: str,
) -> FirstFrameCharacterInputs:
    reference_selection = current_character_reference_selection_for_generation(
        conn,
        project_id=project_id,
        source_frame_version_id=source_frame_selection_version_id,
    )
    if reference_selection is not None:
        snapshot = reference_selection.character_version_snapshot_json
        persona_snapshot = snapshot.get("persona_snapshot_json")
        if not isinstance(persona_snapshot, dict):
            raise stale_first_frame_inputs()
        character_name = persona_snapshot.get("name")
        if not isinstance(character_name, str) or not character_name:
            raise stale_first_frame_inputs()
        main_character_version_id = snapshot.get("main_character_version_id")
        if not isinstance(main_character_version_id, str):
            raise stale_first_frame_inputs()
        return FirstFrameCharacterInputs(
            main_character_version_id=main_character_version_id,
            character_snapshot=snapshot,
            reference_asset_ids=reference_selection.selected_asset_ids_json,
            character_name=character_name,
            authorized_project_ids=[],
            character_reference_selection_id=reference_selection.id,
            character_version_id=reference_selection.character_version_id,
        )

    main_character = get_project_main_character(conn, project_id=project_id)
    character = read_character(conn, str(main_character["character_id"]))
    if not character_is_available(character, project_id=project_id):
        raise first_frame_error(
            422,
            "CHARACTER_NOT_AVAILABLE",
            "The selected character is inactive, expired, or not authorized for this project.",
        )
    character_snapshot = main_character["character_snapshot"]
    if not isinstance(character_snapshot, dict):
        raise first_frame_error(
            409, "MAIN_CHARACTER_SNAPSHOT_INVALID", "Select the character again."
        )
    reference_asset_ids = character_snapshot.get("reference_asset_ids")
    character_name = character_snapshot.get("name")
    if not isinstance(reference_asset_ids, list) or not all(
        isinstance(asset_id, str) for asset_id in reference_asset_ids
    ):
        raise first_frame_error(
            409, "MAIN_CHARACTER_SNAPSHOT_INVALID", "Select the character again."
        )
    if not isinstance(character_name, str) or not character_name:
        raise first_frame_error(
            409, "MAIN_CHARACTER_SNAPSHOT_INVALID", "Select the character again."
        )
    if not reference_asset_ids:
        raise first_frame_error(
            422,
            "CHARACTER_REFERENCE_REQUIRED",
            "The selected character needs at least one reference image.",
        )
    authorized_project_ids = character_snapshot.get("authorization_project_ids") or []
    if not isinstance(authorized_project_ids, list) or not all(
        isinstance(project, str) for project in authorized_project_ids
    ):
        authorized_project_ids = []
    return FirstFrameCharacterInputs(
        main_character_version_id=str(main_character["version_id"]),
        character_snapshot=character_snapshot,
        reference_asset_ids=cast(list[str], reference_asset_ids),
        character_name=character_name,
        authorized_project_ids=cast(list[str], authorized_project_ids),
    )


def current_first_frame_candidates(conn: sqlite3.Connection, *, project_id: str) -> sqlite3.Row:
    candidates = latest_version(conn, project_id, FIRST_FRAME_CANDIDATES_KIND)
    if candidates is None:
        raise first_frame_error(
            409, "FIRST_FRAME_CANDIDATES_NOT_FOUND", "Generate first-frame candidates first."
        )
    payload = json.loads(str(candidates["payload_json"]))
    if not isinstance(payload, dict):
        raise first_frame_error(
            409, "FIRST_FRAME_CANDIDATES_INVALID", "Generate first-frame candidates again."
        )
    source_version_id = payload.get("source_frame_selection_version_id")
    main_character_version_id = payload.get("main_character_version_id")
    reference_selection_id = payload.get("character_reference_selection_id")
    character_version_id = payload.get("character_version_id")
    if not isinstance(source_version_id, str) or not isinstance(main_character_version_id, str):
        raise first_frame_error(
            409, "FIRST_FRAME_CANDIDATES_INVALID", "Generate first-frame candidates again."
        )
    if (reference_selection_id is None) != (character_version_id is None) or (
        reference_selection_id is not None
        and (
            not isinstance(reference_selection_id, str) or not isinstance(character_version_id, str)
        )
    ):
        raise first_frame_error(
            409, "FIRST_FRAME_CANDIDATES_INVALID", "Generate first-frame candidates again."
        )
    require_current_first_frame_inputs(
        conn,
        project_id=project_id,
        source_frame_selection_version_id=source_version_id,
        main_character_version_id=main_character_version_id,
        character_reference_selection_id=reference_selection_id,
        character_version_id=character_version_id,
    )
    return candidates


def require_current_first_frame_inputs(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    source_frame_selection_version_id: str,
    main_character_version_id: str,
    character_reference_selection_id: str | None = None,
    character_version_id: str | None = None,
) -> None:
    if character_reference_selection_id is not None and character_version_id is not None:
        try:
            source_selection = current_source_frame_selection(conn, project_id=project_id)
            reference_selection = current_character_reference_selection_for_generation(
                conn,
                project_id=project_id,
                source_frame_version_id=source_frame_selection_version_id,
                expected_selection_id=character_reference_selection_id,
            )
        except HTTPException as exc:
            if exc.status_code in {404, 409}:
                raise stale_first_frame_inputs() from exc
            raise
        if (
            str(source_selection["id"]) != source_frame_selection_version_id
            or reference_selection is None
            or reference_selection.character_version_id != character_version_id
            or reference_selection.character_version_snapshot_json.get("main_character_version_id")
            != main_character_version_id
        ):
            raise stale_first_frame_inputs()
        return
    if character_reference_selection_id is not None or character_version_id is not None:
        raise stale_first_frame_inputs()

    try:
        source_selection = current_source_frame_selection(conn, project_id=project_id)
        main_character = get_project_main_character(conn, project_id=project_id)
        character = read_character(conn, str(main_character["character_id"]))
    except HTTPException as exc:
        if exc.status_code in {404, 409}:
            raise first_frame_error(
                409,
                "FIRST_FRAME_CANDIDATES_STALE",
                "Generate first-frame candidates again using the current source frame "
                "and character.",
            ) from exc
        raise
    if (
        str(source_selection["id"]) != source_frame_selection_version_id
        or str(main_character["version_id"]) != main_character_version_id
        or not character_is_available(character, project_id=project_id)
    ):
        raise first_frame_error(
            409,
            "FIRST_FRAME_CANDIDATES_STALE",
            "Generate first-frame candidates again using the current source frame and character.",
        )


def stale_first_frame_inputs() -> HTTPException:
    return first_frame_error(
        409,
        "FIRST_FRAME_CANDIDATES_STALE",
        "Generate first-frame candidates again using the current source frame and character.",
    )


def read_character_reference_asset(
    conn: sqlite3.Connection,
    *,
    actor: CurrentUser,
    asset_id: str,
    authorized_project_ids: list[str],
) -> sqlite3.Row:
    row = conn.execute(
        """
        SELECT id, project_id, kind, storage_uri, sha256, size_bytes, content_type
        FROM assets WHERE id = ?
        """,
        (asset_id,),
    ).fetchone()
    if row is None:
        raise first_frame_error(
            422, "CHARACTER_REFERENCE_NOT_FOUND", "A character reference image is missing."
        )
    # A character library is a workspace-global entity, but its reference images
    # may belong to another project. Gate the read so an employee cannot pull
    # bytes from a project they have no access to, unless that project is within
    # the character's declared authorization scope (cross-project library use).
    try:
        require_asset_access(
            conn, actor=actor, asset_id=asset_id, action="character_reference.read"
        )
    except HTTPException:
        if str(row["project_id"]) not in authorized_project_ids:
            raise
    if str(row["content_type"]) not in FIRST_FRAME_IMAGE_CONTENT_TYPES:
        raise first_frame_error(
            422,
            "CHARACTER_REFERENCE_INVALID",
            "Character references must be JPEG, PNG, or WebP images.",
        )
    return cast(sqlite3.Row, row)


def read_asset_image(storage: StorageAdapter, asset: sqlite3.Row) -> ImageInput:
    content_type = str(asset["content_type"])
    if content_type not in FIRST_FRAME_IMAGE_CONTENT_TYPES:
        raise first_frame_error(
            422,
            "FIRST_FRAME_IMAGE_TYPE_UNSUPPORTED",
            "Source and character reference images must be JPEG, PNG, or WebP.",
        )
    try:
        reference = storage_object_ref_from_uri(str(asset["storage_uri"]))
        require_storage_match(storage, reference)
        content = storage.get_object(reference.key)
    except (KeyError, OSError, StorageBackendUnavailable, ValueError) as exc:
        raise first_frame_error(
            503,
            "FIRST_FRAME_INPUT_STORAGE_UNAVAILABLE",
            "Source or character reference storage is temporarily unavailable.",
        ) from exc
    return ImageInput(
        content=content,
        content_type=content_type,
        filename=f"{asset['id']}.{image_extension(content_type)}",
    )


def edit_once_with_retry(
    provider: ImageProvider,
    *,
    model: FirstFrameModel,
    prompt: str,
    source_image: ImageInput,
    character_reference_images: list[ImageInput],
    quantity: int,
) -> list[GeneratedImage]:
    for attempt in range(2):
        try:
            return provider.edit(
                model=model,
                prompt=prompt,
                source_image=source_image,
                character_reference_images=character_reference_images,
                output_count=quantity,
            )
        except RetryableImageProviderFailed as exc:
            if attempt == 1:
                raise first_frame_error(
                    502,
                    "FIRST_FRAME_PROVIDER_FAILED",
                    "The image provider could not generate a first frame.",
                ) from exc
        except ImageProviderFailed as exc:
            raise first_frame_error(
                502,
                "FIRST_FRAME_PROVIDER_FAILED",
                "The image provider could not generate a first frame.",
            ) from exc
    raise AssertionError("image provider retry loop must return or raise")


def normalize_prompt(prompt: str | None, *, character_name: str) -> str:
    clean = (prompt or "").strip()
    if clean:
        return clean
    return (
        "保留原图的镜头位置、人物姿态、动作、场景、构图、道具、光线与色调，"
        f"只将原人物身份替换为角色库人物“{character_name}”；"
        "保持自然皮肤、正确肢体和真实透视；不得增加或删除主体。"
    )


def image_extension(content_type: str) -> str:
    return {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}[content_type]


def delete_created_first_frames(
    storage: StorageAdapter,
    created_assets: list[tuple[str, str]],
    *,
    actor_id: str,
) -> None:
    for _, storage_key in created_assets:
        try:
            storage.delete_object(storage_key, actor_id=actor_id)
        except (OSError, StorageBackendUnavailable):
            pass


def first_frame_error(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code, "message": message})
