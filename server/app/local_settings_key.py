from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet

KEYCHAIN_SERVICE = "com.xiangshu.video-replica.settings"
KEYCHAIN_ACCOUNT = "local-settings"
WINDOWS_APP_DIR = "VideoReplicaWorkbench"
WINDOWS_KEY_FILE = "settings-key.dpapi"
WINDOWS_KEY_PATH_ENV = "VIDEO_REPLICA_DPAPI_KEY_PATH"
COMMAND_TIMEOUT_SECONDS = 10

CommandRunner = Callable[..., subprocess.CompletedProcess[str]]


class LocalSettingsKeyStoreError(RuntimeError):
    pass


def load_or_create_local_settings_key(
    *,
    platform: str | None = None,
    environ: Mapping[str, str] | None = None,
    runner: CommandRunner | None = None,
    generated_key: str | None = None,
) -> str:
    current_platform = platform or sys.platform
    current_environ = environ or os.environ
    command_runner = runner or subprocess.run
    new_key = generated_key or Fernet.generate_key().decode("ascii")

    if current_platform == "darwin":
        return _load_or_create_macos_key(command_runner, new_key)
    if current_platform == "win32":
        return _load_or_create_windows_key(current_environ, command_runner, new_key)
    raise LocalSettingsKeyStoreError(
        "automatic local settings key storage is supported only on macOS and Windows"
    )


def persist_local_settings_key(
    key: str,
    *,
    platform: str | None = None,
    environ: Mapping[str, str] | None = None,
    runner: CommandRunner | None = None,
) -> bool:
    try:
        Fernet(key.encode("ascii"))
    except (UnicodeEncodeError, ValueError) as exc:
        raise LocalSettingsKeyStoreError("refusing to persist an invalid settings key") from exc

    current_platform = platform or sys.platform
    current_environ = environ or os.environ
    command_runner = runner or subprocess.run
    if current_platform == "darwin":
        _persist_macos_key(command_runner, key)
        return True
    if current_platform == "win32":
        _write_windows_key(
            command_runner,
            _windows_key_path(current_environ),
            key,
            replace=True,
        )
        return True
    return False


def _run(
    runner: CommandRunner,
    args: Sequence[str],
    **kwargs: Any,
) -> subprocess.CompletedProcess[str]:
    try:
        return runner(
            list(args),
            capture_output=True,
            text=True,
            timeout=COMMAND_TIMEOUT_SECONDS,
            check=False,
            **kwargs,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise LocalSettingsKeyStoreError("local settings key store command failed") from exc


def _find_macos_key(runner: CommandRunner) -> str | None:
    result = _run(
        runner,
        (
            "security",
            "find-generic-password",
            "-a",
            KEYCHAIN_ACCOUNT,
            "-s",
            KEYCHAIN_SERVICE,
            "-w",
        ),
    )
    if result.returncode != 0:
        return None
    key = result.stdout.strip()
    if not key:
        raise LocalSettingsKeyStoreError("macOS Keychain returned an empty settings key")
    return key


def _load_or_create_macos_key(runner: CommandRunner, generated_key: str) -> str:
    existing = _find_macos_key(runner)
    if existing is not None:
        return existing

    result = _run(
        runner,
        (
            "security",
            "add-generic-password",
            "-a",
            KEYCHAIN_ACCOUNT,
            "-s",
            KEYCHAIN_SERVICE,
            "-w",
        ),
        input=f"{generated_key}\n{generated_key}\n",
    )
    if result.returncode == 0:
        return generated_key

    # Another local process may have created the item after our first read.
    existing = _find_macos_key(runner)
    if existing is not None:
        return existing
    raise LocalSettingsKeyStoreError("unable to create the macOS Keychain settings key")


def _persist_macos_key(runner: CommandRunner, key: str) -> None:
    result = _run(
        runner,
        (
            "security",
            "add-generic-password",
            "-U",
            "-a",
            KEYCHAIN_ACCOUNT,
            "-s",
            KEYCHAIN_SERVICE,
            "-w",
        ),
        input=f"{key}\n{key}\n",
    )
    if result.returncode != 0:
        raise LocalSettingsKeyStoreError("unable to persist the macOS Keychain settings key")


def _windows_key_path(environ: Mapping[str, str]) -> Path:
    local_app_data = environ.get("LOCALAPPDATA", "").strip()
    if not local_app_data:
        raise LocalSettingsKeyStoreError("LOCALAPPDATA is required for Windows key storage")
    return Path(local_app_data) / WINDOWS_APP_DIR / "secrets" / WINDOWS_KEY_FILE


def _load_or_create_windows_key(
    environ: Mapping[str, str],
    runner: CommandRunner,
    generated_key: str,
) -> str:
    key_path = _windows_key_path(environ)
    if key_path.exists():
        return _read_windows_key(runner, key_path)

    try:
        _write_windows_key(runner, key_path, generated_key, replace=False)
    except LocalSettingsKeyStoreError:
        # A second process may have won the first-start race. Its DPAPI file is
        # the shared source of truth; never overwrite it with this process's key.
        if key_path.exists():
            return _read_windows_key(runner, key_path)
        raise
    return generated_key


def _write_windows_key(
    runner: CommandRunner,
    key_path: Path,
    key: str,
    *,
    replace: bool,
) -> None:
    key_path.parent.mkdir(parents=True, exist_ok=True)
    move_command = (
        "Move-Item -LiteralPath $temporary -Destination $path -Force"
        if replace
        else "[IO.File]::Move($temporary, $path)"
    )
    script = """
$path = $env:VIDEO_REPLICA_DPAPI_KEY_PATH
if ([string]::IsNullOrWhiteSpace($path)) { throw "DPAPI key path is required" }
$plain = [Console]::In.ReadToEnd()
$secure = ConvertTo-SecureString $plain -AsPlainText -Force
$encrypted = ConvertFrom-SecureString $secure
$temporary = "$path.$PID.tmp"
[IO.File]::WriteAllText($temporary, $encrypted, [Text.UTF8Encoding]::new($false))
try { MOVE_COMMAND }
finally { if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary -Force } }
""".replace("MOVE_COMMAND", move_command).strip()
    result = _run(
        runner,
        (
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            script,
        ),
        input=key,
        env={**os.environ, WINDOWS_KEY_PATH_ENV: str(key_path)},
    )
    if result.returncode != 0:
        raise LocalSettingsKeyStoreError("unable to create the Windows DPAPI settings key")


def _read_windows_key(runner: CommandRunner, key_path: Path) -> str:
    script = """
$path = $env:VIDEO_REPLICA_DPAPI_KEY_PATH
if ([string]::IsNullOrWhiteSpace($path)) { throw "DPAPI key path is required" }
$encrypted = [IO.File]::ReadAllText($path)
$secure = ConvertTo-SecureString $encrypted
$pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
try { [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer) }
finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer) }
""".strip()
    result = _run(
        runner,
        (
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            script,
        ),
        env={**os.environ, WINDOWS_KEY_PATH_ENV: str(key_path)},
    )
    if result.returncode != 0:
        raise LocalSettingsKeyStoreError("unable to decrypt the Windows DPAPI settings key")
    key = result.stdout.strip()
    if not key:
        raise LocalSettingsKeyStoreError("Windows DPAPI returned an empty settings key")
    return key
