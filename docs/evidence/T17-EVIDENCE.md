# T17 — Second-Device Enroll, One-Shot Pairing & First-Device Approval (DEV-02)

## Task Information

| Field | Value |
| --- | --- |
| **Task ID** | T17 / DEV-02 |
| **Owner** | Backend (Agent) |
| **Reviewer** | session-internal code review |
| **Branch / Base SHA** | `feat/customer-v3-t17-second-device-pairing` / base `e30ea64` (main after PR #48; T17 merged origin/main — PR #48's 033–036 chain landed first, so the T17 migration was renumbered 033→037 and the canonical probe key of revision 034 was adopted) |
| **Date** | 2026-08-23 |
| **Evidence Level** | `AUTOMATED_VERIFIED` (real PostgreSQL 16 fixture; red → green) |

## Exit-Gate Verification

Task exit gate: *实现第二设备 enroll、第一设备批准和一次性配对*；验收栏 *候选设备摘要绑定、批准不可转用、第二设备不充值* (task list §4 T17) + DEV-02 gate: *并发 slot2 只有一个成功；批准不可转用；响应丢失可恢复* — No-Go red line: *第二设备不得再次首充或创建第二用户* — delivered as revision `037_device_pairing_requests` (the pairing half of the frozen 028 topic, its own revision per the 026 precedent; drafted as 033 on the 032 head, renumbered to 037 after PR #48's 033–036 chain landed on main first — Alembic takes its order from `down_revision`, never from the file name) plus the T17 extension of the frozen modules `server/app/customer_device_service.py` and `server/app/customer_device_routes.py` (code checklist §3.2) with 30 fail-first tests appended to `server/tests/test_customer_devices.py` (frozen name, §3.3; 52 total with T16's 22).

- **Candidate digest binding, approval not transferable** — a pairing row is created *bound to the keyed digest of the candidate device fingerprint* (the raw fingerprint never reaches the database); the partial unique index `uq_device_pairing_requests_active` keeps at most one active request per (code, digest) so a different fingerprint enrolling the same code gets its own fresh PENDING row and can never consume another candidate's approval.
- **Second device never re-charges** — the pairing table carries no wallet or order columns at all, and the consumption branch only inserts one `customer_devices` row: the full-flow test proves the chain grew by exactly one device row while `recharge_orders` / `wallet_transactions` / customer users all stayed at their activation-time counts (1/1/1).
- **Concurrent slot 2: exactly one winner** — two approved rival candidates race the consume branch behind the code-row lock; the barrier test proves one 201 and one 409 `DEVICE_SLOTS_FULL`, exactly one `CONSUMED` pairing row and exactly two BOUND devices (slot 1 + the winner's slot 2).
- **Response loss recoverable** — the 201 carrying the one-time credential is the only sealed surface (the T14 envelope engine, operation `device_enroll`, scope = the candidate fingerprint digest): the same key replays the sealed credential, a different body with the same key answers 409, and the read-only replay probe runs *before* the shared limiter so the retry spends no abuse budget.

## 1. Migration `037_device_pairing_requests` (new, PG-only)

`device_pairing_requests` — `id`, `activation_code_id` (FK), `candidate_fingerprint_hmac` + `candidate_fingerprint_key_version`, `display_name` / `platform` (copied onto the device row at consumption), `status`, `expires_at`, `approved_at` / `approved_by_device_id` (FK), `consumed_at` / `consumed_device_id` (FK), `created_at`.

- **Four-state shape coupling** (the 028 three-state precedent, generalized): `PENDING` proves no transition column; `APPROVED` proves `approved_at` + `approved_by_device_id` and no consumption; `CONSUMED` proves both transitions plus `consumed_device_id`; `EXPIRED` proves no consumption (it may carry a lapsed approval — the audit stays visible). Enforced by `ck_device_pairing_requests_status_shape`.
- **`uq_device_pairing_requests_active`** — partial unique on `(activation_code_id, candidate_fingerprint_hmac) WHERE status IN ('PENDING','APPROVED')`: retried enrolls reuse the active row, terminal rows never block a fresh request (the DEV-01 release-aware constraint precedent).
- **Downgrade guard** — a non-empty table refuses with `RuntimeError` (pairing rows are the audit evidence of who approved which second device; the 027/028/032 guard precedent); an unused schema downgrades symmetrically. SQLite stays on the internal P0 lane (early return, the 025–036 precedent).
- **Head-assertion sweep** — after the origin/main merge landed PR #48's 033–036 chain, 22 head assertions across 10 test files were swept `036_low_review_constraint_guards` → `037_device_pairing_requests` (test_db ×5, test_customer_security ×2 — the 032-downgrade-guard test keeps its own match, test_character_domain ×4, test_recharge_orders ×2, test_postgres_migrations ×4 + downgrade-chain comments now "eleven steps", test_internal_billing, test_activation_code_schema + leg comment, test_settings, test_sqlite_to_postgres, test_characters); `test_customer_devices`' own two 033 references (downgrade-guard match + head assertion) were renumbered in place. `device_pairing_requests` was already registered in `reconcile_customer_billing.py`'s `PG_ONLY_TABLES` at T13 — no reconcile change.

## 2. The Service Layer (`server/app/customer_device_service.py`)

1. **`fingerprint_digests_for(value)`** — the digest ladder across *every* configured device-domain key version plus the highest version (the PR #44 rotation-window rule); no configured version raises `ActivationKeyError` (→ 503, never a silent misclassification).
2. **`lookup_active_pairing`** — `SELECT … FOR UPDATE` on the active (PENDING/APPROVED) row for the candidate digest *probing every key version*; a row past `expires_at` is flipped to `EXPIRED` in the same transaction (lazy, terminal) and reported absent.
3. **`create_pairing_request`** — inserts the PENDING row with `expires_at = now + 900 s` (`PAIRING_TTL_SECONDS`); a concurrent duplicate raises `UniqueViolation` for the route's race-lost lane.
4. **`approve_pairing_request`** — `FOR UPDATE` then the state machine: missing or cross-code → `not_found` (identical answers, no IDOR oracle); `CONSUMED` → `already_consumed`; lapsed (or already `EXPIRED`) → `expired` (a still-PENDING lapsed row flips here); `APPROVED` → `already_approved`; a pairing naming the approver's own fingerprint → `self_approval` (defensive depth — the enroll path structurally prevents it by refusing bound fingerprints); otherwise PENDING→APPROVED with `approved_at` + `approved_by_device_id`.
5. **`consume_pairing_request`** — under the caller's code-row lock: `next_free_slot` (None → the caller answers 409 and the pairing stays APPROVED — a freed slot may yet consume it inside the expiry window); otherwise a fresh `customer_devices` BOUND row (the candidate digest as `fingerprint_hmac` plus the caller's lowest-retained-key digest as `fingerprint_canonical` — the revision-034 cross-version probe key, the activation-route M2 precedent; a `secrets.token_urlsafe(32)` credential digested before storage) plus the `CONSUMED` flip with `consumed_at` + `consumed_device_id`.

## 3. The Routes (`server/app/customer_device_routes.py`)

**`POST /api/customer/devices/enroll`** — one endpoint, two answers, driven by the pairing state (§12.2 steps 1/2/4/6):

- Mandatory `Idempotency-Key` (400 `IDEMPOTENCY_KEY_REQUIRED`); the PENDING 202 carries no secret, so the pairing row itself is the retry-stable identity and *nothing is sealed* — the envelope placeholder is only ever inserted on the consumption branch (a rolled-back consumption takes the placeholder with it, keeping the key reusable).
- Malformed codes do not short-circuit: the request still passes the shared limiter, then answers the unified 400 `PAIRING_UNAVAILABLE` with the constant anti-enumeration delay and an audited failure event (the T15/ACT-08 pattern — the enroll accepts a plaintext code exactly like the activation route and must not become a second enumeration surface; it draws from the same `activate:ip` / `activate:code` budgets, a blocked IP mints no code-dimension row).
- Read-only replay probe before the limiter (the T15 review P2 rule — a lost-201 retry spends no budget); strict replay judgement inside the transaction (same key + different body → 409 `IDEMPOTENCY_CONFLICT`, lapsed/purged envelope → 409, unopenable ciphertext → 503).
- Inside the transaction: `SELECT now()` (SES-01), the code row locked `FOR UPDATE` (unknown / ISSUED / SUSPENDED / REVOKED / expired-batch → the unified 400), a bound candidate fingerprint → 409 `USER_ALREADY_ACTIVATED` (checked across every key version plus the revision-034 canonical probe key), then the state machine — PENDING → 202; APPROVED → seal + consume + 201 `{device_id, slot_no, device_token}` (scope = the candidate digest, probed across versions for the rotation window; the same-key concurrent loser replays from the winner's envelope); no active row → the fail-fast slots check (409 `DEVICE_SLOTS_FULL`) and the PENDING insert, whose `UniqueViolation` raises `_PairingRaceLost` — re-read the winner's row in a fresh transaction, answer its 202 (a vanished winner answers 409 `PAIRING_CONFLICT`). A defensive outer `UniqueViolation` handler maps `uq_customer_devices_fingerprint` and `uq_customer_devices_fingerprint_canonical` to 409 (the T13 / activation-route M2 precedent).
- Keys unavailable → 503 `DEVICE_SERVICE_UNAVAILABLE` (fail-closed, never a misleading 401).

**`POST /api/customer/device-pairings/{pairing_id}/approve`** — Bearer first-device credential (the shared `_authenticate`), `SELECT now()`, the outcome mapping (404 `PAIRING_NOT_FOUND` for missing and cross-code alike, 409 `PAIRING_EXPIRED` / `PAIRING_ALREADY_CONSUMED` / `PAIRING_SELF_APPROVAL`, otherwise 200 `{pairing_request_id, status: APPROVED}`). **No envelope by design**: the state machine is the idempotency (re-approving answers the current state) and no secret ever rides the response (§12.2 step 6 — the enroll's one-time credential is the sealed surface).

## 4. Test Coverage (30 red → green, T17 half of the 52)

| Group | Cases |
| --- | --- |
| Enroll contract (7) | missing Idempotency-Key → 400; PENDING created bound to the candidate digest (V2-keyed, shape-coupled columns empty); same-key retry returns the same pairing id with **zero envelopes**; unknown code → unified 400 `PAIRING_UNAVAILABLE`; ISSUED (never activated) code → the same unified 400; already-bound fingerprint → 409 `USER_ALREADY_ACTIVATED`; both slots BOUND → 409 `DEVICE_SLOTS_FULL` with no pairing row created |
| Full six-step flow (3) | enroll → approve → enroll again answers 201 with slot-2 credentials; the pairing row CONSUMED with `consumed_at`/`consumed_device_id`, the device row BOUND on slot 2 with the candidate's name/platform and the owner's user_id; **no second charge** — 1 recharge order, 1 wallet transaction, 1 customer user; the lost-201 retry replays the sealed credentials (`X-Idempotent-Replay`, one BOUND row, one CONSUMED row — zero re-execution); the same key against a different body → 409 `IDEMPOTENCY_CONFLICT` |
| Concurrency & expiry (5) | two approved rivals race the consume branch (2-thread barrier): exactly one 201 + one 409 `DEVICE_SLOTS_FULL`, one CONSUMED row, two BOUND devices; a lapsed PENDING flips EXPIRED and a fresh request is created; a lapsed APPROVED also restarts fresh (the lapsed approval stays visible in the audit); consumption against two BOUND slots answers 409 and the pairing row **stays APPROVED** |
| Approve contract (7) | missing Bearer → 401 `DEVICE_CREDENTIAL_REQUIRED`; happy path records `approved_at` + `approved_by_device_id` (the approver's device id); missing / random / **cross-code** pairings all answer one 404 (IDOR — the foreign pairing stays PENDING); repeated approval is idempotent (identical body); after consumption → 409 `PAIRING_ALREADY_CONSUMED`; lapsed → 409 `PAIRING_EXPIRED` with the row flipped EXPIRED; a pairing naming the approver's own digest → 409 `PAIRING_SELF_APPROVAL` |
| Transferability (1) | a different fingerprint enrolling the same code gets its own PENDING pairing — the approved row stays APPROVED untouched and no second device row appears |
| Codex review regression locks (6, PR #49) | mid-flight unbind: a stale authenticated device object fed to the service layer answers ``revoked`` with the pairing untouched (P1); rotation between 202 and 201: the stored (digest, version) pair stays (V1 digest, version 1) (P2); a lapsed APPROVED flips EXPIRED on the approve path and a fresh request + approval succeeds (P2); the shape CHECK rejects a PENDING-with-approved_at and an EXPIRED-with-lone-approved_at (P2); the partial unique blocks a second active row (PENDING/APPROVED) while EXPIRED/CONSUMED rows free the slot for reuse (P2); the 037 downgrade refuses once any pairing row exists and downgrades symmetrically once emptied (P2) |
| Strengthened: slots-full round trip | the 409 leaves **zero** device_envelopes, and after the unbind route frees slot 2 the very same key finishes the consumption 201 (P2) |
| GitHub review locks (2, PR #49 connector P1) | while the first device is bound the slot-2 device cannot approve (403 `PAIRING_APPROVER_FORBIDDEN`, the pairing stays PENDING), the first device's own approval works, and the authorization precedes the state machine (an APPROVED pairing is still not re-approvable by slot 2); after the first device is unbound, the surviving slot-2 device is still refused — the lane is the T18 administrator verification |

## Files Changed

| File | Change |
| --- | --- |
| `server/migrations/versions/037_device_pairing_requests.py` | new: the pairing table, four-state shape coupling, partial unique active index, downgrade guard, PG-only early return (renumbered from 033 after the origin/main merge — revises 036_low_review_constraint_guards) |
| `server/app/customer_device_service.py` | T17 section: `fingerprint_digests_for`, `ActivePairing`/`ConsumedPairing`, `lookup_active_pairing` (lazy expiry, cross-version probe), `create_pairing_request`, `approve_pairing_request`, `consume_pairing_request` (writes the revision-034 `fingerprint_canonical` probe key) |
| `server/app/customer_device_routes.py` | T17 section: the enroll route (two-phase 202/201, shared limiter, unified code rejection + audit + delay, read-only replay probe, `_PairingRaceLost` lane, defensive UniqueViolation mapping) and the approve route (Bearer auth, outcome mapping, no envelope by design) |
| `server/tests/test_customer_devices.py` | 30 fail-first T17 cases (52 total with T16's 22), TRUNCATE extended with `device_pairing_requests`, the two in-file 033 references renumbered to 037 |
| 10 test files (head sweep) | 22 head assertions `036` → `037` (post-merge renumber sweep, + downgrade-chain comment updates): test_db, test_customer_security, test_character_domain, test_recharge_orders, test_postgres_migrations, test_internal_billing, test_activation_code_schema, test_settings, test_sqlite_to_postgres, test_characters; test_admin_activation_routes TRUNCATE keeps `device_pairing_requests` (037's FK chain) under the 036 replica-role guard |

## Regression

```text
# PostgreSQL fixture up (docker customer-v3-pg-test, PG16 :5433)
$ uv run python -m pytest tests/test_customer_devices.py -q
52 passed   # 22 T16 + 30 T17
$ uv run pytest tests -q
870 passed   # full suite, zero regression (T16 baseline 840 + 30 new)
$ uv run ruff check . && uv run ruff format --check . && uv run mypy app
all green (149 files formatted, 61 source files typed)
# client / tauri / e2e untouched by this task; CI gates re-verify
# secret scan: clean (test keys are runtime-generated secrets.token_* fixtures, T13–T16 precedent)

# Post-merge re-verification (origin/main merged: PR #48's 033–036 chain landed
# first → migration renumbered 033→037, revision 034's canonical key adopted)
$ uv run python -m pytest tests -q
910 passed   # full suite incl. PR #48's ten new tests, zero regression
$ uv run ruff check . && uv run ruff format --check . && uv run mypy app
all green (155 files formatted, 61 source files typed)
# npm run check: frontend 324 tests + biome green; cargo fmt --check + cargo check green
```

## Session Code-Review Notes

Self-review during implementation (pre-subagent):

1. **The PENDING 202 must not burn the idempotency key.** Sealing an envelope on the PENDING answer would have made the key unspendable on the later consume retry (same key, structurally required — the client cannot know the pairing got approved in between). The two-phase design seals *only* the consumption 201; the PENDING answer's retry identity is the pairing row itself, so a key that saw only 202s flows naturally into the consuming 201.
2. **Unreferenced imports after route assembly** — the approve route uses only five of the seven APPROVE_* outcome constants (the already-approved and approved lanes share one answer); the unused two were dropped from the import block rather than kept "for completeness" (F401).
3. **A test indexing slip** — `_pairing_row` returns seven columns and the consumed-pairing assertion read `row[5]` (consumed_at) where it meant `row[6]` (consumed_device_id); caught red in the first green run, fixed to assert both.
4. **Slots-full at consumption keeps the pairing APPROVED.** The natural implementation flipped the row CONSUMED-then-checked; the contract's reading is the opposite — an unbind inside the expiry window must still let the approved candidate consume. The rolled-back transaction also takes the envelope placeholder with it, so the failed consume never spends the key.

## Codex Review Fixes (PR #49, REQUEST CHANGES → resolved)

Independent Codex review of the branch diff returned 1 P1 + 6 P2 + 1 P3; every finding was substantively fixed with a regression lock:

1. **P1 — a revoked device could win an approval race.** The route's `_authenticate` snapshot is unlocked, so a concurrent unbind committing between authentication and the state transition let a released credential approve. Fix: `approve_pairing_request` now re-locks the approver row (`SELECT … FOR UPDATE`) and re-validates it as `BOUND` *before* touching the pairing row (lock order devices → pairing, the tail of the enroll route's code → devices → pairing — the two routes cannot deadlock); the new `APPROVE_REVOKED` outcome answers the same 401 `DEVICE_REVOKED` a fresh request would get. Locked by the stale-approver service test.
2. **P2 — key rotation could mislabel the stored fingerprint digest.** Consumption copied the pairing row's digest (keyed under V1 at creation) but stamped the current highest version. Fix: `ActivePairing` now carries `candidate_fingerprint_key_version` end-to-end and the device row records the truthful (digest, version) pair; the fresh token stays keyed with the current highest version (`token_key_version`). Locked by the rotation-between-202-and-201 test.
3. **P2 — expired APPROVED rows never flipped through approve.** The expiry branch updated only PENDING rows, so a dead APPROVED kept occupying the partial-unique active index until some later enroll touched it. Fix: the flip now covers both lapsed PENDING and lapsed APPROVED (the approval lineage stays visible in the audit). Locked by the lapsed-approved approve test.
4. **P2 — the four-state CHECK permitted malformed EXPIRED audit rows.** The EXPIRED arm constrained only the consumption columns, admitting a lone `approved_at` or `approved_by_device_id`. Fix: the arm now requires the approval columns to arrive as a pair (both NULL or both set). Locked by the shape CHECK test.
5. **P2 — consume did not lock the current device rows.** `next_free_slot` ran on a plain SELECT, diverging from §12.2 step 4 and risking a transient 409 while an unbind was completing. Fix: the enroll route locks the code's BOUND device rows (`FOR UPDATE`) after the code-row lock and before the pairing row. Locked by the strengthened slots-full round-trip test (409 → unbind → same-key 201).
6. **P2 — the slots-full test did not prove the envelope-placeholder rollback.** It now asserts zero surviving `device_enroll` envelopes after the 409 and that the same key finishes the consumption once a slot is freed.
7. **P2 — migration 033's invariants lacked direct tests.** Added: shape CHECK violations (half-written approval columns), partial-unique active blocking + terminal (EXPIRED/CONSUMED) reuse, and the downgrade guard (refuses with rows, symmetric once emptied, restores head).
8. **P3 — `_PairingRaceLost` reported a stale PENDING.** The lane stays as defensive depth (documented unreachable under the code-row lock), but the re-read's 202 now mirrors the winner's actual state (a 202 carrying status=APPROVED tells the client to retry and land on the consumption branch).
9. **P1 (GitHub connector) — the approval lane was not restricted to the first device.** The outcome ladder accepted any bound device of the activation code, so once slot 1 was unbound while slot 2 stayed bound, the surviving slot-2 device could approve a new candidate — bypassing §12.2 step 3, which grants the lane to the *first* currently-bound device and routes the unavailable-first-device case to the T18 administrator verification. Fix: `approve_pairing_request` validates the approver against `activation_code_activations.first_device_id` (written once at activation, never rewritten — the unlocked read is race-free; a missing/NULL fact row fails closed) *before* the state machine, so a non-first device cannot even re-approve an APPROVED pairing; the new `APPROVE_FORBIDDEN` outcome answers 403 `PAIRING_APPROVER_FORBIDDEN` (the caller is a legitimate device of this very code, so the refusal names the lane rather than pretending the pairing does not exist). Locked by both GitHub review-lock tests.

## Section 14 Ledger Record

```text
任务/工作包：T17 / DEV-02
Owner / Reviewer：后端（Agent 执行）/ Codex 独立评审（1 P1+6 P2+1 P3 逐条实质修复）+ GitHub connector 评审（P1 批准权未限定首设备，已修复）
分支 / 基线 SHA：feat/customer-v3-t17-second-device-pairing / 基线 e30ea64（main，PR #48 合入后 T17 分支 merge origin/main：PR #48 的 033–036 链先落地，T17 迁移重编号 033→037，并采纳 034 canonical 探测键）
上游规格段落：客户版任务清单 V3 §4 T17、§12.3 DEV-02；代码开发清单 V3 §3.2 customer_device_service.py/customer_device_routes.py、§3.3 test_customer_devices.py 冻结名、迁移主题 037（原 033，PR #48 落地 033–036 链后重编号）；激活码开发文档 §12.2 六步契约、§6.1 API 表、§13.2 错误码；测试与验收规格 §6
改动文件：server/migrations/versions/037_device_pairing_requests.py（新增：四态状态机+形状耦合+partial unique active 索引+downgrade 守卫，PG-only，原 033 重编号）、server/app/customer_device_service.py（T17 小节：fingerprint_digests_for/lookup_active_pairing/create_pairing_request/approve_pairing_request/consume_pairing_request 含 fingerprint_canonical）、server/app/customer_device_routes.py（enroll 两阶段 202/201 路由+approve 路由约 640 行，enroll 探测含 canonical、UniqueViolation 双约束名映射）、server/tests/test_customer_devices.py（+30 用例）、10 个测试文件 22 处 head 断言 036→037（合并 main 后重扫）、docs/evidence/T17-EVIDENCE.md、任务与证据账本
失败测试或回归锁定：先红后绿 30 例——enroll 契约 7（幂等键必需/PENDING 创建绑定候选摘要/同键重试同 pairing_id 零信封/未知码统一 400/未激活码统一 400/已绑指纹 409/两槽满 409）+完整六步流 3（slot2 绑定+无充值计数锁定/丢 201 密封凭据重放/同键异参 409）+并发与过期 5（双线程 barrier slot2 单赢家 1×201+1×409+单 CONSUMED+双 BOUND/过期 PENDING 翻转新建/APPROVED 过期重启留审计/消费时槽满保持 APPROVED）+approve 契约 7（Bearer 必需/批准人 lineage/缺失跨码统一 404 IDOR/重复幂等/已消费 409/过期 409/self-approval 409）+不可转用 1（异指纹得自身 PENDING，批准行不动，无新设备行）+Codex 评审回归锁定 6（中途解绑 stale approver→revoked/202-201 间轮换存储真实 (digest,version) 对/过期 APPROVED 经 approve 翻转释放占用/形状 CHECK 拒半写批准列/partial unique 活跃阻塞+终态复用/037 downgrade 非空守卫+空库对称降级）+GitHub connector 评审锁定 2（首设备存活时 slot-2 不能批准 403+授权先于状态机，首设备解绑后 slot-2 仍被拒——T18 管理员核验是唯一通道）
实现结果：第二设备 enroll/第一设备批准/一次性配对落地（迁移 033+应用层）：配对行绑定候选指纹 keyed digest（明文永不过库，partial unique (code,digest) WHERE active 保单活跃行）；四态状态机 PENDING/APPROVED/CONSUMED/EXPIRED 形状耦合 CHECK；enroll 单路由状态驱动两阶段（PENDING→202 无信封不烧键，APPROVED→消费分支密封 201 一次性凭据）；消费持码行锁+next_free_slot 选空槽，两槽满 409 配对行保持 APPROVED（解绑后过期窗内仍可消费）；并发 slot2 恰一成功（码行锁串行化+_PairingRaceLost 输家重读赢家行）；approve Bearer 第一设备鉴权状态机即幂等（无 secret 无信封）；批准不可转用（绑定摘要+异指纹新 PENDING）；第二设备零充值（配对表无钱列+全流计数锁定）；防枚举复用 T15 维度预算（activate:ip/code 同池，malformed 不短路统一拒绝+审计+常数时延，IP blocked 不消费 code 维度）；只读回放预检免限流预算
验证命令与通过数：专项 52 passed（T16 22+T17 30）；全量 870 passed 零回归；ruff/format/mypy 全绿（149 files formatted，61 source files typed）；head 断言迁移专项 135+69 复验通过（test_db/test_internal_billing/test_recharge_orders/test_settings/test_characters/test_character_domain + test_postgres_migrations/test_activation_code_schema/test_customer_security/test_sqlite_to_postgres）；首轮全量暴露 033 FK 连锁：test_admin_activation_routes.py TRUNCATE 补 device_pairing_requests 后 38 errors→38 passed；Codex 独立评审（REQUEST CHANGES：1 P1+6 P2+1 P3）逐条实质修复+回归锁定后专项 50/全量 868 复验；GitHub connector 评审 P1（批准权未限定首设备）修复+2 例回归锁定后专项 52/全量 870 复验；合并 main（PR #48 落地 033–036 链，迁移重编号 033→037+采纳 034 canonical）后全量复验 910 passed 零回归+ruff/format/mypy 全绿（155/61）+npm check 前端 324 tests/biome 绿+cargo fmt/check 绿
证据层级：AUTOMATED_VERIFIED
安全与可观测性：候选指纹只以 keyed HMAC-SHA256 摘要过库（跨版本探测）；配对/批准/消费事件日志仅含标识符；缺失=跨码统一 404（无 IDOR 预言）；enroll 与 activate 共享防枚举面（统一 400+失败审计+告警阈值+常数 PBKDF2 时延）；一次性凭据 secrets.token_urlsafe(32)+keyed digest 存储，AEAD 信封密封（AAD 绑定 operation/scope/key_digest）；密钥配置故障 503 fail-closed；SLOT 租约与配对过期共用 PG 事务内时钟（SES-01）
迁移与回滚：037 PG-only（SQLite early return，025-036 先例）；downgrade 非空守卫（配对行是批准 lineage 审计证据，027/028/032 先例）空库对称降级；回滚=降级 037+还原代码
外部授权记录：无；未调用真实 ZPay/COS/付费 Provider/对外发码/灰度/公网发布
未测试项：管理员核验批准通道（T18 经同一状态机写穿）；login/租约维度限流（T19）；客户端 OpenAPI 重新生成（T28 前端门禁）；approve-vs-unbind 双线程并发证明（FOR UPDATE 重新验证语义覆盖）；STAGING/REAL_CHAIN/PRODUCTION
Lore 提交 SHA：见 PR squash 合并 SHA
```
