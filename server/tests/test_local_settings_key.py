from __future__ import annotations

import subprocess
from pathlib import Path

from cryptography.fernet import Fernet

from app.local_settings_key import load_or_create_local_settings_key, persist_local_settings_key


def completed(
    args: list[str],
    *,
    returncode: int = 0,
    stdout: str = "",
    stderr: str = "",
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args, returncode, stdout, stderr)


def test_macos_keychain_reuses_existing_key_without_writing() -> None:
    expected = Fernet.generate_key().decode("ascii")
    calls: list[list[str]] = []

    def run(args: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return completed(args, stdout=f"{expected}\n")

    actual = load_or_create_local_settings_key(
        platform="darwin",
        environ={"USER": "tester"},
        runner=run,
    )

    assert actual == expected
    assert len(calls) == 1
    assert calls[0][:2] == ["security", "find-generic-password"]


def test_macos_keychain_receives_new_key_over_stdin_not_command_line() -> None:
    generated = Fernet.generate_key().decode("ascii")
    calls: list[tuple[list[str], dict[str, object]]] = []

    def run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append((args, kwargs))
        if args[1] == "find-generic-password":
            return completed(args, returncode=44, stderr="not found")
        return completed(args)

    actual = load_or_create_local_settings_key(
        platform="darwin",
        environ={"USER": "tester"},
        runner=run,
        generated_key=generated,
    )

    assert actual == generated
    assert [call[0][1] for call in calls] == [
        "find-generic-password",
        "add-generic-password",
    ]
    assert calls[1][1]["input"] == f"{generated}\n{generated}\n"
    assert generated not in " ".join(calls[1][0])


def test_macos_keychain_persists_an_explicit_key_without_argv_exposure() -> None:
    key = Fernet.generate_key().decode("ascii")
    calls: list[tuple[list[str], dict[str, object]]] = []

    def run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append((args, kwargs))
        return completed(args)

    persisted = persist_local_settings_key(
        key,
        platform="darwin",
        environ={},
        runner=run,
    )

    assert persisted is True
    assert "-U" in calls[0][0]
    assert calls[0][1]["input"] == f"{key}\n{key}\n"
    assert key not in " ".join(calls[0][0])


def test_windows_dpapi_reuses_existing_encrypted_key_file(tmp_path: Path) -> None:
    expected = Fernet.generate_key().decode("ascii")
    key_file = tmp_path / "VideoReplicaWorkbench" / "secrets" / "settings-key.dpapi"
    key_file.parent.mkdir(parents=True)
    key_file.write_text("dpapi-ciphertext", encoding="utf-8")
    calls: list[tuple[list[str], dict[str, object]]] = []

    def run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append((args, kwargs))
        return completed(args, stdout=f"{expected}\n")

    actual = load_or_create_local_settings_key(
        platform="win32",
        environ={"LOCALAPPDATA": str(tmp_path)},
        runner=run,
    )

    assert actual == expected
    assert key_file.read_text(encoding="utf-8") == "dpapi-ciphertext"
    assert len(calls) == 1
    assert expected not in " ".join(calls[0][0])
    assert str(key_file) not in calls[0][0]
    assert calls[0][1]["env"]["VIDEO_REPLICA_DPAPI_KEY_PATH"] == str(key_file)


def test_windows_dpapi_receives_new_key_over_stdin_not_command_line(tmp_path: Path) -> None:
    generated = Fernet.generate_key().decode("ascii")
    calls: list[tuple[list[str], dict[str, object]]] = []

    def run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append((args, kwargs))
        return completed(args)

    actual = load_or_create_local_settings_key(
        platform="win32",
        environ={"LOCALAPPDATA": str(tmp_path)},
        runner=run,
        generated_key=generated,
    )

    assert actual == generated
    assert len(calls) == 1
    assert calls[0][1]["input"] == generated
    assert generated not in " ".join(calls[0][0])
    assert "[IO.File]::Move" in " ".join(calls[0][0])
    key_path = tmp_path / "VideoReplicaWorkbench" / "secrets" / "settings-key.dpapi"
    assert str(key_path) not in calls[0][0]
    assert calls[0][1]["env"]["VIDEO_REPLICA_DPAPI_KEY_PATH"] == str(key_path)


def test_windows_dpapi_can_replace_key_only_during_validated_import(tmp_path: Path) -> None:
    key = Fernet.generate_key().decode("ascii")
    calls: list[tuple[list[str], dict[str, object]]] = []

    def run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append((args, kwargs))
        return completed(args)

    persisted = persist_local_settings_key(
        key,
        platform="win32",
        environ={"LOCALAPPDATA": str(tmp_path)},
        runner=run,
    )

    assert persisted is True
    assert calls[0][1]["input"] == key
    assert "Move-Item" in " ".join(calls[0][0])
    assert key not in " ".join(calls[0][0])
    key_path = tmp_path / "VideoReplicaWorkbench" / "secrets" / "settings-key.dpapi"
    assert str(key_path) not in calls[0][0]
    assert calls[0][1]["env"]["VIDEO_REPLICA_DPAPI_KEY_PATH"] == str(key_path)
