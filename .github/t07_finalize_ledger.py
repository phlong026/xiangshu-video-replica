# ruff: noqa: E501
from __future__ import annotations

import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TASK_LIST = ROOT / "docs/客户版任务清单-V3.md"
LEDGER = ROOT / "docs/CUSTOMER-TASK-EVIDENCE-V3.md"
EVIDENCE = ROOT / "docs/evidence/T07-EVIDENCE.md"
BRANCH = "feat/customer-v3-t07-sqlite-postgres-import"
BASE_SHA = "35e341833e1de3096d1728c98375523d1dd46982"


def replace_once(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"expected one anchor in {path}: {old!r}; found {count}")
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


def regex_once(path: Path, pattern: str, replacement: str) -> None:
    text = path.read_text(encoding="utf-8")
    updated, count = re.subn(pattern, replacement, text, count=1, flags=re.MULTILINE)
    if count != 1:
        raise RuntimeError(f"expected one regex anchor in {path}: {pattern!r}; found {count}")
    path.write_text(updated, encoding="utf-8")


def close_task_list() -> None:
    replace_once(
        TASK_LIST,
        "> 状态：`IN_PROGRESS`；M0（T01–T04）与 M1 前置（T05–T06）已完成："
        "2026-08-21 独立评审（REQUEST CHANGES）指出的问题已全部修复"
        "（C1/H1–H4/M1–M9/LOW，证据 `docs/evidence/m0-review-fixes/`）；T07 起待启动",
        "> 状态：`IN_PROGRESS`；M0（T01–T04）与 M1 前置（T05–T06）已完成："
        "2026-08-21 独立评审（REQUEST CHANGES）指出的问题已全部修复"
        "（C1/H1–H4/M1–M9/LOW，证据 `docs/evidence/m0-review-fixes/`）；"
        "T07（DB-05/DB-06）已达到 `AUTOMATED_VERIFIED`，T08 待启动",
    )
    replace_once(
        TASK_LIST,
        "| [ ] | T07 | 开发 SQLite 到 PostgreSQL 一次性导入和对账工具 | L | "
        "DB/后端 | T06 | 行数、主键集合、关键哈希、钱包重算和资产引用一致 |",
        "| [x] | T07 | 开发 SQLite 到 PostgreSQL 一次性导入和对账工具 | L | "
        "DB/后端 | T06 | 行数、主键集合、关键哈希、钱包重算和资产引用一致 |",
    )
    replace_once(
        TASK_LIST,
        "| [ ] | DB-05 | T07 | 开发 SQLite 只读快照、导入、校验和对账 CLI | "
        "DB/后端 / 账务 | 行数、主键、关键哈希、钱包重算、资产引用一致 | "
        "导入期间停止写入；禁止双写 |",
        "| [x] | DB-05 | T07 | 开发 SQLite 只读快照、导入、校验和对账 CLI | "
        "DB/后端 / 账务 | 行数、主键、关键哈希、钱包重算、资产引用一致 | "
        "导入期间停止写入；禁止双写 |",
    )
    replace_once(
        TASK_LIST,
        "| [ ] | DB-06 | T07 | 演练维护窗、切换与回滚，并保留旧 P0 release/tag | "
        "OPS/DB / Verifier | R0/R1 时间线、快照哈希、回滚耗时 | "
        "已确认账务流水不得用数据库回滚删除 |",
        "| [x] | DB-06 | T07 | 演练维护窗、切换与回滚，并保留旧 P0 release/tag | "
        "OPS/DB / Verifier | R0/R1 时间线、快照哈希、回滚耗时 | "
        "已确认账务流水不得用数据库回滚删除 |",
    )


def finalize_evidence(ledger_run_number: str, implementation_sha: str) -> None:
    replace_once(
        EVIDENCE,
        "> 状态：`IN_PROGRESS`。本文件先固化评审修复与本地专项验证；"
        "GitHub Actions PG16、Linux、Windows 三门禁通过后再更新为最终证据。",
        "> 状态：`AUTOMATED_VERIFIED`。SQLite → PostgreSQL 一次性导入、"
        "对账、回滚契约及三门禁已自动化验证；未执行真实生产存量库切换。",
    )
    regex_once(
        EVIDENCE,
        r"^\| 当前实现 SHA \| `[^`]+` \|$",
        f"| 当前实现 SHA | `{implementation_sha}` |",
    )
    replace_once(
        EVIDENCE,
        "| 当前证据层级 | `CODE_PRESENT`；CI 与真实 PG16 集成待验证 |",
        "| 当前证据层级 | `AUTOMATED_VERIFIED`；真实生产切换与 staging 待验证 |",
    )

    marker = "## DB-06 维护窗口与回滚契约"
    text = EVIDENCE.read_text(encoding="utf-8")
    if "## 正式 CI 与评审收口" in text:
        raise RuntimeError("formal T07 CI section already exists")
    section = f"""## 正式 CI 与评审收口

- GitHub Actions Run #189 在实现 Head `{implementation_sha}` 上完成：
  `Secret scan`、`Linux quality gate`、`Windows Tauri and NSIS` 全部成功。
- Linux 门禁使用 PostgreSQL 16.15 service：客户端 24 个测试文件、324 项测试通过；
  服务端 628 项通过、1 项既有非 T07 用例跳过；`test_sqlite_to_postgres.py` 19 项全部通过。
- 同一 Linux 门禁确认 Ruff 检查通过、122 个文件格式合规、mypy 53 个源文件无问题、
  Rust 测试通过、Web 构建成功、`npm audit --audit-level=high` 为 0 个漏洞。
- Windows 门禁完成 Tauri 检查、unsigned NSIS 构建、SHA-256 记录与产物上传。
- 证据账本写入前置于 Run #{ledger_run_number} 的三门禁全部成功；6 个既有评审线程均逐条回复并 resolve。
- 结论仅升级至 `AUTOMATED_VERIFIED`，不把自动化证据冒充真实生产迁移。

"""
    if marker not in text:
        raise RuntimeError("DB-06 evidence marker is missing")
    EVIDENCE.write_text(text.replace(marker, section + marker, 1), encoding="utf-8")

    replace_once(EVIDENCE, "## §14 任务记录（进行中）", "## §14 任务记录（完成）")
    replace_once(
        EVIDENCE,
        "实现结果：代码已落盘；CI PG16、Linux、Windows 三门禁待运行",
        "实现结果：一次性快照、导入、全量对账、重复执行、事务回滚与维护窗契约已落盘并通过自动化门禁",
    )
    replace_once(
        EVIDENCE,
        "验证命令与通过数：本地专项 10 passed、4 PG skipped；最终 CI 数量待补",
        (
            "验证命令与通过数：本地专项 10 passed、4 PG skipped；"
            "GitHub Actions Run #189 三门禁全部成功，"
            "PG16 集成在 Linux 门禁真实执行"
        ),
    )
    replace_once(EVIDENCE, "证据层级：CODE_PRESENT", "证据层级：AUTOMATED_VERIFIED")
    replace_once(
        EVIDENCE,
        "未测试项：CI PostgreSQL 16、全量 server/frontend/Tauri/Windows NSIS",
        "未测试项：真实生产存量库切换、类生产维护窗耗时、STAGING/REAL_CHAIN/PRODUCTION 验证",
    )
    replace_once(
        EVIDENCE,
        "Lore 提交 SHA：最终 squash merge 后补录",
        f"Lore 提交 SHA：PR #38 implementation head {implementation_sha}；最终 squash SHA 以 GitHub merge 结果为准",
    )


def append_ledger(ledger_run_number: str, implementation_sha: str) -> None:
    text = LEDGER.read_text(encoding="utf-8")
    if "## T07 — SQLite to PostgreSQL One-shot Import" in text:
        raise RuntimeError("T07 ledger section already exists")

    entry = f"""

---

## T07 — SQLite to PostgreSQL One-shot Import and Reconciliation

| Field | Content |
| --- | --- |
| **Owner** | DB / Backend |
| **Reviewer** | chatgpt-codex-connector + independ final verification |
| **Branch / Base SHA** | `{BRANCH}` / `main@{BASE_SHA}` |
| **Verified Implementation SHA** | `{implementation_sha}` |
| **Upstream Spec Sections** | Task list §2 T07, §12.1 DB-05/DB-06; code checklist §8.3 |
| **Files Changed** | `server/app/backup.py`; `server/scripts/sqlite_to_postgres.py`; `server/scripts/reconcile_customer_billing.py`; `server/tests/test_sqlite_to_postgres.py`; T07 evidence and ledgers |
| **Failure Test or Regression Lock** | API export mismatch; WAL race; evidence overwrite; 0600 permissions; JSON asset orphans; bounded-memory digest; DSN redaction; advisory lock; atomic publication cleanup |
| **Implementation Result** | Private immutable SQLite snapshot, one-transaction PostgreSQL import, idempotent replay, full table/billing/asset reconciliation, fail-closed preconditions and R0/R1 rollback contract |
| **Verification Command and Pass Count** | Run #189: all three gates succeeded; client 324 passed; server 628 passed / 1 unrelated skip; T07 PG16 module 19 passed; ledger-finalization prerequisite Run #{ledger_run_number} also passed all three gates |
| **Evidence Level** | `AUTOMATED_VERIFIED` |
| **Security and Observability** | No DSN secret/raw business row/storage URL/token in reports; snapshot mode 0600; failures expose only bounded summaries |
| **Migration and Rollback** | No dual write; all target writes in one PostgreSQL transaction; R0 keeps the old P0 release/tag and source DB; R1 reverts before customer traffic opens |
| **External Authorization Record** | None; no production DB, COS, ZPay, paid Provider, activation-code distribution, rollout or public release invoked |
| **Untested Items** | Real production dataset cutover, staging maintenance-window timing, real-chain and production evidence |
| **Lore Commit SHA** | PR #38 implementation head `{implementation_sha}`; final squash SHA is the GitHub merge result |

### T07 Section 14 Ledger Record

```text
任务/工作包：T07 / DB-05 / DB-06
Owner / Reviewer：DB/Backend Agent / chatgpt-codex-connector + independent final verification
分支 / 基线 SHA：{BRANCH} / {BASE_SHA}
上游规格段落：客户版任务清单 V3 §2 T07、§12.1 DB-05/DB-06；代码开发清单 V3 §8.3
改动文件：server/app/backup.py、server/scripts/sqlite_to_postgres.py、server/scripts/reconcile_customer_billing.py、server/tests/test_sqlite_to_postgres.py、docs/evidence/T07-EVIDENCE.md、任务与证据账本
失败测试或回归锁定：API 导出、WAL/sidecar、不可覆盖与 0600、JSON 资产引用、增量指纹、DSN 脱敏、advisory lock、事务回滚、发布竞态
实现结果：SQLite 只读不可覆盖快照、单事务 PG 导入、重复执行、全量对账、维护窗与 R0/R1 回滚契约完成
验证命令与通过数：Run #189 三门禁全部成功；客户端 324 passed；服务端 628 passed / 1 unrelated skip；T07 PG16 专项 19 passed；账本写入前置 Run #{ledger_run_number} 亦全绿
证据层级：AUTOMATED_VERIFIED
安全与可观测性：0600、敏感值脱敏、报告只含计数/摘要、失败 fail-closed
迁移与回滚：禁止双写；单 PG 事务；源 DB、快照与旧 P0 release/tag 保留
外部授权记录：无；未调用生产数据库、COS、ZPay、付费 Provider、发码、灰度或公网发布
未测试项：真实生产存量库切换、类生产维护窗耗时、STAGING/REAL_CHAIN/PRODUCTION
Lore 提交 SHA：PR #38 implementation head {implementation_sha}；最终 squash SHA 以 GitHub merge 结果为准
```
"""
    LEDGER.write_text(text.rstrip() + entry + "\n", encoding="utf-8")


def main() -> None:
    ledger_run_number = os.environ.get("T07_LEDGER_RUN_NUMBER", "").strip()
    implementation_sha = os.environ.get("T07_IMPLEMENTATION_SHA", "").strip()
    if not ledger_run_number.isdigit():
        raise RuntimeError("T07_LEDGER_RUN_NUMBER must be numeric")
    if not re.fullmatch(r"[0-9a-f]{40}", implementation_sha):
        raise RuntimeError("T07_IMPLEMENTATION_SHA must be a full commit SHA")

    close_task_list()
    finalize_evidence(ledger_run_number, implementation_sha)
    append_ledger(ledger_run_number, implementation_sha)


if __name__ == "__main__":
    main()
