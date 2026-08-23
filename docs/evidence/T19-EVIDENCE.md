# T19 — Session Login, Heartbeat, Logout & the 30/90-Second Database Lease (SES-01)

## Task Information

| Field | Value |
| --- | --- |
| **Task ID** | T19 / SES-01 |
| **Owner** | Backend (Agent) |
| **Reviewer** | Qoder CodeReview subagent + security self-review (see the ledger record) |
| **Branch / Base SHA** | `feat/customer-v3-t19-session-lease` / base `ed65a03` (main after PR #49) |
| **Date** | 2026-08-23 |
| **Evidence Level** | `AUTOMATED_VERIFIED` (PG 16 fixture, localhost:5433, real Alembic chain 001→037) |

## Exit-Gate Verification

Task exit gate (task list §4 T19): *旧 token heartbeat/logout 失败，崩溃后租约可释放* — both delivered as automated tests on the dedicated migrated fixture database `t19_customer_sessions_test` (module fixture: DROP/CREATE + `alembic upgrade head`, function fixture: TRUNCATE sweep under `session_replication_role = replica`):

```bash
$ uv run python -m pytest tests/test_customer_sessions.py -v
35 passed  # 29 fail-first + 6 review regression locks (5 subagent + 1 PR #51 connector)
  # Old-token failure after takeover/recovery: 401 SESSION_REPLACED on both
  #   heartbeat and late logout — the late logout leaves the new session's
  #   device_id and lease byte-identical (test_late_logout_after_takeover_...).
  # Crash-recovery lease release: _expire_lease pulls the lease into the past
  #   (the crash simulation) → the other device's login takes over (201,
  #   epoch + 1, system TIMEOUT event with actor_user_id NULL) and a
  #   lapsed-lease heartbeat answers 401 SESSION_EXPIRED without resurrecting
  #   the session (test_login_after_lease_expiry_..., test_heartbeat_on_...).
```

## Implementation Highlights

- **§12.3 login state machine under a row lock** (`customer_session_service.login_session`): the user's single `customer_session_state` row is locked `FOR UPDATE` — no row (defensive) → epoch-1 establish; same device + currently valid session token → **renew only** (same token, same epoch, lease = now + 90 s, status 200); same device without a usable token → recovery (epoch + 1, fresh token; the replaced token can never heartbeat again); lapsed lease (any device) → takeover (epoch + 1 + the system `TIMEOUT` event, no actor); another device with a live lease → 409 `OTHER_DEVICE_ONLINE` with the masked name hint and the remaining lease — never a silent kick, and nothing changes in the database.
- **heartbeat** (`heartbeat_session`): the row is located by the presented token's digests probed across every configured key version (the PR #44 rotation-window rule) and locked; a matching token under a live lease renews it (epoch untouched) and appends a `HEARTBEAT` event; an unknown/replaced token answers `replaced` (one answer for forged and replaced — no oracle); a matching token under a lapsed lease answers `expired` and never resurrects the session (no event, no lease write).
- **logout** (`logout_session`): the T16 unbind precedent — the lease is pulled into the past via `GREATEST(now, created_at + 1 µs)` (the `lease_after_created` CHECK backstop) with the **full microsecond precision** of the transaction clock, and a `LOGOUT` event with reason `user_logout` lands on the append-only audit trail. The released slot is immediately re-loggable by the other device; the old token's next heartbeat answers 401 `SESSION_EXPIRED`.
- **Idempotency envelopes (dev doc §6.3)**: login and logout carry a mandatory `Idempotency-Key`. The replay probe runs *before* authentication (the unbind-envelope precedent — a lost response replays even though the credential is by then unusable), the envelope scope is the credential's keyed digest probed across key versions (the enroll precedent), a same-key/different-body retry answers 409 `IDEMPOTENCY_CONFLICT`, and a business 409 rolls back with the transaction so the key stays reusable. The sealed login payload carries the state-machine outcome (`_outcome`) so a replay reproduces the original 200/201 status code.
- **Rate limiting (T15)**: login draws the shared `login:ip` budget (env `VIDEO_REPLICA_RATE_LIMIT_LOGIN_IP`, default 10) through the PG UPSERT counters — two API instances share one budget — and a refusal answers 429 `RATE_LIMITED` with a `Retry-After` header.
- **Stable error codes (§13.2 + the T16 REQUIRED/INVALID precedent)**: 401 `DEVICE_CREDENTIAL_REQUIRED` / `DEVICE_CREDENTIAL_INVALID` / `DEVICE_REVOKED` / `SESSION_TOKEN_REQUIRED` / `SESSION_REPLACED` / `SESSION_EXPIRED`; 409 `OTHER_DEVICE_ONLINE` / `IDEMPOTENCY_CONFLICT`; 400 `IDEMPOTENCY_KEY_REQUIRED`; 429 `RATE_LIMITED`; 503 `SESSION_SERVICE_UNAVAILABLE` (PG runtime / AEAD / device-key misconfiguration, fail-closed).
- **SES-01 clock discipline (review P1 fix)**: all three routes sample `SELECT now()` *inside the business transaction* (the unbind/activation precedents) and pass it down as `now=` — the lease judgement, every written timestamp and the envelope recovery window share one server-side PostgreSQL clock; the application process clock never decides a lease (multi-instance clock skew would otherwise bend the 90-second lease and can push `last_heartbeat_at` before `created_at`, violating the 029 CHECKs).
- **Audit semantics**: LOGIN/HEARTBEAT/LOGOUT events carry `actor_user_id` (user-driven events, review P3 fix); only the system TIMEOUT runs actor-less. A deliberate spec note: after a user logout the *next* login still records the system TIMEOUT first (the takeover arm runs whenever the prior lease is lapsed — the table has no released-reason marker); the audit trail therefore reads LOGOUT → TIMEOUT → LOGIN, which is the documented-by-evidence literal §12.3 behaviour, not an anomaly.

## Key Defects Caught During Development (all fixed before commit)

| # | Defect | Why it mattered | Fix |
| --- | --- | --- | --- |
| 1 | The login row lock had **no `user_id` filter** (`SELECT … LIMIT 1 FOR UPDATE`) | With more than one user in `customer_session_state` the state machine would lock and mutate *some other user's* session row | `WHERE user_id = %s FOR UPDATE`; the caller supplies the authenticated device's `user_id` / `activation_code_id` |
| 2 | `logout` wrote `GREATEST(%s::timestamptz::text, …)` — a text-vs-timestamptz GREATEST | Type-mismatched GREATEST; the T16 unbind precedent uses two timestamptz operands | Aligned verbatim: `GREATEST(%s::timestamptz, created_at::timestamptz + interval '1 microsecond')` with the full-precision transaction clock (the PR #47 P2 lesson: whole-second trimming once left a released lease alive up to a second) |
| 3 | `heartbeat` wrote `last_heartbeat_at` trimmed to whole seconds | A same-second heartbeat lands before `created_at` and trips the `heartbeat_not_before_created` CHECK (found by the real DB run) | Full-precision `datetime.isoformat()` for `last_heartbeat_at`/`updated_at` |
| 4 | The TIMEOUT event carried the *new* device's `activation_code_id` | The event describes the timed-out session; its binding columns must be the old row's | `old_row`'s activation code / device id / session id / epoch |
| 5 | The same-key race branch answered a blanket 409 instead of replaying | The unbind precedent replays the winner's sealed envelope after `ON CONFLICT DO NOTHING` loses the race | `_find_envelope` + the replay helpers (409 only for hash mismatch / purged / lapsed; decryption failure → 503) |
| 6 | 429 lacked the `Retry-After` header | The activation-route contract exposes it via the exception headers | `blocked.headers = {RETRY_AFTER_HEADER: str(decision.retry_after_seconds)}` |

## Independent Code Review — REQUEST_CHANGES → all findings substantively fixed

The CodeReview subagent audit returned REQUEST_CHANGES: **1 P1 + 2 P2 + 5 P3**, every finding substantively fixed with a regression lock where testable. A second round on the opened PR (the chatgpt-codex-connector review on PR #51) added one more P2, fixed the same way:

| # | Severity | Finding | Fix |
| --- | --- | --- | --- |
| R1 | **P1** | The lease judgement and every written timestamp used the *application process* clock (`server_now_utc`) while the envelope recovery window sampled the PG clock in the same transaction — a direct violation of the SES-01 “PostgreSQL is the only trusted clock” red line; clock skew across API instances would bend the 90 s lease and can violate the 029 CHECK constraints | All three routes sample `SELECT now()` inside the business transaction (`_transaction_now`, the unbind/activation precedents) and pass it down as `now=`; the service docstring now states the clock contract |
| R2 | P2 | A missing device-domain key made `_token_digests` raise an uncaught `ActivationKeyError` → 500 on heartbeat/logout (and on the login scope probe), violating the 503 fail-closed contract the evidence file claimed | The scope computation joined the AEAD try-block (`except (ActivationKeyError, IdempotencyKeyError) → 503`) on login/logout; heartbeat wraps the service call with the same fail-closed answer — locked by `test_heartbeat_without_device_keys_fails_closed_503` |
| R3 | P2 | The `login:ip` limiter ran *before* the replay probe — legitimate network retries burned the shared budget and could lock a user out of their own cached 201 (the exact inversion of the activate/enroll precedent, T15 review P2 rule) | The fully validated replay probe now precedes the limiter (the activation-route structure); locked by `test_login_idempotent_replay_spends_no_rate_limit_budget` (budget spent → fresh key 429 → same-key retry still replays) |
| R4 | P3 | LOGIN/HEARTBEAT events carried `actor_user_id` NULL — user-driven events would be misread as system events in the audit trail | LOGIN/HEARTBEAT/LOGOUT now record the acting user (only TIMEOUT stays actor-less); locked by `test_user_driven_events_record_the_acting_user` |
| R5 | P3 | The concurrency test started two threads without a barrier — a serialized run satisfied the same assertions | Both concurrent tests align request starts on `threading.Barrier(2)` |
| R6 | P3 | Two first-writers hitting the defensive no-row branch would race the PK INSERT → uncaught `UniqueViolation` → 500 | `INSERT … ON CONFLICT (user_id) DO NOTHING` + loser re-reads the winner's committed row and re-drives the state machine (retry-bounded paranoia); locked by `test_concurrent_first_logins_on_a_missing_row_never_500` |
| R7 | P3 | `_login_ip_limit` used `max(1, int(raw))` — an operator setting `0` to disable the limiter got a near-total ban instead of the safe default (the T15 `_positive_int_env` semantics refuse non-positive overrides) | `login_ip_limit()` added to `security_rate_limit.py` on the `_positive_int_env` precedent; locked by `test_login_ip_limit_env_falls_back_to_safe_defaults` |
| R8 | P3 | The post-logout re-login writes a system TIMEOUT (spec-literal but audit-misleading) | Documented in Implementation Highlights (audit semantics note); the table has no released-reason marker, so the literal §12.3 takeover arm stays authoritative |
| R9 | P2 (PR #51) | The envelope recovery-window verdict compared `recovery_expires_at` against the *application process* clock (`datetime.now(UTC)`) although the deadline itself was minted from `SELECT now()` — a skewed API node would reject a still-valid lost-response replay early (or accept an expired one), violating the SES-01 trusted-clock rule the lease already follows | `_find_envelope` samples the PostgreSQL `now()` in the same envelope-read transaction and threads it through `_envelope_recoverable` (the activation-route `_server_now` precedent); locked by `test_replay_recovery_window_uses_the_postgresql_clock` — an application clock skewed a decade ahead still replays the sealed 201 |

## Files Changed

| File | Change |
| --- | --- |
| `server/app/customer_session_service.py` | New — the §12.3 state machine (login/heartbeat/logout), lease constants, masked device names, append-only event writes |
| `server/app/customer_session_routes.py` | New — the three routes, Bearer device/session authentication, idempotency envelopes, replay-probe-before-limiter, `login:ip` rate limiting, stable error codes, in-transaction PG clock sampling (lease + envelope recovery verdict) |
| `server/app/main.py` | Mount `customer_session_router` (append-only, two lines) |
| `server/app/security_rate_limit.py` | `login_ip_limit()` on the `_positive_int_env` precedent (review P3 fix; the T15 frozen module grows one accessor, no behaviour change) |
| `server/tests/test_customer_sessions.py` | New — 35 cases (29 fail-first + 6 review regression locks: 5 subagent + 1 PR #51 connector) |
| `docs/evidence/T19-EVIDENCE.md`, the two ledgers | Evidence records |

No new migration: revision 029 (T13) already provides `customer_session_state`, `customer_session_events` and `customer_idempotency_envelopes`.

## Regression

```
$ uv run python -m pytest tests/test_customer_sessions.py -q    # 35 passed
$ uv run python -m pytest tests -q                              # 958 passed, 2 warnings, 0 failed
  # 923 on the T18-merged base + 35 new (29 fail-first + 6 review locks); zero regression.
  # The 2 warnings are pre-existing environment artifacts, not failures: the
  # httpx deprecation and the Windows GBK subprocess-reader thread in test_db.py
  # (PytestUnhandledThreadExceptionWarning: UnicodeDecodeError 'gbk' — a
  # subprocess _readerthread reading non-ASCII output on Windows).
  # An earlier run in a bash-less terminal showed 941 passed + 3 skipped — the
  # three POSIX-launcher/secret-scan tests skip when no bash is on PATH; the
  # normal environment (and CI's Linux gate) runs them, as the 958-pass run shows.
$ uv run ruff check .          → All checks passed!
$ uv run ruff format --check . → 160 files already formatted
$ uv run mypy app              → Success: no issues found in 64 source files
```

## Section 14 Ledger Record

```text
任务/工作包：T19 / SES-01
Owner / Reviewer：后端（Agent 执行）/ Qoder CodeReview 子代理独立评审（REQUEST_CHANGES：1 P1+2 P2+5 P3，逐条实质修复含 5 例回归锁定）+ PR #51 chatgpt-codex-connector 二轮评审（1 P2：信封 recovery 窗口时钟，已修复含回归锁定）+ 安全自评审（结论见 CUSTOMER-TASK-EVIDENCE-V3.md T19 记录与 commit message）
分支 / 基线 SHA：feat/customer-v3-t19-session-lease / 基线 ed65a03（main，PR #49 合入后）
上游规格段落：客户版任务清单 V3 §4 T19、§12.3 SES-01；代码开发清单 V3 §3.2 customer_session_service.py/customer_session_routes.py、§3.3 test_customer_sessions.py 冻结名；激活码开发文档 §12.3 登录状态机、§6.1 API 表、§6.3 幂等、§13.2 错误码；测试与验收规格 §2.3/§3.4
改动文件：server/app/customer_session_service.py（新增：login/heartbeat/logout 状态机+租约常量+脱敏+事件写入+防御分支 ON CONFLICT 竞争保护）、server/app/customer_session_routes.py（新增：三路由+Bearer 设备/会话双层鉴权+幂等信封（重放探针先于限流）+login:ip 限流+稳定错误码+事务内 PG 时钟采样）、server/app/main.py（挂载 customer_session_router）、server/app/security_rate_limit.py（login_ip_limit() 对齐 _positive_int_env 先例，评审 P3 修复）、server/tests/test_customer_sessions.py（新增 35 用例：29 先红后绿+6 评审回归锁定）、docs/evidence/T19-EVIDENCE.md、任务与证据账本
失败测试或回归锁定：先红后绿 29 例+评审回归锁定 6 例，共 35——模块单元 4（90 秒租约常量冻结/设备名脱敏 2/login_ip_limit env 回落先例语义）+login 契约门 5（Bearer 必需/未知凭据/已释放凭据 401 DEVICE_REVOKED/幂等键必需/login:ip 限流 429+Retry-After）+幂等重放不烧预算 1（预算耗尽后新键 429 而同键重放仍返密封响应+REPLAY 头——探针先于限流）+§12.3 状态机 5（同设备有效 token 续租 200 同 token 同 epoch 同 session_id/同设备无 token 恢复 epoch+1 旧 token 401 SESSION_REPLACED/异设备在线 409 脱敏提示+零 LOGIN 事件/租约过期接管 TIMEOUT actor NULL+旧 token 401/无行防御 epoch-1）+幂等信封 4（丢响应重放同 token 同 epoch 无第二 LOGIN/同键异参 409/409 冲突不烧键过期后同键成功/重放 recovery 窗口用 PG 时钟——应用时钟偏移十年仍可重放，PR #51 connector P2）+heartbeat 6（Bearer 必需/续租 epoch 不变 session_id 对齐 DB/伪造 token 401 SESSION_REPLACED/过期租约 401 SESSION_EXPIRED 不复活无事件/logout 后 401 SESSION_EXPIRED/设备密钥缺失 503 fail-closed 非 500）+logout 6（幂等键必需/租约置过去+LOGOUT 事件/对方设备立即可登录/丢响应重放 204+REPLAY 头无第二 LOGOUT/接管后迟到 logout 401 不触碰新会话 lease 不变/过期租约 401）+事件审计 1（LOGIN/HEARTBEAT/LOGOUT 记录 actor_user_id，仅 TIMEOUT 无 actor）+并发 2（Barrier 对齐双线程 lapsed 起单赢家 201+409 epoch 恰 2/防御分支双首写 ON CONFLICT 输家重读无 500 epoch 恰 2）+红线 1（事件表无明文凭据）
实现结果：§12.3 登录状态机落地：user 单行 FOR UPDATE 锁内判定续租（同 token 同 epoch 200）/恢复（epoch+1 新 token）/接管（lapsed+TIMEOUT 系统事件 actor NULL，事件绑定旧会话列）/冲突（409 脱敏设备名+剩余租约，零库变更）；三路由均在业务事务内采样 SELECT now()（SES-01 PG 唯一可信时钟，unbind/激活先例）以 now= 传入服务层，租约判定/写入时间戳/信封 recovery 窗口共用同一服务器时钟（评审 P1 修复：进程时钟不再决定租约）；heartbeat 跨密钥版本 digest 探测定位行锁，epoch 不变续租，未知/被替换 token 统一 401 SESSION_REPLACED（无预言机），lapsed 401 SESSION_EXPIRED 不复活；logout 复用 T16 unbind 模式 GREATEST 拉过去租约+LOGOUT 事件（reason user_logout），全微秒精度事务时钟（PR #47 P2 教训）；防御分支 INSERT ON CONFLICT DO NOTHING+输家重读赢家行重驱动状态机（无 UniqueViolation 500，评审 P3 修复）；login/logout 幂等信封（重放探针先于限流——完全校验通过的重放零预算消耗，激活路由 T15 review P2 规则；scope 跨版本探测+业务 409 随事务回滚不烧键+密封 payload 携带 outcome 还原 200/201）；login:ip 共享限流（T15 UPSERT 计数器，login_ip_limit() 对齐 _positive_int_env 先例，默认 10/窗口，429+Retry-After）；LOGIN/HEARTBEAT/LOGOUT 事件记录 actor_user_id（仅 TIMEOUT 无 actor，评审 P3 修复）；错误码全量对齐 §13.2+T16 先例；设备域密钥/AEAD/PG 故障一律 503 fail-closed（评审 P2 修复：不误报 401 以免客户端擦除有效凭据）
验证命令与通过数：专项 35 passed（含 -W error::PytestUnhandledThreadExceptionWarning 干净复验）；全量 958 passed 零回归（T18 合并后基线 923+新增 35；2 个既存 warning：httpx deprecation+test_db.py Windows GBK subprocess reader 线程，均为环境产物非失败；一轮无 bash 终端下 941 passed+3 skipped 为 POSIX launcher/secret scan 的 PATH 探测性 skip，正常环境与 CI Linux 门禁全跑）；ruff/format/mypy 全绿（160 files formatted，64 source files typed）
证据层级：AUTOMATED_VERIFIED
安全与可观测性：session token 只以 keyed digest 过库（跨版本探测）；409 冲突设备名脱敏（首两字符+**，空名返回空）不泄露全名；TIMEOUT 系统事件无 actor 而 LOGIN/HEARTBEAT/LOGOUT 记录 actor_user_id（用户驱动与系统事件审计可区分）；事件表 append-only（029 触发器拒 UPDATE/DELETE）且测试锁定无明文凭据；信封 AEAD 密文（AAD 绑定 operation/scope/key_digest）+request_hash 用 sha256 替代避免明文 secret 入哈希；三路由租约判定/写入时间戳/信封 recovery 窗口共用事务内 PG 时钟（SES-01 唯一可信时钟，客户端本地时间非真源，评审 P1 修复；信封 recovery 过期判定同样用信封读取事务内采样的 PG 时钟而非进程时钟——PR #51 connector P2 修复，应用时钟偏移不影响重放判定）；完全校验通过的幂等重放零限流预算消耗（合法网络重试不烧 login:ip，激活先例）；伪造与被替换 token 统一 401 无预言机；设备域密钥/AEAD/PG 配置故障一律 503 fail-closed 不误报 401（§13.2 客户端擦除凭据合同）
迁移与回滚：无新迁移（029 已预留全部结构）；回滚=还原代码（会话状态表数据可保留，租约到期自然释放）
外部授权记录：无；未调用真实 ZPay/COS/付费 Provider/对外发码/灰度/公网发布
未测试项：显式原子 switch 与 epoch fencing（T20 SES-02/SES-03）；业务写路由事务内 fencing（T21 SES-04/SES-05）；客户端 30 秒心跳合同与 OpenAPI 重新生成（T28 前端门禁）；多 API 实例同租约竞态的进程级证明（PG 行锁语义覆盖，同 T13 100 并发先例）；STAGING/REAL_CHAIN/PRODUCTION
Lore 提交 SHA：见 PR squash 合并 SHA
```
