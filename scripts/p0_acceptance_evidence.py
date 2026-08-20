#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ACCEPTANCE_TESTS = (
    "server/tests/test_zpay.py::test_zpay_signature_matches_official_sorting_and_unencoded_values",
    "server/tests/test_recharge_orders.py::test_create_recharge_order_builds_server_owned_zpay_form",
    "server/tests/test_recharge_orders.py::test_create_recharge_order_rejects_invalid_amounts",
    "server/tests/test_payments.py::test_valid_notify_credits_wallet_and_duplicate_notify_is_idempotent",
    "server/tests/test_payments.py::test_concurrent_duplicate_notifies_credit_once",
    "server/tests/test_payments.py::test_return_page_is_display_only_even_with_valid_payment_fields",
    "server/tests/test_payments.py::test_control_sync_paid_result_uses_the_same_credit_service",
    "server/tests/test_wallet_billing_service.py::test_reserve_moves_one_credit_and_is_idempotent",
    "server/tests/test_wallet_billing_service.py::test_finalize_success_settles_only_an_archived_result",
    "server/tests/test_wallet_billing_service.py::test_real_provider_result_must_be_archived_in_cos",
    "server/tests/test_wallet_billing_service.py::test_finalize_failure_or_cancellation_releases_credit_once",
    "server/tests/test_media.py::test_media_storage_prefers_cos_when_configured",
    "server/tests/test_generation.py::test_archived_generation_settles_once_but_archive_failure_stays_reserved",
    "server/tests/test_generation.py::test_undownloadable_archived_result_does_not_settle",
    "server/tests/test_generation.py::test_terminal_provider_failure_releases_reserved_credit",
)

VERIFIED_BEHAVIORS = (
    "固定签名与服务端下单参数",
    "重复和并发回调只入账一次",
    "同步返回页不入账，主动查单复用同一入账服务",
    "任务冻结幂等且余额不足不部分写入",
    "归档且可下载后才结算，真实 Provider 结果必须进入 COS",
    "归档或下载失败保持冻结，Provider 失败或取消只返还一次",
)

NOT_VERIFIED = (
    "real ZPay payment",
    "real COS archive and signed download",
    "real Provider generation",
)


def _git_value(repo_root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo_root,
        check=False,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else "unknown"


def _manifest(
    *,
    repo_root: Path,
    command: list[str],
    returncode: int,
    generated_at: str,
) -> dict[str, Any]:
    passed = returncode == 0
    return {
        "schema_version": "internal-p0.fake-acceptance.v1",
        "generated_at": generated_at,
        "verification_level": "LOCALLY_VERIFIED" if passed else "FAILED",
        "scope": "fake_and_contract_only",
        "status": "passed" if passed else "failed",
        "source": {
            "branch": _git_value(repo_root, "branch", "--show-current"),
            "commit": _git_value(repo_root, "rev-parse", "HEAD"),
            "dirty": bool(
                _git_value(repo_root, "status", "--porcelain", "--untracked-files=all")
            ),
        },
        "command": command,
        "tests": list(ACCEPTANCE_TESTS),
        "verified_behaviors": list(VERIFIED_BEHAVIORS),
        "not_verified": list(NOT_VERIFIED),
        "redaction": {
            "merchant_key_saved": False,
            "payment_signature_saved": False,
            "full_callback_query_saved": False,
        },
    }


def _markdown(manifest: dict[str, Any], command: list[str]) -> str:
    passed = manifest["status"] == "passed"
    verified = "\n".join(f"- {item}" for item in VERIFIED_BEHAVIORS)
    missing = "\n".join(f"- {item}" for item in NOT_VERIFIED)
    return f"""# 内部运营 P0-6 Fake 验收证据

- 结论：{manifest["verification_level"]}
- 自动化：{"通过" if passed else "失败"}
- 范围：仅固定签名样本、Fake 回调和本地存储合同
- 时间：{manifest["generated_at"]}
- 提交：{manifest["source"]["commit"]}
- 分支：{manifest["source"]["branch"]}

## 已验证

{verified}

## 未验证

{missing}

以上真实链路必须取得授权后另行执行；本证据不能替代真实 ZPay、COS 或 Provider 验收。

## 可复现命令

```text
{shlex.join(command)}
```

详细 pytest 输出见同目录 `pytest.log`。证据不保存商户密钥、完整签名或完整回调查询串。
"""


def run(output_dir: Path) -> int:
    repo_root = Path(__file__).resolve().parents[1]
    resolved_output = output_dir if output_dir.is_absolute() else repo_root / output_dir
    resolved_output.mkdir(parents=True, exist_ok=True)
    command = [
        "python",
        "-m",
        "pytest",
        "--rootdir",
        "server",
        *ACCEPTANCE_TESTS,
        "-q",
    ]
    completed = subprocess.run(
        [sys.executable, *command[1:]],
        cwd=repo_root,
        check=False,
        capture_output=True,
        text=True,
    )
    combined_log = completed.stdout
    if completed.stderr:
        combined_log += "\n" + completed.stderr
    (resolved_output / "pytest.log").write_text(combined_log, encoding="utf-8")

    generated_at = datetime.now(UTC).replace(microsecond=0).isoformat()
    manifest = _manifest(
        repo_root=repo_root,
        command=command,
        returncode=completed.returncode,
        generated_at=generated_at,
    )
    (resolved_output / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (resolved_output / "P0-6-Fake验收证据.md").write_text(
        _markdown(manifest, command),
        encoding="utf-8",
    )
    print(resolved_output)
    return completed.returncode


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate redacted internal P0 fake evidence"
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("output/internal-p0-acceptance")
        / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ"),
    )
    args = parser.parse_args()
    raise SystemExit(run(args.output_dir))


if __name__ == "__main__":
    main()
