# T13 — First-Activation Atomic Transaction (ACT-05 / ACT-06)

## Task Information

| Field | Value |
| --- | --- |
| **Task ID** | T13 / ACT-05 + ACT-06 |
| **Owner** | Backend/Billing (Agent) |
| **Reviewer** | session-internal code review |
| **Branch / Base SHA** | `feat/customer-v3-t13-first-activation` / base `83a5bb2` (T12, PR #43 squash) |
| **Date** | 2026-08-23 |
| **Evidence Level** | `AUTOMATED_VERIFIED` (real PostgreSQL 16 fixture; red → green) |

## Exit-Gate Verification

Task exit gate: *user、wallet、activation、slot1、PAID order、CHARGE、session 全有或全无* (task list §3 T13) + ACT-05 gate (same) + ACT-06 gate: *同一码、同用户名和同设备的 100 并发测试 — 仅一个成功事实、一个首充、一个当前 session；SQLite/单连接测试不能作为并发证据* — delivered as `server/app/activation_code_routes.py` (frozen name, mounted in `main.py`) over migrations `028_customer_devices_and_activations` + `029_customer_sessions_and_idempotency` (both frozen names, chained off the then-current head 031), with 26 fail-first tests (`server/tests/test_activation_code_routes.py` 21 contract cases incl. five PR-review regressions + `server/tests/test_customer_activation.py` 5 concurrency cases on dedicated migrated PG fixture databases):

- **全有或全无** — `POST /api/customer/activate` runs the whole chain in exactly one `pg_transaction()`: lock the code row (`FOR UPDATE OF c`, join batch) → server-generated `customer` user (savepoint username-collision retry, ≤5 attempts) → funded wallet (batch `credits_snapshot`) → slot-1 device row (keyed fingerprint/token digests) → `provider=activation_code` PAID first-charge order (frozen batch price: `credits × unit_price_fen_snapshot`; base = charged, PRICE-01 satisfied; `min=1/step=1`, non-ZPay provider skips the 026 min/step gate) → unique CHARGE ledger row (idempotency key `activation_code:charge:{order_id}`) → activation fact (attaches 027's dangling `first_device_id` FK) → code → ACTIVE + ACTIVATED event → epoch-1 session with the 90-second lease (T19 `SESSION_LEASE_SECONDS`). A rejection anywhere rolls back every row; the 100-thread race proves exactly one of each fact survives.
- **ACT-06** — 100 threads, one barrier, one code, distinct Idempotency-Keys and distinct fingerprints: exactly one 201 and 99 unified 400 `ACTIVATION_UNAVAILABLE`; afterwards the database holds exactly one customer user / wallet / activation / device / PAID order / CHARGE / session row and the code is ACTIVE.

## 1. The Atomic Chain (dev doc §12.1)

Redeeming an ISSUED code creates the customer identity in one transaction:

1. `SELECT … FOR UPDATE OF c` locks the code (joined batch snapshot columns) — concurrent redemptions of one code serialize here and every loser observes the winner's ACTIVE state.
2. Status/expiry check: non-ISSUED or batch-expired → unified 400 (anti-enumeration).
3. Fast-path fingerprint check (`WHERE fingerprint_hmac = %s AND status = 'BOUND'`) → 409 `USER_ALREADY_ACTIVATED`; the concurrent cross-code race is settled by the partial unique index `uq_customer_devices_fingerprint` (UniqueViolation → same 409).
4. `users` insert inside a savepoint: a `users_username_key` collision only rolls the attempt back and a fresh candidate retries inside the same activation transaction (§12.1 note — recovery must never create a second user).
5. `wallets` (available = batch credits), `customer_devices` slot 1 (fingerprint/token digests keyed under the device-domain HMAC key, versions recorded), `recharge_orders` (PAID, `CUSTOMER_STANDARD`), `wallet_transactions` (CHARGE), `activation_code_activations`, `activation_codes` → ACTIVE + bound_user_id, `activation_code_events` (ACTIVATED, actor = new user, request id), `customer_session_state` (epoch 1, lease 90 s, token digest), `customer_session_events` (ACTIVATED).

Server-generated username `customer-{12 hex}` carries no code fragment, phone or enumerable order. The route fails closed 503 `ACTIVATION_SERVICE_UNAVAILABLE` without a PG runtime (SQLite lane) — checked *before* key resolution so a misconfigured deployment reports the service outage, not a key problem; keys resolve next (503 `ACTIVATION_KEYS_UNAVAILABLE`), both before any DB write.

## 2. Idempotency Envelope (revision 029, dev doc §11.2 / §12.1)

`customer_idempotency_envelopes` follows the T12 admin pattern with one deliberate difference — the admin snapshot stores plaintext operator payloads, but a first-activation response carries one-time credentials, so the envelope stores the response AES-GCM-sealed under `VIDEO_REPLICA_CUSTOMER_IDEMPOTENCY_AEAD_KEY` (base64url, exactly 32 bytes; `_V<n>` rotation like the activation-code keys):

1. `Idempotency-Key` header is mandatory (400 `IDEMPOTENCY_KEY_REQUIRED`); only its SHA-256 digest ever reaches the database.
2. `INSERT … ON CONFLICT (operation, scope, key_digest) DO NOTHING` — the winner keeps the placeholder id; concurrent same-key writers block on the unique index until the first transaction commits/rolls back.
3. Key reuse: `request_hash` (sha256 of canonical JSON incl. the normalized code) must match, else 409 `IDEMPOTENCY_CONFLICT`. A complete envelope replays the sealed response with `X-Idempotent-Replay: true` and restores the original request id; a purged/expired/incomplete envelope answers 409 (the key is spent — the client must use a fresh key; T14 owns the cleanup story).
4. The winner seals the response with `operation/scope/key_digest` bound as AAD (a ciphertext cannot be replayed against a different envelope row) and back-fills `ciphertext/key_version/recovery_expires_at` (window env-overridable, default 24 h) before commit.
5. A business failure rolls the placeholder back with the transaction — the key stays reusable (tested: suspended code → 400, same key then succeeds with the corrected code).

## 3. Anti-Enumeration Groundwork (ACT-08)

Unknown, malformed, undelivered-agnostic, expired-batch, suspended, revoked and already-ACTIVE codes all answer the single unified 400 `ACTIVATION_UNAVAILABLE` with an identical message (seven-scenario parameterized test). The code-side rejection never distinguishes the sub-state; the fingerprint conflict is the only differentiated answer (409, different resource class). T15 layers the shared rate limiter and timing parity on top.

## 4. Test Coverage (26 red → green + 2 adapted)

| Group | Cases |
| --- | --- |
| Contract (`test_activation_code_routes.py`, 21) | atomic happy path (201, full chain incl. wallet balance, epoch-1 lease); request-id echo; missing Idempotency-Key 400; unified 400 ×7 (unknown/malformed/expired/suspended/revoked/active/generated-shape); same fingerprint second code 409 `USER_ALREADY_ACTIVATED`; same key + same body replays identical identity (replay header, original request id restored); same key + different body 409; SQLite fail-closed 503; log scan: no plaintext code/token in any captured record; **PR-review regressions** — naive batch expiry answers the unified 400 (never a TypeError 500), envelope row shape after success (ciphertext/key_version/recovery deadline present, purged_at NULL), recovery-window env override (60 s → stored deadline follows), expired recovery window refuses replay (409, no replay header), same key + whitespace-padded body still replays (normalized request hash) |
| Concurrency (`test_customer_activation.py`, 5) | ACT-06 100 threads/one barrier/one code → one 201 + 99 × 400, one of each fact row afterwards; two threads sharing one key + body → both 201 with identical username/device token/session token and exactly one CHARGE (envelope recovery); two threads racing one fingerprint with different codes → one 201 + one 409, losing code untouched; business failure releases the key (suspended → retry with corrected code succeeds); username collision regenerates inside the same transaction (monkeypatched generator, `customer-collide0` → `customer-fresh0`) |
| Schema adaptation (`test_activation_code_schema.py`, 2) | one-shot uniqueness test now seeds the slot-1 `customer_devices` row (028 attached 027's deferred `first_device_id` FK); downgrade-guard test walks the longer chain (029 → 028 refuses with an activation fact via "cannot downgrade 028"; after removing the fact four steps reach 026) |

## 5. Head-Roll Maintenance (031 → 029)

Migrations 028/029 use their frozen file names and chain off the then-current head 031 (the T12 review P1 arrangement: the frozen 028–030 themes link off the live head in order; 030_user_fair_queue remains reserved for T25). Both are PG-only; the SQLite lane advances `alembic_version` only. Eleven existing test files' head assertions moved from `031_admin_write_idempotency` to `029_customer_sessions_and_idempotency` (incl. `test_db.py` bootstrap checks, the SQLite→PG importer's target head and the T10 schema downgrade walks). The T07 cutover tooling stays fail-closed: `customer_devices`, `customer_session_state`, `customer_session_events` and `customer_idempotency_envelopes` join `PG_ONLY_TABLES` (reconcile + importer), expected empty on the target head at cutover time.

## Files Changed

| File | Change |
| --- | --- |
| `server/app/activation_code_routes.py` | new (frozen name, 742 lines): versioned device-domain/ AEAD key resolution, keyed digests, AES-GCM envelope, the one-transaction activation chain, unified anti-enumeration rejections |
| `server/migrations/versions/028_customer_devices_and_activations.py` | new (frozen name, PG-only): `customer_devices` (slot 1/2, digest + version columns, BOUND/UNBOUND/REVOKED shape coupling), partial unique indexes `uq_customer_devices_slot` / `uq_customer_devices_fingerprint` (WHERE status='BOUND'), attaches 027's deferred `first_device_id` FK, activated-guard downgrade refusal |
| `server/migrations/versions/029_customer_sessions_and_idempotency.py` | new (frozen name, PG-only): `customer_session_state` (user_id PK = single-session invariant, epoch monotonic trigger), `customer_session_events` (append-only trigger, six event types), `customer_idempotency_envelopes` (unique (operation, scope, key_digest), ciphertext/key_version/recovery_expires_at three-state coupling CHECK, purged_at) |
| `server/app/main.py` | mount `customer_activation_router` |
| `server/tests/test_activation_code_routes.py` | new: 21 fail-first contract cases (16 original + 5 PR-review regressions) on a dedicated migrated fixture DB |
| `server/tests/test_customer_activation.py` | new: 5 concurrency cases (incl. the ACT-06 100-thread race) on a dedicated migrated fixture DB |
| `server/tests/test_activation_code_schema.py` | two T10 cases adapted to the 028/029 chain (slot-1 seed row, downgrade walk) |
| `server/tests/test_admin_activation_routes.py` | clean-state TRUNCATE covers the new tables |
| 9 test files + `server/scripts/sqlite_to_postgres.py` + `server/scripts/reconcile_customer_billing.py` | head assertions 031 → 029; PG_ONLY_TABLES covers the four new tables |

## PRICE-01 Boundary

The first-charge order prices strictly by the frozen batch snapshot (`credits × unit_price_fen_snapshot`) and records base = charged; PRICE-01 (no external sales batches until the price decision is frozen) remains the process gate upstream — T13 never mints batches.

## Regression

```text
# PostgreSQL fixture up (docker customer-v3-pg-test, PG16 :5433)
$ uv run python -m pytest tests/test_activation_code_routes.py tests/test_customer_activation.py tests/test_activation_code_schema.py -q
36 passed
$ uv run python -m pytest tests -q
771 passed, 3 skipped          # full suite on the PG fixture (zero regressions)
$ uv run ruff check . && uv run ruff format --check . && uv run mypy app
all green (138 files formatted, 57 source files typed)
# client: npm run check --workspace client → 24 files / 324 tests passed
# e2e lint: biome check e2e → clean; tauri: cargo fmt --check + cargo check --locked → clean
# secret scan (scripts/verify_no_secrets.sh patterns, PowerShell equivalents): clean
```

## Session Code-Review Fixes

The pre-merge review (CodeReview agent) found no P1 issues; one P2 and four P3 findings, all addressed in this branch:

1. **P2 — naive batch expiry turned into a 500**: T12's admin API accepts naive `activation_expires_at` timestamps, but `_run_activation` compared the stored string against the tz-aware `SELECT now()` result — an aware-vs-naive `TypeError` escaped as 500 on every activation attempt of a legal batch. Fixed: the stored expiry coerces to UTC when naive (the same semantics T12 applies at creation), plus a regression test (`test_naive_batch_expiry_answers_unified_400_not_500`, the test helper needed the same coercion for its backdating path).
2. **P3 — request hash used the raw body**: the idempotency fingerprint now freezes the *normalized* (strip'd) body, matching the business path — a retry differing only in whitespace replays instead of burning the key on a 409 (regression test).
3. **P3 — dead parameters**: `_insert_customer_user` dropped its unused `hmac_key`/`server_now` parameters; the `face_value_fen` column left the SELECT (never read).
4. **P3 — replay path was silent**: the idempotent replay (a one-time credential re-issued from the sealed envelope) now logs an identifiers-only info line (`scope`, `key_version`, `request id`) — security-sensitive and observable, still no plaintext.
5. **P3 — test blind spots**: added regressions for the recovery-window env override, the expired-window refusal (409, back-dated while honouring the CHECK coupling), the completed envelope row shape, and the whitespace-padding replay.

## Section 14 Ledger Record

```text
任务/工作包：T13 / ACT-05 + ACT-06
Owner / Reviewer：后端/账务（Agent 执行）/ 会话内代码评审
分支 / 基线 SHA：feat/customer-v3-t13-first-activation / 基线 83a5bb2（T12 PR #43 squash）
上游规格段落：客户版任务清单 V3 §3 T13、§12.2 ACT-05/ACT-06；代码开发清单 V3（activation_code_routes.py、028/029 迁移冻结名）；激活码开发文档 §11.2 幂等信封、§11.3 并发不变量、§12.1 首次激活事务、§7 密钥红线；测试与验收规格 §2
改动文件：server/app/activation_code_routes.py（新增 742 行：版本化密钥解析+keyed 摘要+AES-GCM 信封+单事务激活链+统一防枚举拒绝）、server/migrations/versions/028_customer_devices_and_activations.py（新增，PG-only，冻结名：customer_devices 两槽+指纹/token 摘要+部分唯一索引+027 遗留 first_device_id FK 挂接+激活存在拒绝降级）、server/migrations/versions/029_customer_sessions_and_idempotency.py（新增，PG-only，冻结名：customer_session_state 单会话不变量+epoch 单调触发器、customer_session_events 追加只触发器、customer_idempotency_envelopes 唯一(operation,scope,key_digest)+三态耦合 CHECK）、server/app/main.py（挂载）、server/tests/test_activation_code_routes.py（新增 16 用例）、server/tests/test_customer_activation.py（新增 5 并发用例含 ACT-06 100 并发）、server/tests/test_activation_code_schema.py（2 用例适配）、server/tests/test_admin_activation_routes.py（TRUNCATE 纳新表）、9 个既有测试文件+2 个脚本（head 断言 031→029、PG_ONLY_TABLES 纳四张新表）、docs/evidence/T13-EVIDENCE.md、任务与证据账本
失败测试或回归锁定：先红后绿——契约 16 例（原子全链/请求 id 回显/幂等键必填/统一 400 七场景/同指纹二码 409/同键同体重放+replay 头/同键异体 409/SQLite fail-closed 503/日志无明文）；并发 5 例（100 并发恰一成功 99 个统一 400+全库恰一份事实/同键并发恢复同一身份且仅一笔 CHARGE/同指纹跨码并发一胜一 409 败者码不动/业务失败释放幂等键可重试/用户名碰撞事务内保存点重试）；schema 2 例适配（slot1 种子行满足 028 挂接 FK、降级链 029→028 激活守卫→删事实后四步至 026）
实现结果：POST /api/customer/activate 单事务创建 user（服务器生成名+保存点重试）→wallet→slot1 设备（keyed 摘要+版本）→PAID 首充订单（批次快照定价 base=charged 满足 PRICE-01，min/step=1 非zpay不触发 026 门槛）→唯一 CHARGE（幂等键 activation_code:charge:{order_id}）→激活事实（挂接 first_device_id）→码 ACTIVE+ACTIVATED 事件→epoch-1 会话+90 秒租约；幂等信封 029（摘要入库、AAD 绑定 operation/scope/key_digest、24h 恢复窗、业务失败回滚释放键）；统一 400 防枚举（ACT-08 地基）；PG 运行时先于密钥 fail-closed 503
验证命令与通过数：test_activation_code_routes 16 passed + test_customer_activation 5 passed + test_activation_code_schema 10 passed；全量 766 passed, 3 skipped（PG fixture）；ruff/format/mypy 全绿；client workspace 324 passed；biome e2e clean；cargo fmt+check clean；secret 扫描等价模式 clean
证据层级：AUTOMATED_VERIFIED
安全与可观测性：激活码/设备 token/session token 明文只存在于 HTTP 响应与 AEAD 信封列（日志扫描测试锁定）；幂等键仅存 SHA-256 摘要；指纹与凭据摘要 keyed HMAC（版本化轮换）；统一防枚举拒绝；request id 全链路（响应头+事件+信封回放恢复）
迁移与回滚：新迁移 028/029（冻结文件名，从当日 head 031 顺延链接，030 仍为 T25 预留）；PG-only（SQLite 车道仅 revision 推进）；028 激活事实存在时拒绝降级（审计链保护）；029 downgrade 对称（会话/信封为运行时缓存非业务事实）；T07 导入工具保持新表 PG-only 豁免但必须为空
外部授权记录：无；未调用真实 ZPay/COS/付费 Provider/对外发码/灰度/公网发布
未测试项：AEAD 幂等恢复完善与过期清理（T14/ACT-07）；共享限流与防枚举时延近似（T15/ACT-08）；第二设备与配对（T16-T18）；session 生命周期（T19-T20）；ZPay 续充共存（T22/BILL-01）；前端与桌面端（T28+）；STAGING/REAL_CHAIN/PRODUCTION
Lore 提交 SHA：见 PR squash 合并 SHA
```
