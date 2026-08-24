# T23 — Audited Admin Adjustments (BILL-02)

## Task Information

| Field | Value |
| --- | --- |
| **Task ID** | T23 / BILL-02 |
| **Owner** | Backend (Agent) |
| **Reviewer** | CodeReview sub-agent pass (0 P1 / 0 P2 / 4 P3, all substantively fixed with regression locks) + PR #54 chatgpt-codex-connector pass (1 P2, fixed with a regression lock — see the review sections below) + security self-review (see the ledger record) |
| **Branch / Base SHA** | `feat/customer-v3-t23-admin-adjustment` / base `af0308f` (main, PR #52 T20 merged) |
| **Date** | 2026-08-24 |
| **Evidence Level** | `AUTOMATED_VERIFIED` (PG 16 fixture, localhost:5433, real Alembic chain 001→039) |

## Exit-Gate Verification

Task exit gate (task list §5 T23): *双确认、来源单、幂等、真实 actor；禁止直接改余额* — all delivered as automated tests on the dedicated migrated fixture database `t23_admin_adjustments_test` (module fixture: DROP/CREATE + `alembic upgrade head` through revision 039, function fixture: TRUNCATE sweep under `session_replication_role = replica`):

```bash
$ uv run python -m pytest tests/test_admin_customer_routes.py -v
29 passed  # 24 red→green cases + 5 review regression locks
  # 双确认 (double confirmation): POST without confirm=true answers 400
  #   CONFIRMATION_REQUIRED; without a reason 400 REASON_REQUIRED; without an
  #   Idempotency-Key 400 IDEMPOTENCY_KEY_REQUIRED (test_missing_confirmation_
  #   is_rejected, test_missing_reason_is_rejected, test_missing_idempotency_
  #   key_is_rejected).
  # 来源单 (source document): the frozen enum CS_TICKET / REFUND_APPROVAL /
  #   COMPENSATION_APPROVAL / LEDGER_CORRECTION + a non-blank ref are enforced
  #   at the route (400 ADJUSTMENT_VALIDATION_FAILED) and by the revision 039
  #   CHECK constraints (test_invalid_source_document_is_rejected,
  #   test_admin_adjustments_check_constraints — every violating shape raises
  #   CheckViolation from PostgreSQL itself).
  # 幂等 (idempotency): same key + same params replays the sealed 201 with
  #   X-Idempotent-Replay: true and charges exactly once; same key against
  #   different params or a different target user answers 409
  #   IDEMPOTENCY_CONFLICT; a failed write rolls its placeholder back so the
  #   key stays reusable (test_same_key_replays_once_without_double_charging,
  #   test_same_key_conflicting_params_is_rejected,
  #   test_same_key_different_target_user_is_rejected,
  #   test_failed_write_does_not_burn_the_key).
  # 真实 actor (real actor): the audit row names the acting admin session
  #   user, auditors are read-only 403 AUDITOR_READ_ONLY, anonymous writes 401
  #   (test_adjustment_creates_paid_order_charge_and_audit_row,
  #   test_auditor_cannot_adjust_but_can_read, test_unauthenticated_write_is_
  #   rejected).
  # 禁止直接改余额 (no direct balance edits): every credit lands as one
  #   atomic transaction — PAID admin_adjustment order + CHARGE ledger row +
  #   wallet increment + append-only audit row; the summed CHARGE deltas
  #   reconcile the wallet balance exactly (test_ledger_difference_is_zero_
  #   after_adjustment), and UPDATE/DELETE/TRUNCATE on admin_adjustments are
  #   refused by PostgreSQL triggers (test_admin_adjustments_append_only);
  #   the RESTRICT FKs refuse every cascade delete of the referenced order
  #   or either user (test_audit_foreign_keys_refuse_cascade_delete).
```

## Implementation Highlights

- **One atomic adjustment transaction** (`POST /api/control/customers/{user_id}/adjustments`): inside a single `pg_transaction` the route inserts a `provider='admin_adjustment'` `status='PAID'` recharge order (revision 026 shapes: created PAID by double confirmation, no third-party trade number, `paid_at` stamped from the transaction clock), a wallet `CHARGE` row (`available_delta = credits`, `task_id=NULL`, `billing_round=NULL`, idempotency key `admin_adjustment:charge:{order_id}`), the atomic `UPDATE wallets SET available_credits = available_credits + credits RETURNING available_credits` increment (the response balance is the real post-update row, never a stale pre-read plus credits — locked by `test_response_balance_is_the_post_update_row`), and one `admin_adjustments` audit row naming the real acting administrator — the four writes commit or roll back together, so no balance mutation can ever exist without its ledger row.
- **The write contract** (dev doc §15, the T12/T18 pattern): real admin session via the T09 gate (`AdminWriter`; auditors read-only 403), mandatory `Idempotency-Key` header (400), `confirm=true` (400 `CONFIRMATION_REQUIRED`), non-blank `reason` (400 `REASON_REQUIRED`), and the route-level source-document validation (frozen enum + non-blank ref → 400 before the ledger is touched; the revision 039 CHECK constraints are the defense in depth).
- **The 031 idempotency snapshot layer** (shared with T12/T18): the business write runs inside a transaction that first inserts an `admin_write_idempotency` placeholder keyed by (actor, canonical route, key digest). The canonical route template — never the concrete path — plus the frozen request fingerprint (path params + body) means a key replayed against a different target user answers 409 instead of silently replaying the first response (the PR #43 review lesson, locked by `test_same_key_different_target_user_is_rejected`). Same key + same params replays the sealed response with `X-Idempotent-Replay: true`; a business failure (404/400) rolls the placeholder back so the key stays reusable; a committed placeholder whose response snapshot never landed (the malformed-envelope window) answers 409 on key reuse — never a TypeError-turned-500 (locked by `test_half_committed_placeholder_answers_409_not_500`).
- **Amount discipline**: `amount_fen = credits × internal_base_unit_price_fen` frozen on the order (`charged == base`, the PRICE-01 floor holds by construction — there is no lane where the operator types an arbitrary amount). The min/step recharge ladder is deliberately **not** enforced: revision 026 scopes it to zpay orders only; an audited adjustment's amount is defined by its source document. An int4 overflow guard refuses credits whose derived `amount_fen` exceeds 2^31-1 before the INSERT (PostgreSQL would otherwise answer a raw 500).
- **Pricing scope inference** (revision 026/027 pairing): a target user bound to a *current* activation code (ACTIVE or SUSPENDED — the same current-binding rule as 027's partial unique index; a REVOKED code keeps its binding for audit only) prices as `CUSTOMER_STANDARD`; an internal account stays `INTERNAL` — the scope is stamped on the order alongside the frozen snapshot columns (locked by `test_suspended_code_prices_as_customer_revoked_does_not`).
- **Append-only audit** (revision 039, the 029/036/038 precedents): the `admin_adjustments` table rejects UPDATE/DELETE through a row-level trigger and TRUNCATE through the shared 036 guard function; one unique index pins exactly one audit row per adjustment order; the downgrade refuses once any audit row exists (operator lineage survives any rollback).
- **Fail-closed runtime**: SQLite / a missing DSN answers 503 `ADJUSTMENT_SERVICE_UNAVAILABLE` — never a legacy-lane answer (the T12/T18 precedent; the admin-session dependency's own fail-closed lane is T09's contract, stubbed in the dedicated test so the request reaches the adjustment route body).
- **SES-01 clock discipline**: `paid_at` / `created_at` sample `SELECT now()` on the caller's transaction — the application process clock never stamps the ledger.

## Independent Code Review — PR #54 connector (1 P2, substantively fixed)

The PR #54 chatgpt-codex-connector review returned **1 P2**, substantively fixed with a regression lock:

| # | Severity | Finding | Fix |
| --- | --- | --- | --- |
| R5 | **P2** | With the wallet balance already near the PostgreSQL int4 ceiling, an otherwise valid adjustment (the `amount_fen` guard passes — it only sees `credits × unit_price`) overflows `available_credits + credits`; PostgreSQL raises `NumericValueOutOfRange`, surfacing as an unexpected 500 on a financial endpoint | The atomic increment now carries the bound in its WHERE clause (`available_credits <= 2147483647 - %s`) so the post-increment balance stays inside the int4 column range; a skipped row with the wallet still present answers a stable 400 `ADJUSTMENT_VALIDATION_FAILED` (the vanished-wallet shape keeps its 404). Locked by `test_wallet_balance_overflow_is_rejected_not_500` — a wallet parked at 2,147,483,647 plus credits=1 answers 400, and the refused write leaves nothing behind (balance untouched, no order/CHARGE/audit rows — the whole transaction rolled back) |

## Independent Code Review — sub-agent pass (0 P1 + 0 P2 + 4 P3, all substantively fixed)

The CodeReview sub-agent pass (pre-PR) returned **4 P3 findings, zero P1/P2** — every finding substantively fixed with a regression lock:

| # | Severity | Finding | Fix |
| --- | --- | --- | --- |
| R1 | P3 | A committed `admin_write_idempotency` placeholder whose response snapshot never landed (the malformed-envelope window) would crash the replay path with a `TypeError` → 500 instead of answering 409 | `_load_idempotent_snapshot` now returns a typed `_IdempotencySnapshot` (the 038 pattern) treating a NULL response as a conflict; locked by `test_half_committed_placeholder_answers_409_not_500` — a seeded half-committed placeholder with the exact request hash answers 409 `IDEMPOTENCY_CONFLICT`, never a 500, and no adjustment lands |
| R2 | P3 | The response's `wallet_balance_after` was computed as a stale pre-read balance plus credits — a concurrent charge/settle committing between the read and the increment would be invisible | The increment now uses `UPDATE wallets SET available_credits = available_credits + %s RETURNING available_credits` (with a None guard that fails the transaction loudly); locked by `test_response_balance_is_the_post_update_row` — an out-of-band balance move to 77 then credits=5 reports 82, the real row |
| R3 | P3 | Pricing-scope inference counted only `ACTIVE` codes — a `SUSPENDED` code (its binding still current, the same account must keep its price) would wrongly price as `INTERNAL` | `_infer_pricing_scope` now follows revision 027's current-binding rule (`ACTIVE` or `SUSPENDED`; `REVOKED` keeps its binding for audit only); locked by `test_suspended_code_prices_as_customer_revoked_does_not` |
| R4 | P3 | The 039 FKs used CASCADE (order) / SET NULL (users) — a trigger suspension (`session_replication_role = replica`) would silently erase or orphan audit rows, contradicting the append-only design | All three FKs are `RESTRICT` (deleted referenced rows fail loudly); locked by `test_audit_foreign_keys_refuse_cascade_delete` — pg_constraint metadata (3 FKs, RESTRICT/NO ACTION only) plus ForeignKeyViolation on order/target-user/admin-user deletes with the audit row surviving |

## Files Changed

| File | Change |
| --- | --- |
| `server/migrations/versions/039_admin_adjustments.py` | **New** — append-only `admin_adjustments` table (FK-unique order link, RESTRICT target/admin user FKs — the R4 fix, source-document enum + non-blank CHECKs, request id), three lookup indexes, the rewrite-refusing trigger, the shared 036 TRUNCATE guard, and a lineage-preserving downgrade guard |
| `server/app/admin_customer_routes.py` | **New (frozen name, code checklist §9.3)** — `POST/GET /api/control/customers/{user_id}/adjustments`: the atomic adjustment transaction behind the T12 idempotency snapshot layer, the §15 write contract, pricing-scope inference, the int4 overflow guard, and the audit-trail listing for operators and auditors |
| `server/app/main.py` | Mount `admin_customer_router` |
| `server/scripts/reconcile_customer_billing.py` | `PG_ONLY_TABLES` gains `admin_adjustments` — the 039 table is PG-only on the target head, so the T07 import/reconcile contract expects it empty (the T18 precedent when 038 added `admin_device_events`; without this the first full run failed 5 `test_real_pg_*` cases with `source/target table contract differs (extra=1)`) |
| 10 migration test files (`test_db`, `test_settings`, `test_characters`, `test_character_domain`, `test_internal_billing`, `test_recharge_orders`, `test_postgres_migrations`, `test_activation_code_schema`, `test_customer_security`, `test_customer_devices`) | Head-revision assertions `038_admin_device_operations` → `039_admin_adjustments` (the standard per-migration maintenance — every new head revision updates these; 22 assertions across upgrade-head/downgrade-rehearsal/guard-refusal tests; the 038 downgrade-guard error-message matches and the `validate_revision_pair` literal stay untouched) |
| `server/tests/test_admin_customer_routes.py` | **New** — 29 cases: schema/check-constraint/append-only/RESTRICT-FK, the happy path with its four-table assertions, the write contract, business validation (non-positive/overflow/unknown user/wallet-less/wallet-balance-overflow), pricing scope (CUSTOMER_STANDARD vs INTERNAL vs suspended/revoked), price-snapshot freezing, the four idempotency behaviours + the half-committed placeholder lock, the post-update-balance lock, fail-closed 503, and the audit listing |
| `docs/evidence/T23-EVIDENCE.md`, the two ledgers | Evidence records |

## Regression

```
$ uv run python -m pytest tests/test_admin_customer_routes.py -q   # 29 passed (24 red→green + 5 review locks)
$ uv run python -m pytest tests -q                                  # 1023 passed, 2 warnings, 0 failed
  # 994 on the T20-merged base + 29 new (24 T23 cases + 5 review locks); zero regression.
  # The 2 warnings are the pre-existing environment artifacts (httpx deprecation
  # and the Windows GBK subprocess-reader thread in test_db.py), not failures.
$ uv run ruff check .          → All checks passed!
$ uv run ruff format --check . → 165 files already formatted
$ uv run mypy app              → Success: no issues found in 66 source files
$ npm run check                → green (secret scan / client biome+tsc+vitest 324 / e2e / tauri cargo / server gates)
```

## Section 14 Ledger Record

```text
任务/工作包：T23 / BILL-02（账务/管理）
Owner / Reviewer：后端（Agent 执行）/ CodeReview 子代理评审（0 P1/0 P2/4 P3 逐条实质修复含 4 例回归锁定）+ PR #54 chatgpt-codex-connector 评审（1 P2 已修复含回归锁定：钱包余额 int4 溢出在原子 UPDATE 条件加界 400 拒绝非 500）+ 安全自评审
分支 / 基线 SHA：feat/customer-v3-t23-admin-adjustment / 基线 af0308f（main，T20 PR #52 合并后）
上游规格段落：客户版任务清单 V3 §5 T23、§12.5 BILL-02；代码开发清单 V3 §9.1/§9.3 admin_customer_routes.py 冻结名；激活码开发文档 §15 管理写契约；测试与验收规格账本差额为零
改动文件：server/migrations/versions/039_admin_adjustments.py（新增：append-only 审计表+trigger+036 共享 TRUNCATE guard+血统保 downgrade）、server/app/admin_customer_routes.py（新增：调账创建+审计列表，原子四写：PAID order+CHARGE+钱包增量+审计行）、server/app/main.py（挂载路由）、server/scripts/reconcile_customer_billing.py（PG_ONLY_TABLES 加 admin_adjustments——T07 导入对账契约适配，T18 先例）、10 个迁移测试文件 head 断言 038→039（每迁移标准维护，22 处；038 guard 消息匹配与 validate_revision_pair 字面参数保持不动）、server/tests/test_admin_customer_routes.py（新增 29 用例：24 红→绿 + 5 评审回归锁定）、docs/evidence/T23-EVIDENCE.md、任务与证据账本
失败测试或回归锁定：先红后绿 24 例（schema 形状+唯一索引 2；CHECK 约束四形状 CheckViolation 1；append-only UPDATE/DELETE/TRUNCATE RaiseException+幸存计数 1；成功流四表断言 1；内部 scope 1；写契约 5：幂等键/确认/reason/auditor 只读/未认证；业务校验 4：非正数/int4 溢出/未知用户/无钱包；来源单枚举+空 ref 1；价格快照冻结 1；幂等 4：同键重放单次入账/同键异参 409/同键异目标 409/失败不烧键；账本差额为零 1；fail-closed 503 1；审计列表+分页 2）+ 评审回归锁定 5 例（半提交占位符 409 非 500；响应余额为 RETURNING 后真实行；SUSPENDED 计入/CODE_REVOKED 不计入当前绑定定价；RESTRICT FK 元数据+删除拒绝+审计行幸存；钱包余额 int4 溢出 400 拒绝且事务完整回滚）
实现结果：后台调账作为单个原子事务落地（双确认+来源单+幂等快照+真实 actor 审计行四写同事务提交或回滚）；金额纪律 credits×内部单价快照冻结（PRICE-01 由构造成立，min/step 仅管 zpay 不适用调账——026 约束口径）；int4 溢出应用层防护；pricing_scope 按 027 当前绑定口径推导（ACTIVE/SUSPENDED 计入，REVOKED 仅审计）；钱包增量用 RETURNING 后真实行；admin_adjustments append-only（039 trigger+036 共享 TRUNCATE guard+全 RESTRICT FK+downgrade 血统保护）；审计列表供 operator/auditor（auditor 只读经 T09 门）；缺 PG 配置 503 fail-closed；SES-01 全部时间戳用事务内 PG 时钟
验证命令与通过数：专项 29 passed；全量 1023 passed（994 基线+29 新增，零回归；2 警告为既有环境噪声）；ruff/format/mypy 全绿（165 files formatted，66 source files typed）；npm run check 全仓门禁绿（secret/client 324/e2e/tauri/server）
证据层级：AUTOMATED_VERIFIED
安全与可观测性：调账永远经 T09 admin session 门（真实 actor 写入审计行；auditor 403 AUDITOR_READ_ONLY）；§15 写契约全量执行（Idempotency-Key/confirm/reason/request id）；幂等快照层同键异参/异目标 409 防止跨资源重放（PR #43 教训回归锁定）；余额变更与 CHARGE 凭据原子绑定+账本差额为零断言（禁止直接 UPDATE 余额）；admin_adjustments 三重 append-only 防护；金额溢出与来源单形状在应用层 400 拒绝（PG CHECK 为纵深防御）；密钥/凭据不入日志
迁移与回滚：迁移 039_admin_adjustments（down_revision=038）；有审计数据时 downgrade 拒绝（保操作员血统）；回滚需先人工导出审计
外部授权记录：无；未调用真实 ZPay/COS/付费 Provider/对外发码/灰度/公网发布
未测试项：T33 管理页面（读端点已就绪）；真实 ZPay 续充与调账的联合对账（BILL-01/BILL-02 真实链路随 T35+）；STAGING/REAL_CHAIN/PRODUCTION
Lore 提交 SHA：见 PR squash 合并 SHA
```
