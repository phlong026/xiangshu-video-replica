# Customer Edition Task Evidence Record V3

> Note: This file is the evidence ledger for `docs/客户版任务清单-V3.md`; each task closure must record details per Section 14 template. The task list remains the single source of truth for status.
>
> **Evidence location (M0 review M8 unification, 2026-08-21)**: per-task evidence documents live under `docs/evidence/` (T02–T06 evidence files moved from the repository root; run-fix evidence under `docs/evidence/m0-review-fixes/`). Historical self-references inside those documents to their original root paths are preserved as record snapshots.

## T01 — Freeze V3 Main Specifications

| Field | Content |
| --- | --- |
| **Owner** | Architecture/Product |
| **Reviewer** | (N/A - spec freeze doesn't require independent reviewer) |
| **Branch / SHA** | `feat/customer-v3-t01-freeze-spec` / `7e75576aaf462b5c492d02651b4256734d2a6334` (PR #28 squash) |
| **Upstream Spec Sections** | `docs/客户版开发计划-V3.md` §1; `docs/客户版任务清单-V3.md` Header & Table T01 |
| **Files Changed** | - Update `docs/客户版任务清单-V3.md` Header status<br>- Update Task Table T01 status `[~]`→`[x]`<br>- Add `docs/客户版任务证据记录-V3.md` (this file as ENG version) |
| **Failure Test or Regression Lock** | N/A for spec freeze tasks |
| **Implementation Result** | User session confirmed V3 execution plan and boundaries; frozen downstream design dependencies on file mapping and document structure |
| **Verification Command and Pass Count** | N/A |
| **Evidence Level** | `CODE_PRESENT` (here refers to documentation freeze) |
| **Security and Observability** | N/A |
| **Migration and Rollback** | R0 preserves current internal P0 release/tag; V3 branch evolves independently |
| **External Authorization Record** | None |
| **Untested Items** | N/A |
| **Lore Commit SHA** | `7e75576aaf462b5c492d02651b4256734d2a6334` |

### Acceptance Evidence

#### Development Plan Conclusion Consistency

- Plan §1 states clearly: "This is not adding a few pages on top of the existing system. This scan identified 33 modules directly depending on `sqlite3` in the current runtime layer... Customer edition must complete PostgreSQL migration first, then build activation codes, device/session, fair queueing, and multi-instance"
- Effort model: 95–165 person-days base effort → risk-adjusted 110–185 person-days management; recommended configuration: 2 backend + 1 frontend/Tauri + 1 QA + 0.5–1 OPS
- Lane division: A(DB/billing)/B(device/auth)/C(worker/queue)/D(customer frontend)/E(security/deployment)
- Milestones M0–M6 clearly defined, especially M0/M1 exit gates constraining subsequent feature development order

#### Unique File Mapping Frozen

Per unique implementation file mappings frozen in `docs/客户版代码开发清单-V3.md` §3:

**Migration themes sequence** (cannot override existing revisions):
- `server/migrations/versions/025_postgres_runtime_compatibility.py`
- `server/migrations/versions/026_customer_security_and_billing.py`
- `server/migrations/versions/027_activation_code_catalog.py`
- `server/migrations/versions/028_customer_devices_and_activations.py`
- `server/migrations/versions/029_customer_sessions_and_idempotency.py`
- `server/migrations/versions/030_user_fair_queue.py`

**Backend business modules**:
`activation_code_service.py`, `activation_code_routes.py`, `customer_device_service.py`, `customer_device_routes.py`, `customer_session_service.py`, `customer_session_routes.py`, `customer_idempotency.py`, `customer_auth.py`, `customer_queue.py`, `security_rate_limit.py`, `admin_auth_routes.py`, `admin_activation_routes.py`, `admin_customer_routes.py`, `admin_device_routes.py`, `admin_session_routes.py`, `admin_audit_routes.py`

**Client directories**:
- `client/src/customer/*.tsx` (ActivationPage/LoginPage/DevicePairingPage/SessionConflictDialog/DeviceManagementPage/useCustomerSession.ts/customer-state.ts)
- `client/src/admin/*.tsx` (ActivationCodeBatchesPage/ActivationCodesPage/DeliveriesPage/CustomersPage/DevicesPage/SessionsPage/AuditEventsPage)
- `client/src-tauri/src/customer_credentials.rs`

**Server tests**:
`t05/postgres_migrations.py`, `test_sqlite_to_postgres.py`, and all customer-domain test files (activation/code/service/routes/devices/sessions/fencing/idempotency/recharge/queue_fairness/admin/auth/security/ha_smoke/real_chain_contracts)

#### Prohibited Parallel Execution Red Lines

Strictly enforce prohibited parallel items from Plan §5:
- ❌ T13 NOT before T08/T10 data constraints completed
- ❌ T20 switch NOT before T19 lease state machine passed  
- ❌ T21 NOT just batch dependency replacement; must verify fencing per write route
- ❌ T25 fair queue NOT SQLite-first then "migrate later"
- ❌ T36 staging NOT single API/Worker health checks pretending to be multi-instance
- ❌ T40 real payments and Provider submissions require manual authorization

#### First Batch Scope Confirmation

Per Plan §12 "Development Start Suggestion": First batch starts only T02–T06; before this batch closes, do not implement first activation business logic (T13) to avoid rework on incorrect transaction model.

---

## Evidence Maintenance Rules

1. **Status sync**: Only update task status (`[ ]/[~]/[x]/[!]`) in `docs/客户版任务清单-V3.md`
2. **Evidence registration**: Detailed evidence for each task registered in corresponding section of this file
3. **SHA recording**: Complete Lore commit SHA recorded in both task list and this file
4. **Blocking markers**: Tasks requiring external authorization/resources marked with `[!]` and documented blocking items

---

## T01 Section 14 Ledger Record

```text
任务/工作包：T01
Owner / Reviewer：架构/产品（Agent 执行）/ chatgpt-codex-connector（PR #28 评审）
分支 / 基线 SHA：feat/customer-v3-t01-freeze-spec / 基线 4f197b4
上游规格段落：docs/客户版开发计划-V3.md §1/§7；docs/客户版代码开发清单-V3.md §3
改动文件：docs/客户版任务清单-V3.md（T01 状态 [~]→[x]、Header）、docs/CUSTOMER-TASK-EVIDENCE-V3.md（本文件）、.gitignore（忽略 .worktrees/ 并行工作区）
失败测试或回归锁定：规格冻结类任务，无失败测试；回归锁定由 T02 基线承担
实现结果：用户 2026-08-20 会话确认 V3 口径；冻结六段迁移主题与唯一文件映射；账本 T01 已关闭
验证命令与通过数：N/A（纯文档）
证据层级：CODE_PRESENT（文档冻结）
安全与可观测性：N/A
迁移与回滚：R0 保留内部 P0 release/tag
外部授权记录：无
未测试项：N/A
Lore 提交 SHA：7e75576aaf462b5c492d02651b4256734d2a6334（PR #28 squash 合并）
```

---

## T02–T06 Evidence Index (M0 review M8 backfill)

Per-task evidence documents (moved to `docs/evidence/` on 2026-08-21; SHAs are
the squash-merge commits on `main`):

| Task | Squash SHA (main) | PR | Evidence document |
| --- | --- | --- | --- |
| T02 | `7b81df86dff0c1e4cb558595e63c712d4ee38979` | #29 | `docs/evidence/T02-EVIDENCE.md` (+ `docs/evidence/t02/` gate artifacts) |
| T03 | `66b520e98f107db143ce23c98ba62d676ac8ef28` | #30 | `docs/evidence/T03-EVIDENCE.md` |
| T04 | `81303219ba4326a0530571a5c3263fdf8bfb7aa5` | #31 | `docs/evidence/T04-SQLITE-INVENTORY.md` |
| T05 | `c152766bbef54e07e7db7b89804ff071c2bf82cb` | #32 | `docs/evidence/T05-EVIDENCE.md` |
| T06 | `d797e6dafaa5356db94c3d36afd12af93d7835af` | #33 | `docs/evidence/T06-EVIDENCE.md` |

M0-review remediation runs (evidence under `docs/evidence/m0-review-fixes/`):

| Run | Scope | PR |
| --- | --- | --- |
| P0 | C1 (revision 025) + H2 (CI PG service) + review P1 downgrade guard + LOW-2 | #35 |
| P1/P2 code | H1 worker exit + H3 alembic DSN + M1–M6 + M7 doc + LOW-1/3 | #36 |
| P2 docs | H4 inventory addendum + M8 evidence unification + M9 ledger correction + H1 exit-gate wording | #37 |

---

## T07 — SQLite to PostgreSQL One-shot Import and Reconciliation

| Field | Content |
| --- | --- |
| **Owner** | DB / Backend |
| **Reviewer** | chatgpt-codex-connector + independent final verification |
| **Branch / Base SHA** | `feat/customer-v3-t07-sqlite-postgres-import` / `main@35e341833e1de3096d1728c98375523d1dd46982` |
| **Verified Implementation SHA** | `c26bc0732d9fe66142dae3c50ac9c908bdf578a8` |
| **Upstream Spec Sections** | Task list §2 T07, §12.1 DB-05/DB-06; code checklist §8.3 |
| **Files Changed** | `server/app/backup.py`; `server/scripts/sqlite_to_postgres.py`; `server/scripts/reconcile_customer_billing.py`; `server/tests/test_sqlite_to_postgres.py`; T07 evidence and ledgers |
| **Failure Test or Regression Lock** | API export mismatch; WAL race; evidence overwrite; 0600 permissions; JSON asset orphans; bounded-memory digest; DSN redaction; advisory lock; atomic publication cleanup |
| **Implementation Result** | Private immutable SQLite snapshot, one-transaction PostgreSQL import, idempotent replay, full table/billing/asset reconciliation, fail-closed preconditions and R0/R1 rollback contract |
| **Verification Command and Pass Count** | Run #189: all three gates succeeded; client 324 passed; server 628 passed / 1 unrelated skip; T07 PG16 module 19 passed; ledger-finalization prerequisite Run #195 also passed all three gates |
| **Evidence Level** | `AUTOMATED_VERIFIED` |
| **Security and Observability** | No DSN secret/raw business row/storage URL/token in reports; snapshot mode 0600; failures expose only bounded summaries |
| **Migration and Rollback** | No dual write; all target writes in one PostgreSQL transaction; R0 keeps the old P0 release/tag and source DB; R1 reverts before customer traffic opens |
| **External Authorization Record** | None; no production DB, COS, ZPay, paid Provider, activation-code distribution, rollout or public release invoked |
| **Untested Items** | Real production dataset cutover, staging maintenance-window timing, real-chain and production evidence |
| **Lore Commit SHA** | PR #38 implementation head `c26bc0732d9fe66142dae3c50ac9c908bdf578a8`; final squash SHA is the GitHub merge result |

## T08 — Billing Provider / Pricing Scope Conditional Constraints

| Field | Content |
| --- | --- |
| **Owner** | Billing / DB |
| **Reviewer** | chatgpt-codex-connector |
| **Branch / Base SHA** | `feat/customer-v3-t08-billing-provider-constraints` / `main@9f60eea615ab9dee177eb0892b3789dabda196dd` |
| **Verified Implementation SHA** | PR squash merge result (see `docs/evidence/T08-EVIDENCE.md` for blob integrity hashes) |
| **Upstream Spec Sections** | Task list §2 T08, §12.1 DB-07; code checklist §3.1 (frozen migration name `026_customer_security_and_billing`); acceptance spec §7 (provider/price-scope shapes verified by PG check constraints); activation-code dev doc §12.1 |
| **Files Changed** | `server/migrations/versions/026_customer_security_and_billing.py` (new); `server/tests/test_postgres_migrations.py` (+2 tests); 7 test files' head-revision assertions; task list + evidence ledger |
| **Failure Test or Regression Lock** | 4 legal shapes accepted and 12 illegal shapes rejected by PG16 CheckViolation; downgrade guard refuses with customer rows and restores verbatim 022 shapes on an empty ledger; red-green record against the 025 head |
| **Implementation Result** | PG-only revision 026 enforces provider enum (zpay/activation_code/admin_adjustment), pricing_scope enum (INTERNAL/CUSTOMER_STANDARD), scope pairing, paid-on-creation for non-zpay, trade-number presence rules, customer price floor (charged >= base), and min/step ladders limited to zpay |
| **Verification Command and Pass Count** | `pytest tests/test_postgres_migrations.py` → 9 passed; full suite → 636 passed; ruff/format/mypy green; `npm run check` full gate green |
| **Evidence Level** | `AUTOMATED_VERIFIED` (real PostgreSQL 16 locally and in the CI Linux gate) |
| **Security and Observability** | Constraints enforced by the database layer, not application code (DB-07 No-Go); SQLite internal runtime untouched |
| **Migration and Rollback** | PG-only append-only revision (025 precedent); guarded downgrade keeps confirmed billing rows intact |
| **External Authorization Record** | None |
| **Untested Items** | Business write paths for activation_code/admin_adjustment orders (T13 activation transaction, T23 adjustment API); STAGING/REAL_CHAIN/PRODUCTION |
| **Lore Commit SHA** | PR squash merge SHA |

### T08 Section 14 Ledger Record

```text
任务/工作包：T08 / DB-07
Owner / Reviewer：Billing/DB（Agent 执行）/ chatgpt-codex-connector（PR 评审）
分支 / 基线 SHA：feat/customer-v3-t08-billing-provider-constraints / 基线 9f60eea615ab9dee177eb0892b3789dabda196dd
上游规格段落：客户版任务清单 V3 §2 T08、§12.1 DB-07；代码开发清单 V3 §3.1；测试与验收规格 V3 §7；激活码开发文档 §12.1
改动文件：server/migrations/versions/026_customer_security_and_billing.py（新增）、server/tests/test_postgres_migrations.py、7 个测试文件 head 断言、任务与证据账本
失败测试或回归锁定：先红后绿——4 组合法形状 + 12 组非法形状 PG16 CheckViolation；downgrade 守卫（有客户行拒绝降级、空账本对称回退）
实现结果：026 PG-only 迁移以 8 条 provider 条件 CHECK 约束扩展账务来源、价格域、客户价下限与 min/step 阶梯适用范围
验证命令与通过数：test_postgres_migrations 9 passed；全量 636 passed；ruff/format/mypy 全绿；npm run check 全仓门禁通过
证据层级：AUTOMATED_VERIFIED
安全与可观测性：约束全部由数据库层强制；SQLite 内部运行时零改动
迁移与回滚：PG-only、downgrade 带数据守卫，空账本对称回退并逐字恢复 022 约束
外部授权记录：无
未测试项：activation_code/admin_adjustment 业务写入路径（T13/T23）；STAGING/REAL_CHAIN/PRODUCTION
Lore 提交 SHA：见 PR squash 合并 SHA
```

### T07 Section 14 Ledger Record

```text
任务/工作包：T07 / DB-05 / DB-06
Owner / Reviewer：DB/Backend Agent / chatgpt-codex-connector + independent final verification
分支 / 基线 SHA：feat/customer-v3-t07-sqlite-postgres-import / 35e341833e1de3096d1728c98375523d1dd46982
上游规格段落：客户版任务清单 V3 §2 T07、§12.1 DB-05/DB-06；代码开发清单 V3 §8.3
改动文件：server/app/backup.py、server/scripts/sqlite_to_postgres.py、server/scripts/reconcile_customer_billing.py、server/tests/test_sqlite_to_postgres.py、docs/evidence/T07-EVIDENCE.md、任务与证据账本
失败测试或回归锁定：API 导出、WAL/sidecar、不可覆盖与 0600、JSON 资产引用、增量指纹、DSN 脱敏、advisory lock、事务回滚、发布竞态
实现结果：SQLite 只读不可覆盖快照、单事务 PG 导入、重复执行、全量对账、维护窗与 R0/R1 回滚契约完成
验证命令与通过数：Run #189 三门禁全部成功；客户端 324 passed；服务端 628 passed / 1 unrelated skip；T07 PG16 专项 19 passed；账本写入前置 Run #195 亦全绿
证据层级：AUTOMATED_VERIFIED
安全与可观测性：0600、敏感值脱敏、报告只含计数/摘要、失败 fail-closed
迁移与回滚：禁止双写；单 PG 事务；源 DB、快照与旧 P0 release/tag 保留
外部授权记录：无；未调用生产数据库、COS、ZPay、付费 Provider、发码、灰度或公网发布
未测试项：真实生产存量库切换、类生产维护窗耗时、STAGING/REAL_CHAIN/PRODUCTION
Lore 提交 SHA：PR #38 implementation head c26bc0732d9fe66142dae3c50ac9c908bdf578a8；最终 squash SHA 以 GitHub merge 结果为准
```

## T09 — Per-operator Admin Session/CSRF and Customer-Production Fail-Closed

| Field | Content |
| --- | --- |
| **Owner** | Security/Backend/OPS |
| **Reviewer** | chatgpt-codex-connector |
| **Branch / Base SHA** | `feat/customer-v3-t09-admin-session-csrf` / `main@e50f931` (PR #39 squash) |
| **Verified Implementation SHA** | PR squash merge result (see `docs/evidence/T09-EVIDENCE.md`) |
| **Upstream Spec Sections** | Task list §2 T09, §12.1 DB-08; code checklist §3.2 (frozen `admin_auth_routes.py`); activation-code dev doc §15 (`admin_sessions` from revision 026); acceptance spec §8 |
| **Files Changed** | `server/app/admin_auth_routes.py` (new, non-object-JSON guard); `server/scripts/issue_admin_exchange_credential.py` (new); `server/tests/test_admin_auth.py` (new, 39 cases incl. 3 PR-review locks); `server/app/bootstrap.py` (version-aware key discovery + min-length gate); `server/app/main.py`; `server/app/control_auth.py`; `deploy/customer.env.example` (new); `client/vite.config.ts` (Node-25 webstorage test compat); 11 stash-restored tracked files with Windows hardening |
| **Failure Test or Regression Lock** | 39 red→green cases: credential issue/verify/expiry/tamper/single-use (nonce-digest PK collision), non-object JSON bodies rejected as malformed, session whoami/logout/expiry/revocation/disable-invalidation, CSRF missing/mismatch, auditor read-only, secure cookie shape, per-violation + aggregated fail-closed gate, boot with only a rotated `_V2` key, weak key (< 32 B) rejected at boot, runtime legacy-identity 403, PG-unavailable 503 |
| **Implementation Result** | `ASX1` single-use HMAC exchange credential (versioned keys) → HttpOnly `admin_session` cookie (path `/api/control`, strict, secure in production) + per-session CSRF (`X-Admin-CSRF`); SHA-256 digests only in DB; PostgreSQL time the sole clock; AdminReader/AdminWriter RBAC; customer-production gate fails closed in both bootstrap `main()` and API `_lifespan` (legacy single-admin mapping / dev identity / local assets / missing-or-weak HMAC key — any configured `…_VN` version suffices after rotation; SQLite/DSN via T05 gate) |
| **Verification Command and Pass Count** | `pytest tests/test_admin_auth.py` → 39 passed; full suite → 677 passed (PG fixture); ruff/format/mypy green; client check (biome 54 / tsc / vitest 324), check:e2e, check:tauri, verify_no_secrets all green. PR #40 review: 3 Codex P2 findings substantively fixed (see `docs/evidence/T09-EVIDENCE.md` §3) |
| **Evidence Level** | `AUTOMATED_VERIFIED` |
| **Security and Observability** | digests-only storage; logs record exception class + actor/session ids only; placeholders-only env example; key ≥ 32 bytes with version rotation |
| **Migration and Rollback** | no new migration (reuses published 026 `admin_sessions`); internal SQLite lane behaviour unchanged |
| **External Authorization Record** | None |
| **Untested Items** | admin frontend pages (T32); multi-instance session behaviour (T36); STAGING/REAL_CHAIN/PRODUCTION |
| **Lore Commit SHA** | PR squash merge SHA |

### T09 Section 14 Ledger Record

```text
任务/工作包：T09 / DB-08
Owner / Reviewer：安全/后端/OPS（Agent 执行）/ chatgpt-codex-connector（PR 评审，3 条 P2 意见已逐条实质修复）
分支 / 基线 SHA：feat/customer-v3-t09-admin-session-csrf / 基线 e50f931（PR #39 squash）
上游规格段落：客户版任务清单 V3 §2 T09、§12.1 DB-08；代码开发清单 V3 §3.2；激活码开发文档 §15；测试与验收规格 V3 §8
改动文件：server/app/admin_auth_routes.py（新增，含非对象 JSON 防护）、server/scripts/issue_admin_exchange_credential.py（新增）、server/tests/test_admin_auth.py（新增 39 用例）、server/app/bootstrap.py（版本化密钥发现+长度校验）、server/app/main.py、server/app/control_auth.py、deploy/customer.env.example（新增）、client/vite.config.ts、11 个 stash 事故重建文件、docs/evidence/T09-EVIDENCE.md、任务与证据账本
失败测试或回归锁定：先红后绿——凭据签发/验签/过期/篡改/单次使用（nonce 摘要主键撞唯一约束）/非对象 JSON 拒收、会话全生命周期、CSRF、RBAC、cookie 形状、安全门逐项+聚合（含仅 _V2 可启动、短密钥启动即拒）、运行时 legacy 403、PG 缺失 503
实现结果：ASX1 一次性 HMAC 凭据 → HttpOnly cookie + CSRF（仅摘要入库，PG 唯一时钟）；客户生产安全门双重 fail-closed，五类启动拒绝全部落地；PR #40 评审 3 条 P2 意见逐条实质修复（密钥轮换启动、启动期强度校验、非对象 JSON 401）
验证命令与通过数：test_admin_auth 39 passed；全量 677 passed（PG fixture）；ruff/format/mypy、client check、check:e2e、check:tauri、verify_no_secrets 全绿
证据层级：AUTOMATED_VERIFIED
安全与可观测性：仅摘要入库；日志无凭据/token；密钥≥32字节版本化；env 样例全占位符
迁移与回滚：无新迁移（复用 026）；内部 SQLite 车道零变化
外部授权记录：无
未测试项：T32 管理端页面；T36 多实例；STAGING/REAL_CHAIN/PRODUCTION
Lore 提交 SHA：见 PR squash 合并 SHA
```

## T10 — Activation Code Catalog Schema (ACT-01, migration 027)

| Field | Content |
| --- | --- |
| **Owner** | DB/Backend |
| **Reviewer** | chatgpt-codex-connector |
| **Branch / Base SHA** | `feat/customer-v3-t10-activation-code-schema` / `main@4cc04b3` (PR #40 squash) |
| **Verified Implementation SHA** | PR squash merge result (see `docs/evidence/T10-EVIDENCE.md`) |
| **Upstream Spec Sections** | Task list §3 T10, §12.2 ACT-01; code checklist §3.3 (frozen `027_activation_code_catalog.py`); activation-code dev doc §5/§11.2/§11.3/§12.1 |
| **Files Changed** | `server/migrations/versions/027_activation_code_catalog.py` (new, frozen name: 5 tables + full constraint set); `server/tests/test_activation_code_schema.py` (new, 9 red→green PG cases); `server/tests/test_postgres_migrations.py` (head assertions 026→027; 026 downgrade-guard adapted to the longer chain with `-2` + transactional-rollback lock); `server/scripts/reconcile_customer_billing.py` (`PG_ONLY_TABLES` += five 027 catalog tables); `server/scripts/sqlite_to_postgres.py` (comment); `server/tests/test_sqlite_to_postgres.py` (new empty-catalog-accepted/row-fails-closed contract test + `validate_revision_pair` head); SQLite-lane head assertions 026→027 in `test_db.py`, `test_character_domain.py`, `test_characters.py`, `test_internal_billing.py`, `test_recharge_orders.py`, `test_settings.py` |
| **Failure Test or Regression Lock** | 9 catalog cases: exact column sets per table (no-plaintext red line), batch shapes (status/positive snapshots/expiry window/creator FK), global-unique `code_digest` across batches, status-machine shape matrix (ISSUED unbound, ACTIVE bound+timestamped, SUSPENDED/REVOKED proven, UNASSIGNED/ASSIGNED rejected), partial unique index for one current binding per user, delivery traceability (actor FK/non-blank channel), export ciphertext-only (AEAD+SHA256+key version+short expiry), one-shot activation facts (code/user/first-charge order each UNIQUE), downgrade refuses existing activation facts and multi-step downgrades roll back atomically; plus the T07 import contract: empty 027 catalog tables accepted, any catalog row fails closed |
| **Implementation Result** | `027_activation_code_catalog` lands `activation_code_batches` (frozen commercial snapshots), `activation_codes` (digest + key version + masked form only, 4-state machine CHECK), `activation_code_deliveries`, `activation_code_exports` (AEAD ciphertext + SHA-256 + one-time download audit) and `activation_code_activations` (triple-unique one-shot fact); PG-only per 025/026 precedent; `first_device_id` FK deferred to the T16 device revision under the append-only fix rule; T07 cutover tooling keeps the catalog PG-only-exempted-but-empty invariant via `PG_ONLY_TABLES` |
| **Verification Command and Pass Count** | `pytest tests/test_activation_code_schema.py tests/test_postgres_migrations.py` → 19 passed; `pytest tests/test_sqlite_to_postgres.py` → 26 passed; full suite + ruff/format/mypy green (recorded at PR); CI three gates green. Pre-PR review: 1 P2 + 2 P3 findings substantively fixed with red→green locks (see `docs/evidence/T10-EVIDENCE.md` §Pre-PR Review Fixes) |
| **Evidence Level** | `AUTOMATED_VERIFIED` |
| **Security and Observability** | no plaintext code in DB (column-set assertions); digest + versioned keys; exports carry AEAD ciphertext + SHA-256 only; every catalog row traces to a real `users.id` |
| **Migration and Rollback** | new frozen-name migration 027; PG-only (SQLite lane unchanged); symmetric downgrade on an empty catalog; fail-loud once activation facts exist |
| **External Authorization Record** | None |
| **Untested Items** | application layer (T11 generation/HMAC/AEAD export, T12 admin API); activation transaction (T13); device FK (T16); STAGING/REAL_CHAIN/PRODUCTION |
| **Lore Commit SHA** | PR squash merge SHA |

### T10 Section 14 Ledger Record

```text
任务/工作包：T10 / ACT-01
Owner / Reviewer：DB/后端（Agent 执行）/ chatgpt-codex-connector（PR 评审）
分支 / 基线 SHA：feat/customer-v3-t10-activation-code-schema / 基线 4cc04b3（PR #40 squash）
上游规格段落：客户版任务清单 V3 §3 T10、§12.2 ACT-01；代码开发清单 V3 §3.3（027_activation_code_catalog.py 冻结名）；激活码开发文档 §5/§11.2/§11.3/§12.1
改动文件：server/migrations/versions/027_activation_code_catalog.py（新增 5 表全约束）、server/tests/test_activation_code_schema.py（新增 9 用例）、server/tests/test_postgres_migrations.py（head 断言与 downgrade guard 适配 027 链）、server/scripts/reconcile_customer_billing.py（PG_ONLY_TABLES 纳入 5 张 027 目录表）、server/scripts/sqlite_to_postgres.py（注释）、server/tests/test_sqlite_to_postgres.py（新增空目录接受/有行拒收合同测试 + validate_revision_pair head 027）、test_db/test_character_domain/test_characters/test_internal_billing/test_recharge_orders/test_settings 六个 SQLite 车道套件 head 断言 026→027 联动
失败测试或回归锁定：先红后绿——9 用例锁定 5 表精确列集（无明文列红线）、批次形状、码摘要全局唯一、状态机形状矩阵、当前有效绑定一户一码（部分唯一索引）、发放可追溯、导出仅密文（AEAD+SHA256+短时效+key version）、激活事实三重唯一（code/user/首充订单）、downgrade 拒绝已有激活事实且多步降级事务性回滚；T07 导入合同测试锁定空目录表接受、目录有行 fail closed
实现结果：027_activation_code_catalog 落地批次/码/发放/导出/激活事实 5 表（PG-only），全部不变量由数据库约束证明；码仅存 HMAC 摘要+key version+掩码；激活事实链禁止 downgrade 删除；first_device_id 留待 T16 设备迁移按追加修复规则补 FK；T07 导入工具保持“目录表 PG-only 豁免但必须为空”不变量
验证命令与通过数：test_activation_code_schema 9 passed + 迁移套件 19 passed + test_sqlite_to_postgres 26 passed；全量与 lint 数字见 PR；CI 三门禁全绿
证据层级：AUTOMATED_VERIFIED
安全与可观测性：无明文激活码入库（列集断言锁定）；摘要+版本化 key；导出仅 AEAD 密文+SHA256；所有操作行追溯真实 users.id
迁移与回滚：新迁移 027（冻结名）；PG-only（SQLite 车道零变化）；空目录 downgrade 对称；有激活事实时 fail-loud
外部授权记录：无
未测试项：应用层（T11/T12）；激活事务链路（T13）；设备 FK（T16）；STAGING/REAL_CHAIN/PRODUCTION
Lore 提交 SHA：见 PR squash 合并 SHA
```
