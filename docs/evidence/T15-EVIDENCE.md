# T15 — Shared Multi-Instance Rate Limiting & Anti-Enumeration (ACT-08)

## Task Information

| Field | Value |
| --- | --- |
| **Task ID** | T15 / ACT-08 |
| **Owner** | Security/Backend (Agent) |
| **Reviewer** | session-internal code review |
| **Branch / Base SHA** | `feat/customer-v3-t15-rate-limit-anti-enumeration` / base `c206323` (T14, PR #45 squash) |
| **Date** | 2026-08-23 |
| **Evidence Level** | `AUTOMATED_VERIFIED` (real PostgreSQL 16 fixture; red → green) |

## Exit-Gate Verification

Task exit gate: *不存在、过期、作废响应近似；429 与指标可验证* (task list §3 T15) + ACT-08 gate: *IP/码摘要/账户多维限流；错误和时延近似* — No-Go red line: *进程内限流不能作为多 API 方案* — delivered as the frozen module `server/app/security_rate_limit.py` plus migration `032_security_rate_limits` (two PG-only tables), wired into the activation route with 18 fail-first tests in `server/tests/test_customer_security.py` (frozen name):

- **Shared budget across instances** — the fixed-window counter state lives in `security_rate_limit_counters` and is mutated by one atomic UPSERT (`INSERT … ON CONFLICT … RETURNING`): `test_counter_shared_across_connections` spends the budget from two independent psycopg connections (two API instances) and observes the same hit count and verdicts. No Redis, no message queue — PostgreSQL is the shared truth, per the architecture red lines.
- **429 with a verifiable budget & Retry-After** — exceeding either dimension answers 429 `RATE_LIMITED` with `Retry-After` in seconds until the window closes; the window resets after expiry and the dimensions stay independent (three dedicated PG cases).
- **Anti-enumeration response parity** — every code-side rejection (unknown, malformed, expired, suspended, revoked, already-active) already shares the single 400 `ACTIVATION_UNAVAILABLE` body from T13; T15 closes the remaining timing side channel with a constant PBKDF2-SHA256 cost (~120k iterations) on the unified rejection path, asserted by `test_unified_rejection_applies_anti_enumeration_delay` (unknown / malformed / expired all run the delay) and the measurable-baseline module unit.
- **Failure metrics & alert threshold** — every rejection appends one event to the append-only `security_auth_failures` (dimension + identifier + request id + server clock); `failure_metrics` aggregates the trailing window and `failure_alert_active` compares against the operator threshold — crossing it emits an ERROR-level structured log line, the hook the T37 / OPS-02 alerting pipeline consumes.

## 1. The Limiter Engine (`server/app/security_rate_limit.py`, frozen name)

1. **Fixed-window consumption** — `consume_rate_limit(dimension, identifier, limit, window_seconds)` is one UPSERT: a fresh bucket starts at 1, a lapsed window restarts at 1, a live bucket increments. Two API instances racing on the same bucket serialize on the primary key, so the shared budget can never be exceeded by landing on different processes. The decision (`RateLimitDecision`) carries `allowed`, `retry_after_seconds` (ceil to the window end, min 1) and `hit_count`.
2. **Frozen dimension vocabulary** — `activate:ip`, `activate:code`, `login:ip`, `login:account` (CHECK-constrained in 032; the login pair is reserved for the T19 lane so the same engine carries over). The code dimension is keyed by the *digest* of the normalized code — the plaintext never reaches a counter row or a failure event.
3. **Failure auditing** — `record_auth_failure` appends to the trigger-guarded audit table; `failure_metrics` / `failure_alert_active` walk the `(dimension, occurred_at)` index.
4. **Configuration** — `VIDEO_REPLICA_RATE_LIMIT_ACTIVATE_IP` (default 10), `VIDEO_REPLICA_RATE_LIMIT_ACTIVATE_CODE` (default 5), `VIDEO_REPLICA_RATE_LIMIT_WINDOW_SECONDS` (default 300), `VIDEO_REPLICA_RATE_LIMIT_FAILURE_ALERT_THRESHOLD` (default 20); non-numeric or non-positive values fall back to the safe defaults — an environment typo can never disable the limits (locked by a module unit).
5. **Clock** — windows and metrics are decided on the server clock (`SELECT now()` from the same PostgreSQL the counter lives in); `now` is injectable only from tests.

## 2. The Route Integration (`activation_code_routes.py`)

The limiter consumes budget in its own regularly committed transaction (a normal `pg_transaction`, not autocommit) *outside* the activation transaction: the budget must never be refunded when the business transaction rolls back — a rejected attempt is exactly what the limiter exists to count, and every API instance behind the load balancer draws from this same PostgreSQL budget.

- **IP dimension** — consumed by *every* activation attempt, malformed codes included, or a format-probing burst would bypass the abuse budget (`test_malformed_requests_share_the_ip_budget`).
- **Code dimension** — consumed only for well-formed codes (a malformed input has no digest); one real code hammered across different IPs trips its own bucket (`test_activate_code_dimension_blocks_code_burst`).
- **Idempotent replay is free (review P2)** — a read-only probe runs *before* the limiter: a fully validated, openable envelope short-circuits into the replay response without spending any budget, honouring the T14 contract that retries are side-effect free; every other envelope state falls through to the original flow (`test_idempotent_replay_spends_no_rate_limit_budget`).
- **Unified failure audit** — every code-side rejection appends the failure event (identifier = keyed digest, or the fixed `"malformed"` marker) and applies the constant anti-enumeration delay; the delay runs even when the audit write itself failed, so the timing profile never depends on database health. Crossing the alert threshold emits an ERROR-level log record.
- **429 shape** — `RATE_LIMITED` with `Retry-After` riding the exception (the error path does not go through the injected `Response`).

## 3. Migration `032_security_rate_limits`

Chained off the live head `029_customer_sessions_and_idempotency` (030 stays reserved for T25; numbering follows the T12/031 precedent of skipping past the frozen 028–030 window). PG-only: SQLite (internal P0 lane) is a no-op that advances the revision only, matching 027–029.

- `security_rate_limit_counters` — one row per `bucket_key` (`{dimension}|{identifier}`), CHECK-constrained non-negative hit count and non-blank window.
- `security_auth_failures` — append-only audit: a `BEFORE UPDATE OR DELETE` row trigger refuses any rewrite; a `(dimension, occurred_at)` index serves the metrics scans. The identifier for the code dimension is the keyed digest (red line: no plaintext code in this table, locked by test).
- **Downgrade guard (review P3 #2)** — refuses loudly once any failure event exists (026/028 precedent): the audit trail is the only record of (attempted) enumeration attacks and must survive a rollback; an unused schema downgrades symmetrically.
- **Retention constraint documented in place** — the append-only trigger means the future retention cleanup (OPS task) cannot be a plain time-windowed DELETE: it must land as a session-identifier exemption in the trigger or partition drops. Recorded in the migration comments so the OPS task inherits the constraint by design.

## 4. Test Coverage (18 red → green + 2 PR #46 review locks = 20)

| Group | Cases |
| --- | --- |
| Module units (no database, always run, 3) | bucket key joins dimension + identifier; env overrides honoured and non-numeric/non-positive values fall back to the safe defaults; the anti-enumeration delay burns a measurable constant baseline |
| PG integration (dedicated migrated fixture DB `t15_customer_security_test`, 8) | window allows then blocks at the limit; window resets after expiry; dimensions stay independent; **two independent connections (two API instances) share one budget atomically**; failure record + trailing-window metrics; metrics window scoping excludes older events; alert threshold crossing; the failure record holds no plaintext code |
| Route integration (5) | exceeding the IP limit answers 429 with Retry-After; malformed requests share the IP budget; the code dimension blocks a single-code burst across different IPs; every unified rejection records a failure event with the digest identifier; unknown / malformed / expired rejections all apply the constant anti-enumeration delay |
| Review-fix locks (2) | a fully validated idempotent replay spends no rate-limit budget (3 replays all 201 + replay header, budget intact) while the next fresh attempt still trips the limiter; downgrade refuses once failure events exist and succeeds on an empty schema (round-trip 032 → 029 → head) |
| PR #46 review locks (2) | once an IP is blocked, further attempts from it stop minting new code-dimension counters (no bucket sprawl from a blocked source); the CORS layer exposes `Retry-After` so browser clients can read the 429 backoff hint |

## Files Changed

| File | Change |
| --- | --- |
| `server/migrations/versions/032_security_rate_limits.py` | new: counters + append-only failures tables, dimension CHECK vocabulary, downgrade guard, retention-constraint comments |
| `server/app/security_rate_limit.py` | new (frozen name, 308 lines): shared fixed-window consumption, failure auditing + metrics + alert threshold, constant anti-enumeration delay, env configuration with safe fallbacks |
| `server/app/activation_code_routes.py` | limiter wired into the activation route (IP dimension counts malformed attempts; code dimension only well-formed codes), unified failure audit + alert log + constant delay, 429 with Retry-After, read-only replay probe ahead of the limiter (review P2) |
| `server/tests/test_customer_security.py` | new (frozen name): 18 fail-first cases (3 module units + 8 PG integration on the dedicated migrated fixture DB + 5 route integration + 2 review-fix locks) |
| `server/scripts/reconcile_customer_billing.py` | `PG_ONLY_TABLES` registers the two 032 tables (the T07 import/reconcile contract: a new PG-only table must be declared or `_validate_schema` fails closed) |
| `server/scripts/sqlite_to_postgres.py` | `_validate_schema` comment updated for the 032 tables |
| `deploy/customer.env.example` | the four rate-limit variables documented with defaults, plus the reverse-proxy real-client-IP deployment guidance (X-Forwarded-For + `--proxy-headers` + `forwarded-allow-ips`, L4 passthrough alternative, enterprise-NAT budget note) — review P3 #3; carries the T36 topology caveat (loopback middleware vs. proxy real-IP, M2 review M4) |
| 11 existing test files | head-revision assertions advanced `029 → 032` (~20 sites across `test_db` / `test_sqlite_to_postgres` / `test_recharge_orders` / `test_internal_billing` / `test_settings` / `test_characters` / `test_character_domain` / `test_postgres_migrations` / `test_activation_code_schema` / …); the two downgrade-guard tests now target absolute revisions instead of relative steps (the chain grew by one); the T13 route tests raise the limiter budget via env (1000) and truncate the security tables in fixtures so the new shared state cannot leak between cases |

## Regression

```text
# PostgreSQL fixture up (docker customer-v3-pg-test, PG16 :5433)
$ uv run python -m pytest tests/test_customer_security.py -q
18 passed
$ uv run python -m pytest tests/test_customer_security.py tests/test_activation_code_routes.py \
    tests/test_customer_activation.py tests/test_activation_code_schema.py tests/test_customer_idempotency.py -q
80 passed                      # 20 T15 (incl. 2 PR #46 locks) + 60 T13/T14 activation & idempotency cases
$ uv run python -m pytest tests -q
818 passed                     # full suite on the PG fixture (814 after the T15 implementation
                               # round + 4 review-fix tests across two review rounds; zero regressions)
$ uv run ruff check . && uv run ruff format --check . && uv run mypy app
all green (145 files formatted, 59 source files typed)
# client / tauri / e2e untouched by this task; CI gates re-verify
# secret scan: clean (test keys are runtime-generated secrets.token_* fixtures, T13/T14 precedent)
```

## Session Code-Review Notes

Self-review during implementation (pre-subagent):

1. **Counter table is hot-path shared state** — one UPSERT per request per dimension; the bucket primary key is the serialization point, which is the intended multi-instance contract (a longer busy-wait is a capacity signal, not a correctness issue).
2. **The T07 import tool regression was caught by the full suite, not the special** — adding a PG-only migration without registering the tables in `PG_ONLY_TABLES` breaks `_validate_schema` with `source/target table contract differs`; the fix plus ~20 head-revision assertion updates across 9 files is exactly the cross-task blast radius a new migration carries in this repository.
3. **Relative-step downgrade tests are chain-fragile** — the two downgrade-guard tests used `-5` / `-2` relative steps, which silently stopped reaching the guard revision once 032 extended the chain; converted to absolute revision targets so the guarded step cannot drift again.

### Post-Review Fixes (session subagent review: REQUEST_CHANGES → 1 P2 + 2 P3, all fixed)

1. **P2 — idempotent replay was charged rate-limit budget.** The limiter ran before the envelope lookup, so a legitimate retry (the client lost the response of an already-successful activation and replays the same `Idempotency-Key`) spent IP + code budget — with the default code budget of 5/5 min a few network retries would lock a legal user out of their own cached response, violating the T14 contract that retries are side-effect free. Fixed with a read-only probe ahead of the limiter: `_probe_replayable_response` walks the rotation-window scopes, and only a fully validated, openable envelope (matching request hash, live recovery window, loadable key version, AEAD-verifiable ciphertext) short-circuits into the replay response; every other envelope state falls through to the original flow unchanged. Locked by `test_idempotent_replay_spends_no_rate_limit_budget`. The fix initially introduced two static findings (a quoted annotation ruff UP037, and a `str | None` dict entry mypy flagged for the malformed-code request hash) — resolved in place with a `canonical_code or ""` normalization (a malformed request never matches a stored envelope, so the value is semantically inert) and a comment explaining why.
2. **P3 #2 — migration downgrade had no guard and the append-only/cleanup tension was undocumented.** Added the non-empty `security_auth_failures` guard (026/028 precedent: audit evidence must survive a rollback; `test_downgrade_refuses_once_failures_exist` round-trips 032 → 029 → head) and wrote the retention constraint into the migration comments: the future cleanup cannot be a plain DELETE against the trigger — it must be a session-identifier exemption or partition drops, an inherited OPS design constraint.
3. **P3 #3 — deployment guidance gaps.** `deploy/customer.env.example` now documents all four rate-limit variables with defaults and adds the reverse-proxy real-client-IP guidance: behind an L7 proxy every customer would otherwise share the proxy's single IP bucket and trip a site-wide 429 within minutes (X-Forwarded-For + uvicorn `--proxy-headers` with `forwarded-allow-ips` converged to the proxy address, or L4 passthrough; enterprise-NAT exits are a documented tuning knob).

### PR #46 Review Fixes (commit `0bbdf82`, post-squash follow-up on the T15 branch)

1. **Blocked IP kept minting code-dimension counters.** A blocked source could still hammer the route with arbitrary well-formed codes and each distinct digest created a fresh `security_rate_limit_counters` row — unbounded counter-table growth from a single abuser. Fixed in `activation_code_routes.py`: once the IP dimension has already blocked the request, no code-dimension counter is consumed or created. Locked by a test asserting the counters table holds only the IP row while the blocked attempts continue.
2. **CORS hid `Retry-After`.** The 429 carries its backoff hint in `Retry-After`, but the CORS allow-headers/expose-headers configuration did not expose it, so browser clients could not read the hint. Fixed in `main.py` by adding `Retry-After` to the CORS expose list. Locked by a test on the CORS layer.

Count after this round: 18 → **20** cases in `test_customer_security.py`; full suite 816 → **818 passed**. Zero regressions; ruff/format/mypy re-run green.

### Deployment Topology Caveat (M2 review M4, 2026-08-23)

`require_loopback_client` applies unconditionally to every route (`main.py`), which conflicts with the IP dimension this task builds on: with the reverse-proxy guidance above, the *real* client IP reaching the route is non-loopback → 403 `LOOPBACK_ONLY` before the limiter is ever reached; without `--proxy-headers`, every customer shares the proxy's single IP bucket. **The IP-dimension rate limit currently has no runnable deployment topology** (this is the 2026-08-20 review H-03 resurfacing); it depends on T36 splitting the entry assembly per listener. T36's acceptance must add a joint test: real client IP traverses the reverse proxy and reaches the activation route's limiter. The limiter itself and the PG-shared budget are correct and fully tested at the route layer; only the production ingress wiring is deferred.

## Section 14 Ledger Record

```text
任务/工作包：T15 / ACT-08
Owner / Reviewer：安全/后端（Agent 执行）/ 会话内代码评审
分支 / 基线 SHA：feat/customer-v3-t15-rate-limit-anti-enumeration / 基线 c206323（T14 PR #45 squash）
上游规格段落：客户版任务清单 V3 §3 T15、§12.2 ACT-08；代码开发清单 V3 §3 security_rate_limit.py、§11.1 test_customer_security.py 冻结名；激活码开发文档 §11.3 并发与滥用、§7 密钥红线；测试与验收规格 §6
改动文件：server/migrations/versions/032_security_rate_limits.py（新增：counters+append-only failures 两表、维度 CHECK 词表、downgrade 守卫、保留期约束注释）、server/app/security_rate_limit.py（新增 308 行冻结名：共享固定窗口消费 UPSERT、失败审计+指标+告警阈值、常数防枚举时延、env 安全回退）、server/app/activation_code_routes.py（限流接入：IP 维度含 malformed、code 维度仅合法格式码、统一失败审计+告警日志+常数时延、429+Retry-After、限流前只读 replay 预检）、server/tests/test_customer_security.py（新增 20 用例：实现轮 18 + PR #46 评审修复 2）、server/scripts/reconcile_customer_billing.py（PG_ONLY_TABLES 登记 032 两表）、server/scripts/sqlite_to_postgres.py（注释同步）、deploy/customer.env.example（4 个限流变量+反代 IP 部署指导）、11 个测试文件（head 断言 029→032 约 20 处、downgrade 守卫测试改绝对 revision、T13 路由测试限流预算 env 提升+security 表 truncate）、docs/evidence/T15-EVIDENCE.md、任务与证据账本
失败测试或回归锁定：先红后绿 20 例——模块级 3（桶键、env 覆盖+非法回退、常数时延基线）+PG 集成 8（专用迁移库 t15_customer_security_test：窗口允许后阻断/过期重置/维度独立/跨连接共享预算/失败记录+指标/指标窗口作用域/告警阈值/审计无明文码）+路由集成 5（429+Retry-After/malformed 共享 IP 预算/code 维度爆破阻断/统一拒绝记录失败/统一拒绝施加时延）+评审锁定 2（replay 零预算、downgrade 非空守卫往返）+PR #46 评审锁定 2（blocked IP 停止铸造 code 计数器、CORS 暴露 Retry-After）；T13/T14 五文件 60 passed 作为回归锁定
实现结果：多 API 实例共享限流落地 PG（单 UPSERT 原子消费，无 Redis/MQ）；激活接口 IP 维度全部尝试（含 malformed）计数、code 维度按 HMAC 摘要计数（明文永不过库）；超限 429 RATE_LIMITED+Retry-After；窗口过期自动重置；每次拒绝追加 append-only 审计事件并聚合成指标，超阈值打 ERROR 告警日志（T37/OPS-02 消费）；未知/过期/作废等全部拒绝共享统一 400 响应体+常数 PBKDF2 时延（关闭时序侧信道）；幂等 replay 只读预检零预算短路（T14 重试无副作用契约保持）
验证命令与通过数：专项 20 passed；五文件 80 passed；全量 818 passed（814 实现轮 + 4 两轮评审修复新增，零回归，PG fixture）；ruff/format/mypy 全绿（145 files formatted，59 source files typed）；实现轮全量 814 passed + 静态全绿后经会话评审修复 3 条（1 P2+2 P3）复跑全量 816；PR #46 评审修复 2 条（blocked IP 停止铸造计数器、CORS Retry-After）后专项 18→20、全量 816→818 复确认
证据层级：AUTOMATED_VERIFIED
安全与可观测性：计数器与审计表 code 维度只存 keyed 摘要（测试锁定明文不出现）；append-only 触发器拒绝改写审计；审计写失败时时延照常（时序剖面不依赖数据库健康）；窗口与指标只用服务器时钟；env 非法值回退安全默认（环境笔误不会关闭限流）；429 头经异常路径携带；告警阈值 ERROR 日志为 T37 管道挂钩
迁移与回滚：新迁移 032（避开冻结 028–030 区间，从 head 029 顺延；030 仍留给 T25）；PG-only（SQLite 仅 revision 推进）；downgrade 非空守卫（审计必须存活）；T07 导入工具登记 PG_ONLY_TABLES 保持 fail-closed 契约；回滚=032 downgrade（空表时对称）+还原代码
外部授权记录：无；未调用真实 ZPay/COS/付费 Provider/对外发码/灰度/公网发布
未测试项：T19 登录车道复用（login:ip/login:account 维度已建表但路由未接）；真实多实例负载均衡拓扑下的联测（T36）；反代 IP 传递的真实部署验证（运维验收随 T36）；T37/OPS-02 告警管道消费；STAGING/REAL_CHAIN/PRODUCTION
Lore 提交 SHA：见 PR squash 合并 SHA
```
