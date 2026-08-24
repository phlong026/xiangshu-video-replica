# T18 — Administrator Verified Approval, Unbind & Credential Revocation (DEV-03)

## Task Information

| Field | Value |
| --- | --- |
| **Task ID** | T18 / DEV-03 |
| **Owner** | Backend/Management (Agent) |
| **Reviewer** | independent code-review subagent (APPROVE: 0 P1 / 0 P2 / 2 P3, both substantively fixed with the re-verified 64-test run) + GitHub connector review on PR #50 (P2: the verification view must include the delivery records — fixed with a regression test, 65 re-verified) |
| **Branch / Base SHA** | `feat/customer-v3-t18-admin-verified-unbind-revoke` / base `ed65a03` (main, PR #49 squash) |
| **Date** | 2026-08-23 |
| **Evidence Level** | `AUTOMATED_VERIFIED` (real PostgreSQL 16 fixture; red → green) |

## Exit-Gate Verification

Task exit gate: *实现管理员核验批准、解绑和凭据撤销*；验收栏 *真实 actor、原因、二次确认和审计存在* (task list §4 T18) + DEV-03 gate: *真实 actor、reason、二次确认、审计和凭据失效* — delivered as revision `038_admin_device_operations` (the admin lineage column on the frozen 037 pairing table + the append-only `admin_device_events` audit table) plus the frozen new module `server/app/admin_device_routes.py` (code checklist §3.2) and the T18 extension of `server/app/customer_device_service.py`, with 13 fail-first tests appended to `server/tests/test_customer_devices.py` (frozen name, §3.3; 65 total with T16's 22 + T17's 30).

- **Real actor** — every write runs behind the T09 admin session / CSRF / RBAC gate (`AdminWriter`: admin cookie + `X-Admin-CSRF` + `role=admin`; an auditor answers 403 `AUDITOR_READ_ONLY`), and the `admin_user_id` FK lands in the audit row from the authenticated `AdminActor` — never from the request body (dev doc §15).
- **Reason + second confirmation** — the shared T12 `AdminWriteContract` carries `confirm: bool` + `reason: str`: `confirm=false` (or a missing/blank reason, missing `Idempotency-Key`) answers 400 before any transaction opens; the reason text is persisted in the audit row.
- **Audit exists** — every successful mutation lands exactly one append-only `admin_device_events` row (revision 038: event/admin/target/device-or-pairing/code/reason/request-id/created-at; the 029 append-only UPDATE/DELETE trigger + the 036 shared TRUNCATE guard); the downgrade refuses once any audit row exists.
- **Credential invalidation** — the admin unbind reuses the T16 `_revoke_session_riding_device` core (epoch bump, lease pulled into the past, `LOGOUT` event) with the administrator as the acting user; the revocation lane writes the terminal `REVOKED` state (the 028 shape).

## 1. Migration `038_admin_device_operations` (new, PG-only)

`device_pairing_requests.approved_by_admin_user_id` (FK `users.id`, nullable) + the new `admin_device_events` table.

- **Approval lineage shape coupling** — the 037 four-state CHECK is regenerated as the `_APPROVAL_LINEAGE` form: `approved_at` non-null proves exactly one approver (`(approved_by_device_id IS NULL) != (approved_by_admin_user_id IS NULL)`), so a device approval and an admin approval are mutually exclusive and both can never be claimed by one row.
- **`admin_device_events`** — `event IN ('PAIRING_ADMIN_APPROVED','DEVICE_ADMIN_UNBOUND','DEVICE_CREDENTIAL_REVOKED')`, `admin_user_id`/`target_user_id` FK `users`, `device_id` FK `customer_devices`, `pairing_request_id` FK `device_pairing_requests`, `activation_code_id` NOT NULL, `reason`/`request_id` non-blank CHECKs, `created_at`.
- **Event/target shape** — `ck_admin_device_events_target_shape`: an approval event proves `pairing_request_id` and no device; an unbind/revocation event proves `device_id` and no pairing (the 028 three-state precedent applied to the audit trail).
- **Append-only + TRUNCATE guard** — the 029 no-UPDATE/no-DELETE trigger function and the 036 shared `refuse_truncate_of_audit_tables()` BEFORE-TRUNCATE trigger are both attached.
- **Downgrade guard** — refuses with `RuntimeError` once `admin_device_events` has any row *or* any pairing carries an admin approval (the operator lineage must survive any rollback); an unused schema downgrades symmetrically. SQLite stays on the internal P0 lane (early return, the 025–037 precedent).

## 2. The Service Layer (`server/app/customer_device_service.py`, T18 section)

1. **`_revoke_session_riding_device`** — extracted from the T16 unbind tail (identical SQL): the epoch bump + past lease + `LOGOUT` event, now parameterized by the acting user and the reason so the admin lanes record the administrator as the actor.
2. **`_insert_admin_device_event`** — one INSERT into the audit table (nine columns); called from each mutation inside its transaction.
3. **`admin_approve_pairing_request`** — the §12.2 step 3 fallback lane: pairing header read unlocked for its immutable `code_id` → the activation fact read for `user_id` + `first_device_id` → **the first device row locked `FOR UPDATE`**: still `BOUND` → `first_device_available` (the admin must not shortcut a live first device, checked before the state machine exactly like the T17 `revoked` gate); a missing row counts as unavailable (the recovery lane) → the pairing row locked `FOR UPDATE` → the T17 state machine replays (`not_found` / `already_consumed` / `expired` with the lazy flip / `already_approved`) → PENDING → APPROVED with `approved_at` + `approved_by_admin_user_id` + the `PAIRING_ADMIN_APPROVED` audit row. Lock order devices → pairing (the tail of the enroll route's code → devices → pairing order).
4. **`admin_unbind_device`** — lock the device row `FOR UPDATE` → missing → `not_found`, not `BOUND` → `not_bound` → `UNBOUND` + `unbound_at` → the shared session revocation (actor = the administrator, reason = the operator's text) → the `DEVICE_ADMIN_UNBOUND` audit row.
5. **`revoke_device_credential`** — the same structure writing the terminal `REVOKED` + `revoked_at` (the 028 shape: REVOKED proves `revoked_at` and `unbound_at IS NULL`) → the shared session revocation → the `DEVICE_CREDENTIAL_REVOKED` audit row.

## 3. The Routes (`server/app/admin_device_routes.py`, frozen name)

Prefix `/api/control`, behind the T09 admin gate: reads are `AdminReader` (auditor-accessible), writes are `AdminWriter` (admin-only).

- **`GET /api/control/devices`** — the dev doc §6.2 device list: filters `status`/`activation_code_id`/`user_id`, bounded pagination; display metadata and states only (fingerprints and token digests never leave the store).
- **`GET /api/control/device-pairings/{pairing_id}`** — the verification view an operator reads before approving: the pairing row, the code (masked), **the delivery records** (`activation_code_deliveries`: channel / external order / recipient / delivered-by / delivered-at — the §12.2 step-3 issuance evidence, PR #50 connector review P2), the activation fact and the first device's status, plus the `admin_lane` summary (`OPEN` / `CLOSED_FIRST_DEVICE_BOUND` / `CLOSED_NO_ACTIVATION` — a missing activation fact is a different closure reason than a live first device; the write path answers 404 fail-closed there) — a snapshot for the operator's eyes; the write path re-validates under lock.
- **`POST /api/control/device-pairings/{pairing_id}/approve`** — the fallback approval: 404 `PAIRING_NOT_FOUND`, 403 `PAIRING_FIRST_DEVICE_AVAILABLE` (a live first device owns the approval), 409 `PAIRING_ALREADY_CONSUMED`, 409 `PAIRING_EXPIRED`, otherwise 200 `{pairing_id, status: APPROVED, outcome}`.
- **`POST /api/control/devices/{device_id}/unbind`** / **`POST /api/control/devices/{device_id}/revoke-credential`** — 404 `DEVICE_NOT_FOUND`, 409 `DEVICE_ALREADY_RELEASED`, otherwise 200 `{device_id, status, outcome}`.
- **Every write** — the shared T12 admin write contract (Idempotency-Key required, `confirm=true`, non-blank reason; 400 contract violations before any transaction) behind `_write_with_idempotency` with the device lane's own 503 fail-closed code `DEVICE_SERVICE_UNAVAILABLE` (the newly parameterized `unavailable_code` — §13.2 keeps one code per domain); `X-Request-Id` echoed; the snapshot layer replays or 409-conflicts by request hash.
- **The deferred 409 for the lazy EXPIRED flip** — `DeferredHTTPWriteError` (new, `admin_activation_routes.py`): the expired outcome *writes* (the lazy PENDING/APPROVED → EXPIRED flip, the T17 customer-lane semantic) before answering 409; a plain `HTTPException` inside the business callback would roll the transaction back and drop the flip. The deferred error snapshots the 409 response, commits the flip and re-raises *after* the commit — the idempotent replay of the same key returns the same 409. Branches with no side effects (404/403/already-consumed/not-bound) keep raising `HTTPException` directly (rollback, the key stays free for a retry — the T12 precedent).

## 4. Test Coverage (13 red → green, T18 half of the 65)

| Group | Cases |
| --- | --- |
| Verification view (1) | the §12.2 step-3 evidence bundle: pairing + masked code + the delivery records (channel / external order / recipient / delivered-by) + activation fact + first-device status + `admin_lane=CLOSED_FIRST_DEVICE_BOUND` while the first device stays bound; unknown pairing → 404 (PR #50 connector review P2 regression lock) |
| Gate & write contract (4) | no admin cookie → 401; auditor (read-only role) → 403 `AUDITOR_READ_ONLY` even on a valid session; missing Idempotency-Key → 400; `confirm=false` or blank reason → 400 — no audit row, no state change |
| Verified approval (3) | while the first device is BOUND → 403 `PAIRING_FIRST_DEVICE_AVAILABLE` (the pairing stays PENDING, zero audit rows); after the first device is unbound → 200, the pairing APPROVED with `approved_by_admin_user_id` + one `PAIRING_ADMIN_APPROVED` audit row (admin actor, target user, reason, request id) and the candidate can then consume it (the full §12.2 step-3 recovery); repeated approval → 200 idempotent (no second audit row) |
| Admin unbind (2) | releases the device (`UNBOUND` + `unbound_at`, slot freed) and revokes the riding session with the administrator as the `LOGOUT` actor (epoch +1, lease in the past) + one `DEVICE_ADMIN_UNBOUND` audit row; a second unbind → 409 `DEVICE_ALREADY_RELEASED`, unknown device → 404 |
| Credential revocation (2) | writes the terminal `REVOKED` + `revoked_at` (the 028 shape: `unbound_at` stays NULL) + the riding-session revocation + one `DEVICE_CREDENTIAL_REVOKED` audit row; the released credential then answers 401 `DEVICE_REVOKED` on the customer lane (the client-side wipe signal) |
| Idempotency (1) | the same key replays the unbind response (`X-Idempotent-Replay: true`, one audit row, no double state change); a different body with the same key → 409 `IDEMPOTENCY_CONFLICT` |
| Migration invariants (2) | the `ck_admin_device_events_target_shape` rejects an unbound event without a device (and an approval event without a pairing); the 038 downgrade refuses once an audit row exists (version stays at head through the single-transaction chain) and downgrades symmetrically once emptied (both tables truncated together behind the replica role — the 038 FK pairs them) |

Plus the T17 regression lock updated for the new head: `test_pairing_downgrade_refuses_once_rows_exist` asserts version `038_admin_device_operations` after its refusal and truncates `admin_device_events, device_pairing_requests` together (the new FK forbids truncating the pairing table alone).

## Files Changed

| File | Change |
| --- | --- |
| `server/migrations/versions/038_admin_device_operations.py` | new: `approved_by_admin_user_id` + the `_APPROVAL_LINEAGE` regeneration of the 037 status shape, `admin_device_events` with the event/target shape CHECK, append-only + TRUNCATE guard triggers, downgrade guard, PG-only early return (revises 037) |
| `server/app/customer_device_service.py` | T18 section: `_revoke_session_riding_device` (extracted from the T16 unbind tail), `_insert_admin_device_event`, `admin_approve_pairing_request`, `admin_unbind_device`, `revoke_device_credential` + the T18 outcome/event constants |
| `server/app/admin_device_routes.py` | new frozen module: the device list, the pairing verification view, the approve/unbind/revoke-credential writes behind the admin write contract and the idempotency snapshot layer |
| `server/app/admin_activation_routes.py` | `DeferredHTTPWriteError` + `_write_with_idempotency` learns it (snapshot the error, commit the side effects, re-raise after the commit) and the `unavailable_code`/`unavailable_message` parameters (T12 call sites unchanged — activation defaults) |
| `server/app/main.py` | mount `admin_device_router` (+2 lines) |
| `server/scripts/reconcile_customer_billing.py` | `PG_ONLY_TABLES` registers `admin_device_events` (T18/DEV-03): the T07 import source has no SQLite counterpart, an empty target row is expected — a non-empty one still fails closed |
| `server/tests/test_customer_devices.py` | 13 fail-first T18 cases (65 total), the admin-session/`_admin_write` helpers, `route_state` TRUNCATE extended with `admin_device_events`, `_insert_pairing_row` extended with `approved_by_admin_user_id`, the 037 downgrade-lock test adapted to the 038 head (paired TRUNCATE) |
| `server/tests/test_admin_activation_routes.py` | fixture TRUNCATE adds `admin_device_events` (038 FK chain: the audit table references users/customer_devices/device_pairing_requests/activation_codes) |
| 10 test files | head-assertion sweep 037→038 (22 sites: test_db 5, test_character_domain 4, test_postgres_migrations 4 — incl. the "Twelve steps" chain comments, test_customer_security 2, test_recharge_orders 2, test_settings/test_characters/test_internal_billing/test_sqlite_to_postgres/test_activation_code_schema 1 each) |

## Regression

```text
# PostgreSQL fixture up (docker customer-v3-pg-test, PG16 :5433)
$ uv run python -m pytest tests/test_customer_devices.py -q
65 passed   # 22 T16 + 30 T17 + 13 T18
$ uv run pytest tests -q
923 passed   # full suite, zero regression (T17 re-verified baseline 910 + 13 new)
$ uv run ruff check . && uv run ruff format --check . && uv run mypy app
all green (157 files formatted, 62 source files typed)
# client / tauri / e2e untouched by this task; CI gates re-verify
# secret scan: clean (test keys are runtime-generated secrets.token_* fixtures, T13–T17 precedent)
```

## Session Code-Review Notes

Self-review during implementation (pre-subagent):

1. **The lazy EXPIRED flip vs. the idempotency layer.** The first green run failed `test_admin_approve_outcomes`: the 409 for the expired pairing rolled the transaction back and dropped the T17 lazy flip (PENDING → EXPIRED). Root cause: the T17 customer route answers *outside* its `with pg_transaction()` block, so the flip commits; the T18 business callback raised `HTTPException` *inside* the idempotency layer's transaction. Fix: `DeferredHTTPWriteError` — the layer snapshots the 409, commits the flip and re-raises after the commit (the replay answers the same 409); side-effect-free branches keep the plain raise (the key stays free for a retry).
2. **The downgrade-guard test row violated the shape CHECK it was guarding.** The first insert of a `DEVICE_ADMIN_UNBOUND` audit row omitted `device_id` — exactly the half-written shape `ck_admin_device_events_target_shape` exists to reject. The test now seeds a plain-SQL device row first (inert literal digests; `route_state` sets no device-key env, so the application-layer helper would have failed on key configuration instead of the migration semantics under test).
3. **The 038 FK pairs the two tables in cleanup paths.** `TRUNCATE device_pairing_requests` alone now fails (`FeatureNotSupported` — referenced by `admin_device_events`); the 037 downgrade-lock test truncates both together behind the replica role, and `route_state` already truncated `admin_device_events` first.
4. **`_insert_pairing_row` needed the new lineage column** for the consumed-pairing fixture (an admin-approved CONSUMED row is now shape-legal: `approved_at` + `approved_by_admin_user_id` + the consumption pair).

Independent review (subagent, APPROVE 0 P1 / 0 P2 / 2 P3 — both fixed):

5. **The deferred-409 replay had code-level correctness but no regression lock.** The expired-outcome test proved the lazy flip survives the 409 but never replayed the same `Idempotency-Key`. Fixed: `test_admin_approve_outcomes` now pins the key and asserts the replay answers the same 409 with `X-Idempotent-Replay: true` and an identical body — the snapshot layer recorded the error response while committing the flip, so the replay must not re-execute.
6. **The verification view's `admin_lane` label conflated two closure reasons.** With no activation fact the lane is closed, but the label claimed `CLOSED_FIRST_DEVICE_BOUND` while the write path actually answers 404 `PAIRING_NOT_FOUND` (fail-closed). Fixed: the three-valued label adds `CLOSED_NO_ACTIVATION` (display-only; the write path re-validates under lock either way).

GitHub connector review on PR #50 (P2 — fixed):

7. **The verification view omitted the delivery records.** Dev doc §12.2 step 3 requires the admin to verify the issuance/delivery record before opening the fallback lane, but the view only returned the code row and the activation fact — the operator could not see who delivered the code, through which channel, or to which recipient. Fixed: the view now returns the full `activation_code_deliveries` evidence (channel / external_order_ref / recipient_ref / delivered_by_user_id / delivered_at, ordered chronologically; the table stores no plaintext by design) with a regression test asserting the complete §12.2 step-3 evidence bundle (65 re-verified).

## Section 14 Ledger Record

```text
任务/工作包：T18 / DEV-03
Owner / Reviewer：后端/管理（Agent 执行）/ 独立评审子代理（APPROVE：0 P1/0 P2/2 P3，均实质修复后专项 64 复验通过）+ GitHub connector 评审（PR #50 P2：核验视图须含发放记录，已修复+回归锁定，专项 65 复验通过）
分支 / 基线 SHA：feat/customer-v3-t18-admin-verified-unbind-revoke / 基线 ed65a03（main，PR #49 squash）
上游规格段落：客户版任务清单 V3 §4 T18、§12.3 DEV-03；代码开发清单 V3 §3.2 admin_device_routes.py 冻结名、§3.3 test_customer_devices.py 冻结名、迁移主题 038；激活码开发文档 §6.1/§6.2 管理端 API 表、§9.2 设备撤销原子吊销、§12.2 step 3 首设备不可用的管理员核验通道、§13.2 错误码、§15 管理端真实操作人；测试与验收规格 §2
改动文件：server/migrations/versions/038_admin_device_operations.py（新增：approved_by_admin_user_id+APPROVAL_LINEAGE 状态形状重生成+admin_device_events 审计表+事件/目标形状 CHECK+append-only+TRUNCATE guard+downgrade 守卫，PG-only）、server/app/customer_device_service.py（T18 小节：_revoke_session_riding_device 提取共享+_insert_admin_device_event+admin_approve_pairing_request/admin_unbind_device/revoke_device_credential）、server/app/admin_device_routes.py（新增冻结名：设备列表+配对核验视图+approve/unbind/revoke-credential 三写路由）、server/app/admin_activation_routes.py（DeferredHTTPWriteError+_write_with_idempotency 支持 deferred 409 提交后重抛+unavailable_code 参数化）、server/app/main.py（挂载+2 行）、server/scripts/reconcile_customer_billing.py（PG_ONLY_TABLES 注册 admin_device_events——T07 导入源无 SQLite 对应表，空表预期/非空仍 fail-closed）、server/tests/test_customer_devices.py（+13 用例，admin 会话 helper、route_state TRUNCATE 补 admin_device_events、_insert_pairing_row 补 approved_by_admin_user_id、037 downgrade 锁定测试适配 038 head）、server/tests/test_admin_activation_routes.py（fixture TRUNCATE 补 admin_device_events——038 FK 引用连锁）、10 个测试文件 22 处 head 断言 037→038（含 3 处链注释 Twelve steps）、docs/evidence/T18-EVIDENCE.md、任务与证据账本
失败测试或回归锁定：先红后绿 13 例——核验视图 1（§12.2 step-3 证据包：配对+掩码码+发放记录（channel/external_order/recipient/delivered-by）+激活事实+首设备状态+首设备存活时 admin_lane=CLOSED_FIRST_DEVICE_BOUND；未知配对 404（PR #50 connector P2 回归锁定））+门禁与写契约 4（无 cookie 401/auditor 403 只读/缺幂等键 400/confirm=false 或空 reason 400 且零审计行零状态变化）+核验批准 3（首设备存活 403 配对保持 PENDING 零审计行/首设备解绑后 200 approved_by_admin_user_id+PAIRING_ADMIN_APPROVED 审计行（actor/target/reason/request-id）+候选随后可消费/重复批准 200 幂等无第二审计行）+管理解绑 2（UNBOUND+unbound_at+骑乘会话吊销 actor=管理员 epoch+1 lease 过去+DEVICE_ADMIN_UNBOUND 审计行/二次解绑 409+未知设备 404）+凭据撤销 2（REVOKED+revoked_at 且 unbound_at 保持 NULL（028 形状）+骑乘会话吊销+DEVICE_CREDENTIAL_REVOKED 审计行/撤销后凭据在客户道 401 DEVICE_REVOKED）+幂等 1（同键重放 X-Idempotent-Replay+单审计行+无双重状态变化+deferred 409 同键重放同 409/同键异参 409）+迁移不变量 2（ck_admin_device_events_target_shape 拒无 device 的 unbound 事件/038 downgrade 有审计行拒绝版本保持 head+清空后对称降级）
实现结果：管理员核验批准/解绑/凭据撤销落地（迁移 038+应用层）：三写路由全部走 T09 admin 会话/CSRF/RBAC 门（AdminWriter role=admin，auditor 403）+T12 共享写契约（幂等键+confirm+reason，契约违规在事务开启前 400）+幂等快照层（设备域自有 503 DEVICE_SERVICE_UNAVAILABLE）；真实 actor 从认证态 AdminActor 落审计行（永不取自请求体）；每成功变更恰一条 append-only admin_device_events（029 UPDATE/DELETE 触发器+036 共享 TRUNCATE guard）；批准通道先锁 first_device_id 行 FOR UPDATE（BOUND→403 不短路存活首设备，缺失→恢复通道）再锁配对行复用 T17 状态机（approved_by_admin_user_id lineage 与设备批准互斥）；解绑/撤销复用 T16 会话吊销核心（actor 参数化=管理员）；过期 409 经 DeferredHTTPWriteError 提交后重抛（lazy 翻转保留+同键重放同 409，无副作用分支普通 raise 回滚键保持可重试）
验证命令与通过数：专项 65 passed（T16 22+T17 30+T18 13，含评审修复后新增 deferred 409 同键重放锁定+核验视图证据包锁定）；全量 923 passed 零回归（T17 重验基线 910+新增 13）；ruff/format/mypy 全绿（157 files formatted，62 source files typed）；sqlite→PG 导入对账专项 35 passed（PG_ONLY_TABLES 注册后）；npm check 全仓门禁绿；独立评审子代理 APPROVE（0 P1/0 P2/2 P3：deferred 409 同键重放回归锁定+admin_lane 三分支标签，均已修复复验）；GitHub connector 评审 P2（核验视图含发放记录 activation_code_deliveries，§12.2 step-3 发放证据完整）已修复+回归锁定复验
证据层级：AUTOMATED_VERIFIED
安全与可观测性：管理写仅 admin 角色（cookie+CSRF+写方法校验）；actor/reason/request-id 全链路入审计与日志；审计表 append-only（UPDATE/DELETE 触发器拒绝+TRUNCATE guard）；指纹与 token 摘要永不出库（列表/核验视图仅显示元数据与状态）；幂等快照层按请求哈希重放或 409；密钥配置故障 503 fail-closed；统一 404 无跨用户枚举预言
迁移与回滚：038 PG-only（SQLite early return，025-037 先例）；downgrade 有审计行或管理批准 lineage 时拒绝（操作人审计必须存活），空库对称降级；回滚=降级 038+还原代码
外部授权记录：无；未调用真实 ZPay/COS/付费 Provider/对外发码/灰度/公网发布
未测试项：管理端点未接入独立管理限流（T37/OPS-02 安全硬化统一收口）；T33 管理端页面消费这些 API（前端任务）；真实运维工单系统联动（人工流程）；STAGING/REAL_CHAIN/PRODUCTION
Lore 提交 SHA：见 PR squash 合并 SHA
```
