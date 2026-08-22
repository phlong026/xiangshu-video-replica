# T12 — Admin Activation Code Management API (ACT-04)

## Task Information

| Field | Value |
| --- | --- |
| **Task ID** | T12 / ACT-04 |
| **Owner** | Backend/Admin (Agent) |
| **Reviewer** | session-internal code review |
| **Branch / Base SHA** | `feat/customer-v3-t12-admin-activation-routes` / base `d7e293d` (T11, PR #42 squash) |
| **Date** | 2026-08-22 |
| **Evidence Level** | `AUTOMATED_VERIFIED` (real PostgreSQL 16 fixture; red → green) |

## Exit-Gate Verification

Task exit gate: *actor、reason、幂等键、request id 和审计齐全* (task list §3 T12) + ACT-04 gate: *RBAC、CSRF、reason、Idempotency-Key、request id 和审计测试；未登录、auditor 写入和缺少二次确认必须拒绝* — delivered as `server/app/admin_activation_routes.py` (frozen name, mounted in `main.py`) over migration `028_admin_write_idempotency`, with 37 fail-first tests (`server/tests/test_admin_activation_routes.py`, on a dedicated migrated PG fixture database):

- **actor** — every route resolves the operator through the T09 `AdminWriter`/`AdminReader` session dependencies (HttpOnly admin cookie + CSRF); the actor's `users.id` lands on the batch, the idempotency snapshot and every catalog event.
- **reason** — the §15 write contract refuses any mutation without `confirm=true` plus a non-blank `reason` (400 `CONFIRMATION_REQUIRED` / `REASON_REQUIRED`); the reason is persisted on deliveries and lifecycle events.
- **幂等键** — `Idempotency-Key` header is mandatory (400 `IDEMPOTENCY_KEY_REQUIRED`); same key + same canonical request replays the stored response with `X-Idempotent-Replay: true`, same key + different body is a 409 `IDEMPOTENCY_CONFLICT`, and concurrent same-key writers serialize on the 028 unique index (two-thread barrier test).
- **request id** — every fresh write returns `X-Request-Id` (UUID) echoed inside the payload, and the replay path restores the original one; lifecycle events persist it.
- **审计** — GENERATED/EXPORTED/DELIVERED/SUSPENDED/RESUMED/REVOKED events land in the append-only `activation_code_events` table with actor + reason + request id.
- **红线** — unauthenticated calls 401; auditor (read role) writes 403; the GET list needs only the reader role; CSRF failures reject.

## 1. The §15 Admin Write Contract

`AdminWriteContract` (pydantic: `confirm`, `reason`) is the shared body shape for every mutation. `_require_write_contract` validates in the documented order key → confirm → reason so the operator gets the first missing element as the error (`400 IDEMPOTENCY_KEY_REQUIRED` / `CONFIRMATION_REQUIRED` / `REASON_REQUIRED`). The internal-P0 `CONTROL_ADMIN_USER_ID` path is untouched — these routes are PG-runtime only and fail closed with 503 `ACTIVATION_SERVICE_UNAVAILABLE` when `pg_transaction` raises (SQLite deployments never expose admin activation management).

## 2. Idempotency Snapshot Layer (revision 028)

`_write_with_idempotency` runs the business write and its snapshot in **one** transaction:

1. `INSERT … ON CONFLICT (actor_user_id, route, idempotency_key_digest) DO NOTHING` — the winner gets the placeholder row id; a concurrent same-key writer blocks on the unique index until the first transaction commits/rolls back (PG insert-wait semantics), so replay never races the original.
2. On key reuse the stored snapshot is loaded; `request_hash` (sha256 of canonical JSON `{route, body}` with the **route template**, never the concrete path) must match and the response columns must be complete, otherwise 409 `IDEMPOTENCY_CONFLICT`. A match replays the stored status + body with `X-Idempotent-Replay: true`.
3. The winner's business result is back-filled (`response_status`, `response_body`) before commit; a business failure rolls the placeholder back with the transaction, releasing the key for an honest retry.

The raw key never reaches the database (sha256 digest — the `admin_sessions` precedent). Migration 028 additionally CHECKs the (status, body) NULL-pairing so a half-written placeholder fails closed at the storage layer. Revision numbering follows the code-checklist §3.1 clause ("实际编号以实施当日 Alembic head 为准"): the admin-write idempotency theme has no frozen number, so it lands as 028 on the then-current head 027, PG-only per the 025–027 precedent (the internal SQLite lane keeps its legacy control path).

**Plaintext red line**: the one-time download route deliberately bypasses the snapshot layer — its response contains the plaintext codes, which must never persist (not in a snapshot, not in a log). The `downloaded_at` one-shot constraint on the exports table is the anti-replay mechanism there, and the write contract (key/confirm/reason) is still enforced for the audit trail.

## 3. Routes (all under `/api/control`)

| Route | Method | Role | Notes |
| --- | --- | --- | --- |
| `/activation-code-batches` | POST | writer | 201; name/face-value/credits/quantity/expiry validation (400 `BATCH_VALIDATION_FAILED`) |
| `/activation-code-batches/{batch_id}/generate` | POST | writer | 201; keys resolved **outside** the transaction (503 `ACTIVATION_KEYS_UNAVAILABLE` without a DB write), batch locked `FOR UPDATE` (404 `BATCH_NOT_FOUND` / 409 `BATCH_NOT_OPEN`), frozen budget enforced (409 `BATCH_BUDGET_EXCEEDED`), export sealed in the same transaction — response carries `export_id` + masked codes only |
| `/activation-code-exports/{export_id}/download` | POST | writer | 200; one-time AEAD package; 404 `EXPORT_NOT_FOUND` / 409 `EXPORT_ALREADY_DOWNLOADED` / 409 `EXPORT_EXPIRED`; bypasses the snapshot layer (plaintext) |
| `/activation-codes/{code_id}/deliver` | POST | writer | 201; channel validation, ISSUED-only transition |
| `/activation-codes/{code_id}/suspend` `/resume` `/revoke` | POST | writer | 200; T11 six-state matrix via `assert_code_transition` (409 `CODE_TRANSITION_INVALID`); `resume` clears `suspended_at` back to NULL per the 027 shape matrix |
| `/activation-codes` | GET | reader | paged list with `batch_id`/`status` filters; items carry masked codes only, never the digest |

Key-version helpers added to `activation_code_service.py` (`highest_code_hmac_key_version`, `highest_export_aead_key_version`, `configured_export_aead_keys`) resolve the highest configured version before the transaction opens and support every configured version on download (rotation window).

## 4. Test Coverage (37 red → green)

| Group | Cases |
| --- | --- |
| Authn/RBAC/CSRF | unauthenticated 401 ×2; auditor write 403; auditor GET list 200; admin GET list 200; CSRF-missing write rejected |
| Batches | happy path lands actor + OPEN status; name blank / face value ≤ 0 / credits ≤ 0 / quantity ≤ 0 / expiry blank → 400 with code `BATCH_VALIDATION_FAILED` |
| Generate | happy path (codes + export + GENERATED/EXPORTED events, masked only in response); unknown batch 404; closed batch 409; budget overrun 409 leaves zero extra rows; missing HMAC key 503 with no DB rows; missing AEAD key 503 with no export row; code-digest uniqueness across the batch |
| Download | happy path returns plaintext once + actor persisted; second download 409 `EXPORT_ALREADY_DOWNLOADED`; expired 409 `EXPORT_EXPIRED`; unknown export 404; no snapshot row is ever written for a download (plaintext red line) |
| Deliver | happy path ISSUED + DELIVERED event + reason; unknown code 404; non-ISSUED source 409; channel blank 400; duplicate delivery on the same code 409 |
| Suspend/Resume/Revoke | ACTIVE→SUSPENDED lands `suspended_at` + event; SUSPENDED→ACTIVE clears `suspended_at`; ISSUED→REVOKED terminal; SUSPENDED→REVOKED; un-activated resume 409 (no SUSPENDED→ISSUED edge); revoked-code suspend 409; revoke event carries reason + request id |
| Listing | filter by batch/status; page shape (masked only) |
| Write contract | one test walks key/confirm/reason rejections across three routes |
| Concurrency | two threads, one barrier, same key on create-batch → both 201-or-replay, exactly one batch row, one snapshot row |

## 5. Head-Roll Maintenance (027 → 028)

028 is PG-only but SQLite `alembic_version` advances too, so nine existing test files' head assertions moved to `028_admin_write_idempotency` (~24 sites incl. `test_db.py` bootstrap checks and the SQLite→PG importer's target head). Two downgrade-guard tests keep their semantics with one more relative step (`-1`→`-2` for the 027 activation-fact guard, `-2`→`-3` for the 026 billing guard, whose after-rollback head assertion is now 028). The T07 cutover tooling stays fail-closed: `admin_write_idempotency` joins `PG_ONLY_TABLES` (reconcile + importer), expected empty on the target head at cutover time, any row is divergent state.

## Files Changed

| File | Change |
| --- | --- |
| `server/app/admin_activation_routes.py` | new (frozen name): §15 write contract, 028 idempotency snapshot layer, 8 routes |
| `server/migrations/versions/028_admin_write_idempotency.py` | new: PG-only snapshot table + unique (actor, route, key digest) + response-pairing CHECK |
| `server/app/activation_code_service.py` | added key-version helpers (`_configured_key_versions`, `highest_code_hmac_key_version`, `highest_export_aead_key_version`, `configured_export_aead_keys`) |
| `server/app/main.py` | mount `admin_activation_router` |
| `server/tests/test_admin_activation_routes.py` | new: 37 fail-first cases on a dedicated migrated fixture DB |
| 9 test files (`test_db`, `test_postgres_migrations`, `test_activation_code_schema`, `test_sqlite_to_postgres`, `test_internal_billing`, `test_recharge_orders`, `test_character_domain`, `test_characters`, `test_settings`) | head assertions 027 → 028; downgrade-guard step counts +1 |
| `server/scripts/reconcile_customer_billing.py` | `admin_write_idempotency` in `PG_ONLY_TABLES` |
| `server/scripts/sqlite_to_postgres.py` | PG-only-tables comment covers 028 |

## PRICE-01 Boundary

The batch-creation API validates the frozen commercial snapshot (name/face value/credits/quantity/expiry) but implements no customer-vs-internal price decision: PRICE-01 (task list §12) explicitly forbids generating **对外销售批次** until that relationship is frozen. The API is ready; the go/no-go for external sales batches stays a PRICE-01 process gate.

## Regression

```text
# PostgreSQL fixture up (scripts/pg-fixture.sh start; docker customer-v3-pg-test, PG16 :5433)
$ uv run python -m pytest tests/test_admin_activation_routes.py -q
37 passed
$ uv run python -m pytest tests -q
747 passed, 2 warnings          # full suite on the PG fixture (zero regressions)
$ uv run ruff check . && uv run ruff format --check . && uv run mypy app
all green
# No client/e2e/Tauri changes in this task; npm run check gates re-verified in CI
```

## Section 14 Ledger Record

```text
任务/工作包：T12 / ACT-04
Owner / Reviewer：后端/管理（Agent 执行）/ 会话内代码评审
分支 / 基线 SHA：feat/customer-v3-t12-admin-activation-routes / 基线 d7e293d（T11 PR #42 squash）
上游规格段落：客户版任务清单 V3 §3 T12、§12.2 ACT-04；代码开发清单 V3（admin_activation_routes.py 冻结名）；激活码开发文档 §11.3 幂等不变量、§15 管理写合同；测试与验收规格 §2
改动文件：server/app/admin_activation_routes.py（新增 894 行：写合同+幂等快照层+8 路由）、server/migrations/versions/028_admin_write_idempotency.py（新增，PG-only）、server/app/activation_code_service.py（追加 4 个密钥版本解析函数）、server/app/main.py（挂载）、server/tests/test_admin_activation_routes.py（新增 37 用例，专用迁移 fixture 库）、9 个既有测试文件（head 断言 027→028，downgrade 守卫步数 +1）、server/scripts/reconcile_customer_billing.py + sqlite_to_postgres.py（PG_ONLY_TABLES 纳入 admin_write_idempotency）、docs/evidence/T12-EVIDENCE.md、任务与证据账本
失败测试或回归锁定：先红后绿——未登录 401/auditor 写 403/auditor 读 200/CSRF 拒；批次校验（名称/面值/额度/数量/有效期 400）；生成（未知批次 404/关闭批次 409/超发 409 零残留/密钥缺失 503 零写入）；下载（一次性/过期/未知/明文不入快照）；发放（渠道校验/状态机/重复发放 409）；暂停/恢复/作废（六态矩阵+suspended_at 形状+事件含 reason 与 request id）；幂等（同键同参回放+replay 头/同键异参 409/并发双线程 barrier 串行化单批次）；写合同（key/confirm/reason 顺序报错）
实现结果：§15 管理写合同（CSRF+reason+Idempotency-Key+request id）+ 028 幂等快照层（actor/route/key digest 唯一、request_hash 冻结、同事务占位-回填、业务失败回滚释放键）+ 8 条路由（批次/生成/下载/发放/暂停/恢复/作废/列表）；明文码仅存于一次性下载响应（绕过快照层，downloaded_at 一次性约束防重放）；SQLite 车道 fail-closed 503
验证命令与通过数：test_admin_activation_routes 37 passed；全量 747 passed（PG fixture）；ruff/format/mypy 全绿
证据层级：AUTOMATED_VERIFIED
安全与可观测性：管理写全链路 actor 可追溯（批次/快照/事件均落 users.id）；幂等键仅存 sha256 摘要；明文不入库不入快照不入日志；RBAC 写/读分离；CSRF 强制；request id 全响应+全事件
迁移与回滚：新迁移 028（编号按代码开发清单 §3.1 head 顺延条款）；PG-only（SQLite 车道零变化，仅 revision 推进）；downgrade 对称（快照为重放缓存非业务事实）；T07 导入工具保持 admin_write_idempotency PG-only 豁免但必须为空
外部授权记录：无；未调用真实 ZPay/COS/付费 Provider/对外发码/灰度/公网发布；PRICE-01 决议未冻结前不得生成对外销售批次（流程红线已登记）
未测试项：首次激活原子事务（T13）；AEAD 幂等恢复（T14）；共享限流与防枚举（T15）；管理端前端页面（T32）；多实例部署形态（T36+）；STAGING/REAL_CHAIN/PRODUCTION
Lore 提交 SHA：见 PR squash 合并 SHA
```
