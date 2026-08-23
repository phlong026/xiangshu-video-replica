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
| **Files Changed** | `server/migrations/versions/027_activation_code_catalog.py` (new, frozen name: 6 tables incl. the append-only event table + full constraint set); `server/tests/test_activation_code_schema.py` (new, 10 red→green PG cases); `server/tests/test_postgres_migrations.py` (head assertions 026→027; 026 downgrade-guard adapted to the longer chain with `-2` + transactional-rollback lock); `server/scripts/reconcile_customer_billing.py` (`PG_ONLY_TABLES` += six 027 catalog tables); `server/scripts/sqlite_to_postgres.py` (comment); `server/tests/test_sqlite_to_postgres.py` (new empty-catalog-accepted/row-fails-closed contract test + `validate_revision_pair` head); SQLite-lane head assertions 026→027 in `test_db.py`, `test_character_domain.py`, `test_characters.py`, `test_internal_billing.py`, `test_recharge_orders.py`, `test_settings.py` |
| **Failure Test or Regression Lock** | 10 catalog cases: exact column sets per table (no-plaintext red line), batch shapes (status/positive snapshots/expiry window incl. the same-day timestamp-cast case/creator FK), global-unique `code_digest` across batches, six-state machine shape matrix (GENERATED pre-delivery, ISSUED proven, ACTIVE bound+timestamped, SUSPENDED/REVOKED proven + coupling, EXPIRED unactivated-only, unknown states rejected), partial unique index for one current binding per user, delivery traceability (actor FK/non-blank channel), export ciphertext-only (AEAD+SHA256+key version+short expiry), append-only events (typed CHECK + UPDATE/DELETE refused by trigger), one-shot activation facts (code/user/first-charge order each UNIQUE), downgrade refuses existing activation facts and multi-step downgrades roll back atomically; plus the T07 import contract: empty 027 catalog tables accepted, any catalog row fails closed |
| **Implementation Result** | `027_activation_code_catalog` lands `activation_code_batches` (frozen commercial snapshots), `activation_codes` (digest + key version + masked form only, six-state machine CHECK per acceptance spec §2.1), `activation_code_deliveries`, `activation_code_exports` (AEAD ciphertext + SHA-256 + one-time download audit), `activation_code_activations` (triple-unique one-shot fact) and `activation_code_events` (append-only audit trail enforced by a BEFORE UPDATE OR DELETE trigger); PG-only per 025/026 precedent; `first_device_id` FK deferred to the T16 device revision under the append-only fix rule; T07 cutover tooling keeps the catalog PG-only-exempted-but-empty invariant via `PG_ONLY_TABLES` |
| **Verification Command and Pass Count** | `pytest tests/test_activation_code_schema.py tests/test_postgres_migrations.py tests/test_sqlite_to_postgres.py` → 46 passed; full suite + ruff/format/mypy green (recorded at PR); CI three gates green. Pre-PR review: 1 P2 + 2 P3 fixed; PR #41 Codex review: 2 P1 fixed (six-state machine + append-only event table) — all with red→green locks (see `docs/evidence/T10-EVIDENCE.md`) |
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

## T11 — Activation Code Generation, Versioned Digests and AEAD Export (ACT-02 + ACT-03)

| Field | Content |
| --- | --- |
| **Owner** | Backend/Security |
| **Reviewer** | session-internal code review (0 P1 / 2 P2 / 5 P3, all substantively fixed) + PR #42 chatgpt-codex-connector (2 P1 + 1 P2, all substantively resolved) |
| **Branch / Base SHA** | `feat/customer-v3-t11-activation-code-service` / `main@570cd42` (PR #41 squash) |
| **Verified Implementation SHA** | PR squash merge result (see `docs/evidence/T11-EVIDENCE.md`) |
| **Upstream Spec Sections** | Task list §3 T11, §12.2 ACT-02/ACT-03; code checklist (frozen `activation_code_service.py`); activation-code dev doc §5/§12.1; acceptance spec §2.1 |
| **Files Changed** | `server/app/activation_code_service.py` (new, 460 lines: normalization/CSPRNG/masking, versioned HMAC/AEAD key resolution, rotation-window digests, six-state matrix, AES-GCM envelope, batch generation + one-time audited export); `server/tests/test_activation_code_service.py` (new, 22 red→green cases: 12 unit + 10 PG on a dedicated migrated fixture database); `deploy/customer.env.example` (T11 key families registered with generation commands and `_V2` rotation comments) |
| **Failure Test or Regression Lock** | 22 cases: entropy floor (140 bit ≥128) + full-alphabet + confusable-free + 500-code uniqueness, human-variant normalization (deterministically seeded confusables), malformed-format rejection, stable masking (prefix + first/last 4 visible, middle 20 hidden), digest determinism/keyed-ness/64-hex, HMAC key env resolution (V2/un-suffixed V1/short-key rejected/missing explicit), key-rotation verification window (old versions verifiable, highest first), full 6×6 transition matrix, AEAD roundtrip with no plaintext in ciphertext, tamper + wrong-batch rejection, AEAD key resolution (invalid base64/short/48-byte rejected — exactly 32 required), GENERATED landing + events + no plaintext in catalog, unknown batch rejected, budget overrun rejected (frozen `quantity` snapshot), concurrent generation serialized by batch-row FOR UPDATE (lock-timeout red test), cross-batch digest uniqueness (60+60), one-time audited download (FOR UPDATE + conditional UPDATE + whole-life caplog plaintext scan), expiry rejected (`downloaded_at` stays NULL), EXPORTED events, cross-batch export refused, unknown export rejected |
| **Implementation Result** | `XS04` 140-bit Crockford-base32 codes (CSPRNG, injectable `rng` for fixtures only); HMAC-SHA256 versioned digests stored as digest + key version + masked form only; AES-256-GCM export envelope bound to its batch via AAD with SHA-256 integrity and short TTL; `fetch_export_package` is the single one-time audited download path; six-state matrix exported for T12/T13; batch `quantity` enforced as the frozen issuance budget |
| **Verification Command and Pass Count** | `pytest tests/test_activation_code_service.py` → 22 passed; full suite → 710 passed (PG fixture); ruff/format/mypy green. Session-internal review: 2 P2 + 5 P3 all fixed with red tests; PR #42 Codex review: 2 P1 + 1 P2 substantively resolved — budget race locked with `FOR UPDATE` (+ red concurrency test), private-COS delivery scoped to T36/COS-01 with code-level hand-off comments, AEAD key validated as exactly 32 bytes (see `docs/evidence/T11-EVIDENCE.md` §PR #42 Review Fixes) |
| **Evidence Level** | `AUTOMATED_VERIFIED` |
| **Security and Observability** | no predictable codes (CSPRNG + entropy-floor lock); no reversible DB fields (digest + mask only); no plaintext in columns/events/logs (whole-life caplog scan); exports carry AEAD ciphertext + SHA-256 only; download actor persisted; keys ≥32 bytes, versioned rotation with an old-version verification window |
| **Migration and Rollback** | no new migration (application layer over published 027); SQLite lane unchanged |
| **Untested Items** | admin API routes (T12); first-activation atomic transaction (T13); AEAD idempotent recovery (T14); shared rate limiting / anti-enumeration (T15); real private-COS object delivery (T36+); STAGING/REAL_CHAIN/PRODUCTION |
| **Lore Commit SHA** | PR squash merge SHA |

### T11 Section 14 Ledger Record

```text
任务/工作包：T11 / ACT-02 + ACT-03
Owner / Reviewer：后端/安全（Agent 执行）/ 会话内代码评审（0 P1、2 P2 + 5 P3 全部实质修复）+ PR #42 chatgpt-codex-connector（2 P1 + 1 P2 全部实质处置：FOR UPDATE 预算串行化+并发红测试、私有 COS 投递 T36/COS-01 边界论证+代码移交注释、AEAD 密钥恰 32 字节）
分支 / 基线 SHA：feat/customer-v3-t11-activation-code-service / 基线 570cd42（PR #41 squash）
上游规格段落：客户版任务清单 V3 §3 T11、§12.2 ACT-02/ACT-03；代码开发清单 V3（activation_code_service.py 冻结名）；激活码开发文档 §5/§12.1；测试与验收规格 §2.1
改动文件：server/app/activation_code_service.py（新增 460 行）、server/tests/test_activation_code_service.py（新增 22 用例：12 单元 + 10 PG）、deploy/customer.env.example（T11 双密钥族登记）、docs/evidence/T11-EVIDENCE.md、任务与证据账本
失败测试或回归锁定：先红后绿——熵结构（140 bit）、碰撞（500 码唯一+跨批次 60+60）、掩码、旧 key 验证窗、6×6 转移矩阵、AEAD 无明文、篡改/错批次拒、一次性下载审计（caplog 全生命周期）、过期拒、超发拒、跨批次导出拒、未知批次/导出拒
实现结果：XS04 140-bit 码 + 版本化 HMAC 摘要 + 批次绑定 AEAD 导出 + 六态矩阵；批次 quantity 冻结预算；明文仅存于返回值与内存
验证命令与通过数：test_activation_code_service 22 passed；全量 710 passed（PG fixture）；ruff/format/mypy 全绿
证据层级：AUTOMATED_VERIFIED
安全与可观测性：无可预测码、无可逆字段、列/事件/日志全链路无明文、导出仅密文+SHA256、下载 actor 落审计、密钥版本化轮换
迁移与回滚：无新迁移；SQLite 车道零变化
外部授权记录：无；未调用真实 ZPay/COS/付费 Provider/发码/灰度/公网发布
未测试项：T12 管理 API；T13 激活事务；T14 AEAD 幂等恢复；T15 限流/防枚举；私有 COS 真实投递；STAGING/REAL_CHAIN/PRODUCTION
Lore 提交 SHA：见 PR squash 合并 SHA
```

## T12 — Admin Activation Code Management API (ACT-04)

| Field | Content |
| --- | --- |
| **Owner** | Backend/Admin |
| **Reviewer** | session-internal code review |
| **Branch / Base SHA** | `feat/customer-v3-t12-admin-activation-routes` / `main@d7e293d` (T11, PR #42 squash) |
| **Verified Implementation SHA** | PR squash merge result (see `docs/evidence/T12-EVIDENCE.md`) |
| **Upstream Spec Sections** | Task list §3 T12, §12.2 ACT-04; code checklist (frozen `admin_activation_routes.py`); activation-code dev doc §11.3 (idempotency invariant), §15 (admin write contract); acceptance spec §2 |
| **Files Changed** | `server/app/admin_activation_routes.py` (new, 894 lines: §15 write contract, revision-031 idempotency snapshot layer, 8 routes); `server/migrations/versions/031_admin_write_idempotency.py` (new, PG-only); `server/app/activation_code_service.py` (key-version helpers appended); `server/app/main.py` (router mount); `server/tests/test_admin_activation_routes.py` (new, 38 red→green cases on a dedicated migrated fixture DB); 9 existing test files (head assertions 027→031, downgrade-guard step counts +1); `server/scripts/reconcile_customer_billing.py` + `server/scripts/sqlite_to_postgres.py` (`admin_write_idempotency` in `PG_ONLY_TABLES`) |
| **Failure Test or Regression Lock** | 38 cases: unauthenticated 401 / auditor write 403 / auditor read 200 / CSRF rejected; batch payload validation (400 `BATCH_VALIDATION_FAILED`); generate (unknown batch 404 / closed batch 409 / budget overrun 409 with zero stray rows / missing HMAC or AEAD key 503 with no DB rows); one-time download (second download 409 / expired 409 / unknown 404 / no snapshot row ever persists the plaintext / reason + request id persist on the export audit columns); deliver (channel validation / six-state matrix / duplicate 409); suspend/resume/revoke (shape matrix incl. `suspended_at` cleared on resume, no SUSPENDED→ISSUED edge, events carry reason + request id); listing filters; write-contract order (key → confirm → reason); concurrent same-key two-thread barrier → single batch row + single snapshot row + replay header; same key + same body against a different resource → 409 `IDEMPOTENCY_CONFLICT` with the second resource untouched (PR #43 review P2) |
| **Implementation Result** | §15 admin write contract (CSRF + reason + Idempotency-Key + request id) on the T09 session stack; revision-031 idempotency snapshot layer (unique (actor, route, key digest), request_hash freeze incl. concrete path params, placeholder-then-backfill in the same transaction, business failure rolls the key back); plaintext codes only ever live in the one-time download response (that route bypasses the snapshot layer; `downloaded_at` one-shot is the anti-replay; the download reason + request id persist on the export audit columns — PR #43 review P1); SQLite lane fails closed 503 |
| **Verification Command and Pass Count** | `pytest tests/test_admin_activation_routes.py` → 38 passed; full suite → 748 passed (PG fixture); ruff/format/mypy green (see `docs/evidence/T12-EVIDENCE.md`) |
| **Evidence Level** | `AUTOMATED_VERIFIED` |
| **Security and Observability** | every admin write traceable to a real `users.id` (batch, snapshot, events); idempotency keys stored as sha256 digests only; plaintext never in DB/snapshot/logs; writer/reader RBAC split; CSRF enforced; request id on every response and event |
| **Migration and Rollback** | new migration 031 (numbered past the frozen 028–030 suggested window per PR #43 review P1; the device/session/queue frozen themes chain off the then-current head); PG-only (SQLite lane unchanged, revision only); symmetric downgrade (snapshots are replay caches, not business facts); T07 cutover keeps `admin_write_idempotency` PG-only-exempted-but-empty |
| **External Authorization Record** | None; PRICE-01 decision unfrozen — no external sales batches may be generated (process red line registered) |
| **Untested Items** | first-activation atomic transaction (T13); AEAD idempotent recovery (T14); shared rate limiting / anti-enumeration (T15); admin frontend pages (T32); multi-instance topology (T36+); STAGING/REAL_CHAIN/PRODUCTION |
| **Lore Commit SHA** | PR squash merge SHA |

### T12 Section 14 Ledger Record

```text
任务/工作包：T12 / ACT-04
Owner / Reviewer：后端/管理（Agent 执行）/ 会话内代码评审
分支 / 基线 SHA：feat/customer-v3-t12-admin-activation-routes / 基线 d7e293d（T11 PR #42 squash）
上游规格段落：客户版任务清单 V3 §3 T12、§12.2 ACT-04；代码开发清单 V3（admin_activation_routes.py 冻结名）；激活码开发文档 §11.3 幂等不变量、§15 管理写合同；测试与验收规格 §2
改动文件：server/app/admin_activation_routes.py（新增 894 行：写合同+幂等快照层+8 路由）、server/migrations/versions/031_admin_write_idempotency.py（新增，PG-only，含 download 审计耦合 CHECK；编号 031 避开冻结的 028–030 建议区间，PR #43 评审 P1 修复）、server/app/activation_code_service.py（追加 4 个密钥版本解析函数；fetch_export_package 增 download_reason/download_request_id 必填参数）、server/app/main.py（挂载）、server/tests/test_admin_activation_routes.py（新增 38 用例，专用迁移 fixture 库）、9 个既有测试文件（head 断言 027→031，downgrade 守卫步数 +1）、server/scripts/reconcile_customer_billing.py + sqlite_to_postgres.py（PG_ONLY_TABLES 纳入 admin_write_idempotency）、docs/evidence/T12-EVIDENCE.md、任务与证据账本
失败测试或回归锁定：先红后绿——未登录 401/auditor 写 403/auditor 读 200/CSRF 拒；批次校验（名称/面值/额度/数量/有效期 400）；生成（未知批次 404/关闭批次 409/超发 409 零残留/密钥缺失 503 零写入）；下载（一次性/过期/未知/明文不入快照/downloaded_at+reason+request id 审计元组落库）；发放（渠道校验/状态机/重复发放 409）；暂停/恢复/作废（六态矩阵+suspended_at 形状+事件含 reason 与 request id）；幂等（同键同参回放+replay 头/同键异参 409/同键跨资源 409 仅目标 A 生效/并发双线程 barrier 串行化单批次）；写合同（key/confirm/reason 顺序报错）
实现结果：§15 管理写合同（CSRF+reason+Idempotency-Key+request id）+ 028 幂等快照层（actor/route/key digest 唯一、request_hash 冻结、同事务占位-回填、业务失败回滚释放键）+ 8 条路由（批次/生成/下载/发放/暂停/恢复/作废/列表）；明文码仅存于一次性下载响应（绕过快照层，downloaded_at 一次性约束防重放）；SQLite 车道 fail-closed 503
验证命令与通过数：test_admin_activation_routes 38 passed；全量 748 passed（PG fixture）；ruff/format/mypy 全绿
证据层级：AUTOMATED_VERIFIED
安全与可观测性：管理写全链路 actor 可追溯（批次/快照/事件均落 users.id）；幂等键仅存 sha256 摘要；明文不入库不入快照不入日志；RBAC 写/读分离；CSRF 强制；request id 全响应+全事件
迁移与回滚：新迁移 031（避开冻结的 028–030 建议编号区间，PR #43 评审 P1；设备/会话/队列冻结主题将来从当日 head 顺延链接）；PG-only（SQLite 车道零变化，仅 revision 推进）；downgrade 对称（快照为重放缓存非业务事实）；T07 导入工具保持 admin_write_idempotency PG-only 豁免但必须为空
外部授权记录：无；未调用真实 ZPay/COS/付费 Provider/对外发码/灰度/公网发布；PRICE-01 决议未冻结前不得生成对外销售批次（流程红线已登记）
未测试项：首次激活原子事务（T13）；AEAD 幂等恢复（T14）；共享限流与防枚举（T15）；管理端前端页面（T32）；多实例部署形态（T36+）；STAGING/REAL_CHAIN/PRODUCTION
Lore 提交 SHA：见 PR squash 合并 SHA
```


## T13 — First-Activation Atomic Transaction (ACT-05 / ACT-06)

| Field | Content |
| --- | --- |
| **Owner** | Backend/Billing |
| **Reviewer** | session-internal code review |
| **Branch / Base SHA** | `feat/customer-v3-t13-first-activation` / `main@83a5bb2` (T12, PR #43 squash) |
| **Verified Implementation SHA** | PR squash merge result (see `docs/evidence/T13-EVIDENCE.md`) |
| **Upstream Spec Sections** | Task list §3 T13, §12.2 ACT-05/ACT-06; code checklist (frozen `activation_code_routes.py`, migrations `028_customer_devices_and_activations` / `029_customer_sessions_and_idempotency`); activation-code dev doc §11.2 (idempotency envelope), §11.3 (concurrency invariants), §12.1 (first-activation transaction), §7 (key red lines); acceptance spec §2 |
| **Files Changed** | `server/app/activation_code_routes.py` (new, 742 lines: versioned key resolution, keyed digests, AES-GCM envelope, one-transaction activation chain, unified anti-enumeration); `server/migrations/versions/028_customer_devices_and_activations.py` (new, PG-only, frozen name: two-slot `customer_devices` + digest/version columns + partial unique indexes `uq_customer_devices_slot`/`uq_customer_devices_fingerprint` + 027 deferred `first_device_id` FK attach + activated-guard downgrade refusal); `server/migrations/versions/029_customer_sessions_and_idempotency.py` (new, PG-only, frozen name: `customer_session_state` single-session invariant + epoch monotonic trigger, `customer_session_events` append-only trigger, `customer_idempotency_envelopes` unique (operation, scope, key_digest) + three-state coupling CHECK + purged_at); `server/app/main.py` (router mount; PR #44 review P1 — CORS `allow_headers` adds `Idempotency-Key`/`X-Request-Id`, `expose_headers` adds `X-Request-Id`/`X-Idempotent-Replay`); `server/tests/test_activation_code_routes.py` (new, 24 red→green cases: 16 original + 5 session-review + 3 PR #44-review regressions); `server/tests/test_customer_activation.py` (new, 5 concurrency cases incl. ACT-06 100 threads); `server/tests/test_activation_code_schema.py` (2 T10 cases adapted + 3 PR #44-review trigger cases); `server/tests/test_admin_activation_routes.py` (TRUNCATE covers new tables); 9 test files + `server/scripts/sqlite_to_postgres.py` + `server/scripts/reconcile_customer_billing.py` (head 031→029, PG_ONLY_TABLES + four new tables) |
| **Failure Test or Regression Lock** | 32 cases: atomic happy path (201, full chain incl. wallet balance + epoch-1 90 s lease); request-id echo; missing Idempotency-Key 400; unified 400 ×7 (unknown/malformed/expired/suspended/revoked/active/generated); same fingerprint second code 409 `USER_ALREADY_ACTIVATED`; same key + same body replays identical identity (replay header + original request id); same key + different body 409; SQLite fail-closed 503 (runtime checked before keys); log scan — no plaintext code/token; 100 threads/one barrier/one code → one 201 + 99 × 400 with exactly one of each fact row; concurrent same-key recovery → identical username/device token/session token, one CHARGE; concurrent same-fingerprint cross-code → one 201 + one 409, losing code untouched; business failure releases the key; username collision regenerates in-transaction (savepoint); session-review regressions (naive batch expiry → unified 400, envelope row shape, recovery-window env override, expired-window refusal, whitespace-padding replay); PR #44-review regressions (CORS preflight permits `Idempotency-Key`/`X-Request-Id` + actual response exposes replay markers; rotation-window fingerprint check — V1-bound device + V2 added → second code still 409 with one user/CHARGE; envelope scoped under V1 still replays after V2 is added; both 029 triggers installed — epoch decrease and audit-table UPDATE/DELETE rejected) |
| **Implementation Result** | `POST /api/customer/activate` creates the whole customer chain in exactly one `pg_transaction()`: FOR UPDATE code lock → server-generated `customer` user (savepoint retry ≤5) → funded wallet → slot-1 device (keyed digests + versions) → PAID `provider=activation_code` order (frozen batch price, base=charged per PRICE-01) → unique CHARGE (`activation_code:charge:{order_id}`) → activation fact (attaches 027's dangling FK) → ACTIVE code + ACTIVATED event → epoch-1 session + 90 s lease; idempotency envelope per revision 029 (sha256 key digest only, AAD-bound AES-GCM sealed response, 24 h recovery window, business failure rolls the placeholder back); unified 400 anti-enumeration; PG runtime fails closed 503 before key resolution |
| **Verification Command and Pass Count** | `pytest tests/test_activation_code_routes.py tests/test_customer_activation.py tests/test_activation_code_schema.py` → 42 passed; full suite → 780 passed (PG fixture; the three previously-skipped network-dependent cases also ran green); ruff/format/mypy green; client workspace → 324 passed; biome e2e clean; cargo fmt+check clean; secret-scan patterns clean (see `docs/evidence/T13-EVIDENCE.md`) |
| **Evidence Level** | `AUTOMATED_VERIFIED` |
| **Security and Observability** | plaintext code/device token/session token only in the HTTP response and the AEAD envelope column (log-scan test); idempotency keys stored as sha256 digests; fingerprint + credential digests keyed HMAC with versioned rotation; unified anti-enumeration rejections; request id on every response, event and replay |
| **Migration and Rollback** | new migrations 028/029 (frozen names, chained off the live head 031; 030 stays reserved for T25); PG-only (SQLite lane advances the revision only); 028 refuses downgrade while an activation fact exists (audit-chain guard); 029 downgrade symmetric (runtime caches, not business facts); T07 cutover keeps the four new tables PG-only-exempted-but-empty |
| **External Authorization Record** | None; no real ZPay/COS/paid provider/external codes/gray release/public launch |
| **Untested Items** | AEAD recovery completion + expired-envelope cleanup (T14/ACT-07); shared rate limiting + timing parity (T15/ACT-08); second device + pairing (T16–T18); session lifecycle (T19–T20); ZPay coexistence recharge (T22/BILL-01); frontend/desktop (T28+); STAGING/REAL_CHAIN/PRODUCTION |
| **Lore Commit SHA** | PR squash merge SHA |

### T13 Section 14 Ledger Record

```text
任务/工作包：T13 / ACT-05 + ACT-06
Owner / Reviewer：后端/账务（Agent 执行）/ 会话内代码评审
分支 / 基线 SHA：feat/customer-v3-t13-first-activation / 基线 83a5bb2（T12 PR #43 squash）
上游规格段落：客户版任务清单 V3 §3 T13、§12.2 ACT-05/ACT-06；代码开发清单 V3（activation_code_routes.py、028/029 迁移冻结名）；激活码开发文档 §11.2 幂等信封、§11.3 并发不变量、§12.1 首次激活事务、§7 密钥红线；测试与验收规格 §2
改动文件：server/app/activation_code_routes.py（新增 742 行）、server/migrations/versions/028_customer_devices_and_activations.py（新增，PG-only，冻结名）、server/migrations/versions/029_customer_sessions_and_idempotency.py（新增，PG-only，冻结名）、server/app/main.py（挂载+CORS 幂等头/expose 头）、server/tests/test_activation_code_routes.py（新增 24 用例，含会话评审 5 条+PR #44 评审 3 条回归）、server/tests/test_customer_activation.py（新增 5 并发用例含 ACT-06 100 并发）、server/tests/test_activation_code_schema.py（2 用例适配+3 条触发器回归）、server/tests/test_admin_activation_routes.py（TRUNCATE 纳新表）、9 个既有测试文件+2 个脚本（head 断言 031→029、PG_ONLY_TABLES 纳四张新表）、docs/evidence/T13-EVIDENCE.md、任务与证据账本
失败测试或回归锁定：先红后绿——契约 24 例（含会话评审 P2/P3 修复回归：naive 过期统一 400、信封形状、恢复窗 env、过期拒绝重放、空白填充重放；PR #44 评审回归：CORS 预检允许幂等头/实际响应 expose 回放标记、轮换窗口指纹检查 V1 绑定设备+V2 新增二码仍 409 且一 user/一 CHARGE、V1 作用域信封 V2 新增后仍可重放）（原子全链/请求 id 回显/幂等键必填/统一 400 七场景/同指纹二码 409/同键同体重放+replay 头/同键异体 409/SQLite fail-closed 503/日志无明文）；并发 5 例（100 并发恰一成功+全库恰一份事实/同键并发恢复同一身份且仅一笔 CHARGE/同指纹跨码并发一胜一 409/业务失败释放幂等键/用户名碰撞事务内保存点重试）；schema 2 例适配+3 例触发器回归（两触发器存在、epoch 降低拒绝、审计表 UPDATE/DELETE 拒绝）
实现结果：单事务激活链（user+wallet+slot1+PAID order+CHARGE+activation+ACTIVE code+事件+epoch-1 session/90s 租约）全有或全无；幂等信封 029（摘要入库/AAD 绑定/24h 恢复窗/业务失败回滚释放键）；统一 400 防枚举；PG fail-closed 先于密钥检查
验证命令与通过数：专项 42 passed；全量 780 passed（PG fixture，含此前 3 个 skip 的网络依赖用例）；ruff/format/mypy 全绿；client 324 passed；biome/cargo/secret 扫描 clean
证据层级：AUTOMATED_VERIFIED
安全与可观测性：明文只存在于 HTTP 响应与 AEAD 信封列；幂等键仅存摘要；指纹/凭据 keyed HMAC 版本化；统一防枚举；request id 全链路
迁移与回滚：028/029 冻结名从 head 031 顺延；PG-only；028 激活存在拒绝降级；029 对称；T07 导入新表必须为空
外部授权记录：无；未调用真实 ZPay/COS/付费 Provider/对外发码/灰度/公网发布
未测试项：T14 幂等恢复完善；T15 限流/防枚举；T16-T18 设备；T19-T20 会话；T22 ZPay 续充；T28+ 前端；STAGING/REAL_CHAIN/PRODUCTION
Lore 提交 SHA：见 PR squash 合并 SHA
```


## T14 — AEAD Idempotency Recovery & Expired-Envelope Cleanup (ACT-07)

| Field | Content |
| --- | --- |
| **Owner** | Backend/Security |
| **Reviewer** | session-internal code review |
| **Branch / Base SHA** | `feat/customer-v3-t14-idempotency-recovery` / `main@fec36c7` (T13, PR #44 squash) |
| **Verified Implementation SHA** | PR squash merge result (see `docs/evidence/T14-EVIDENCE.md`) |
| **Upstream Spec Sections** | Task list §3 T14, §12.2 ACT-07; code checklist §9.1 (frozen `customer_idempotency.py` / `test_customer_idempotency.py`), §10.1 (purge CLI), §12 (maintenance service/timer); activation-code dev doc §11.2 (idempotency envelope), §7 (key red lines); acceptance spec §2 |
| **Failure Test or Regression Lock** | 18 cases: module units 8 (request hash whitespace-stable + param-distinguishing; key digest hides the raw key; seal/open round-trip; wrong AAD rejected; ciphertext holds no plaintext secret — ACT-07; AEAD rotation window resolves V1/V2; retired key version fails closed; recovery-window env override); PG integration 7 on the dedicated migrated fixture DB (envelope lifecycle insert→complete→load→open; same-key different-hash conflict evidence; expired window visible; purge clears only expired — expired/live/already-purged triple, second run returns 0; purged envelope no longer recoverable; the 029-reserved recovery index exists); CLI 3 (real purge, dry-run keeps rows, missing DSN exits 1). T13's 42 activation cases kept green as the refactor regression lock (incl. the 100-thread ACT-06 race and same-key recovery) |
| **Implementation Result** | The envelope engine extracted from T13's route into the frozen shared module `server/app/customer_idempotency.py` (operation-generalized for T17/T19/T22 reuse): versioned AEAD keys, sha256 key digest + normalized request hash, AES-256-GCM seal/open with `operation/scope/key_digest` AAD binding, envelope persistence, and the T14 cleanup story — `count_expired_envelopes` / `purge_expired_envelopes` null the ciphertext triple under `purged_at` in one UPDATE walking the 029-reserved recovery index (CHECK coupling keeps a purged row payload-free; idempotent re-run). The maintenance CLI (`scripts/purge_idempotency_envelopes.py`, dry-run / fail-closed DSN, counts-only output) runs from the new sandboxed `video-replica-maintenance.service` daily timer (04:10, staggered against the 03:20 backup). `activation_code_routes.py` refactored onto the module with zero behaviour change; expired windows answer 409 (key spent); retired key versions inside the window answer 503 |
| **Verification Command and Pass Count** | `pytest tests/test_customer_idempotency.py` → 18 passed; four-file T13+T14 special → 60 passed; full suite → 795 passed + 3 skipped (798 collected = 780 baseline + 18 new, PG fixture, zero regressions; the 3 skips are the pre-existing Windows-environment bash cases — POSIX launcher ×2 + secret-scan shell — which run on the Linux CI gates; one gate1_e2e thread-timing flaky in an earlier run was isolated and passed on re-run); ruff/format/mypy green (142 files formatted, 58 source files typed); post-review fix re-run: special 60 passed + full 795/3 re-confirmed; CLI verified end-to-end (--help, missing-DSN exit 1, unmigrated database fails loud) |
| **Evidence Level** | `AUTOMATED_VERIFIED` |
| **Security and Observability** | No directly usable plaintext secret in the envelope (sealed ciphertext is the only persisted copy of the one-time response; tests lock both the key name and the value out of the ciphertext); raw idempotency key stored as sha256 digest only; AAD binding prevents cross-row replay; purge output counts only; retired key versions fail closed 503 inside the recovery window; expiry decided on the server clock only |
| **Migration and Rollback** | No new migration (029 already reserved `purged_at`, the payload three-state coupling CHECK and the recovery index — T14 ships the job that uses them); rollback = revert code (envelope schema unchanged; the purge is safely interruptible and re-runnable) |
| **External Authorization Record** | None; no real ZPay/COS/paid provider/external codes/gray release/public launch |
| **Untested Items** | T17/T19/T22 reuse of the shared engine (delivered by those tasks); real systemd environment for the maintenance timer (ops acceptance lands with the T36 deployment manual); shared rate limiting + timing parity (T15/ACT-08); STAGING/REAL_CHAIN/PRODUCTION |
| **Lore Commit SHA** | PR squash merge SHA |

### T14 Section 14 Ledger Record

```text
任务/工作包：T14 / ACT-07
Owner / Reviewer：后端/安全（Agent 执行）/ 会话内代码评审
分支 / 基线 SHA：feat/customer-v3-t14-idempotency-recovery / 基线 fec36c7（T13 PR #44 squash）
上游规格段落：客户版任务清单 V3 §3 T14、§12.2 ACT-07；代码开发清单 V3 §9.1 customer_idempotency.py/test_customer_idempotency.py、§10.1 purge CLI、§12 maintenance service/timer 冻结名；激活码开发文档 §11.2 幂等信封、§7 密钥红线；测试与验收规格 §2
改动文件：server/app/customer_idempotency.py（新增 334 行冻结名）、server/scripts/purge_idempotency_envelopes.py（新增维护 CLI）、server/app/activation_code_routes.py（重构接入共享模块，行为零变化）、server/tests/test_customer_idempotency.py（新增 18 用例）、deploy/systemd/video-replica-maintenance.service/.timer（新增冻结名；OnFailure=告警挂钩）、deploy/systemd/video-replica-maintenance-alert.service（新增：purge 重试预算耗尽的 ALERT 级 journald 告警单元，PR #45 评审 P2）、deploy/customer.env.example（补 T13 设备域密钥占位+T14 AEAD 密钥/恢复窗口占位）、docs/evidence/T14-EVIDENCE.md、任务与证据账本
失败测试或回归锁定：先红后绿 18 例（模块级 8+PG 集成 7+CLI 3）；T13 42 例作为重构回归锁定全部保持绿（含 ACT-06 100 并发与同键恢复）
实现结果：幂等信封引擎提取为共享模块（operation 泛化供 T17/T19/T22 复用）；恢复窗口到期后同 key 409；purge 单条 UPDATE 清空密文三列并记 purged_at（029 CHECK 耦合、幂等重跑为 0）；maintenance timer 每日清理；路由重构后信封行为与 T13 完全一致
验证命令与通过数：专项 18 passed；T13+T14 四文件 60 passed；全量 795 passed + 3 skipped（总数 798，零回归；3 个 skip 为既存 Windows 环境性 bash 用例，Linux CI 上全跑；含一次 gate1_e2e 线程时序 flaky 的隔离重跑）；评审修复后终跑专项 60 + 全量 795/3 复确认；ruff/format/mypy 全绿
证据层级：AUTOMATED_VERIFIED
安全与可观测性：信封不保存可直接使用的明文 secret（密文为唯一持久化副本，测试锁定）；原始幂等键仅存 SHA-256 摘要；AAD 绑定防跨行重放；purge 输出仅计数；退役密钥版本 503 fail-closed；到期判定只用服务器时钟
迁移与回滚：无新迁移（029 已预留全部结构）；回滚=还原代码，purge 可安全中止与重跑
外部授权记录：无
未测试项：T17/T19/T22 共享引擎复用；maintenance timer 真实 systemd 环境（随 T36）；T15；STAGING/REAL_CHAIN/PRODUCTION
Lore 提交 SHA：见 PR squash 合并 SHA
```


## T15 — Shared Multi-Instance Rate Limiting & Anti-Enumeration (ACT-08)

| Field | Content |
| --- | --- |
| **Owner** | Security/Backend |
| **Reviewer** | session-internal code review |
| **Branch / Base SHA** | `feat/customer-v3-t15-rate-limit-anti-enumeration` / `main@c206323` (T14, PR #45 squash) |
| **Verified Implementation SHA** | PR squash merge result (see `docs/evidence/T15-EVIDENCE.md`) |
| **Upstream Spec Sections** | Task list §3 T15, §12.2 ACT-08; code checklist §3 (frozen `security_rate_limit.py`), §11.1 (frozen `test_customer_security.py`); activation-code dev doc §11.3 (concurrency & abuse), §7 (key red lines); acceptance spec §6 |
| **Failure Test or Regression Lock** | 18 cases: module units 3 (bucket key; env overrides + non-numeric/non-positive fall back to safe defaults — an env typo can never disable the limits; measurable constant anti-enumeration delay baseline); PG integration 8 on the dedicated migrated fixture DB `t15_customer_security_test` (window allows then blocks; window resets after expiry; dimensions independent; **two independent connections — two API instances — share one budget atomically**; failure record + trailing-window metrics; metrics window scoping; alert threshold; the failure record holds no plaintext code); route integration 5 (429 with Retry-After; malformed requests share the IP budget; the code dimension blocks a single-code burst across IPs; every unified rejection records a failure event with the digest identifier; unknown/malformed/expired rejections all apply the constant delay); review-fix locks 2 (a fully validated idempotent replay spends no rate-limit budget while the next fresh attempt still trips the limiter; downgrade refuses once failure events exist, empty schema round-trips 032 → 029 → head). T13/T14 five-file 78 passed as the regression lock |
| **Implementation Result** | The shared limiter lands in PostgreSQL (one atomic UPSERT per consumption — no Redis, no message queue, per the architecture red lines): the activation route spends the IP dimension on *every* attempt (malformed included) and the code dimension (keyed digest, never the plaintext) only for well-formed codes; exceeding either answers 429 `RATE_LIMITED` with `Retry-After`; windows reset after expiry. Every code-side rejection appends an append-only (trigger-guarded) failure event aggregatable into trailing-window metrics, crossing the operator threshold emits an ERROR-level log record — the T37/OPS-02 hook; the unified rejection path burns a constant PBKDF2-SHA256 cost so unknown/expired/suspended/revoked/already-active codes share one latency profile with the T13 unified 400 body. A read-only probe ahead of the limiter lets a fully validated idempotent replay short-circuit with zero budget (T14 retry contract preserved) |
| **Verification Command and Pass Count** | `pytest tests/test_customer_security.py` → 18 passed; five-file T13/T14+T15 special → 78 passed; full suite → 816 passed (814 after the implementation round + 2 review-fix tests, PG fixture, zero regressions); ruff/format/mypy green (145 files formatted, 59 source files typed); post-review-fix re-run: special 78 + full 816 re-confirmed |
| **Evidence Level** | `AUTOMATED_VERIFIED` |
| **Security and Observability** | code-dimension counters and failure events store the keyed digest only (test-locked: no plaintext code in the audit table); append-only trigger refuses any rewrite of the failure audit; the anti-enumeration delay runs even when the audit write fails (the timing profile never depends on database health); windows and metrics decided on the server clock only; 429 `Retry-After` rides the exception path; alert-threshold crossing emits an ERROR-level structured log for the T37/OPS-02 pipeline |
| **Migration and Rollback** | New migration 032 (chains off head 029; 030 stays reserved for T25 — T12/031 numbering precedent); PG-only (SQLite advances the revision only); downgrade refuses once failure events exist (audit must survive a rollback — 026/028 guard precedent); retention cleanup must not be a plain DELETE against the append-only trigger (session-identifier exemption or partition drops — documented in the migration for the future OPS task); `PG_ONLY_TABLES` in the T07 import/reconcile tool registers both tables keeping the fail-closed contract |
| **External Authorization Record** | None; no real ZPay/COS/paid provider/external codes/gray release/public launch |
| **Untested Items** | T19 login-lane reuse (login:ip / login:account dimensions tabled but unrouted); real multi-instance load-balancer topology (T36); reverse-proxy real-client-IP delivery verification (ops acceptance with T36); T37/OPS-02 alerting-pipeline consumption; STAGING/REAL_CHAIN/PRODUCTION |
| **Lore Commit SHA** | PR squash merge SHA |

### T15 Section 14 Ledger Record

```text
任务/工作包：T15 / ACT-08
Owner / Reviewer：安全/后端（Agent 执行）/ 会话内代码评审
分支 / 基线 SHA：feat/customer-v3-t15-rate-limit-anti-enumeration / 基线 c206323（T14 PR #45 squash）
上游规格段落：客户版任务清单 V3 §3 T15、§12.2 ACT-08；代码开发清单 V3 §3 security_rate_limit.py、§11.1 test_customer_security.py 冻结名；激活码开发文档 §11.3 并发与滥用、§7 密钥红线；测试与验收规格 §6
改动文件：server/migrations/versions/032_security_rate_limits.py（新增：counters+append-only failures 两表、维度 CHECK 词表、downgrade 守卫、保留期约束注释）、server/app/security_rate_limit.py（新增 308 行冻结名：共享固定窗口消费 UPSERT、失败审计+指标+告警阈值、常数防枚举时延、env 安全回退）、server/app/activation_code_routes.py（限流接入：IP 维度含 malformed、code 维度仅合法格式码、统一失败审计+告警日志+常数时延、429+Retry-After、限流前只读 replay 预检）、server/tests/test_customer_security.py（新增 18 用例）、server/scripts/reconcile_customer_billing.py（PG_ONLY_TABLES 登记 032 两表）、server/scripts/sqlite_to_postgres.py（注释同步）、deploy/customer.env.example（4 个限流变量+反代 IP 部署指导）、11 个测试文件（head 断言 029→032 约 20 处、downgrade 守卫测试改绝对 revision、T13 路由测试限流预算 env 提升+security 表 truncate）、docs/evidence/T15-EVIDENCE.md、任务与证据账本
失败测试或回归锁定：先红后绿 18 例——模块级 3+PG 集成 8（专用迁移库 t15_customer_security_test）+路由集成 5+评审锁定 2；T13/T14 五文件 78 passed 作为回归锁定
实现结果：多 API 实例共享限流落地 PG（单 UPSERT 原子消费，无 Redis/MQ）；激活接口 IP 维度全部尝试（含 malformed）计数、code 维度按 HMAC 摘要计数（明文永不过库）；超限 429 RATE_LIMITED+Retry-After；窗口过期自动重置；每次拒绝追加 append-only 审计事件并聚合成指标，超阈值打 ERROR 告警日志（T37/OPS-02 消费）；未知/过期/作废等全部拒绝共享统一 400 响应体+常数 PBKDF2 时延（关闭时序侧信道）；幂等 replay 只读预检零预算短路（T14 重试无副作用契约保持）
验证命令与通过数：专项 18 passed；五文件 78 passed；全量 816 passed（814 实现轮 + 2 评审修复新增，零回归，PG fixture）；ruff/format/mypy 全绿（145 files formatted，59 source files typed）；实现轮全量 814 + 静态全绿后经代码评审修复 3 条（1 P2+2 P3）复跑专项 18 + 回归 60 + 静态全绿 + 全量 816 复确认
证据层级：AUTOMATED_VERIFIED
安全与可观测性：计数器与审计表 code 维度只存 keyed 摘要（测试锁定明文不出现）；append-only 触发器拒绝改写审计；审计写失败时时延照常（时序剖面不依赖数据库健康）；窗口与指标只用服务器时钟；env 非法值回退安全默认；429 头经异常路径携带；告警阈值 ERROR 日志为 T37 管道挂钩
迁移与回滚：新迁移 032（避开冻结 028–030 区间，从 head 029 顺延；030 仍留给 T25）；PG-only（SQLite 仅 revision 推进）；downgrade 非空守卫；T07 导入工具登记 PG_ONLY_TABLES 保持 fail-closed 契约；回滚=032 downgrade（空表时对称）+还原代码
外部授权记录：无；未调用真实 ZPay/COS/付费 Provider/对外发码/灰度/公网发布
未测试项：T19 登录车道复用；真实多实例负载均衡拓扑联测（T36）；反代 IP 传递的真实部署验证（随 T36）；T37/OPS-02 告警管道消费；STAGING/REAL_CHAIN/PRODUCTION
Lore 提交 SHA：见 PR squash 合并 SHA
```


## T16 — Two Device Slots, Credentials & Unbind History (DEV-01)

| Field | Value |
| --- | --- |
| **Task ID** | T16 / DEV-01 |
| **Owner / Reviewer** | Backend/DB (Agent) / session-internal code review |
| **Branch / Base SHA** | feat/customer-v3-t16-device-slots-unbind / base 517e1d2 (T15, PR #46 squash) |
| **Date** | 2026-08-23 |
| **Verified Implementation SHA** | PR squash merge result (see docs/evidence/T16-EVIDENCE.md) |
| **Upstream Spec Sections** | Task list §3 T16, §12.3 DEV-01; code checklist §3.2 (frozen customer_device_service.py / customer_device_routes.py), §3.3 (frozen test_customer_devices.py); dev doc §3.2 device rules, §6.1 API table, §12.4 fencing, §13.2 error codes; acceptance spec §2.2/§3.3 |
| **Failure Test or Regression Lock** | 22 cases: module units 2 (digest determinism; slot-count constant) + credential & two-slot view 7 (three-state 401s; slot view after activation) + unbind semantics 7 (slot reuse + history preserved; atomic session revocation; 404 missing/IDOR; 409 already-unbound; other-device unbind leaves own slot/session intact) + third-device block 2 (next_free_slot state machine; PG-level UniqueViolation/CheckViolation) + key rotation 1 (V2-issued credential under dual-version config; V2 retired → 401) + review-fix locks 3 (PG fail-closed 503; unconfigured keys → 503 not 401; REVOKED row → 401 DEVICE_REVOKED) + idempotent unbind 3 (PR #47 Codex P2: missing key → 400 with the row untouched; lost-204 own-device retry replays the sealed 204 with zero re-execution; same key + different target → 409 IDEMPOTENCY_CONFLICT) |
| **Implementation Result** | The two current device slots, credentials, unbind history and the third-device block land as the application layer over the 028 schema (no new migration): the device credential authenticates via Authorization: Bearer with keyed HMAC-SHA256 digests probed across every configured key version; GET /api/customer/devices answers the two-slot status plus release history; DELETE flips BOUND→UNBOUND (row never deleted, slot immediately reusable — immune to the DEV-01 No-Go by the partial unique index) with a mandatory Idempotency-Key (PR #47 Codex P2: the T14 envelope engine seals the audit payload, a lost 204 replays with the same key + same target, the recovery probe runs before credential authentication, the same key on a different target answers 409 IDEMPOTENCY_CONFLICT) and atomically revokes the session riding the released device (epoch+1, immediately-expired lease with full microsecond precision + a 1µs GREATEST backstop, LOGOUT device_unbound event); both slots full → next_free_slot is None and PostgreSQL refuses a third BOUND row; stable error codes 401 REQUIRED/INVALID/REVOKED, 404 (missing = foreign, no IDOR oracle), 409 ALREADY_UNBOUND, 503 fail-closed (missing PG runtime / key misconfiguration — never a misleading 401 or a 500); unbind clock sampled from SELECT now() inside the transaction (SES-01) |
| **Verification Command and Pass Count** | pytest tests/test_customer_devices.py → 22 passed; full suite → 839 passed + 1 time-boundary flaky re-run green → 840 confirmed (the flaky is test_e2e_fake_provider.py, storage-signature x-expires second rollover, SQLite generation lane — unrelated to this task's files); ruff/format/mypy all green (148 files formatted, 61 source files typed); implementation round 16 red→green + static green + full 834, then session review fixes (1 P2 + 3 P3) re-verified: special 19 + full 837, then PR #47 Codex review fixes (3 P2: lease microsecond precision / DELETE idempotency key + envelope recovery / ledger escape corruption) re-verified: special 22 + static green + full 840 |
| **Evidence Level** | AUTOMATED_VERIFIED |
| **Security and Observability** | the device token reaches the database only as a keyed digest (rotation-window probing); logs and events carry identifiers only; IDOR answers 404 identically for missing and foreign devices; the INVALID/REVOKED 401 distinction is the §13.2 client wipe signal (tokens are 256-bit random, not enumerable); key misconfiguration answers 503 rather than a misleading 401/500; the unbind audit event carries the request id; the success log prints after commit |
| **Migration and Rollback** | no new migration (028's customer_devices / customer_session_state / customer_session_events schema fully ready); rollback = code revert (no schema change) |
| **External Authorization Record** | None; no real ZPay/COS/paid provider/external codes/gray release/public launch |
| **Untested Items** | device endpoints not behind the shared limiter (registered for the T19 review; device tokens are 256-bit, brute-force infeasible); thread-level same-key concurrent-DELETE proof (envelope ON CONFLICT + FOR UPDATE semantics cover it, T13/T14 precedent); T17 enroll wiring of next_free_slot; client OpenAPI regeneration (frontend-integration gate); STAGING/REAL_CHAIN/PRODUCTION |
| **Lore Commit SHA** | PR squash merge SHA |

### T16 Section 14 Ledger Record

```text
任务/工作包：T16 / DEV-01
Owner / Reviewer：后端/DB（Agent 执行）/ 会话内代码评审
分支 / 基线 SHA：feat/customer-v3-t16-device-slots-unbind / 基线 517e1d2（T15 PR #46 squash）
上游规格段落：客户版任务清单 V3 §3 T16、§12.3 DEV-01；代码开发清单 V3 §3.2 customer_device_service.py/customer_device_routes.py、§3.3 test_customer_devices.py 冻结名；激活码开发文档 §3.2 设备规则、§6.1 API 表、§12.4 fencing、§13.2 错误码；测试与验收规格 §2.2/§3.3
改动文件：server/app/customer_device_service.py（新增：跨密钥版本凭据解析、两槽视图、next_free_slot、unbind_device 原子会话吊销）、server/app/customer_device_routes.py（新增：Bearer 设备凭据鉴权、GET/DELETE 两路由、稳定错误码、503 fail-closed、PG 事务内时钟、提交后日志、幂等键+信封恢复）、server/tests/test_customer_devices.py（新增 22 用例，专用迁移库 t16_customer_devices_test）、server/app/main.py（路由挂载 +2 行）、docs/evidence/T16-EVIDENCE.md、任务与证据账本
失败测试或回归锁定：先红后绿 22 例——模块级 2（摘要确定性/槽数常量）+凭据与视图 7（三态 401、激活后槽视图）+解绑语义 7（槽复用+历史保留、原子会话吊销、404 缺失/IDOR、409 重复、他设备解绑自身不受扰）+第三设备阻断 2（next_free_slot 状态机 + PG 层 UniqueViolation/CheckViolation）+密钥轮换 1（V2 签发双版本可鉴权、V2 退役后 401）+评审锁定 3（PG fail-closed 503、密钥未配置 503 非 401、REVOKED 行 401）+幂等解绑 3（PR #47 Codex P2：无键 400+行未动、丢 204 同键重试重放零重执行、同键异目标 409）
实现结果：两当前设备槽+凭据+解绑历史+第三设备阻断落地应用层（028 schema 无新迁移）：设备凭据 Bearer 鉴权（keyed digest 跨版本探测，明文永不过库）；GET /api/customer/devices 返回两槽状态+释放历史；DELETE 解绑（BOUND→UNBOUND+unbound_at，行不删除，槽立即可复用——partial unique index 免疫 DEV-01 No-Go）携带强制 Idempotency-Key（PR #47 Codex P2：T14 信封引擎密封审计载荷，丢失 204 同键同目标可重放，预检在凭据鉴权前，同键异目标 409 IDEMPOTENCY_CONFLICT，无键 400）；解绑原子吊销所骑会话（epoch+1+立即过期租约（全微秒精度+GREATEST 1µs 兑底，PR #47 Codex P2）+LOGOUT device_unbound 事件）；两槽满 next_free_slot=None+数据库拒绝第三行；错误码 401 REQUIRED/INVALID/REVOKED、404（缺失=他人，无 IDOR 预言）、409 ALREADY_UNBOUND、503 fail-closed（无 PG/密钥未配置）；解绑时钟取 PG 事务内 now()（SES-01）
验证命令与通过数：专项 22 passed；全量 839 passed + 1 时间边界 flaky 单独复跑通过→ 840 确认（flaky 为 test_e2e_fake_provider.py 存储签名 x-expires 秒翻转，SQLite 生成 lane，与本任务文件无依赖）；ruff/format/mypy 全绿（148 files formatted，61 source files typed）；实现轮 16 红→绿 + 静态全绿 + 全量 834 后经会话内代码评审修复 1 P2+3 P3 复跑专项 19 + 全量 837，再经 PR #47 Codex 评审修复 3 P2（lease 微秒精度/DELETE 幂等键+信封恢复/账本转义）复跑专项 22 + 静态全绿 + 全量 840 复确认
证据层级：AUTOMATED_VERIFIED
安全与可观测性：设备凭据只以 keyed HMAC-SHA256 摘要过库（跨版本探测兼容轮换窗口）；日志与事件仅含标识符；IDOR 统一 404（缺失=他人同应答）；401 INVALID 与 REVOKED 的区分是 §13.2 客户端擦除信号（token 为 256-bit 随机+keyed digest，不可枚举构造）；密钥配置故障 503 而非误导 401/500；解绑审计事件携带 request_id；成功日志提交后打印
迁移与回滚：无新迁移（028 的 customer_devices/customer_session_state/customer_session_events schema 完全就绪）；回滚=还原代码（无 schema 变更）
外部授权记录：无；未调用真实 ZPay/COS/付费 Provider/对外发码/灰度/公网发布
未测试项：设备端点未接入共享限流（登记为 T19 评审项；设备 token 256-bit 不可暴破）；同键并发双 DELETE 线程级证明（信封 ON CONFLICT + FOR UPDATE 语义覆盖，T13/T14 同前例）；T17 enroll 接入 next_free_slot 的路由级联测；客户端 OpenAPI 重新生成（前端接入任务门禁）；STAGING/REAL_CHAIN/PRODUCTION
Lore 提交 SHA：见 PR squash 合并 SHA
```

## T17 — Second-Device Enroll, One-Shot Pairing & First-Device Approval (DEV-02)

| Field | Value |
| --- | --- |
| **Task ID** | T17 / DEV-02 |
| **Owner / Reviewer** | Backend (Agent) / Codex independent review (PR #49 REQUEST_CHANGES: 1 P1 + 6 P2 + 1 P3, all substantively fixed) + GitHub connector review (P1 approval lane not restricted to the first device, fixed) |
| **Branch / Base SHA** | feat/customer-v3-t17-second-device-pairing / base e30ea64 (main after PR #48; the branch merged origin/main — PR #48's 033–036 chain landed first, so the T17 migration was renumbered 033→037 and revision 034's canonical probe key was adopted) |
| **Date** | 2026-08-23 |
| **Verified Implementation SHA** | PR squash merge result (see docs/evidence/T17-EVIDENCE.md) |
| **Upstream Spec Sections** | Task list §4 T17, §12.3 DEV-02; code checklist §3.2 (frozen customer_device_service.py / customer_device_routes.py), §3.3 (frozen test_customer_devices.py), migration theme 037 (renumbered from 033 after PR #48's 033–036 chain landed on main first); dev doc §12.2 six-step contract, §6.1 API table, §13.2 error codes; acceptance spec §6 |
| **Failure Test or Regression Lock** | 30 cases: enroll contract 7 (mandatory Idempotency-Key; PENDING created bound to the candidate V2-keyed digest with shape-coupled columns empty; same-key retry returns the same pairing id with zero envelopes; unknown code → unified 400 PAIRING_UNAVAILABLE; ISSUED-never-activated code → the same unified 400; already-bound fingerprint → 409 USER_ALREADY_ACTIVATED; both slots BOUND → 409 DEVICE_SLOTS_FULL with no pairing row) + full six-step flow 3 (enroll → approve → enroll again answers 201 with slot-2 credentials, pairing CONSUMED with consumed_at/consumed_device_id, device row BOUND on slot 2 with the candidate's name/platform and the owner's user_id, and no second charge — 1 recharge order / 1 wallet transaction / 1 customer user; lost-201 retry replays the sealed credentials with zero re-execution; same key + different body → 409 IDEMPOTENCY_CONFLICT) + concurrency & expiry 5 (two approved rivals race the consume branch behind a 2-thread barrier: exactly one 201 + one 409 DEVICE_SLOTS_FULL, one CONSUMED row, two BOUND devices; lapsed PENDING flips EXPIRED and a fresh request is created; lapsed APPROVED restarts fresh with the lapsed approval kept visible in the audit; consumption against two BOUND slots answers 409 and the pairing row stays APPROVED) + approve contract 7 (missing Bearer → 401; happy path records approved_at + approved_by_device_id; missing / random / cross-code pairings all answer one 404 with the foreign pairing untouched — IDOR; repeated approval idempotent with identical body; after consumption → 409 PAIRING_ALREADY_CONSUMED; lapsed → 409 PAIRING_EXPIRED with the row flipped EXPIRED; a pairing naming the approver's own digest → 409 PAIRING_SELF_APPROVAL) + transferability 1 (a different fingerprint enrolling the same code gets its own PENDING pairing — the approved row stays APPROVED untouched and no second device row appears) + Codex review regression locks 6 (PR #49: mid-flight unbind — a stale authenticated device object fed to the service layer answers revoked with the pairing untouched, the P1; key rotation between 202 and 201 keeps the stored (digest, version) pair truthful; a lapsed APPROVED flips EXPIRED on the approve path and a fresh request + approval succeeds; the shape CHECK rejects a PENDING-with-approved_at and an EXPIRED-with-lone-approved_at; the partial unique blocks a second active row while EXPIRED/CONSUMED rows free the slot for reuse; the 037 downgrade refuses once pairing rows exist and downgrades symmetrically once emptied) + strengthened: the slots-full 409 leaves zero device_enroll envelopes and the very same key finishes the consumption once a slot is freed + GitHub review locks 2 (PR #49 connector P1: while the first device is bound the slot-2 device cannot approve — 403 PAIRING_APPROVER_FORBIDDEN with the pairing staying PENDING, the first device's own approval still works, and the authorization precedes the state machine; after the first device is unbound, the surviving slot-2 device is still refused — the T18 administrator verification is the only lane) |
| **Implementation Result** | The second-device enroll / first-device approval / one-shot pairing land as revision 037 (drafted as 033 on the 032 head, renumbered when PR #48's 033–036 chain merged to main first; Alembic order comes from down_revision, never the file name) plus the application layer: the pairing row binds the keyed digest of the candidate fingerprint (the raw value never reaches the database; the partial unique index on (code, digest) WHERE active keeps at most one active row); the four-state machine PENDING/APPROVED/CONSUMED/EXPIRED is shape-coupled by a CHECK whose EXPIRED arm requires the approval columns to arrive as a pair (PR #49 Codex P2); the single enroll route is state-driven two-phase (PENDING → 202 with no envelope so the key never burns; APPROVED → the consumption branch seals the 201 one-time credential with the T14 AEAD engine, operation device_enroll, scope = the candidate digest); the consumption runs under the code-row lock with the code's BOUND device rows locked after it (lock order code → devices → pairing, PR #49 Codex P2) and next_free_slot picking the empty slot — both slots full answers 409 with the pairing row kept APPROVED (an unbind inside the expiry window still consumes); concurrent slot-2 rivals produce exactly one winner (code-row serialization + the _PairingRaceLost loser re-reading the winner's row, now mirroring the winner's real status — PR #49 Codex P3); approve authenticates the first device via Bearer and re-locks the approver row to re-validate BOUND inside the transaction before touching the pairing row (the TOCTOU fix — a concurrent unbind winner yields the same 401 DEVICE_REVOKED a fresh request would get, PR #49 Codex P1), then validates the approver against activation_code_activations.first_device_id *before* the state machine (the approval lane belongs to the first currently-bound device; once it is unavailable the lane moves to the T18 administrator verification, never down to the surviving slot 2 — PR #49 GitHub connector P1, 403 PAIRING_APPROVER_FORBIDDEN for a non-first device, including re-approval of an APPROVED pairing), the state machine itself as the idempotency (no envelope by design); approval is not transferable (digest binding; a different fingerprint gets its own PENDING); the second device never re-charges (no wallet columns on the pairing table + full-flow count locks); key rotation between 202 and 201 keeps the stored (digest, version) pair truthful while the fresh token stays keyed with the current highest version (PR #49 Codex P2); lapsed APPROVED rows flip EXPIRED on both the lookup and approve paths, freeing the active index (PR #49 Codex P2); the enroll shares the activation lane's anti-enumeration surface |
| **Verification Command and Pass Count** | pytest tests/test_customer_devices.py → 52 passed (22 T16 + 30 T17); full suite → 870 passed zero regression (T16 baseline 840 + 30 new; the first full run surfaced the 033-FK TRUNCATE cascade: test_admin_activation_routes.py's fixture truncate had to list device_pairing_requests — 38 fixture errors → 38 passed after the fix, 862 pre-review); ruff/format/mypy all green (149 files formatted, 61 source files typed); head-assertion sweep 032→033 re-verified: 135 + 69 passed across the ten affected files; PR #49 Codex review (REQUEST CHANGES: 1 P1 + 6 P2 + 1 P3) — every finding substantively fixed with a regression lock, re-verified: special 50 + full 868 + static green; PR #49 GitHub connector review (P1: the approval lane was not restricted to the first device) — fixed with 2 regression locks, re-verified: special 52 + full 870 + static green; post-merge re-verification (origin/main merged, PR #48's 033–036 chain landed first, migration renumbered 033→037, revision 034's canonical probe key adopted): full suite 910 passed zero regression + ruff/format/mypy green (155/61) + npm run check frontend 324 tests/biome green + cargo fmt/check green |
| **Evidence Level** | AUTOMATED_VERIFIED |
| **Security and Observability** | the candidate fingerprint reaches the database only as a keyed HMAC-SHA256 digest (cross-version probing for the rotation window); pairing/approval/consumption logs carry identifiers only; missing and cross-code pairings answer one 404 (no IDOR oracle); the enroll shares the activation lane's anti-enumeration surface (unified 400 + audited failure events + alert threshold + constant delay; same activate:ip / activate:code budgets; a blocked IP mints no code-dimension row; malformed codes do not short-circuit the limiter); the one-time credential is a secrets.token_urlsafe(32) value stored only as a keyed digest and sealed with the T14 AEAD envelope (AAD binds operation/scope/key_digest); key misconfiguration answers 503 fail-closed; the pairing TTL and binding timestamps share the in-transaction PostgreSQL clock (SES-01) |
| **Migration and Rollback** | new PG-only revision 037_device_pairing_requests (four-state shape-coupled status, partial unique active index on (activation_code_id, candidate_fingerprint_hmac), downgrade refuses once pairing rows exist — approval-lineage audit evidence, the 027/028/032 guard precedent; SQLite early-returns, the 025–036 precedent; renumbered from 033 after the PR #48 origin/main merge, revising 036_low_review_constraint_guards; the consumption INSERT and the enroll bound-probe adopt revision 034's fingerprint_canonical cross-version probe key — the activation-route M2 precedent); rollback = downgrade 037 + code revert |
| **External Authorization Record** | None; no real ZPay/COS/paid provider/external codes/gray release/public launch |
| **Untested Items** | the admin-verification approval lane (T18 writes through the same state machine); login-dimension rate limiting (T19); client OpenAPI regeneration (T28 frontend gate); thread-level approve-vs-unbind concurrent proof (the FOR UPDATE re-validation semantics cover it); STAGING/REAL_CHAIN/PRODUCTION |
| **Lore Commit SHA** | PR squash merge SHA |

### T17 Section 14 Ledger Record

```text
任务/工作包：T17 / DEV-02
Owner / Reviewer：后端（Agent 执行）/ Codex 独立评审（1 P1+6 P2+1 P3 逐条实质修复）+ GitHub connector 评审（P1 批准权未限定首设备，已修复）
分支 / 基线 SHA：feat/customer-v3-t17-second-device-pairing / 基线 e30ea64（main，PR #48 合入后 T17 分支 merge origin/main：PR #48 的 033–036 链先落地，T17 迁移重编号 033→037，并采纳 034 canonical 探测键）
上游规格段落：客户版任务清单 V3 §4 T17、§12.3 DEV-02；代码开发清单 V3 §3.2 customer_device_service.py/customer_device_routes.py、§3.3 test_customer_devices.py 冻结名、迁移主题 037（原 033，PR #48 落地 033–036 链后重编号）；激活码开发文档 §12.2 六步契约、§6.1 API 表、§13.2 错误码；测试与验收规格 §6
改动文件：server/migrations/versions/037_device_pairing_requests.py（新增：四态状态机+形状耦合+partial unique active 索引+downgrade 守卫，PG-only，原 033 重编号）、server/app/customer_device_service.py（T17 小节：fingerprint_digests_for/lookup_active_pairing/create_pairing_request/approve_pairing_request/consume_pairing_request 含 fingerprint_canonical）、server/app/customer_device_routes.py（enroll 两阶段 202/201 路由+approve 路由，enroll 探测含 canonical、UniqueViolation 双约束名映射）、server/tests/test_customer_devices.py（+30 用例）、10 个测试文件 22 处 head 断言 036→037（合并 main 后重扫）、server/tests/test_admin_activation_routes.py（TRUNCATE 列表补 device_pairing_requests——037 FK 引用连锁，保持在 036 replica-role 守卫下）、docs/evidence/T17-EVIDENCE.md、任务与证据账本
失败测试或回归锁定：先红后绿 30 例——enroll 契约 7（幂等键必需/PENDING 创建绑定候选摘要/同键重试同 pairing_id 零信封/未知码统一 400/未激活码统一 400/已绑指纹 409/两槽满 409）+完整六步流 3（slot2 绑定+无充值计数锁定/丢 201 密封凭据重放/同键异参 409）+并发与过期 5（双线程 barrier slot2 单赢家 1×201+1×409+单 CONSUMED+双 BOUND/过期 PENDING 翻转新建/APPROVED 过期重启留审计/消费时槽满保持 APPROVED）+approve 契约 7（Bearer 必需/批准人 lineage/缺失跨码统一 404 IDOR/重复幂等/已消费 409/过期 409/self-approval 409）+不可转用 1（异指纹得自身 PENDING，批准行不动，无新设备行）+Codex 评审回归锁定 6（中途解绑 stale approver→revoked/202-201 间轮换存储真实 (digest,version) 对/过期 APPROVED 经 approve 翻转释放占用/形状 CHECK 拒半写批准列/partial unique 活跃阻塞+终态复用/037 downgrade 非空守卫+空库对称降级）+GitHub connector 评审锁定 2（首设备存活时 slot-2 不能批准 403+授权先于状态机，首设备解绑后 slot-2 仍被拒——T18 管理员核验是唯一通道）
实现结果：第二设备 enroll/第一设备批准/一次性配对落地（迁移 037+应用层）：配对行绑定候选指纹 keyed digest（明文永不过库，partial unique (code,digest) WHERE active 保单活跃行）；四态状态机 PENDING/APPROVED/CONSUMED/EXPIRED 形状耦合 CHECK；enroll 单路由状态驱动两阶段（PENDING→202 无信封不烧键，APPROVED→消费分支密封 201 一次性凭据）；消费持码行锁+next_free_slot 选空槽，两槽满 409 配对行保持 APPROVED（解绑后过期窗内仍可消费）；并发 slot2 恰一成功（码行锁串行化+_PairingRaceLost 输家重读赢家行）；approve Bearer 第一设备鉴权：事务内重锁 approver 验证 BOUND（TOCTOU，PR #49 Codex P1）+对照 activation_code_activations.first_device_id 验证首设备且先于状态机（GitHub connector P1：批准权限定首设备，首设备不可用时走 T18 管理员核验而非降级到 slot-2，非首设备含已 APPROVED 重批准一律 403 PAIRING_APPROVER_FORBIDDEN）+状态机即幂等（无 secret 无信封）；批准不可转用（绑定摘要+异指纹新 PENDING）；第二设备零充值（配对表无钱列+全流计数锁定）；防枚举复用 T15 维度预算（activate:ip/code 同池，malformed 不短路统一拒绝+审计+常数时延，IP blocked 不消费 code 维度）；只读回放预检免限流预算
验证命令与通过数：专项 52 passed（T16 22+T17 30）；全量 870 passed 零回归（T16 基线 840+新增 30；首轮暴露 033 FK 连锁：test_admin_activation_routes.py TRUNCATE 补 device_pairing_requests 后 38 errors→38 passed，评审前 862）；ruff/format/mypy 全绿（149 files formatted，61 source files typed）；head 断言迁移专项 135+69 复验通过；Codex 独立评审（REQUEST CHANGES：1 P1+6 P2+1 P3）逐条实质修复+回归锁定后专项 50/全量 868 复验；GitHub connector 评审 P1（批准权未限定首设备）修复+2 例回归锁定后专项 52/全量 870 复验；合并 main（PR #48 落地 033–036 链，迁移重编号 033→037+采纳 034 canonical）后全量复验 910 passed 零回归+ruff/format/mypy 全绿（155/61）+npm check 前端 324 tests/biome 绿+cargo fmt/check 绿
证据层级：AUTOMATED_VERIFIED
安全与可观测性：候选指纹只以 keyed HMAC-SHA256 摘要过库（跨版本探测）；配对/批准/消费事件日志仅含标识符；缺失=跨码统一 404（无 IDOR 预言）；enroll 与 activate 共享防枚举面（统一 400+失败审计+告警阈值+常数 PBKDF2 时延）；一次性凭据 secrets.token_urlsafe(32)+keyed digest 存储，AEAD 信封密封（AAD 绑定 operation/scope/key_digest）；密钥配置故障 503 fail-closed；配对过期与绑定时间戳共用 PG 事务内时钟（SES-01）
迁移与回滚：037 PG-only（SQLite early return，025-036 先例）；downgrade 非空守卫（配对行是批准 lineage 审计证据，027/028/032 先例）空库对称降级；回滚=降级 037+还原代码
外部授权记录：无；未调用真实 ZPay/COS/付费 Provider/对外发码/灰度/公网发布
未测试项：管理员核验批准通道（T18 经同一状态机写穿）；login/租约维度限流（T19）；客户端 OpenAPI 重新生成（T28 前端门禁）；approve-vs-unbind 双线程并发证明（FOR UPDATE 重新验证语义覆盖）；STAGING/REAL_CHAIN/PRODUCTION
Lore 提交 SHA：见 PR squash 合并 SHA
```

## T18 — Administrator Verified Approval, Unbind & Credential Revocation (DEV-03)

| Field | Value |
| --- | --- |
| **Task ID** | T18 / DEV-03 |
| **Owner / Reviewer** | Backend/Management (Agent) / independent code-review subagent (APPROVE: 0 P1 / 0 P2 / 2 P3, both substantively fixed — the deferred-409 same-key replay regression lock and the three-valued admin_lane label — with the re-verified 64-test run) + GitHub connector review on PR #50 (P2: the verification view must include the delivery records — fixed with a regression test, 65 re-verified) |
| **Branch / Base SHA** | feat/customer-v3-t18-admin-verified-unbind-revoke / base ed65a03 (main, PR #49 squash) |
| **Date** | 2026-08-23 |
| **Verified Implementation SHA** | PR squash merge result (see docs/evidence/T18-EVIDENCE.md) |
| **Upstream Spec Sections** | Task list §4 T18, §12.3 DEV-03; code checklist §3.2 (frozen admin_device_routes.py), §3.3 (frozen test_customer_devices.py), migration theme 038; dev doc §6.1/§6.2 management API tables, §9.2 device revocation atomic session invalidation, §12.2 step 3 admin verification lane (first device unavailable), §13.2 error codes, §15 real operator identity; acceptance spec §2 |
| **Failure Test or Regression Lock** | 13 cases: verification view 1 (the §12.2 step-3 evidence bundle: pairing + masked code + the delivery records (channel / external order / recipient / delivered-by) + activation fact + first-device status + admin_lane=CLOSED_FIRST_DEVICE_BOUND while the first device stays bound; unknown pairing → 404 — the PR #50 connector P2 regression lock) + gate & write contract 4 (no admin cookie → 401; auditor role → 403 AUDITOR_READ_ONLY even on a valid session; missing Idempotency-Key → 400; confirm=false or blank reason → 400 — zero audit rows, zero state change) + verified approval 3 (while the first device stays BOUND → 403 PAIRING_FIRST_DEVICE_AVAILABLE, the pairing stays PENDING with zero audit rows — the admin must not shortcut a live first device; after the first device is unbound → 200, the pairing APPROVED with approved_by_admin_user_id + exactly one PAIRING_ADMIN_APPROVED audit row carrying the real admin actor / target user / reason / request-id, and the candidate can then consume it — the full §12.2 step-3 recovery; repeated approval → 200 idempotent, no second audit row) + admin unbind 2 (releases the device UNBOUND + unbound_at with the slot freed and revokes the riding session with the administrator as the LOGOUT actor (epoch +1, lease pulled into the past) + one DEVICE_ADMIN_UNBOUND audit row; a second unbind → 409 DEVICE_ALREADY_RELEASED, unknown device → 404) + credential revocation 2 (writes the terminal REVOKED + revoked_at with unbound_at staying NULL (the 028 shape) + the riding-session revocation + one DEVICE_CREDENTIAL_REVOKED audit row; the released credential then answers 401 DEVICE_REVOKED on the customer lane — the client-side wipe signal) + idempotency 1 (the same key replays the unbind response X-Idempotent-Replay: true with one audit row and no double state change; the deferred-409 same-key replay answers the identical 409; the same key + a different body → 409 IDEMPOTENCY_CONFLICT) + migration invariants 2 (ck_admin_device_events_target_shape rejects an unbind event without a device and an approval event without a pairing; the 038 downgrade refuses once an audit row exists — the version stays at head through the single-transaction chain — and downgrades symmetrically once emptied, both tables truncated together behind the replica role because the 038 FK pairs them) |
| **Implementation Result** | The administrator verified approval / unbind / credential revocation lands as revision 038_admin_device_operations (the admin lineage column approved_by_admin_user_id on the frozen 037 pairing table, mutually exclusive with the device lane by the regenerated _APPROVAL_LINEAGE CHECK: approved_at non-null proves exactly one approver) plus the append-only admin_device_events audit table (event/target shape CHECK, 029 UPDATE/DELETE trigger + 036 shared TRUNCATE guard, downgrade refuses once audit rows or admin-approval lineage exist) and the application layer: the three write routes run behind the T09 admin session/CSRF/RBAC gate (AdminWriter role=admin, auditor 403) and the T12 shared admin write contract (Idempotency-Key + confirm=true + non-blank reason, contract violations 400 before any transaction opens) with the device lane's own 503 fail-closed code DEVICE_SERVICE_UNAVAILABLE (the newly parameterized unavailable_code — §13.2 keeps one code per domain); the real actor lands in the audit row from the authenticated AdminActor, never from the request body (dev doc §15); the approval lane locks the first_device_id row FOR UPDATE first (still BOUND → 403, a missing row counts as unavailable — the recovery lane) then re-locks the pairing row and replays the T17 state machine (not_found / already_consumed / expired with the lazy flip / already_approved → PENDING becomes APPROVED with approved_at + approved_by_admin_user_id + the PAIRING_ADMIN_APPROVED audit row; lock order devices → pairing, the tail of the enroll route's code → devices → pairing order); the admin unbind and the credential revocation reuse the T16 _revoke_session_riding_device core (epoch bump, past lease, LOGOUT event — extracted from the T16 unbind tail, now parameterized by the acting user so the administrator is the actor) with the revocation lane writing the terminal REVOKED state (the 028 shape); the expired 409 goes through the new DeferredHTTPWriteError — the idempotency layer snapshots the error response, commits the lazy PENDING/APPROVED → EXPIRED flip and re-raises after the commit (the replay answers the same 409), while side-effect-free branches (404/403/already-consumed/not-bound) keep the plain raise so the key stays free for a retry (the T12 precedent); reads are AdminReader (auditor-accessible): the device list (filters + bounded pagination, display metadata only) and the pairing verification view (code masked, activation fact, first-device status, admin_lane OPEN / CLOSED_FIRST_DEVICE_BOUND / CLOSED_NO_ACTIVATION) — the write path re-validates under lock |
| **Verification Command and Pass Count** | pytest tests/test_customer_devices.py → 65 passed (22 T16 + 30 T17 + 13 T18, incl. the review-added deferred-409 same-key replay lock and the verification-view evidence-bundle lock); full suite → 923 passed zero regression (T17 re-verified baseline 910 + 13 new; the first full run surfaced the 038 FK/TRUNCATE chain — test_admin_activation_routes.py's fixture truncate had to list admin_device_events — and the head-assertion sweep 037→038: 22 sites across ten files incl. the "Twelve steps" chain comments; the sqlite→PG import/reconcile suite then needed admin_device_events registered in PG_ONLY_TABLES — 5 failures → 35 passed after the fix); ruff/format/mypy all green (157 files formatted, 62 source files typed); npm run check full-repo gate green (secret scan + client biome/vitest/tsc + e2e + cargo fmt/check + server static + the full suite as its final step) |
| **Evidence Level** | AUTOMATED_VERIFIED |
| **Security and Observability** | admin writes are admin-role-only (cookie + CSRF + write-method checks); actor/reason/request-id flow into the audit rows and logs end-to-end; the audit table is append-only (UPDATE/DELETE trigger refuses, TRUNCATE guard refuses); fingerprints and token digests never leave the store (the list and verification views carry display metadata and states only); the idempotency snapshot layer replays or 409-conflicts by request hash; key-configuration failures answer 503 fail-closed; the unified 404 gives no cross-user enumeration oracle; the revoked credential answers 401 DEVICE_REVOKED — the client-side wipe signal |
| **Migration and Rollback** | new PG-only revision 038_admin_device_operations (approved_by_admin_user_id FK users + admin_device_events with the event/target shape CHECK; downgrade refuses once any audit row exists or any pairing carries an admin approval — the operator lineage must survive any rollback; SQLite early-returns, the 025–037 precedent); rollback = downgrade 038 + code revert |
| **External Authorization Record** | None; no real ZPay/COS/paid provider/external codes/gray release/public launch |
| **Untested Items** | a dedicated admin-lane rate limit (the T37/OPS-02 hardening pass); the T33 management UI consuming these APIs (frontend task); real ops-ticket integration (a human process); thread-level admin-vs-customer concurrent approval proof (the FOR UPDATE serialization covers it); STAGING/REAL_CHAIN/PRODUCTION |
| **Lore Commit SHA** | PR squash merge SHA |

### T18 Section 14 Ledger Record

```text
任务/工作包：T18 / DEV-03
Owner / Reviewer：后端/管理（Agent 执行）/ 独立评审子代理（APPROVE：0 P1/0 P2/2 P3，均实质修复后专项 64 复验通过）+ GitHub connector 评审（PR #50 P2：核验视图须含发放记录，已修复+回归锁定，专项 65 复验通过）
分支 / 基线 SHA：feat/customer-v3-t18-admin-verified-unbind-revoke / 基线 ed65a03（main，PR #49 squash）
上游规格段落：客户版任务清单 V3 §4 T18、§12.3 DEV-03；代码开发清单 V3 §3.2 admin_device_routes.py 冻结名、§3.3 test_customer_devices.py 冻结名、迁移主题 038；激活码开发文档 §6.1/§6.2 管理端 API 表、§9.2 设备撤销原子吊销、§12.2 step 3 首设备不可用的管理员核验通道、§13.2 错误码、§15 管理端真实操作人；测试与验收规格 §2
改动文件：server/migrations/versions/038_admin_device_operations.py（新增：approved_by_admin_user_id+APPROVAL_LINEAGE 状态形状重生成+admin_device_events 审计表+事件/目标形状 CHECK+append-only+TRUNCATE guard+downgrade 守卫，PG-only）、server/app/customer_device_service.py（T18 小节：_revoke_session_riding_device 提取共享+_insert_admin_device_event+admin_approve_pairing_request/admin_unbind_device/revoke_device_credential）、server/app/admin_device_routes.py（新增冻结名：设备列表+配对核验视图+approve/unbind/revoke-credential 三写路由）、server/app/admin_activation_routes.py（DeferredHTTPWriteError+_write_with_idempotency 支持 deferred 409 提交后重抛+unavailable_code 参数化）、server/app/main.py（挂载+2 行）、server/scripts/reconcile_customer_billing.py（PG_ONLY_TABLES 注册 admin_device_events——T07 导入源无 SQLite 对应表，空表预期/非空仍 fail-closed）、server/tests/test_customer_devices.py（+13 用例，admin 会话 helper、route_state TRUNCATE 补 admin_device_events、_insert_pairing_row 补 approved_by_admin_user_id、037 downgrade 锁定测试适配 038 head）、server/tests/test_admin_activation_routes.py（fixture TRUNCATE 补 admin_device_events——038 FK 引用连锁）、10 个测试文件 22 处 head 断言 037→038（含 3 处链注释 Twelve steps）、docs/evidence/T18-EVIDENCE.md、任务与证据账本
失败测试或回归锁定：先红后绿 13 例——核验视图 1（§12.2 step-3 证据包：配对+掩码码+发放记录（channel/external_order/recipient/delivered-by）+激活事实+首设备状态+首设备存活时 admin_lane=CLOSED_FIRST_DEVICE_BOUND；未知配对 404（PR #50 connector P2 回归锁定））+门禁与写契约 4（无 cookie 401/auditor 403 只读/缺幂等键 400/confirm=false 或空 reason 400 且零审计行零状态变化）+核验批准 3（首设备存活 403 配对保持 PENDING 零审计行/首设备解绑后 200 approved_by_admin_user_id+PAIRING_ADMIN_APPROVED 审计行（actor/target/reason/request-id）+候选随后可消费/重复批准 200 幂等无第二审计行）+管理解绑 2（UNBOUND+unbound_at+骑乘会话吊销 actor=管理员 epoch+1 lease 过去+DEVICE_ADMIN_UNBOUND 审计行/二次解绑 409+未知设备 404）+凭据撤销 2（REVOKED+revoked_at 且 unbound_at 保持 NULL（028 形状）+骑乘会话吊销+DEVICE_CREDENTIAL_REVOKED 审计行/撤销后凭据在客户道 401 DEVICE_REVOKED）+幂等 1（同键重放 X-Idempotent-Replay+单审计行+无双重状态变化+deferred 409 同键重放同 409/同键异参 409）+迁移不变量 2（ck_admin_device_events_target_shape 拒无 device 的 unbound 事件/038 downgrade 有审计行拒绝版本保持 head+清空后对称降级）
实现结果：管理员核验批准/解绑/凭据撤销落地（迁移 038+应用层）：三写路由全部走 T09 admin 会话/CSRF/RBAC 门（AdminWriter role=admin，auditor 403）+T12 共享写契约（幂等键+confirm+reason，契约违规在事务开启前 400）+幂等快照层（设备域自有 503 DEVICE_SERVICE_UNAVAILABLE）；真实 actor 从认证态 AdminActor 落审计行（永不取自请求体）；每成功变更恰一条 append-only admin_device_events（029 UPDATE/DELETE 触发器+036 共享 TRUNCATE guard）；批准通道先锁 first_device_id 行 FOR UPDATE（BOUND→403 不短路存活首设备，缺失→恢复通道）再锁配对行复用 T17 状态机（approved_by_admin_user_id lineage 与设备批准互斥）；解绑/撤销复用 T16 会话吊销核心（actor 参数化=管理员）；过期 409 经 DeferredHTTPWriteError 提交后重抛（lazy 翻转保留+同键重放同 409，无副作用分支普通 raise 回滚键保持可重试）
验证命令与通过数：专项 65 passed（T16 22+T17 30+T18 13，含评审修复后新增 deferred 409 同键重放锁定+核验视图证据包锁定）；全量 923 passed 零回归（T17 重验基线 910+新增 13；首轮暴露 038 FK/TRUNCATE 连锁——test_admin_activation_routes.py 补 admin_device_events+head 断言扫 22 处（含 Twelve steps 链注释）+sqlite→PG 对账套件 PG_ONLY_TABLES 注册 admin_device_events 后 5 failed→35 passed）；ruff/format/mypy 全绿（157 files formatted，62 source files typed）；npm check 全仓门禁绿（secret 扫描+前端 biome/vitest/tsc+e2e+cargo fmt/check+服务端静态+全量为末步）；独立评审子代理 APPROVE（0 P1/0 P2/2 P3：deferred 409 同键重放回归锁定+admin_lane 三分支标签 CLOSED_NO_ACTIVATION，均已修复复验）；GitHub connector 评审 P2（核验视图含发放记录 activation_code_deliveries——channel/external_order_ref/recipient_ref/delivered_by_user_id/delivered_at 时间序，§12.2 step-3 发放证据完整，表内无明文码）已修复+回归锁定复验
证据层级：AUTOMATED_VERIFIED
安全与可观测性：管理写仅 admin 角色（cookie+CSRF+写方法校验）；actor/reason/request-id 全链路入审计与日志；审计表 append-only（UPDATE/DELETE 触发器拒绝+TRUNCATE guard）；指纹与 token 摘要永不出库（列表/核验视图仅显示元数据与状态）；幂等快照层按请求哈希重放或 409；密钥配置故障 503 fail-closed；统一 404 无跨用户枚举预言；撤销后凭据 401 DEVICE_REVOKED（客户端擦除信号）
迁移与回滚：038 PG-only（SQLite early return，025-037 先例）；downgrade 有审计行或管理批准 lineage 时拒绝（操作人审计必须存活），空库对称降级；回滚=降级 038+还原代码
外部授权记录：无；未调用真实 ZPay/COS/付费 Provider/对外发码/灰度/公网发布
未测试项：管理道独立限流（T37/OPS-02 安全硬化统一收口）；T33 管理端页面消费这些 API（前端任务）；真实运维工单系统联动（人工流程）；管理-vs-客户双线程并发批准证明（FOR UPDATE 串行化覆盖）；STAGING/REAL_CHAIN/PRODUCTION
Lore 提交 SHA：见 PR squash 合并 SHA
```
