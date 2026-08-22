# T09 — Per-operator Admin Session/CSRF + Customer-Production Fail-Closed (DB-08)

## Task Information

| Field | Value |
| --- | --- |
| **Task ID** | T09 / DB-08 |
| **Owner** | Security/Backend/OPS (Agent) |
| **Reviewer** | chatgpt-codex-connector (PR review) |
| **Branch / Base SHA** | `feat/customer-v3-t09-admin-session-csrf` / base `e50f931` (PR #39 squash) |
| **Date** | 2026-08-22 |
| **Evidence Level** | `AUTOMATED_VERIFIED` (real PostgreSQL 16 fixture; full-suite green) |

## Exit-Gate Verification

Task exit gate: *每个操作可追溯真实 actor；SQLite、本地资产、开发身份、缺密钥、单管理员映射均拒绝启动* — delivered as:

- `server/tests/test_admin_auth.py` — 32 test functions / **39 passed** (parametrized security-gate matrix), covering credential lifecycle, session/CSRF semantics, RBAC, cookie shape and every fail-closed violation;
- `server/tests/test_internal_admin.py` / `test_rbac.py` — legacy control identity now rejects customer-production requests at runtime (403 `LEGACY_CONTROL_IDENTITY_FORBIDDEN`), zero regression on the internal SQLite lane.

## 1. Per-operator Sessions (application layer over revision-026 `admin_sessions`)

**Exchange credential** (out-of-band, never stored):

- Prefix `ASX1`; payload `{actor, exp, nonce, v}`; HMAC-SHA256 signature over `prefix.body`; base64url, no padding.
- Key resolution is versioned: version N reads `VIDEO_REPLICA_ADMIN_SESSION_HMAC_KEY_VN`; version 1 also accepts the un-suffixed variable. Minimum key length 32 bytes (enforced).
- **Single use by construction**: the SHA-256 of the nonce *is* the `admin_sessions` primary key, so replaying a consumed credential collides on the PK and returns 401 `EXCHANGE_CREDENTIAL_REUSED` instead of minting a second session.
- Expiry, tampering (signature flip), malformed shapes, wrong key version: all rejected 401 `EXCHANGE_CREDENTIAL_INVALID` (log records the exception class only — never the credential).

**Session storage & verification** (PostgreSQL only):

- `POST /api/control/admin/session/exchange` → 201: sets HttpOnly cookie `admin_session` (`path=/api/control`, `samesite=strict`, `secure=true` in customer production, `max_age=TTL`) and returns `{session_id, expires_at, csrf_token, actor}`.
- Session token and CSRF token are `secrets.token_urlsafe(32)`; **only SHA-256 digests reach the database** (`session_digest`, `csrf_digest`, `created_ip_digest`, `created_ua_digest`).
- `GET /api/control/admin/session` (whoami) returns the real actor; `DELETE /api/control/admin/session` revokes (`revoked_at`) and clears the cookie.
- **PostgreSQL time is the only clock**: creation, expiry comparison, last-activity refresh and revocation all use `SELECT now()` inside the same transaction.
- Every request re-joins `users`: inactive actor or a role outside {admin, auditor} invalidates the session immediately (401), and expiry is re-checked against DB time.
- TTL is bounded 60–86,400 s (default 8 h) via `VIDEO_REPLICA_ADMIN_SESSION_TTL_SECONDS`; out-of-range values fail startup/validation.

**CSRF**: every write method (POST/PUT/PATCH/DELETE) behind the admin dependency requires header `X-Admin-CSRF`; the supplied token is compared (constant-time) as a SHA-256 digest against the stored per-session digest. Missing → 403 `ADMIN_CSRF_REQUIRED`; mismatched → 403 `ADMIN_CSRF_INVALID`.

**RBAC**: `AdminReader` (cookie + CSRF on writes) and `AdminWriter` (adds `role == admin`); auditors are strictly read-only (403 `AUDITOR_READ_ONLY`). Employee-role actors cannot even exchange a credential (403 `ADMIN_ROLE_REQUIRED`).

**No silent fallback**: when the PG runtime is unavailable (internal SQLite deployments), all admin-session endpoints fail closed with 503 `ADMIN_SESSIONS_UNAVAILABLE` — the legacy proxy-token identity is never used as a substitute.

## 2. Customer-Production Fail-Closed Gate

`bootstrap.customer_production_security_violations()` / `assert_customer_production_security()` (no-op outside `VIDEO_REPLICA_CUSTOMER_PRODUCTION=true`):

| # | Violation rejected at startup |
| --- | --- |
| 1 | Legacy single-admin control identity (`CONTROL_PROXY_TOKEN_DIGEST` / `CONTROL_ADMIN_USER_ID` / related envs set) |
| 2 | Development auth mode (`VIDEO_REPLICA_AUTH_MODE=desktop|development`) |
| 3 | Dev identity header override (`VIDEO_REPLICA_ALLOW_DEV_IDENTITY_HEADER=1`) |
| 4 | Dev desktop identity (`VIDEO_REPLICA_DESKTOP_USER_ID`) |
| 5 | Persistent local assets (`VIDEO_REPLICA_STORAGE_ROOT`; customer production must use the private COS provider) |
| 6 | Missing admin-session HMAC key (no un-suffixed/`_VN` version configured) |
| 7 | Configured-but-weak admin-session HMAC key (< 32 bytes — must fail at boot, not at first credential use) |

Enforced twice: in `bootstrap.main()` (before ready-check/pool warm-up) and in the API `_lifespan` startup (uvicorn aborts on lifespan errors). SQLite/missing-DSN rejection was already delivered by T05 (`validate_customer_production`); the gate stacks on top so *all five* T09 exit-gate categories refuse to boot. All violations are reported together in one fail-closed message. At runtime, `control_auth.get_control_user` additionally rejects customer-production requests with 403 `LEGACY_CONTROL_IDENTITY_FORBIDDEN` (defence in depth).

`deploy/customer.env.example` documents the boundary, the HMAC key generation/rotation commands and TTL knob (placeholders only — no real secrets). Key rotation is supported end-to-end: once outstanding credentials expire, the retired version (e.g. `_V1`/un-suffixed) may be removed while any later configured version keeps customer production booting.

## 3. PR Review Remediation (PR #40, Codex P2 findings — all fixed)

| # | Finding | Fix |
| --- | --- | --- |
| P2-1 | `bootstrap.py`: hard-coded `(_V1, un-suffixed)` existence check aborted every startup after a rotation retired V1, contradicting the documented rotation flow | `_configured_admin_session_keys()` discovers every configured `…_VN`/un-suffixed variable; the gate now requires *any* one of them (test: boot succeeds with only `_V2`) |
| P2-2 | `bootstrap.py`: existence-only check let a < 32-byte key pass boot, then `admin_hmac_key()` raised at first use | every discovered key variable is length-checked (≥ 32 bytes, same bound as `MIN_HMAC_KEY_BYTES`) at boot; fail-closed instead of fail-later |
| P2-3 | `admin_auth_routes.py`: non-object JSON bodies (`null`/`1`/`"x"`/`[1,2]`) made `dict(json.loads(...))` raise `TypeError` → unauthenticated endpoint returned 500 | `isinstance(loaded, dict)` guard rejects them as malformed → stable 401 `EXCHANGE_CREDENTIAL_INVALID` |

## 4. Operator Tooling

`server/scripts/issue_admin_exchange_credential.py` — CLI to mint a short-lived single-use credential for an operator (reads the versioned HMAC key from the environment; prints the credential only).

## Files Changed

| File | Change |
| --- | --- |
| `server/app/admin_auth_routes.py` | new (521 lines): credential issue/verify (non-object JSON guarded), session create/load/revoke, CSRF, RBAC, 3 routes |
| `server/scripts/issue_admin_exchange_credential.py` | new: operator credential CLI |
| `server/tests/test_admin_auth.py` | new: 32 test functions / 39 cases (red → green; +3 PR-review regression locks) |
| `server/app/bootstrap.py` | + security-gate functions (version-aware key discovery + min-length check); called in `main()` before ready check |
| `server/app/main.py` | + `admin_auth_routes` router, gate call in `_lifespan`, `X-Admin-CSRF` in CORS allow-headers |
| `server/app/control_auth.py` | legacy identity rejects customer production at runtime (403) |
| `deploy/customer.env.example` | new: customer-production env template with fail-closed inventory |
| `client/vite.config.ts` | test workers get `--no-experimental-webstorage` (Node ≥ 25 native Web Storage breaks jsdom `localStorage`; vitest#8757; no-op on CI Node 24) |
| 11 further tracked files | restored after a local `git stash` mishap, incl. Windows-suite hardening: explicit bash path vs System32 WSL stub, UTF-8 subprocess decoding, `fsync` on writable handle, 0600 assertions guarded on win32, `TemporaryDirectory` for ffmpeg temp files, isolated child env with `SystemRoot` |

## Regression

```
# PostgreSQL fixture up (scripts/pg-fixture.sh start; docker customer-v3-pg-test, PG16 :5433)
$ uv run python -m pytest tests -q
677 passed in ...            # 39 new T09 cases (36 + 3 PR-review locks); PG suites ran against the fixture
$ uv run ruff check . && uv run ruff format --check . && uv run mypy app
all green
# client gate (npm run check --workspace client): biome 54 files, tsc, vitest 24 files / 324 tests — green
# npm run check:e2e (biome) — green; npm run check:tauri (cargo fmt --check + check --locked) — green
# scripts/verify_no_secrets.sh — green (equivalent direct run via git-bash; npm runner shell
#   resolves bare "bash" to the System32 WSL stub on this machine, not a repository issue)
```

## Section 14 Ledger Record

```text
任务/工作包：T09 / DB-08
Owner / Reviewer：安全/后端/OPS（Agent 执行）/ chatgpt-codex-connector（PR 评审，3 条 P2 意见已逐条实质修复）
分支 / 基线 SHA：feat/customer-v3-t09-admin-session-csrf / 基线 e50f931（PR #39 squash）
上游规格段落：客户版任务清单 V3 §2 T09、§12.1 DB-08；代码开发清单 V3 §3.2（admin_auth_routes.py 冻结名）；激活码开发文档 §15（admin_sessions 表，026 迁移发布）；测试与验收规格 §8
改动文件：server/app/admin_auth_routes.py（新增 521 行，含非对象 JSON 防护）、server/scripts/issue_admin_exchange_credential.py（新增）、server/tests/test_admin_auth.py（新增 39 用例）、server/app/bootstrap.py（安全门 + 版本化密钥发现与长度校验）、server/app/main.py、server/app/control_auth.py、deploy/customer.env.example（新增）、client/vite.config.ts、11 个 stash 事故重建文件
失败测试或回归锁定：先红后绿——39 用例覆盖凭据签发/验签/过期/篡改/单次使用（nonce 摘要主键撞唯一约束）/非对象 JSON 拒收、会话创建/whoami/logout/过期/撤销/禁用即时失效、CSRF 缺失/不匹配、auditor 只读、cookie 安全形状（HttpOnly/strict/path/生产 secure）、安全门逐项违规与聚合报告（含仅 _V2 可启动、短密钥启动即拒）、运行时 legacy 身份 403、PG 缺失 503 fail-closed
实现结果：ASX1 一次性 HMAC 交换凭据 → HttpOnly cookie + 每会话 CSRF（库内仅存 SHA-256 摘要，PG 时间唯一时钟）；AdminReader/AdminWriter RBAC；客户生产安全门在 bootstrap 与 API lifespan 双重 fail-closed（单管理员映射/开发身份/本地资产/缺密钥/弱密钥），密钥轮换后仅保留新版本可正常启动；SQLite/缺 DSN 由 T05 门槛承接，五类启动拒绝全部落地
验证命令与通过数：pytest tests/test_admin_auth.py → 39 passed；全量 → 677 passed（PG fixture 真库）；ruff/format/mypy 全绿；client check（biome 54/tsc/vitest 324）、check:e2e、check:tauri、verify_no_secrets 全绿
证据层级：AUTOMATED_VERIFIED
安全与可观测性：凭据/token/CSRF 仅摘要入库；日志只记异常类名与 actor/session 标识；无真实密钥入仓（customer.env.example 全占位符）；密钥最小 32 字节（启动期即校验）、版本化轮换
迁移与回滚：无新迁移（复用 026 已发布 admin_sessions 表）；内部 SQLite 车道零行为变化（安全门仅客户生产生效）
外部授权记录：无；未调用真实 ZPay/COS/付费 Provider/发码/灰度/公网发布
未测试项：管理端前端页面（T32）；多实例部署下的会话行为（T36 staging）；STAGING/REAL_CHAIN/PRODUCTION
Lore 提交 SHA：见 PR squash 合并 SHA
```
