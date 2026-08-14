from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from app.generation import build_h3_request
from app.settings import mask_config, normalize_config

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_h3_provider_request_contract_does_not_embed_credentials() -> None:
    request = build_h3_request(
        prompt_text="只生成视频，不携带任何 Provider 凭证。",
        first_frame_url="local://first-frame.png",
        duration_seconds=10,
        resolution="768P",
    )
    serialized = json.dumps(request, ensure_ascii=False).lower()

    assert set(request) == {"model", "content", "resolution", "duration", "ratio"}
    assert "authorization" not in serialized
    assert "api_key" not in serialized
    assert "token" not in serialized
    assert "secret" not in serialized


def test_provider_config_normalization_masks_secret_like_fields() -> None:
    normalized = normalize_config(
        {
            "api_key": " metaso-secret-token ",
            "Authorization": " Bearer should-not-echo ",
            "access_key_id": " cloud-access-id ",
            "base_url": " https://metaso.example/api ",
        }
    )

    assert normalized == {
        "api_key": "metaso-secret-token",
        "Authorization": "Bearer should-not-echo",
        "access_key_id": "cloud-access-id",
        "base_url": "https://metaso.example/api",
    }
    assert mask_config(normalized) == {
        "api_key": "********oken",
        "Authorization": "********echo",
        "access_key_id": "********s-id",
        "base_url": "https://metaso.example/api",
    }


@pytest.mark.skipif(
    not (REPO_ROOT / "scripts" / "verify_no_secrets.sh").exists(),
    reason="secret scan script is not installed",
)
def test_secret_scan_script_passes_on_repository_contract_surface() -> None:
    result = subprocess.run(
        ["bash", "scripts/verify_no_secrets.sh"],
        cwd=REPO_ROOT,
        check=False,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr
