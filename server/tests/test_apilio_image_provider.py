from __future__ import annotations

import base64
from collections.abc import Mapping
from dataclasses import dataclass, field

import pytest

from app.first_frames import (
    ApilioImageProvider,
    ImageInput,
    ImageProviderFailed,
    image_aspect_ratio,
    valid_provider_output_url,
)


@dataclass
class RecordedRequest:
    url: str
    headers: Mapping[str, str]
    body: bytes


@dataclass
class FakeApilioTransport:
    response_body: bytes
    response_headers: Mapping[str, str] = field(
        default_factory=lambda: {"content-type": "application/json"}
    )
    requests: list[RecordedRequest] = field(default_factory=list)
    downloads: dict[str, tuple[bytes, str]] = field(default_factory=dict)

    def post(
        self, url: str, *, headers: Mapping[str, str], body: bytes
    ) -> tuple[bytes, Mapping[str, str]]:
        self.requests.append(RecordedRequest(url=url, headers=headers, body=body))
        return self.response_body, self.response_headers

    def get(self, url: str) -> tuple[bytes, Mapping[str, str]]:
        content, content_type = self.downloads[url]
        return content, {"content-type": content_type}


def image(content: bytes, content_type: str, filename: str) -> ImageInput:
    return ImageInput(content=content, content_type=content_type, filename=filename)


def test_gpt_image_edit_uses_apilio_multipart_contract_and_downloads_url_response() -> None:
    transport = FakeApilioTransport(
        response_body=b'{"data":[{"url":"https://cdn.example/first.png"}]}'
    )
    generated_png = png_with_dimensions(940, 1672)
    transport.downloads["https://cdn.example/first.png"] = (generated_png, "image/png")
    provider = ApilioImageProvider(api_key="test-key", transport=transport)

    generated = provider.edit(
        model="gpt-image-2",
        prompt="replace the person",
        source_image=image(b"source", "image/jpeg", "source.jpg"),
        character_reference_images=[
            image(b"front", "image/png", "front.png"),
            image(b"side", "image/webp", "side.webp"),
        ],
        output_count=1,
    )

    assert generated == [type(generated[0])(content=generated_png, content_type="image/png")]
    request = transport.requests[0]
    assert request.url == "https://api.apilio.ai/v1/images/edits"
    assert request.headers["Authorization"] == "Bearer test-key"
    assert b'name="model"\r\n\r\ngpt-image-2' in request.body
    assert b'name="prompt"\r\n\r\nreplace the person' in request.body
    assert request.body.index(b'filename="source.jpg"') < request.body.index(
        b'filename="front.png"'
    )
    assert request.body.index(b'filename="front.png"') < request.body.index(b'filename="side.webp"')
    assert b'name="response_format"\r\n\r\nb64_json' in request.body
    assert b'name="size"\r\n\r\nauto' in request.body


def test_nano_banana_edit_uses_source_ratio_and_2k_defaults() -> None:
    banana_result = png_with_dimensions(940, 1672)
    encoded = base64.b64encode(banana_result).decode("ascii")
    transport = FakeApilioTransport(
        response_body=(f'{{"data":[{{"b64_json":"{encoded}"}}]}}').encode()
    )
    provider = ApilioImageProvider(api_key="test-key", transport=transport)

    generated = provider.edit(
        model="nano-banana-pro-2k",
        prompt="replace the person",
        source_image=image(png_with_dimensions(576, 1024), "image/png", "source.png"),
        character_reference_images=[image(b"front", "image/png", "front.png")],
        output_count=1,
    )

    assert generated[0].content == banana_result
    assert generated[0].content_type == "image/png"
    body = transport.requests[0].body
    assert b'name="model"\r\n\r\nnano-banana-pro-2k' in body
    assert b'name="aspect_ratio"\r\n\r\n9:16' in body
    assert b'name="image_size"\r\n\r\n2K' in body


def test_provider_rejects_malformed_or_unsupported_image_responses() -> None:
    transport = FakeApilioTransport(response_body=b'{"data":[{}]}')
    provider = ApilioImageProvider(api_key="test-key", transport=transport)

    with pytest.raises(ImageProviderFailed, match="missing image output"):
        provider.edit(
            model="gpt-image-2",
            prompt="replace the person",
            source_image=image(b"source", "image/jpeg", "source.jpg"),
            character_reference_images=[],
            output_count=1,
        )


def test_provider_rejects_image_bytes_that_do_not_match_the_reported_type() -> None:
    transport = FakeApilioTransport(
        response_body=b'{"data":[{"url":"https://cdn.example/first.png"}]}'
    )
    transport.downloads["https://cdn.example/first.png"] = (b"not-a-png", "image/png")
    provider = ApilioImageProvider(api_key="test-key", transport=transport)

    with pytest.raises(ImageProviderFailed, match="do not match"):
        provider.edit(
            model="gpt-image-2",
            prompt="replace the person",
            source_image=image(b"source", "image/jpeg", "source.jpg"),
            character_reference_images=[],
            output_count=1,
        )


def test_provider_rejects_a_response_with_the_wrong_candidate_count() -> None:
    result = base64.b64encode(png_with_dimensions(940, 1672)).decode("ascii")
    transport = FakeApilioTransport(
        response_body=(f'{{"data":[{{"b64_json":"{result}"}},{{"b64_json":"{result}"}}]}}').encode()
    )
    provider = ApilioImageProvider(api_key="test-key", transport=transport)

    with pytest.raises(ImageProviderFailed, match="unexpected number"):
        provider.edit(
            model="gpt-image-2",
            prompt="replace the person",
            source_image=image(b"source", "image/jpeg", "source.jpg"),
            character_reference_images=[],
            output_count=1,
        )


def test_provider_output_urls_require_https_and_jpeg_source_ratio_is_preserved() -> None:
    assert valid_provider_output_url("http://cdn.example/first.png") is False
    assert valid_provider_output_url("https://cdn.example/first.png") is True
    assert (
        image_aspect_ratio(image(jpeg_with_dimensions(1024, 576), "image/jpeg", "source.jpg"))
        == "16:9"
    )


def png_with_dimensions(width: int, height: int) -> bytes:
    return (
        b"\x89PNG\r\n\x1a\n"
        + b"\x00\x00\x00\rIHDR"
        + width.to_bytes(4, "big")
        + height.to_bytes(4, "big")
    )


def jpeg_with_dimensions(width: int, height: int) -> bytes:
    return (
        b"\xff\xd8"
        + b"\xff\xc0\x00\x11\x08"
        + height.to_bytes(2, "big")
        + width.to_bytes(2, "big")
        + b"\x03\x01\x11\x00\x02\x11\x00\x03\x11\x00\xff\xd9"
    )
