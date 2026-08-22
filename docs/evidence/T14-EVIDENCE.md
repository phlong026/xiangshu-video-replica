# T14 — AEAD Idempotency Recovery & Expired-Envelope Cleanup (ACT-07)

## Task Information

| Field | Value |
| --- | --- |
| **Task ID** | T14 / ACT-07 |
| **Owner** | Backend/Security (Agent) |
| **Reviewer** | session-internal code review |
| **Branch / Base SHA** | `feat/customer-v3-t14-idempotency-recovery` / base `fec36c7` (T13, PR #44 squash) |
| **Date** | 2026-08-23 |
| **Evidence Level** | `AUTOMATED_VERIFIED` (real PostgreSQL 16 fixture; red → green) |

## Exit-Gate Verification

Task exit gate: *同 key 返回同一 username、device token、session；不重复入账* (task list §3 T14) + ACT-07 gate: *同 key 同参返回同一安全结果；异参冲突；不重复入账* — No-Go red line: *信封不得保存可直接使用的明文 secret* — delivered as the extracted, reusable envelope engine `server/app/customer_idempotency.py` (frozen name) plus the expired-ciphertext cleanup story (`count_expired_envelopes` / `purge_expired_envelopes`, the `video-replica-maintenance` systemd timer and the `purge_idempotency_envelopes` CLI), with `activation_code_routes.py` refactored onto the shared module (zero behaviour change, 42/42 T13 cases green) and 18 fail-first tests in `server/tests/test_customer_idempotency.py` (frozen name):

- **Same key → same identity, no double charge** — T13's concurrency cases (two threads sharing one key + body → both 201 with identical username/device token/session token, exactly one CHARGE) keep passing against the refactored route; the engine guarantees it structurally: `INSERT … ON CONFLICT DO NOTHING` + reload, request-hash equality, AEAD-sealed replay.
- **Same key + different params → 409** — the loaded `request_hash` differing from the retry's normalized fingerprint is the conflict evidence (module-level test + T13 route regression).
- **No usable plaintext secret persists** — the sealed ciphertext is the only persisted copy of the one-time response (ACT-07 red line); tests assert the secret and even its JSON key never appear in the ciphertext, and the raw client key reaches the database only as a SHA-256 digest.
- **Expiry & cleanup (the T14 story)** — every completed envelope carries a recovery window (default 24 h, `VIDEO_REPLICA_CUSTOMER_IDEMPOTENCY_RECOVERY_SECONDS` overridable); after it lapses the route answers 409 (the key is spent) and `purge_expired_envelopes` nulls `ciphertext/key_version/recovery_expires_at` under `purged_at` in one UPDATE, walking the 029-reserved `idx_customer_idempotency_envelopes_recovery` index. The 029 CHECK coupling keeps a purged row structurally payload-free; the purge is idempotent (a second run finds nothing) and a purged envelope is no longer recoverable (load shows no ciphertext → route 409).

## 1. The Extracted Engine (dev doc §11.2 / §12.1)

`customer_idempotency.py` generalizes T13's route-private envelope code with an `operation` parameter, so the later customer write paths (T17 second-device enroll, T19 login, T22 recharge) share one contract:

1. **Versioned AEAD keys** — `VIDEO_REPLICA_CUSTOMER_IDEMPOTENCY_AEAD_KEY` (base64url, exactly 32 decoded bytes; `_V<n>` rotation, V1 also accepts the bare name), `highest_customer_aead_key()` seals new envelopes under the highest configured version, `customer_aead_key(version)` opens recovery under the envelope's own recorded version; a retired version inside the recovery window fails closed (`IdempotencyKeyError` → the route answers 503, never an unhandled 500).
2. **Digests** — `idempotency_key_digest` (raw client key → SHA-256 hex, never the key itself) and `request_hash` (freeze the *normalized* request: values stripped, keys sorted, canonical JSON → SHA-256; whitespace-only retries replay instead of burning the key).
3. **AEAD envelope** — `envelope_aad(operation, scope, key_digest)` binds the ciphertext to its own envelope row (a sealed response can never be replayed against a different row); `seal_response`/`open_response` use AES-256-GCM with a 12-byte random nonce prefix; a ciphertext that fails verification or decodes to a non-dict raises `IdempotencyKeyError`.
4. **Persistence** — `insert_envelope` (placeholder, `ON CONFLICT DO NOTHING`, returns the id or `None` on key reuse), `load_envelope` (returns the frozen `EnvelopeRecord`: request_hash / ciphertext / key_version / recovery_expires_at / purged_at), `complete_envelope` (back-fill the sealed response before commit).
5. **Cleanup** — `count_expired_envelopes` (dry-run count) and `purge_expired_envelopes` (the UPDATE). Both compare `recovery_expires_at::timestamptz <= now::timestamptz` — the 029 column is `Text`, so the cast matches the CHECK constraint's own comparison semantics; `_as_utc` normalizes the caller's clock to second-precision UTC ISO, the same shape the route writes.

## 2. The Route Refactor (zero behaviour change)

`activation_code_routes.py` drops its ten envelope-private helpers and calls the module: `highest_customer_aead_key`/`recovery_window_seconds` resolve configuration, `idempotency_key_digest` + `compute_request_hash` freeze the request, the rotation-window scope walk loads `EnvelopeRecord` per configured device-domain version (highest first, replaying under the found scope's AAD), and the winner seals via `seal_response`/`complete_envelope`. The replay path maps `IdempotencyKeyError` to 503 `ACTIVATION_SERVICE_UNAVAILABLE` (the envelope's key version was retired inside the recovery window, or the ciphertext is corrupt — a server-side failure, error contract §13.2); key resolution catches `(ActivationKeyError, IdempotencyKeyError)`. All 42 T13 activation cases (contract + concurrency + schema/triggers) pass unchanged against the refactored route, including the 100-thread ACT-06 race.

## 3. The Maintenance Story (ACT-07 cleanup)

| Piece | File | Behaviour |
| --- | --- | --- |
| CLI | `server/scripts/purge_idempotency_envelopes.py` | `--dry-run` reports the eligible count; the real run purges inside one transaction; output carries counts only (no business values, credentials or envelope contents); missing DSN (`--database-url` / `VIDEO_REPLICA_DATABASE_URL`) exits 1 fail-closed |
| systemd unit | `deploy/systemd/video-replica-maintenance.service` | oneshot, sandboxed (`NoNewPrivileges`/`ProtectSystem=strict`/…), reads `/etc/video-replica/customer.env`; later tasks (T23 reconciliation, expiry sweeps) append their own `ExecStart=` lines |
| timer | `deploy/systemd/video-replica-maintenance.timer` | daily 04:10 with `RandomizedDelaySec=10m`, `Persistent=true`, staggered against the 03:20 backup timer |
| env placeholders | `deploy/customer.env.example` | documents `VIDEO_REPLICA_DEVICE_FINGERPRINT_HMAC_KEY` (T13 omission backfilled), `VIDEO_REPLICA_CUSTOMER_IDEMPOTENCY_AEAD_KEY` and `VIDEO_REPLICA_CUSTOMER_IDEMPOTENCY_RECOVERY_SECONDS` with generation commands and rotation guidance |

No migration ships with T14 — the schema landed in 029 (the reserved `purged_at` column, the payload three-state coupling CHECK and the recovery index); T14 owns the job that uses them.

## 4. Test Coverage (18 red → green)

| Group | Cases |
| --- | --- |
| Module units (no database, 8) | request hash stable across surrounding whitespace; request hash distinguishes different business params; key digest hides the raw key (sha256, deterministic); seal/open round-trip restores the payload; wrong AAD rejected (`verification failed`); sealed ciphertext holds no plaintext secret (ACT-07); AEAD rotation window resolves both key versions (V1/V2); retired key version fails closed (`not configured`); recovery-window env override (valid value honoured, invalid/missing fall back to the default) |
| PG integration (dedicated migrated fixture DB `t14_customer_idempotency`, 7) | envelope lifecycle (insert → complete → load → open restores the sealed one-time response); same key + different request hash is conflict evidence; expired recovery window is visible to the caller (back-dated under the CHECK coupling); purge clears only expired envelopes (expired/live/already-purged triple: exactly the expired row nulled, live intact, second run returns 0); purged envelope is no longer recoverable (load shows no ciphertext); the 029-reserved recovery scan index exists |
| CLI (3) | the CLI purges expired envelopes (row nulled + `purged_at` set); `--dry-run` keeps rows untouched; missing DSN returns 1 |

## Files Changed

| File | Change |
| --- | --- |
| `server/app/customer_idempotency.py` | new (frozen name, 334 lines): versioned AEAD keys, digests + normalized request hash, AES-GCM seal/open with AAD binding, envelope persistence (insert/load/complete), expired-envelope count/purge |
| `server/scripts/purge_idempotency_envelopes.py` | new (frozen name): the maintenance CLI (dry-run / real / fail-closed DSN) |
| `server/app/activation_code_routes.py` | refactored onto the shared module: ten envelope-private helpers removed, module API called with `operation=ACTIVATE_OPERATION`, replay failures map `IdempotencyKeyError` → 503; zero behaviour change (42/42 T13 cases green) |
| `server/tests/test_customer_idempotency.py` | new (frozen name): 18 fail-first cases (8 module units + 7 PG integration on the dedicated migrated fixture DB + 3 CLI) |
| `deploy/systemd/video-replica-maintenance.service` | new (frozen name): sandboxed oneshot running the purge CLI from `customer.env`; `OnFailure=` alert hook (PR review P2) |
| `deploy/systemd/video-replica-maintenance-alert.service` | new: `OnFailure=` target emitting the ALERT-priority journald record for an exhausted purge retry budget (PR review P2; T37/OPS-02 consumes the same hook) |
| `deploy/systemd/video-replica-maintenance.timer` | new (frozen name): daily 04:10, persistent, staggered against the backup timer |
| `deploy/customer.env.example` | backfills the T13 `VIDEO_REPLICA_DEVICE_FINGERPRINT_HMAC_KEY` placeholder; documents the T14 AEAD key + recovery-window variables with generation/rotation guidance |

## Regression

```text
# PostgreSQL fixture up (docker customer-v3-pg-test, PG16 :5433)
$ uv run python -m pytest tests/test_customer_idempotency.py -q
18 passed
$ uv run python -m pytest tests/test_activation_code_routes.py tests/test_customer_activation.py tests/test_activation_code_schema.py tests/test_customer_idempotency.py -q
60 passed                      # 42 T13 activation cases unchanged against the refactored route + 18 new
$ uv run python -m pytest tests -q
795 passed, 3 skipped         # full suite on the PG fixture (780 baseline + 18 T14 = 798 collected; the
                               # 3 skips are the pre-existing Windows-environment bash cases — POSIX
                               # launcher ×2 + secret-scan shell — which run on the Linux CI gates)
$ uv run ruff check . && uv run ruff format --check . && uv run mypy app
all green (142 files formatted, 58 source files typed)
# CLI end-to-end against the fixture: --help renders; missing DSN exits 1;
# connecting to an unmigrated database fails loud (UndefinedTable), never silently
# client: npm run check --workspace client → unchanged, 324 tests passed
# e2e lint: biome check e2e → clean; tauri: cargo fmt --check + cargo check --locked → clean
# secret scan: clean (test AEAD keys are the public 0..31-bytes fixtures, T13 precedent)
```

## Session Code-Review Notes

Self-review during implementation (pre-PR):

1. **`recovery_expires_at` comparison semantics** — the 029 column is `Text`, so the purge predicates cast both sides to `timestamptz`, exactly matching the CHECK constraint's comparison; the route's expired-window refusal parses the stored ISO string the same way T13 did (unchanged).
2. **Purge vs. activation race** — a just-completed envelope carries a future `recovery_expires_at`, so the purge can never null a live recovery window; an in-flight placeholder (ciphertext NULL) never matches the purge predicate (`ciphertext IS NOT NULL`).
3. **CLI transaction shape** — the real purge runs inside `with conn.transaction()`; the dry-run count is read-only and commits nothing. Output is counts only.
4. **Unused-variable and lint debt** — one F841 (test helper assignment) and E501/I001 formatting issues were fixed before the full run; the formatter reformatted two files, after which every gate is green.

### Post-Review Fixes (session subagent review: APPROVE, 0 P1/P2, 4 P3)

1. **P3 #1 cleanup-scan index — investigated, reverted to a documented known limitation.** The review suggested a partial expression index on `recovery_expires_at::timestamptz` to make the daily purge sargable (029's plain btree on the text column cannot serve the `::timestamptz` comparison). A 032 migration was drafted, then rejected after direct verification against the PG16 fixture: `psycopg.errors.InvalidObjectDefinition: functions in index expression must be marked IMMUTABLE` — the `text -> timestamptz` cast consults the TimeZone GUC and can never enter an index expression; a hand-rolled IMMUTABLE wrapper would lie to the planner and silently corrupt the index when the GUC changes. Postgres itself provides no correct escape here short of a column-type migration to a real `timestamptz` column. Per the review's own sizing (table bounded by successful activations, one scan per day) the sequential scan is accepted as a known limitation; the 032 migration was deleted, the nine head-revision assertions restored to `029_customer_sessions_and_idempotency`, and `test_recovery_scan_index_exists` reasserts 029's plain index with the full rationale in its docstring.
2. **P3 #2 vacuous-skip fixed** — `test_customer_idempotency.py` dropped the module-level `pytestmark` skip in favor of a `pytest.skip()` at the top of the `envelope_dsn` fixture: the module-level units (request hash, digest, AEAD round-trip, rotation windows) and the missing-DSN CLI case now always run, so a machine without the fixture can no longer report a vacuous all-green.
3. **P3 #3 boot-compensation race hardened** — `video-replica-maintenance.service` gained `Restart=on-failure` + `RestartSec=30` with `[Unit] StartLimitIntervalSec=300 / StartLimitBurst=4`: a `Persistent=true` replay racing a same-host PostgreSQL still starting up now retries within a bounded budget instead of failing the day's sweep.
4. **P3 #4 namespace-package risk closed** — `server/scripts/__init__.py` added (docstring only) so `scripts.purge_idempotency_envelopes` imports as a regular package member and cannot be shadowed by another `scripts` directory on `sys.path`.
5. **PR #45 review P2 purge-failure alerting (post-CI)** — the reviewer noted the `Restart=on-failure` budget could exhaust silently: a missed daily sweep would leave expired AEAD envelopes past their recovery window with no metric, `OnFailure=` hook or other alert path, contrary to the acceptance spec (测试与验收规格 §3 line 99: "密文清理延迟和清理失败触发指标与告警"). Fixed by wiring `OnFailure=video-replica-maintenance-alert.service` into the maintenance unit: the moment the retry budget exhausts and the unit enters the failed state, the new alert unit emits an ALERT-priority journald record (`systemd-cat -t video-replica-maintenance -p alert`) carrying the operator remediation path (inspect `journalctl -u video-replica-maintenance.service`, restore PostgreSQL, re-run the purge CLI). The journal record is deliberately the primitive: it is operator-visible on a bare host today and is exactly the hook the T37 / OPS-02 structured-logging-and-alerting pipeline consumes in production; a metrics stack dependency now would invert the task order (red line: OPS work does not leap ahead of T36/T37).

## Section 14 Ledger Record

```text
任务/工作包：T14 / ACT-07
Owner / Reviewer：后端/安全（Agent 执行）/ 会话内代码评审
分支 / 基线 SHA：feat/customer-v3-t14-idempotency-recovery / 基线 fec36c7（T13 PR #44 squash）
上游规格段落：客户版任务清单 V3 §3 T14、§12.2 ACT-07；代码开发清单 V3 §9.1 customer_idempotency.py/test_customer_idempotency.py、§10.1 purge CLI、§12 maintenance service/timer 冻结名；激活码开发文档 §11.2 幂等信封、§7 密钥红线；测试与验收规格 §2
改动文件：server/app/customer_idempotency.py（新增 334 行冻结名：版本化 AEAD 密钥+摘要+规范化请求哈希+AES-GCM 密封/开启+信封持久化+过期计数/清理）、server/scripts/purge_idempotency_envelopes.py（新增维护 CLI：dry-run/真实清理/缺 DSN fail-closed）、server/app/activation_code_routes.py（重构接入共享模块：删除十个信封私有助手、operation 泛化调用、IdempotencyKeyError→503 映射，行为零变化）、server/tests/test_customer_idempotency.py（新增 18 用例）、deploy/systemd/video-replica-maintenance.service/.timer（新增冻结名：沙箱化 oneshot+每日 04:10 timer 与备份错峰）、deploy/customer.env.example（补 T13 设备域密钥占位+T14 AEAD 密钥/恢复窗口占位）、docs/evidence/T14-EVIDENCE.md、任务与证据账本
失败测试或回归锁定：先红后绿 18 例——模块级 8（请求哈希空白稳定/参数区分、摘要隐藏原始键、密封往返、错 AAD 拒绝、密文无明文 secret、轮换窗口双版本、退役版本 fail-closed、恢复窗口 env 覆盖）+PG 集成 7（信封生命周期、同键异参冲突证据、过期窗口可见、purge 只清过期且幂等、purged 不可恢复、恢复索引存在）+CLI 3（真实清理/dry-run 不动行/缺 DSN exit 1）；T13 42 例作为重构回归锁定全部保持绿（含 ACT-06 100 并发与同键恢复）
实现结果：幂等信封引擎提取为共享模块（operation 泛化供 T17/T19/T22 复用）；恢复窗口到期后同 key 409（键已花费）；purge_expired_envelopes 单条 UPDATE 清空密文三列并记 purged_at（029 CHECK 耦合保证 purged 行结构上无载荷、幂等重跑为 0）；maintenance timer 每日清理使表不无限增长；路由重构后信封行为与 T13 完全一致
验证命令与通过数：test_customer_idempotency 18 passed；T13 专项四文件合计 60 passed；全量 795 passed + 3 skipped（总数 798 = 780 基线 + 18 新增，PG fixture，零回归；3 个 skip 为既存 Windows 环境性 bash 用例——POSIX launcher×2 + secret scan shell，Linux CI 门禁上全跑；第一轮全量 797 passed+1 个 gate1_e2e 重启控制器线程时序 flaky，与 T14 无代码关联，单独重跑 2.1s 通过）；评审修复后终跑专项 60 passed + 静态全绿（142 files formatted）+ 全量 795 passed/3 skipped 复确认；ruff/format/mypy 全绿（58 source files typed）；CLI --help/缺 DSN exit 1/未迁移库清晰报错均人工验证
证据层级：AUTOMATED_VERIFIED
安全与可观测性：信封不保存可直接使用的明文 secret（AEAD 密文为一次性响应唯一持久化副本，测试锁定密钥名与值均不出现在密文中）；原始幂等键仅以 SHA-256 摘要入库；AAD 绑定 operation/scope/key_digest 防跨行重放；purge 输出仅计数无业务值；退役密钥版本在恢复窗内 503 fail-closed；到期判定只用服务器时钟
迁移与回滚：无新迁移（029 已预留 purged_at/三态耦合 CHECK/恢复索引，T14 补齐使用它们的任务）；回滚=还原代码（信封表结构不变，purge 的行为可安全中止与重跑）
外部授权记录：无；未调用真实 ZPay/COS/付费 Provider/对外发码/灰度/公网发布
未测试项：T17/T19/T22 对共享信封引擎的复用（后续任务各自交付）；maintenance timer 的真实 systemd 环境运行（Windows 开发机无法验证，运维验收随 T36 部署手册）；共享限流与防枚举时延近似（T15/ACT-08）；STAGING/REAL_CHAIN/PRODUCTION
Lore 提交 SHA：见 PR squash 合并 SHA
```
