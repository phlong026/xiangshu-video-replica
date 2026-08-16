from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import time
import traceback
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO
from urllib.error import URLError
from urllib.request import urlopen

from cryptography.fernet import Fernet

from app.gate1_bootstrap import bootstrap_gate1_database

CommandRunner = Callable[[list[str]], None]


@dataclass(frozen=True)
class Gate1RunPaths:
    run_id: str
    run_dir: Path
    runtime_dir: Path
    media_dir: Path
    logs_dir: Path
    screenshots_dir: Path
    downloads_dir: Path
    browser_dir: Path


@dataclass
class _ManagedProcess:
    process: subprocess.Popen[bytes]
    log_file: BinaryIO


class ManagedProcesses:
    """Own child process groups so failures never leave local Gate 1 services behind."""

    def __init__(self) -> None:
        self._processes: list[_ManagedProcess] = []

    def __enter__(self) -> ManagedProcesses:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        exc_traceback: object,
    ) -> None:
        del exc_type, exc_value, exc_traceback
        self.close()

    def start(
        self,
        *,
        name: str,
        command: list[str],
        cwd: Path,
        env: Mapping[str, str],
        log_path: Path,
    ) -> subprocess.Popen[bytes]:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_file = log_path.open("ab")
        try:
            process = subprocess.Popen(
                command,
                cwd=cwd,
                env=dict(env),
                stdout=log_file,
                stderr=subprocess.STDOUT,
                start_new_session=os.name != "nt",
                creationflags=(
                    getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if os.name == "nt" else 0
                ),
            )
        except BaseException:
            log_file.close()
            raise
        self._processes.append(_ManagedProcess(process=process, log_file=log_file))
        _append_text(log_path, f"\n[harness] started {name} pid={process.pid}\n")
        return process

    def close(self) -> None:
        first_error: BaseException | None = None
        while self._processes:
            managed = self._processes.pop()
            try:
                _stop_process_tree(managed.process)
            except BaseException as exc:
                if first_error is None:
                    first_error = exc
            finally:
                managed.log_file.close()
        if first_error is not None:
            raise first_error


def prepare_gate1_run(output_root: Path, *, run_id: str) -> Gate1RunPaths:
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", run_id) is None:
        raise ValueError("run_id may contain only letters, numbers, dot, underscore, and dash")
    run_dir = output_root.resolve() / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    paths = Gate1RunPaths(
        run_id=run_id,
        run_dir=run_dir,
        runtime_dir=run_dir / "runtime",
        media_dir=run_dir / "media",
        logs_dir=run_dir / "logs",
        screenshots_dir=run_dir / "screenshots",
        downloads_dir=run_dir / "downloads",
        browser_dir=run_dir / "browser",
    )
    for directory in (
        paths.runtime_dir,
        paths.media_dir,
        paths.logs_dir,
        paths.screenshots_dir,
        paths.downloads_dir,
        paths.browser_dir,
    ):
        directory.mkdir()
    return paths


def generate_test_media(
    paths: Gate1RunPaths,
    *,
    ffmpeg_binary: str = "ffmpeg",
    runner: CommandRunner | None = None,
) -> dict[str, Path]:
    authorization_image = paths.media_dir / "authorization.png"
    source_image = paths.media_dir / "source.png"
    reference_video = paths.media_dir / "reference.mp4"
    commands = [
        [
            ffmpeg_binary,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=1200x1600:rate=1",
            "-frames:v",
            "1",
            str(authorization_image),
        ],
        [
            ffmpeg_binary,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=1024x1536:rate=1",
            "-frames:v",
            "1",
            str(source_image),
        ],
        [
            ffmpeg_binary,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=720x1280:rate=24",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000",
            "-t",
            "6",
            "-shortest",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-movflags",
            "+faststart",
            str(reference_video),
        ],
    ]

    command_runner = runner or _media_command_runner(paths.logs_dir / "media.log")
    for command in commands:
        command_runner(command)

    media = {
        "authorization_image": authorization_image,
        "source_image": source_image,
        "reference_video": reference_video,
    }
    missing = [str(path) for path in media.values() if not path.is_file()]
    if missing:
        raise RuntimeError(f"Gate 1 media generation did not create: {', '.join(missing)}")
    return media


def wait_for_http(
    url: str,
    *,
    timeout_seconds: float = 30,
    poll_interval_seconds: float = 0.1,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    last_error: BaseException | None = None
    while time.monotonic() < deadline:
        try:
            with urlopen(url, timeout=min(2.0, timeout_seconds)) as response:  # noqa: S310
                if 200 <= response.status < 400:
                    return
        except (OSError, URLError) as exc:
            last_error = exc
        time.sleep(poll_interval_seconds)
    detail = type(last_error).__name__ if last_error is not None else "not ready"
    raise TimeoutError(f"Timed out waiting for {url}: {detail}")


def write_evidence_manifest(
    paths: Gate1RunPaths,
    *,
    status: str,
    commit_sha: str,
) -> Path:
    manifest_path = paths.run_dir / "sha256-manifest.json"
    files: list[dict[str, str | int]] = []
    for path in sorted(paths.run_dir.rglob("*")):
        if not path.is_file() or path == manifest_path:
            continue
        relative_path = path.relative_to(paths.run_dir)
        if relative_path.parts[0] == "runtime":
            continue
        content = path.read_bytes()
        files.append(
            {
                "path": relative_path.as_posix(),
                "sha256": hashlib.sha256(content).hexdigest(),
                "size_bytes": len(content),
            }
        )
    manifest = {
        "schema_version": 1,
        "run_id": paths.run_id,
        "status": status,
        "commit_sha": commit_sha,
        "files": files,
    }
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return manifest_path


def verify_evidence_manifest(manifest_path: Path) -> None:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    files = manifest.get("files")
    if not isinstance(files, list):
        raise ValueError("Gate 1 evidence manifest files must be a list")
    run_dir = manifest_path.parent.resolve()
    for item in files:
        if not isinstance(item, dict):
            raise ValueError("Gate 1 evidence manifest entry must be an object")
        relative_value = item.get("path")
        expected_sha256 = item.get("sha256")
        expected_size = item.get("size_bytes")
        if not isinstance(relative_value, str) or not isinstance(expected_sha256, str):
            raise ValueError("Gate 1 evidence manifest entry is incomplete")
        if not isinstance(expected_size, int):
            raise ValueError("Gate 1 evidence manifest size is invalid")
        evidence_path = (run_dir / relative_value).resolve()
        try:
            evidence_path.relative_to(run_dir)
        except ValueError as exc:
            raise ValueError("Gate 1 evidence manifest path escapes its run directory") from exc
        if not evidence_path.is_file():
            raise ValueError(f"Gate 1 evidence is missing: {relative_value}")
        content = evidence_path.read_bytes()
        if len(content) != expected_size or hashlib.sha256(content).hexdigest() != expected_sha256:
            raise ValueError(f"Gate 1 evidence hash mismatch: {relative_value}")


def run_gate1(
    *,
    repository_root: Path,
    output_root: Path,
    run_id: str,
    playwright_arguments: list[str] | None = None,
) -> int:
    paths = prepare_gate1_run(output_root, run_id=run_id)
    commit_sha = "unknown"
    status = "failed"
    exit_code = 1
    try:
        commit_sha = _git_commit(repository_root)
        _require_command("ffmpeg")
        _require_command("uv")
        _require_command("npm")
        _require_available_port("127.0.0.1", 8000)
        _require_available_port("127.0.0.1", 5173)

        settings_key = Fernet.generate_key().decode("ascii")
        database_path = paths.runtime_dir / "gate1.sqlite3"
        storage_root = paths.runtime_dir / "storage"
        storage_root.mkdir()
        runtime_env = os.environ.copy()
        runtime_env.update(
            {
                "PYTHONUNBUFFERED": "1",
                "VIDEO_REPLICA_DB_PATH": str(database_path),
                "VIDEO_REPLICA_SETTINGS_KEY": settings_key,
                "VIDEO_REPLICA_DESKTOP_USER_ID": "gate1_admin",
                "VIDEO_REPLICA_FAKE_H3_RESULT_PATH": str(paths.media_dir / "reference.mp4"),
                "VIDEO_REPLICA_FAKE_SOURCE_IMAGE_INSPECTOR": "1",
                "VIDEO_REPLICA_STORAGE_ROOT": str(storage_root),
            }
        )

        previous_key = os.environ.get("VIDEO_REPLICA_SETTINGS_KEY")
        os.environ["VIDEO_REPLICA_SETTINGS_KEY"] = settings_key
        try:
            bootstrap_gate1_database(
                database_path,
                user_id="gate1_admin",
                display_name="Gate 1 Admin",
            )
        finally:
            if previous_key is None:
                os.environ.pop("VIDEO_REPLICA_SETTINGS_KEY", None)
            else:
                os.environ["VIDEO_REPLICA_SETTINGS_KEY"] = previous_key

        media = generate_test_media(paths)
        _write_run_metadata(paths, commit_sha=commit_sha, media=media)

        with ManagedProcesses() as processes:
            processes.start(
                name="api",
                command=[
                    "uv",
                    "--cache-dir",
                    ".uv-cache",
                    "run",
                    "--project",
                    "server",
                    "--locked",
                    "uvicorn",
                    "app.main:app",
                    "--app-dir",
                    "server",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    "8000",
                ],
                cwd=repository_root,
                env=runtime_env,
                log_path=paths.logs_dir / "api.log",
            )
            wait_for_http("http://127.0.0.1:8000/health")
            processes.start(
                name="vite",
                command=[
                    "npm",
                    "run",
                    "dev",
                    "--workspace",
                    "client",
                    "--",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    "5173",
                ],
                cwd=repository_root,
                env=runtime_env,
                log_path=paths.logs_dir / "vite.log",
            )
            wait_for_http("http://127.0.0.1:5173/")

            playwright_env = runtime_env.copy()
            playwright_env.update(
                {
                    "GATE1_RUN_DIR": str(paths.run_dir),
                    "GATE1_WEB_URL": "http://127.0.0.1:5173",
                    "GATE1_API_URL": "http://127.0.0.1:8000",
                    "GATE1_MEDIA_DIR": str(paths.media_dir),
                    "GATE1_WORKER_COMMAND": json.dumps(
                        [
                            "uv",
                            "--cache-dir",
                            ".uv-cache",
                            "run",
                            "--project",
                            "server",
                            "--locked",
                            "python",
                            "-m",
                            "app.generation_worker",
                            "--once",
                        ]
                    ),
                }
            )
            playwright_command = [
                "npx",
                "playwright",
                "test",
                "--config",
                "e2e/gate1/playwright.config.mjs",
                *(playwright_arguments or []),
            ]
            playwright_process = processes.start(
                name="playwright",
                command=playwright_command,
                cwd=repository_root,
                env=playwright_env,
                log_path=paths.logs_dir / "playwright.log",
            )
            exit_code = playwright_process.wait()
            status = "passed" if exit_code == 0 else "failed"
    except BaseException:
        status = "failed"
        (paths.logs_dir / "harness-error.log").write_text(
            traceback.format_exc(),
            encoding="utf-8",
        )
        exit_code = 1
    finally:
        manifest_path = write_evidence_manifest(paths, status=status, commit_sha=commit_sha)
        verify_evidence_manifest(manifest_path)
    return exit_code


def _media_command_runner(log_path: Path) -> CommandRunner:
    def run(command: list[str]) -> None:
        with log_path.open("ab") as log_file:
            subprocess.run(
                command,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                check=True,
            )

    return run


def _stop_process_tree(process: subprocess.Popen[bytes]) -> None:
    if os.name == "nt":
        if process.poll() is not None:
            return
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    else:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
    if process.poll() is not None:
        return
    try:
        process.wait(timeout=5)
        return
    except subprocess.TimeoutExpired:
        pass
    if os.name == "nt":
        process.kill()
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            return
    process.wait(timeout=5)


def _require_available_port(host: str, port: int) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind((host, port))
        except OSError as exc:
            raise RuntimeError(f"Gate 1 requires free port {host}:{port}") from exc


def _require_command(command: str) -> None:
    if shutil.which(command) is None:
        raise RuntimeError(f"Gate 1 requires command: {command}")


def _git_commit(repository_root: Path) -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repository_root,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _write_run_metadata(
    paths: Gate1RunPaths,
    *,
    commit_sha: str,
    media: Mapping[str, Path],
) -> None:
    metadata = {
        "schema_version": 1,
        "run_id": paths.run_id,
        "commit_sha": commit_sha,
        "started_at": datetime.now(UTC).isoformat(),
        "runtime": "React/Vite + FastAPI + one-shot Worker + SQLite + LocalStorageAdapter",
        "gate_scope": "macOS desktop FakeProvider E2E; not Windows WebView2 acceptance",
        "media": {name: path.relative_to(paths.run_dir).as_posix() for name, path in media.items()},
    }
    (paths.run_dir / "run.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _append_text(path: Path, content: str) -> None:
    with path.open("a", encoding="utf-8") as output:
        output.write(content)


def _default_run_id() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def normalize_playwright_arguments(arguments: list[str]) -> list[str]:
    return arguments[1:] if arguments[:1] == ["--"] else arguments


def main() -> None:
    repository_root = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(description="Run the local desktop Gate 1 Playwright flow")
    parser.add_argument(
        "--output-root",
        type=Path,
        default=repository_root / "output" / "playwright" / "gate1",
    )
    parser.add_argument("--run-id", default=_default_run_id())
    parser.add_argument("playwright_args", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    raise SystemExit(
        run_gate1(
            repository_root=repository_root,
            output_root=args.output_root,
            run_id=args.run_id,
            playwright_arguments=normalize_playwright_arguments(args.playwright_args),
        )
    )


if __name__ == "__main__":
    main()
