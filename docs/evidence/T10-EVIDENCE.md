# T10 — Activation Code Catalog Schema (ACT-01)

## Task Information

| Field | Value |
| --- | --- |
| **Task ID** | T10 / ACT-01 |
| **Owner** | DB/后端 (Agent) |
| **Reviewer** | chatgpt-codex-connector (PR review) |
| **Branch / Base SHA** | `feat/customer-v3-t10-activation-code-schema` / base `4cc04b3` (PR #40 squash) |
| **Date** | 2026-08-22 |
| **Evidence Level** | `AUTOMATED_VERIFIED` (real PostgreSQL 16 fixture; red → green) |

## Exit-Gate Verification

Task exit gate: *一码一户、状态机、绑定唯一、可追溯记录通过 PG 验证；数据库不得保存激活码明文* — delivered as revision `027_activation_code_catalog` plus 9 fail-first PG tests (`server/tests/test_activation_code_schema.py`), every invariant proven by PostgreSQL constraints (never application code):

- **One code per user** — partial unique index `uq_activation_codes_bound_user_current` over `ACTIVE`/`SUSPENDED` bindings; revoked rows keep their binding for audit without squatting the user slot, and `activation_code_activations.user_id UNIQUE` makes activation a once-per-user fact.
- **Status machine** — `ISSUED → ACTIVE` with `SUSPENDED`/`REVOKED` side states (dev doc §12.1: activation validates `ISSUED` and flips to `ACTIVE` with a binding); the full shape matrix is one CHECK (`ck_activation_codes_status_shape`): ISSUED must be unbound and untouched, ACTIVE must carry binding + `activated_at`, SUSPENDED requires `suspended_at`, REVOKED requires `revoked_at`.
- **Traceable records** — `activation_code_deliveries` (channel / external order / recipient ref / actor FK) and `activation_code_exports` (AEAD ciphertext + SHA-256 digest + key version + short-lived expiry + one-time download audit); every catalog row traces to a real `users.id`.
- **No plaintext** — exact column sets are asserted per table (`test_catalog_tables_and_columns`): codes store only `code_digest` + `digest_key_version` + `masked_code`; exports store only AEAD `ciphertext` + `ciphertext_sha256`. No plaintext column can slip in without breaking the assertion.

## 1. Tables (dev doc §5 / §11.2, code checklist §3.3)

| Table | Contents |
| --- | --- |
| `activation_code_batches` | frozen commercial snapshots (`face_value_fen`, `unit_price_fen_snapshot`, `credits_snapshot` — later price changes never rewrite history), `quantity`, `activation_expires_at > created_at`, `status IN (OPEN, CLOSED)`, creator FK |
| `activation_codes` | global-unique `code_digest` (CSPRNG never repeats — a cross-batch duplicate means a broken generator and fails loudly), `digest_key_version >= 1`, `masked_code`, 4-state machine CHECK, `bound_user_id` FK |
| `activation_code_deliveries` | channel / `external_order_ref` / `recipient_ref` / `delivered_by_user_id` FK / `delivered_at` / note; multiple delivery records per code allowed (channel corrections) |
| `activation_code_exports` | AEAD `ciphertext` + `ciphertext_sha256` + `key_version >= 1` + `expires_at > created_at` (short-lived) + `downloaded_at` / `downloaded_by_user_id` (one-time download audit) |
| `activation_code_activations` | one-shot fact: `code_id` / `user_id` / `recharge_order_id` each UNIQUE (§11.3), `first_device_id` nullable (FK attaches with the T16 device revision under the append-only fix rule) |

## 2. Migration Semantics

- PostgreSQL-only (025/026 precedent): customer production source of truth; SQLite stays the internal P0 runtime where activation codes are not sold.
- `downgrade()` refuses loudly (`RuntimeError`) once any activation fact exists — the fact chains the customer user, PAID first-charge order and CHARGE ledger rows; an unused catalog downgrades symmetrically (drop 5 tables + indexes, reverse order).
- Multi-step downgrades run inside one transactional-DDL transaction: a blocked 027→026→025 attempt rolls back atomically to head (regression-locked in `test_pg_billing_constraints_downgrade_guard`, 025-guard precedent).

## Files Changed

| File | Change |
| --- | --- |
| `server/migrations/versions/027_activation_code_catalog.py` | new (frozen name): 5 tables, 18 CHECK constraints (incl. timestamp-cast expiry checks and the binding↔activation coupling), 4 unique constraints (`code_digest` + the activation triple-unique) plus the partial unique index, 5 secondary indexes, downgrade guard |
| `server/tests/test_activation_code_schema.py` | new: 9 fail-first PG tests (tables/columns, batch shapes, digest uniqueness, status machine, one-binding-per-user, delivery traceability, export ciphertext/expiry, one-shot activation facts, downgrade) |
| `server/tests/test_postgres_migrations.py` | head assertions 026→027 (rehearsal, re-upgrade, wallet guard stay-at-head) and the 026 downgrade-guard test adapted to the longer chain (`-2`, transactional rollback documented) |
| `server/scripts/reconcile_customer_billing.py` | `PG_ONLY_TABLES` extended with the five revision-027 catalog tables: empty catalog tables on the PG target head pass the T07 import contract, any row fails closed (026 `admin_sessions` precedent) |
| `server/scripts/sqlite_to_postgres.py` | table-contract comment updated for the 026/027 PG-only set |
| `server/tests/test_sqlite_to_postgres.py` | new fail-first contract test (empty catalog accepted + a catalog row fails closed) and the `validate_revision_pair` head updated to 027 |
| `server/tests/test_db.py`, `test_character_domain.py`, `test_characters.py`, `test_internal_billing.py`, `test_recharge_orders.py`, `test_settings.py` | SQLite-lane head assertions 026→027 (the 027 guard is a no-op on SQLite, so the recorded version moves with the head) |

## Pre-PR Review Fixes

Code review before the PR (session-internal, T09 precedent) surfaced three findings, all substantively fixed with fail-first regression locks:

1. **P2 — lexical expiry CHECK**: `activation_expires_at > created_at` / `expires_at > created_at` compared TEXT lexically across two formats (space-separated `CURRENT_TIMESTAMP` default vs ISO-8601 `'T'` application timestamps): a same-day *earlier* moment passed because `'T' > ' '` — exactly the short-lived export case the CHECK exists for. Both checks now cast to `timestamptz` (invalid text fails the cast loudly); regression-locked with `CURRENT_DATE::text || 'T00:00:00+00:00'` inserts on both tables (red before the fix, green after).
2. **P3 — binding↔activation coupling**: SUSPENDED/REVOKED branches of the status matrix did not force `bound_user_id` and `activated_at` to appear together, so a direct-write bug could land a binding without an activation timestamp (squatting the partial-unique user slot). New `ck_activation_codes_binding_activation_coupled` (`(bound_user_id IS NULL) = (activated_at IS NULL)`) plus two red→green matrix cases.
3. **P3 — evidence counts**: the constraint census in this file was corrected to the actual migration (18 CHECK / 4 UNIQUE + partial unique index / 5 secondary indexes).

The same lexical-comparison pattern pre-exists in published revision 026 (`admin_sessions`); per the append-only rule it stays untouched here and is queued for a later fix revision outside T10 scope.

## Regression

```
# PostgreSQL fixture up (scripts/pg-fixture.sh start; docker customer-v3-pg-test, PG16 :5433)
$ uv run python -m pytest tests/test_activation_code_schema.py tests/test_postgres_migrations.py tests/test_sqlite_to_postgres.py -q
45 passed                       # 9 T10 catalog cases (incl. the three review locks) + 10 migration-suite cases + 26 T07 import cases
$ uv run python -m pytest tests -q
687 passed, 2 warnings          # full suite on the PG fixture (zero regressions)
$ uv run ruff check . && uv run ruff format --check . && uv run mypy app
all green
# No client/e2e/Tauri changes in this task; npm run check gates re-verified in CI
```

## Section 14 Ledger Record

```text
任务/工作包：T10 / ACT-01
Owner / Reviewer：DB/后端（Agent 执行）/ chatgpt-codex-connector（PR 评审）
分支 / 基线 SHA：feat/customer-v3-t10-activation-code-schema / 基线 4cc04b3（PR #40 squash）
上游规格段落：客户版任务清单 V3 §3 T10、§12.2 ACT-01；代码开发清单 V3 §3.3（027_activation_code_catalog.py 冻结名）；激活码开发文档 §5/§11.2/§11.3/§12.1；测试与验收规格
改动文件：server/migrations/versions/027_activation_code_catalog.py（新增 5 表全约束）、server/tests/test_activation_code_schema.py（新增 9 用例）、server/tests/test_postgres_migrations.py（head 断言与 downgrade guard 适配 027 链）、server/scripts/reconcile_customer_billing.py（PG_ONLY_TABLES 纳入 5 张 027 目录表）、server/scripts/sqlite_to_postgres.py（注释）、server/tests/test_sqlite_to_postgres.py（新增空目录接受/有行拒收合同测试 + validate_revision_pair head 027）、test_db/test_character_domain/test_characters/test_internal_billing/test_recharge_orders/test_settings 六个 SQLite 车道套件 head 断言 026→027 联动
失败测试或回归锁定：先红后绿——9 用例锁定 5 表精确列集（无明文列红线）、批次形状（status/正数快照/过期窗口/creator FK）、码摘要全局唯一、状态机形状矩阵（ISSUED 无绑定、ACTIVE 绑定+时间戳、SUSPENDED/REVOKED 证明时间戳、UNASSIGNED/ASSIGNED 拒收）、当前有效绑定一户一码（部分唯一索引）、发放可追溯（actor FK/非空渠道）、导出仅密文（AEAD+SHA256+短时效+key version）、激活事实三重唯一（code/user/首充订单）、downgrade 拒绝已有激活事实且多步降级事务性回滚
实现结果：027_activation_code_catalog 落地批次/码/发放/导出/激活事实 5 表（PG-only），全部不变量由数据库约束证明；码仅存 HMAC 摘要+key version+掩码；激活事实链（user/PAID 首充订单/CHARGE）禁止 downgrade 删除；first_device_id 留待 T16 设备迁移按追加修复规则补 FK
验证命令与通过数：test_activation_code_schema 9 passed + 迁移套件 19 passed + test_sqlite_to_postgres 26 passed；全量与 lint 数字见 PR；CI 三门禁全绿
证据层级：AUTOMATED_VERIFIED
安全与可观测性：无明文激活码入库（列集断言锁定）；摘要+版本化 key；导出仅 AEAD 密文+SHA256；所有操作行追溯真实 users.id
迁移与回滚：新迁移 027（冻结名）；PG-only（SQLite 车道零变化）；空目录 downgrade 对称；有激活事实时 fail-loud
外部授权记录：无；未调用真实 ZPay/COS/付费 Provider/发码/灰度/公网发布
未测试项：应用层（T11 CSPRNG 生成/HMAC/AEAD 导出、T12 管理 API）；激活事务链路（T13）；设备 FK（T16）；STAGING/REAL_CHAIN/PRODUCTION
Lore 提交 SHA：见 PR squash 合并 SHA
```
