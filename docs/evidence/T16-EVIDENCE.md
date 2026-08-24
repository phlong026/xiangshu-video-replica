# T16 — Two Device Slots, Credentials & Unbind History (DEV-01)

## Task Information

| Field | Value |
| --- | --- |
| **Task ID** | T16 / DEV-01 |
| **Owner** | Backend/DB (Agent) |
| **Reviewer** | session-internal code review |
| **Branch / Base SHA** | `feat/customer-v3-t16-device-slots-unbind` / base `517e1d2` (T15, PR #46 squash) |
| **Date** | 2026-08-23 |
| **Evidence Level** | `AUTOMATED_VERIFIED` (real PostgreSQL 16 fixture; red → green) |

## Exit-Gate Verification

Task exit gate: *实现两个当前设备槽、凭据、解绑历史和第三设备阻断*；验收栏 *slot 只为 1/2；解绑后复用；历史不删除* (task list §3 T16) + DEV-01 gate: *slot 1/2、第三设备阻断、解绑后槽位复用、历史不删* — No-Go red line: *无条件 `(code_id, slot_no)` 唯一约束不得阻断复用* — delivered as the frozen modules `server/app/customer_device_service.py` and `server/app/customer_device_routes.py` (code checklist §3.2) with 22 fail-first tests in `server/tests/test_customer_devices.py` (frozen name, §3.3; 19 implementation-round + 3 PR #47 Codex-review locks). No new migration: revision 028 already proved the slot invariants in PostgreSQL (partial unique indexes, slot CHECK, shape coupling); T16 delivers the application layer on top.

- **Two current slots, credentials** — the device credential (the long-lived secret returned once at activation time) authenticates `GET /api/customer/devices` and `DELETE /api/customer/devices/{id}` via `Authorization: Bearer`; only its keyed HMAC-SHA256 digest (probed against *every* configured device-domain key version, rotation-window compatible) ever reaches the database. The two-slot status view answers slot 1 / slot 2 each with its currently BOUND device (or null) plus the release history.
- **Unbind history preserved, slot reused** — unbinding flips the row to `UNBOUND` with `unbound_at`, never deletes it; the released slot is immediately reusable (locked by inserting a fresh BOUND row on the same slot after unbind — the DEV-01 No-Go proof against an unconditional unique constraint).
- **Third-device block** — with both slots BOUND `next_free_slot` returns `None`, and PostgreSQL itself refuses a third BOUND row on either slot (partial unique index) as well as a slot number outside 1/2 (CHECK) — both proven by direct SQL assertions.
- **Atomic session revocation on unbind** — when the user's single live session rides the released device, the same transaction bumps the epoch (monotonic trigger), pulls the lease to immediately-expired and appends a `LOGOUT` event with reason `device_unbound`; the released credential answers 401 `DEVICE_REVOKED` afterwards (the client-side signal to wipe stored credentials, §13.2).

## 1. The Service Layer (`server/app/customer_device_service.py`, frozen name)

1. **Credential lookup** — `lookup_device_credential(conn, token)` resolves the token via its digests under all configured key versions (`_token_digests`, the PR #44 review P1 rotation-window rule). The return distinguishes a `BOUND` match (authenticated device) from a released-row match (`row_status` → 401 `DEVICE_REVOKED`) from no match at all (401 `DEVICE_CREDENTIAL_INVALID`). A deployment with no configured key version raises `ActivationKeyError` instead of silently classifying every token as invalid — the routes translate that into a 503 (review P2).
2. **Two-slot view** — `list_device_slots` reads every row of the user; BOUND rows occupy their slot, released rows fall through to the history list. Rows are never deleted, so history outlives slot reuse.
3. **`next_free_slot`** — the lowest free slot of an activation code or `None` when both are BOUND: the third-device block the T17 enroll flow will consult (its 409 `DEVICE_SLOTS_FULL` lane).
4. **`unbind_device`** — `SELECT … FOR UPDATE` on the target row, then: missing or foreign device → `not_found` (identical answers, no IDOR oracle); already released → `not_bound`; otherwise BOUND→UNBOUND with `unbound_at`, and when the user's live session (one row per user) rides the released device, epoch+1 + `GREATEST(now, created_at + 1µs)` lease + `LOGOUT`(`device_unbound`) event — the full-precision transaction clock keeps the revoked lease already-expired (PR #47 Codex review P2: the old whole-second trimming once forced the GREATEST fallback to a full second, letting a lease live up to 1 s past the unbind; the backstop is now one microsecond and only guards a hypothetical same-transaction create-and-unbind path).
5. **Clock** — the unbind timestamps come from `SELECT now()` inside the transaction (SES-01: PostgreSQL is the only trusted clock, the activation-route precedent), sampled by the route and passed in.

## 2. The Routes (`server/app/customer_device_routes.py`, frozen name)

- `GET /api/customer/devices` — the two-slot status + history, `response_model` validated (OpenAPI regeneration for the client is the frontend-integration task's gate).
- `DELETE /api/customer/devices/{id}` — 204 on success, 404 `DEVICE_NOT_FOUND` (missing or foreign), 409 `DEVICE_ALREADY_UNBOUND`. The DELETE carries a mandatory `Idempotency-Key` sealed with the shared T14 envelope engine (PR #47 Codex review P2 — see the post-review section): a client that lost the 204 retries with the same key + same target and replays the sealed 204 (`X-Idempotent-Replay: true`), the recovery probe deliberately running *before* credential authentication; the same key against a different target answers 409 `IDEMPOTENCY_CONFLICT`; a missing key answers 400 `IDEMPOTENCY_KEY_REQUIRED`. `X-Request-Id` echoes into the audit event when supplied. The success log line runs *after* the transaction commits (a rolled-back unbind must not claim success) and carries identifiers only.
- **Stable error codes** — 401 `DEVICE_CREDENTIAL_REQUIRED` / `DEVICE_CREDENTIAL_INVALID` / `DEVICE_REVOKED`, 503 `DEVICE_SERVICE_UNAVAILABLE` for both the missing-PG-runtime fail-closed path and the key-misconfiguration path (review P2: a server-side misconfiguration must never leak as a 500, and even less as a 401 that would trick the client into wiping valid credentials).
- **Authentication is re-validated inside the serving transaction** — the lookup runs on the same connection/transaction that serves the request, so a credential released mid-flight cannot slip through (the §12.4 fencing discipline, ahead of its T21 generalization).

## 3. Test Coverage (22 red → green)

| Group | Cases |
| --- | --- |
| Module units (no database, always run, 2) | keyed digest deterministic + key/value sensitive; MAX_DEVICE_SLOTS == 2 |
| Credential & two-slot view (7) | missing Authorization → 401 REQUIRED; malformed header shapes → 401 REQUIRED; unknown token → 401 INVALID; slot 1 BOUND + is_current, slot 2 free, empty history after activation |
| Unbind semantics (7) | unbind releases the slot, keeps the history row, and a fresh BOUND row reuses the slot; atomic session revocation (epoch 2, immediately-expired lease, LOGOUT `device_unbound` event carrying the request id); missing device → 404; **foreign user's device → 404 with the row untouched (IDOR)**; already-unbound → 409; unbinding the *other* device keeps own slot and session intact (epoch unchanged) |
| Third-device block (2) | `next_free_slot` walks empty → 1, one slot taken → 2, both taken → None, released slot → reusable; PostgreSQL refuses a third BOUND row on both slots (UniqueViolation) and a slot number 3 (CheckViolation) |
| Key rotation (1) | the credential issued under the highest configured version (V2) authenticates while both versions are configured; dropping V2 retires it (401 INVALID) without breaking V1-era rows |
| Review-fix locks (3) | no PG runtime → 503 fail-closed; **no configured device keys → 503, never a 401** (review P2); a REVOKED-status row also answers 401 `DEVICE_REVOKED` |
| Idempotent unbind (3, PR #47 P2) | DELETE without an Idempotency-Key → 400 `IDEMPOTENCY_KEY_REQUIRED` (row untouched, no envelope created); the lost-204 own-device retry replays the sealed 204 (`X-Idempotent-Replay`, sealed request id echoed) with zero re-execution (one row flip, one epoch bump 1→2, one LOGOUT event); the same key against a different target → 409 `IDEMPOTENCY_CONFLICT` with the second target untouched |

## Files Changed

| File | Change |
| --- | --- |
| `server/app/customer_device_service.py` | new (frozen name): credential lookup across key versions, two-slot view, `next_free_slot`, `unbind_device` with atomic session revocation |
| `server/app/customer_device_routes.py` | new (frozen name): Bearer device-credential auth, GET/DELETE routes, stable error codes, 503 fail-closed (PG runtime + key configuration), PG-clock sampling, post-commit success log |
| `server/tests/test_customer_devices.py` | new (frozen name): 22 fail-first cases on the dedicated migrated fixture DB `t16_customer_devices_test` |
| `server/app/main.py` | router mount (+2 lines, `customer_device_router` after the activation router) |

## Regression

```text
# PostgreSQL fixture up (docker customer-v3-pg-test, PG16 :5433)
$ uv run python -m pytest tests/test_customer_devices.py -q
22 passed
$ uv run pytest tests -q
839 passed + 1 time-boundary flaky re-run green → 840 confirmed   # the flaky is tests/test_e2e_fake_provider.py (storage-signature x-expires second rollover, SQLite generation lane — unrelated to this task's files; single re-run green)
$ uv run ruff check . && uv run ruff format --check . && uv run mypy app
all green (148 files formatted, 61 source files typed)
# client / tauri / e2e untouched by this task; CI gates re-verify
# secret scan: clean (test keys are runtime-generated secrets.token_* fixtures, T13–T15 precedent)
```

## Session Code-Review Notes

Self-review during implementation (pre-subagent):

1. **The lease CHECK bites in the same-second window** — pulling `lease_until` to a naive `now` violated `ck_customer_session_state_lease_after_created` when the unbind landed within the same second as the session's creation (microsecond trimming can put `now` at or before `created_at`). Fixed with `GREATEST(now, created_at + interval '1 second')`: the lease expires at most one second after creation — still "immediately expired" for fencing purposes, and provably on the right side of the constraint.
2. **Direct-SQL test helpers need savepoints** — the third-device UniqueViolation cases left the surrounding transaction aborted; wrapping the raw insert in `conn.transaction()` (a savepoint) keeps the case usable.
3. **Fixture keys must be complete** — the first red run failed on `highest_customer_aead_key` (the activation path needs the AEAD key too), a reminder that the T16 fixture mirrors the full T13 activation environment.

### Post-Review Fixes (session subagent review: 1 P2 + 4 P3, P2 and three P3s fixed)

1. **P2 — key misconfiguration leaked as 500 / misleading 401.** An `ActivationKeyError` from a too-short configured key escaped the routes as an unhandled 500, and a deployment with *no* configured key version classified every token as `DEVICE_CREDENTIAL_INVALID` — which per the §13.2 client contract would trick clients into wiping perfectly valid stored credentials. Fixed on both ends: `_token_digests` raises on the empty version list, and `_authenticate` translates `ActivationKeyError` into 503 `DEVICE_SERVICE_UNAVAILABLE` (the activation-route precedent). Locked by `test_unconfigured_device_keys_answer_service_unavailable`.
2. **P3 #1 — application clock sampled before the transaction.** The unbind timestamps now come from `SELECT now()` inside the transaction (SES-01, the activation-route precedent) so `unbound_at`, the pulled lease and the audit event share one server-side clock.
3. **P3 #2 — success log printed before commit.** Moved after the `pg_transaction()` block: a rolled-back unbind must not leave an audit log claiming success.
4. **P3 #3 — device endpoints not behind the T15 shared limiter.** Accepted as out of scope for T16 (the device token is a 256-bit random value, brute-force infeasible; the login-dimension limiter is the T19 lane). Registered here as a known gap for the T19 review to pick up.
5. **P3 #4 — test coverage gaps.** Added the three review-fix locks above (PG fail-closed 503, key-misconfiguration 503, REVOKED-status credential). The concurrent-DOUBLE-DELETE serialization case was judged adequately covered by the FOR UPDATE + 409 pair and not added.

### PR #47 Codex Review Fixes (3 P2, all fixed)

1. **P2-1 — the revoked lease survived up to one second into the future.** `unbind_device` trimmed the transaction clock to whole seconds and fell back to `GREATEST(now, created_at + interval '1 second')`, so a lease could still read as valid for up to 1 s after the unbind committed — contradicting the acceptance requirement that revocation invalidates the session *immediately*. Fixed on both ends: the full PostgreSQL microsecond precision is preserved (`server_now.isoformat()`), and the same-transaction GREATEST backstop tightened from one second to one microsecond (the cross-transaction clock always postdates `created_at`; the backstop only guards a hypothetical create-and-unbind-inside-one-transaction path). The test assertion lost its +2 s forward allowance: `lease_until <= datetime.now(UTC)`.
2. **P2-2 — the lost-204 unbind was unrecoverable.** A client that lost the DELETE 204 could not recover the success: retrying its *own* device unbind answered 401 `DEVICE_REVOKED` (the credential it had just released) and retrying the *other* device answered 409 `DEVICE_ALREADY_UNBOUND` — an ordinary network retry surfaced as an ambiguous fresh failure. Fixed with the shared envelope engine (T14): DELETE now requires an `Idempotency-Key` (400 `IDEMPOTENCY_KEY_REQUIRED` otherwise), seals the minimal audit payload (target device id + request id) under the fixed scope namespace `devices` (operation + client key identify the submission; the target rides the request hash — the same key against a different target answers 409 `IDEMPOTENCY_CONFLICT`), and replays the sealed 204 with `X-Idempotent-Replay: true` — with the recovery probe deliberately *before* credential authentication, because after unbinding its own device the caller's credential is by design no longer resolvable and the sealed envelope itself is the only proof of the completed submission. Locked by three new tests (missing key → 400 with the row untouched; own-device lost-204 retry → replayed 204 with zero re-execution — one row flip, one epoch bump, one LOGOUT event, sealed request id echoed; same key + different target → 409 with the second target untouched). All eight existing DELETE call sites now carry unique keys (the already-unbound conflict test pins the fresh-key retry lane).
3. **P2-3 — evidence-ledger escape corruption.** The T16 evidence append had let Python interpret `\f`/`\t`/`\n` escape sequences, corrupting seven spots in `docs/CUSTOMER-TASK-EVIDENCE-V3.md` (a form feed eating the f of the word feat, a tab eating the t of the word test and of the text fence marker, three `next_free_slot` words split across lines, broken code fences). All seven repaired and re-verified clean (zero control characters, zero split words, fences intact).

## Section 14 Ledger Record

```text
任务/工作包：T16 / DEV-01
Owner / Reviewer：后端/DB（Agent 执行）/ 会话内代码评审
分支 / 基线 SHA：feat/customer-v3-t16-device-slots-unbind / 基线 517e1d2（T15 PR #46 squash）
上游规格段落：客户版任务清单 V3 §3 T16、§12.3 DEV-01；代码开发清单 V3 §3.2 customer_device_service.py/customer_device_routes.py、§3.3 test_customer_devices.py 冻结名；激活码开发文档 §3.2 设备规则、§6.1 API 表、§12.4 fencing、§13.2 错误码；测试与验收规格 §2.2/§3.3
改动文件：server/app/customer_device_service.py（新增：跨密钥版本凭据解析、两槽视图、next_free_slot、unbind_device 原子会话吊销）、server/app/customer_device_routes.py（新增：Bearer 设备凭据鉴权、GET/DELETE 两路由、稳定错误码、503 fail-closed、PG 事务内时钟、提交后日志）、server/tests/test_customer_devices.py（新增 22 用例，专用迁移库 t16_customer_devices_test）、server/app/main.py（路由挂载 +2 行）、docs/evidence/T16-EVIDENCE.md、任务与证据账本
失败测试或回归锁定：先红后绿 22 例——模块级 2（摘要确定性/槽数常量）+凭据与视图 7（三态 401、激活后槽视图）+解绑语义 7（槽复用+历史保留、原子会话吊销、404 缺失/IDOR、409 重复、他设备解绑自身不受扰）+第三设备阻断 2（next_free_slot 状态机 + PG 层 UniqueViolation/CheckViolation）+密钥轮换 1（V2 签发双版本可鉴权、V2 退役后 401）+评审锁定 3（PG fail-closed 503、密钥未配置 503 非 401、REVOKED 行 401）+幂等解绑 3（PR #47 Codex P2：无键 400+行未动、丢 204 同键重试重放零重执行、同键异目标 409）
实现结果：两当前设备槽+凭据+解绑历史+第三设备阻断落地应用层（028 schema 无新迁移）：设备凭据 Bearer 鉴权（keyed digest 跨版本探测，明文永不过库）；GET /api/customer/devices 返回两槽状态+释放历史；DELETE 解绑（BOUND→UNBOUND+unbound_at，行不删除，槽立即可复用——partial unique index 免疫 DEV-01 No-Go）携带强制 Idempotency-Key（PR #47 Codex P2：T14 信封引擎密封审计载荷，丢失 204 同键同目标可重放，预检在凭据鉴权前，同键异目标 409 IDEMPOTENCY_CONFLICT，无键 400）；解绑原子吊销所骑会话（epoch+1+立即过期租约（全微秒精度+GREATEST 1µs 兑底，PR #47 Codex P2）+LOGOUT device_unbound 事件）；两槽满 next_free_slot=None+数据库拒绝第三行；错误码 401 REQUIRED/INVALID/REVOKED、404（缺失=他人，无 IDOR 预言）、409 ALREADY_UNBOUND、503 fail-closed（无 PG/密钥未配置）；解绑时钟取 PG 事务内 now()（SES-01）
验证命令与通过数：专项 22 passed；全量 839 passed + 1 时间边界 flaky 单独复跑通过→ 840 确认（flaky 为 test_e2e_fake_provider.py 存储签名 x-expires 秒翻转，SQLite 生成 lane，与本任务文件无依赖）；ruff/format/mypy 全绿（148 files formatted，61 source files typed）；实现轮 16 红→绿 + 静态全绿 + 全量 834 后经会话内代码评审修复 1 P2+3 P3 复跑专项 19 + 全量 837，再经 PR #47 Codex 评审修复 3 P2（lease 微秒精度/DELETE 幂等键+信封恢复/账本转义）复跑专项 22 + 静态全绿 + 全量 840 复确认
证据层级：AUTOMATED_VERIFIED
安全与可观测性：设备凭据只以 keyed HMAC-SHA256 摘要过库（跨版本探测兼容轮换窗口）；日志与事件仅含标识符；IDOR 统一 404（缺失=他人同应答）；401 INVALID 与 REVOKED 的区分是 §13.2 客户端擦除信号（token 为 256-bit 随机+keyed digest，不可枚举构造）；密钥配置故障 503 而非误导 401/500；解绑审计事件携带 request_id；成功日志提交后打印；幂等信封 AEAD 密封（AAD 绑定 operation/scope/key_digest，原始客户端键永不过库）
迁移与回滚：无新迁移（028 的 customer_devices/customer_session_state/customer_session_events schema 完全就绪）；回滚=还原代码（无 schema 变更）
外部授权记录：无；未调用真实 ZPay/COS/付费 Provider/对外发码/灰度/公网发布
未测试项：设备端点未接入共享限流（登记为 T19 评审项；设备 token 256-bit 不可暴破）；同键并发双 DELETE 线程级证明（信封 ON CONFLICT + FOR UPDATE 语义覆盖，T13/T14 同前例）；T17 enroll 接入 next_free_slot 的路由级联测；客户端 OpenAPI 重新生成（前端接入任务门禁）；STAGING/REAL_CHAIN/PRODUCTION
Lore 提交 SHA：见 PR squash 合并 SHA
```
