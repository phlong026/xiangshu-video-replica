# T20 — Explicit Atomic Switch & Session-Epoch Fencing (SES-02 / SES-03)

## Task Information

| Field | Value |
| --- | --- |
| **Task ID** | T20 / SES-02 / SES-03 |
| **Owner** | Backend (Agent) |
| **Reviewer** | Independent review pending the PR round (to be recorded after the connector/Codex pass) + security self-review (see the ledger record) |
| **Branch / Base SHA** | `feat/customer-v3-t20-session-switch-fencing` / base `939c305` (main, PR #51 T19 merged) |
| **Date** | 2026-08-23 |
| **Evidence Level** | `AUTOMATED_VERIFIED` (PG 16 fixture, localhost:5433, real Alembic chain 001→038) |

## Exit-Gate Verification

Task exit gate (task list §4 T20): *普通登录 409；切换后旧 token/epoch 全实例失效* — both delivered as automated tests on the dedicated migrated fixture database `t19_customer_sessions_test` (module fixture: DROP/CREATE + `alembic upgrade head`, function fixture: TRUNCATE sweep under `session_replication_role = replica`):

```bash
$ uv run python -m pytest tests/test_customer_sessions.py tests/test_customer_fencing.py -v
67 passed  # 54 session (35 T19 + 19 T20 switch/code-gate/revocation) + 13 fencing
  # Plain login against a live other-device lease still answers 409
  #   OTHER_DEVICE_ONLINE — no auto-kick; only the explicit switch displaces
  #   (test_switch_takes_over_the_live_other_device_atomically and the
  #   §12.3 login-conflict tests inherited from T19).
  # After a switch commits, the displaced token answers 401 SESSION_REPLACED
  #   on heartbeat and late logout, and the fencing verifier fences any write
  #   carrying the old epoch — every API instance sees the epoch bump because
  #   it lives in the shared session row (test_verify_rejects_the_replaced_
  #   token_after_a_switch, test_verify_enforces_the_expected_epoch).
```

SES-02 gate (*显式原子 switch；禁止“后登录自动踢人”绕过明确确认*): the switch route only reaches the takeover branch behind the explicit client confirmation flow (`switch_session` = `login_session(takeover=True)`; a plain login never passes the flag), the takeover commits the SWITCH event + epoch bump + fresh token in one transaction (`test_switch_takes_over_the_live_other_device_atomically`, `test_concurrent_switches_from_both_devices_serialize`), and a switch writes no wallet charge (`test_switch_writes_no_wallet_charge`).

SES-03 gate (*码暂停、设备撤销、用户禁用同步使 session 失效；仅删除客户端 token 不算服务端撤销*): the admin code suspend/revoke paths now call `revoke_session` (epoch bump + lease pulled into the past + LOGOUT event in the same transaction — the T16 core generalized with `device_id=None`), the device unbind/revoke paths reuse the same core device-scoped, and `verify_session_context` re-checks the activation-code and device status **inside the write transaction** so a suspended/revoked code or a released device invalidates the session even while the lease still looks alive (`test_verify_rejects_a_suspended_code_even_with_a_live_lease`, `test_verify_rejects_a_revoked_code_even_with_a_live_lease`, `test_verify_rejects_a_released_device_even_with_a_live_lease`). Suspended/revoked accounts never even establish a session (`test_switch_rejects_suspended_code` + the 403 `CODE_SUSPENDED`/`CODE_REVOKED` login/switch gate).

## Implementation Highlights

- **The explicit atomic switch** (`POST /api/customer/sessions/switch`): `switch_session` drives the §12.3 state machine with `takeover=True` — a live lease on the other device is displaced only because the user explicitly confirmed the switch (plain login stays 409, the §12.3 red line). Every other branch matches the login semantics exactly (same-device renewal / recovery / lapsed takeover / missing-row establish) so a switch never invents new states. The SWITCH event, the epoch bump and the fresh token commit atomically; the displaced token's heartbeat and late logout answer 401 `SESSION_REPLACED` leaving the new session byte-identical.
- **Idempotency** (dev doc §6.3): switch carries a mandatory `Idempotency-Key` with its own `session_switch` envelope operation (distinct AAD from `session_login`); the replay probe runs before authentication and before the limiter (the T19 precedent), the scope is the credential digest probed across key versions, and the sealed payload reproduces the original 200/201.
- **Rate limiting**: switch draws the *same* `login:ip` budget as login (a switch is a login-shaped attempt — the limiter must not be bypassable by switching instead), answering 429 `RATE_LIMITED` + `Retry-After` once spent (`test_switch_is_rate_limited_through_the_login_ip_budget`).
- **Code-status gate (SES-03)**: login and switch both check the activation-code status under the device credential — a suspended code answers 403 `CODE_SUSPENDED`, a revoked code 403 `CODE_REVOKED`, so a disabled account never reaches the workspace.
- **Revocation propagation (SES-03)**: the T16 unbind session-revocation core moved into `customer_session_service.revoke_session` — epoch bump + `GREATEST(now, created_at + 1µs)` lease pull (full microsecond precision, the PR #47 P2 lesson) + a reason-tagged LOGOUT event naming the acting user. `device_id` scopes it to the session riding a released/revoked device (the T16/T18 delegation); `None` revokes whatever session the user holds, which the admin code suspend/revoke paths now call (`REASON_CODE_SUSPENDED` / `REASON_CODE_REVOKED`) so a suspended/revoked account loses its session in the same transaction — server-side revocation, never a client-side token wipe.
- **The in-transaction fencing verifier** (new `customer_auth.py`, dev doc §12.4): `verify_session_context` — the module every customer *write* route will call inside its business transaction (T21 wires them up). It resolves the presented session token to the live row under `FOR UPDATE` (serializing against switch/takeover/revocation writers), re-compares the request's `expected_user_id/device_id/session_id/session_epoch/lease_until` against the row, judges the lease on the transaction's `SELECT clock_timestamp()` (the actual post-lock time — PR #52 P2), and re-checks the activation-code and device status (defence in depth). A token that no longer owns the row, or an `expected_*` mismatch, answers `SESSION_REPLACED`; a lapsed lease answers `SESSION_EXPIRED`. The `CustomerSessionContext` carries exactly six fields — user, activation code, device, session id, epoch, lease — never a token or digest.
- **SES-01 clock discipline**: every new judgment — the switch's lease, the revoke_session lease pull, the verifier's lease — samples the PostgreSQL clock on the caller's transaction; the application process clock never decides a lease or a revocation (`_transaction_now_iso` added to the admin routes for the same reason).

## Independent Code Review — PR #52 connector (1 P1 + 2 P2, all substantively fixed)

The PR #52 chatgpt-codex-connector review returned **1 P1 + 2 P2**, every finding substantively fixed with a regression lock:

| # | Severity | Finding | Fix |
| --- | --- | --- | --- |
| R10 | **P1** | The code-status gate (`_require_active_code`) read the activation-code status without a lock — an admin suspend/revoke (which locks the code row, then the riding session) could commit between the gate's read and `login_session` acquiring the session row, leaving a suspended/revoked code holding a live session | The gate now locks the code row `FOR UPDATE` through establishment (it runs inside the same business transaction as `login_session`, so the lock is held to commit), serializing with the admin suspend/revoke lock order (code → session); locked by `test_code_status_gate_locks_the_code_through_establishment` — a concurrent write to the code row blocks on the gate's lock until establishment commits |
| R11 | P2 | The verifier judged the lease against `SELECT now()` — fixed at transaction start — so a write transaction that began while the lease was valid but waited on the `FOR UPDATE` until after it expired still passed with the earlier timestamp | The verifier now judges the lease against `SELECT clock_timestamp()` after the row lock (the actual wall-clock time); locked by `test_verify_judges_the_lease_on_the_post_lock_clock` — a verifier that waits past a 2 s lease answers `SESSION_EXPIRED` |
| R12 | P2 | §12.4 requires re-comparing the complete snapshot `user_id + device_id + session_id + session_epoch + lease`, but the verifier had no `expected_lease_until` — T21 callers could not detect a lease snapshot change before the business transaction | Added `expected_lease_until` and re-compare it (any mismatch → `SESSION_REPLACED`); locked by `test_verify_fences_a_lease_snapshot_that_changed`, with the happy path `test_verify_accepts_the_matching_lease_snapshot` |

## Files Changed

| File | Change |
| --- | --- |
| `server/app/customer_session_routes.py` | `POST /sessions/switch` endpoint (SES-02); the `_replay_login_response` envelope helpers thread both `operation` (session_switch AAD) and the in-transaction PG clock `now` (the T19 PR #51 fix, merged cleanly on rebase); the code-status gate (403 CODE_SUSPENDED / CODE_REVOKED) on login and switch |
| `server/app/customer_session_service.py` | `switch_session` (the §12.3 state machine with `takeover=True`); `revoke_session` — the T16 session-revocation core generalized for SES-03 propagation (device-scoped or user-wide) |
| `server/app/customer_auth.py` | **New (frozen name, code checklist §9.2)** — `verify_session_context` + `CustomerSessionContext`, the in-transaction fencing verifier for the T21 write routes |
| `server/app/customer_device_service.py` | `_revoke_session_riding_device` now delegates to `customer_session_service.revoke_session` (device-scoped) — the shared core lives in one place |
| `server/app/admin_activation_routes.py` | The suspend/revoke paths call `revoke_session` (`REASON_CODE_SUSPENDED`/`REASON_CODE_REVOKED`) so the session dies in the same transaction; `_transaction_now_iso` (SES-01 clock) |
| `server/tests/test_customer_sessions.py` | +19 cases on the T19 base: the 13 switch cases (atomic takeover / same-device renewal / recovery / timeout takeover / missing-row establish / Bearer gates / idempotency-key / lost-response replay / shared login:ip budget / concurrent switches serialize / no wallet charge / suspended-code gate) + code-gate/revocation-propagation additions |
| `server/tests/test_customer_fencing.py` | **New (frozen name, code checklist §3.3)** — 13 cases for `verify_session_context` (minimal context / unknown token / replaced-after-switch / lapsed lease / logged-out / suspended & revoked code with a live lease / released device with a live lease / expected session-id, epoch, device, user enforcement / no token leak) |
| `docs/evidence/T20-EVIDENCE.md`, the two ledgers | Evidence records |

No new migration: revision 029 (T13) already provides `customer_session_state`, `customer_session_events` and `customer_idempotency_envelopes` — the switch reuses the existing session row and event table.

## Regression

```
$ uv run python -m pytest tests/test_customer_sessions.py tests/test_customer_fencing.py -q  # 71 passed (54 session + 17 fencing incl. 4 PR #52 locks)
$ uv run python -m pytest tests -q                                                            # 994 passed, 2 warnings, 0 failed
  # 990 on the 32-new base + 4 PR #52 review regression locks; zero regression.
  # The 2 warnings are the pre-existing environment artifacts (httpx deprecation
  # and the Windows GBK subprocess-reader thread in test_db.py), not failures.
  # 958 on the T19-merged base + 32 new (19 session switch/gate + 13 fencing); zero regression.
  # The 2 warnings are the pre-existing environment artifacts (httpx deprecation
  # and the Windows GBK subprocess-reader thread in test_db.py), not failures.
$ uv run ruff check .          → All checks passed!
$ uv run ruff format --check . → 162 files already formatted
$ uv run mypy app              → Success: no issues found in 65 source files
```

## Section 14 Ledger Record

```text
任务/工作包：T20 / SES-02、SES-03
Owner / Reviewer：后端（Agent 执行）/ PR #52 chatgpt-codex-connector 评审（1 P1+2 P2，逐条实质修复含 4 例回归锁定：码状态门 FOR UPDATE 串行化建立/租约判定用 clock_timestamp() 锁后实际时钟/expected_lease_until 快照重比对）+ 安全自评审（结论见 commit message 与证据账本）
分支 / 基线 SHA：feat/customer-v3-t20-session-switch-fencing / 基线 939c305（main，T19 PR #51 合并后）
上游规格段落：客户版任务清单 V3 §4 T20、§12.3 SES-02/SES-03；代码开发清单 V3 §9.2 customer_auth.py 冻结名、§3.3 test_customer_fencing.py 冻结名；激活码开发文档 §12.3 第五行显式 switch、§12.4 事务内 fencing、§6.1 API 表、§6.3 幂等、§13.2 错误码；测试与验收规格 §2.3/§3.4
改动文件：server/app/customer_session_routes.py（switch 端点+operation/now 双参数信封重放+码状态门 403）、server/app/customer_session_service.py（switch_session=login_session takeover=True+revoke_session 通用化 T16 吊销核心）、server/app/customer_auth.py（新增：verify_session_context 事务内 fencing 校验器）、server/app/customer_device_service.py（_revoke_session_riding_device 委托 revoke_session）、server/app/admin_activation_routes.py（suspend/revoke 调 revoke_session 传播+_transaction_now_iso）、server/tests/test_customer_sessions.py（+19 用例）、server/tests/test_customer_fencing.py（新增 13 用例）、docs/evidence/T20-EVIDENCE.md、任务与证据账本
失败测试或回归锁定：先红后绿 19 例（switch 13：原子顶替/同设备续租/同设备恢复/过期接管/缺行建立/Bearer 必需/未知凭据/幂等键必需/丢响应重放/共享 login:ip 限流/双设备并发串行化/零钱包扣费/停用码 403 门）+码状态门与撤销传播 6 例（停用码不建立会话/验证器拒停用码即使租约存活/拒撤销码即使租约存活/拒释放设备即使租约存活/管理 suspend/revoke 传播会话失效）共 19；fencing 13 例（最小上下文/未知 token/switch 后旧 token 被拒/租约过期/logout 后/停用码活租约/撤销码活租约/释放设备活租约/expected session_id/epoch/device/user 四类二次比对/不泄漏 token 或 digest）+PR #52 评审回归锁定 4 例（expected_lease_until 匹配快照通过/不匹配快照 SESSION_REPLACED/锁等待跨租约后 clock_timestamp 判定 SESSION_EXPIRED/码状态门 FOR UPDATE 使并发写码行阻塞到建立提交）
实现结果：显式原子 switch 落地（switch_session 复用 §12.3 状态机 takeover=True：只有显式确认的客户端流程才进入顶替分支，普通 login 保持 409 无自动踢人；SWITCH 事件+epoch bump+新 token 单事务原子提交，旧 token 的 heartbeat/logout 统一 401 SESSION_REPLACED 不触碰新会话）；switch 幂等信封独立 session_switch operation（AAD 区分）、重放探针先于鉴权先于限流、共享 login:ip 预算（switch 不能绕过限流）；码状态门（login/switch 403 CODE_SUSPENDED/CODE_REVOKED，停用账户不建立会话）；撤销传播（T16 吊销核心通用化为 revoke_session：device_id 限定设备解绑/撤销委托，None=用户全量，管理端 suspend/revoke 同事务调用以使 session 立即失效，服务端撤销而非客户端删 token）；事务内 fencing 校验器 customer_auth.verify_session_context（行锁+expected_* 二次比对+PG 时钟租约+码/设备状态 defense-in-depth 复查，稳定 SESSION_REPLACED/SESSION_EXPIRED，CustomerSessionContext 仅 6 字段最小权限面）；全部新判定用事务内 PG 时钟（SES-01）
验证命令与通过数：专项 71 passed（54 session+17 fencing）；全量 994 passed 零回归（990 的 32 新增基础上 +4 例 PR #52 评审回归锁定；2 个既存 warning：httpx deprecation+Windows GBK subprocess reader）；ruff/format/mypy 全绿（162 files formatted，65 source files typed）
证据层级：AUTOMATED_VERIFIED
安全与可观测性：switch/登录同一码状态门（停用账户不建立会话）；SWITCH/LOGOUT 事件记录 actor 与 reason（撤销传播可审计）；revoke_session 通用化后 T16/T18 设备解绑/撤销与 T20 码停用/撤销共用一条原子吊销核心；fencing 校验器行锁串行化 switch/接管/撤销写入，expected_* 二次比对拦截过期期望（epoch 回跳不可能）；token 只以 keyed digest 过库，CustomerSessionContext 永不携带凭据；租约与吊销判定全部用事务内 PG 时钟（SES-01）；错误码稳定（401 SESSION_REPLACED/EXPIRED、403 CODE_SUSPENDED/REVOKED、409 OTHER_DEVICE_ONLINE、429、503 fail-closed）
迁移与回滚：无新迁移（029 已预留全部结构）；回滚=还原代码（会话行可保留，租约到期自然释放）
外部授权记录：无；未调用真实 ZPay/COS/付费 Provider/对外发码/灰度/公网发布
未测试项：fencing 校验器接入业务写路由（T21 SES-04/SES-05）；客户端 switch 确认流程与 OpenAPI 重新生成（T30/T28 前端门禁）；多 API 实例同 switch 竞态的进程级证明（PG 行锁语义覆盖，T13 100 并发先例）；STAGING/REAL_CHAIN/PRODUCTION
Lore 提交 SHA：见 PR squash 合并 SHA
```
