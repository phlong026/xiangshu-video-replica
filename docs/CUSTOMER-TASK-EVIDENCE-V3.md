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

## T19 — Session Login, Heartbeat, Logout & the 30/90-Second Database Lease (SES-01)

| Field | Value |
| --- | --- |
| **Task ID** | T19 / SES-01 |
| **Owner / Reviewer** | Backend (Agent) / Qoder CodeReview subagent + security self-review (conclusions in the Section 14 record and the commit message) + GitHub connector review on PR #51 (P2: the envelope recovery-window verdict must use the in-transaction PostgreSQL clock — fixed with a regression lock, 35 re-verified) |
| **Branch / Base SHA** | feat/customer-v3-t19-session-lease / base ed65a03 (main after PR #49) |
| **Date** | 2026-08-23 |
| **Verified Implementation SHA** | PR squash merge result (see docs/evidence/T19-EVIDENCE.md) |
| **Upstream Spec Sections** | Task list §4 T19, §12.3 SES-01; code checklist §3.2 (frozen customer_session_service.py / customer_session_routes.py), §3.3 (frozen test_customer_sessions.py); dev doc §12.3 login state machine, §6.1 API table, §6.3 idempotency, §13.2 error codes; acceptance spec §2.3 / §3.4 |
| **Failure Test or Regression Lock** | 35 cases (29 fail-first + 6 review regression locks): module units 3 (the frozen 90-second lease constant; device-name masking incl. empty/short names) + login contract gates 5 (missing Bearer → 401 DEVICE_CREDENTIAL_REQUIRED; unknown credential → 401 DEVICE_CREDENTIAL_INVALID; released credential → 401 DEVICE_REVOKED; missing Idempotency-Key → 400; login:ip budget 2 → the third attempt 429 RATE_LIMITED with Retry-After) + §12.3 state machine 5 (same device + valid session token → 200 renewal with identical token/epoch/session_id and the lease pushed out; same device without a usable token → 201 recovery, epoch 2, fresh token, the stale token's heartbeat 401 SESSION_REPLACED; another device online → 409 OTHER_DEVICE_ONLINE with the masked hint and zero database changes — the full names "Office"/"MacBook" never appear in the response and no LOGIN event is recorded; lapsed lease → 201 takeover, epoch 2, the system TIMEOUT event with actor_user_id NULL, the stale token 401 SESSION_REPLACED; missing session row → defensive epoch-1 establish) + idempotency envelopes 3 (a lost 201 replays with the same token/epoch and X-Idempotent-Replay with zero second LOGIN events; same key + different body → 409 IDEMPOTENCY_CONFLICT; a business 409 rolls back with the transaction — the same key succeeds after the lease actually lapses) + heartbeat 5 (missing Bearer → 401 SESSION_TOKEN_REQUIRED; renewal keeps epoch and session_id aligned with the database row; forged token → 401 SESSION_REPLACED; lapsed lease → 401 SESSION_EXPIRED with no HEARTBEAT event and no resurrection; post-logout heartbeat → 401 SESSION_EXPIRED) + logout 6 (missing Idempotency-Key → 400; the lease lands in the past with the LOGOUT event on the audit trail; the other device logs in immediately (201); a lost 204 replays via the sealed envelope with no second LOGOUT event; a late logout after takeover → 401 SESSION_REPLACED leaving the new session's device and lease byte-identical; a lapsed lease → 401 SESSION_EXPIRED) + concurrency 1 (two threads race from a lapsed state behind the same fixture: exactly one 201 + one 409, epoch advanced exactly once to 2, one current device, the live lease in the future) + credential red line 1 (the append-only event table never contains the plaintext session or device tokens) + review regression locks 6 (login:ip env non-positive/garbage values fall back to the safe default 10 while a valid 7 applies; a fully validated idempotent replay spends zero rate-limit budget — budget exhausted → fresh key 429 → same-key retry still replays the cached 201; heartbeat without device-domain keys answers 503 SESSION_SERVICE_UNAVAILABLE; user-driven LOGIN/HEARTBEAT/LOGOUT events record the acting user while only the system TIMEOUT stays actor-less; two concurrent first-writers on a missing row both answer 201 with exactly one LOGIN event each — the loser re-drives the winner's committed row, never a 500; an API node whose process clock runs a decade ahead of PostgreSQL still replays a still-valid lost-response 201 — the PR #51 connector P2 lock (the recovery-window verdict uses the in-transaction SELECT now(), never the process clock)) |
| **Implementation Result** | The §12.3 single-online session lands on revision 029's structures (no new migration): login locks the user's single customer_session_state row FOR UPDATE **scoped to the authenticated user_id** and decides renewal (same token/epoch, 200) / recovery (epoch + 1, fresh token) / takeover (epoch + 1 + the system TIMEOUT event bound to the timed-out session's binding columns) / conflict (409 with the masked device hint + remaining lease, zero writes — never a silent kick); heartbeat locates the row by the presented token's digests probed across every configured key version (the PR #44 rotation-window rule) and renews the lease with epoch untouched, answering one unified 401 SESSION_REPLACED for forged and replaced tokens (no oracle) and a terminal 401 SESSION_EXPIRED under a lapsed lease (never resurrected); logout reuses the T16 unbind pattern — GREATEST(now, created_at + 1µs) with the full microsecond precision of the transaction clock (the PR #47 P2 lesson) plus a reason-tagged LOGOUT event, releasing the slot immediately; login/logout carry mandatory Idempotency-Keys with the replay probe **before** authentication (the unbind precedent), the envelope scope probed across key versions (the enroll precedent), business failures rolling back so the key stays reusable, and the sealed login payload carrying the state-machine outcome so replays reproduce the original 200/201; login draws the T15 shared login:ip budget (env VIDEO_REPLICA_RATE_LIMIT_LOGIN_IP, default 10) answering 429 + Retry-After; stable error codes per §13.2 plus the T16 REQUIRED/INVALID precedent; PG/AEAD/device-key misconfiguration answers 503 fail-closed (never a misleading 401 that would wipe the client's stored credentials); the independent CodeReview audit (REQUEST_CHANGES: 1 P1 + 2 P2 + 5 P3, all substantively fixed) added — all three routes sample SELECT now() inside the business transaction and pass it down as now= (the SES-01 clock discipline, the unbind/activation precedents; the P1), the fully validated replay probe precedes the login:ip limiter (the activate T15-review rule), the defensive no-row first-write races INSERT ... ON CONFLICT (user_id) DO NOTHING with the loser re-reading the winner's committed row and re-driving the state machine, and user-driven LOGIN/HEARTBEAT/LOGOUT events carry actor_user_id (only the system TIMEOUT stays actor-less); the PR #51 connector P2 extended the same clock discipline to the envelope recovery-window verdict — _find_envelope samples the PostgreSQL now() in the envelope-read transaction so a skewed API node neither rejects a still-valid replay nor accepts an expired one |
| **Verification Command and Pass Count** | pytest tests/test_customer_sessions.py → 35 passed (29 fail-first + 6 review regression locks); full suite → 958 passed zero regression (T18 merged base 923 + 35 new; the 2 warnings are the pre-existing httpx deprecation and a Windows GBK subprocess-reader artifact); ruff check → all green; ruff format --check → 160 files already formatted; mypy app → success, 64 source files; the independent CodeReview audit returned REQUEST_CHANGES (1 P1 + 2 P2 + 5 P3) — every finding substantively fixed with a regression lock where testable (R1-R8 tabulated in docs/evidence/T19-EVIDENCE.md), re-verified by the special 35 + full 958 runs; the PR #51 connector P2 (envelope recovery-window clock) fixed with a regression lock and re-verified on the same runs; the in-development defects caught by the real PG run (missing user_id row-lock scope, whole-second heartbeat CHECK violation, the text-vs-timestamptz GREATEST, the TIMEOUT event's binding columns) are each documented there as well |
| **Evidence Level** | AUTOMATED_VERIFIED |
| **Security and Observability** | session tokens reach the database only as keyed digests (cross-version probing); the 409 conflict hint masks the device name (first two characters + "**"; empty stays empty); user-driven LOGIN/HEARTBEAT/LOGOUT events record the acting user (only the system TIMEOUT carries no actor); the event table is append-only (029 triggers refuse UPDATE/DELETE) with a test locking that no plaintext credential ever appears; the envelope stores the AEAD ciphertext (AAD binds operation/scope/key_digest) and the request hash covers the session token's sha256 — never the raw secret; the lease verdict and the envelope recovery window share the in-transaction PostgreSQL clock (the client's local time is never a truth source; the PR #51 connector P2 lock proves a decade-ahead process clock still replays); forged and replaced tokens get one indistinguishable 401; key misconfiguration answers 503 fail-closed per the §13.2 client contract |
| **Migration and Rollback** | No new migration — revision 029 (T13) already provides customer_session_state (epoch-monotonic trigger), customer_session_events (append-only) and customer_idempotency_envelopes; rollback = code revert (session rows may remain; lapsed leases release naturally) |
| **External Authorization Record** | None; no real ZPay/COS/paid provider/external codes/gray release/public launch |
| **Untested Items** | explicit atomic switch and session-epoch fencing (T20 SES-02/SES-03); in-transaction fencing on the business write routes (T21 SES-04/SES-05); the client-side 30-second heartbeat contract and OpenAPI regeneration (T28 frontend gate); process-level proof of the multi-API-instance same-lease race (the PG row-lock semantics cover it, the T13 100-concurrency precedent); STAGING/REAL_CHAIN/PRODUCTION |
| **Lore Commit SHA** | PR squash merge SHA |

### T19 Section 14 Ledger Record

```text
任务/工作包：T19 / SES-01
Owner / Reviewer：后端（Agent 执行）/ Qoder CodeReview 子代理 + 安全自评审（结论见 commit message）+ PR #51 chatgpt-codex-connector 二轮评审（1 P2：信封 recovery 窗口时钟，已修复含回归锁定）
分支 / 基线 SHA：feat/customer-v3-t19-session-lease / 基线 ed65a03（main，PR #49 合入后）
上游规格段落：客户版任务清单 V3 §4 T19、§12.3 SES-01；代码开发清单 V3 §3.2 customer_session_service.py/customer_session_routes.py、§3.3 test_customer_sessions.py 冻结名；激活码开发文档 §12.3 登录状态机、§6.1 API 表、§6.3 幂等、§13.2 错误码；测试与验收规格 §2.3/§3.4
改动文件：server/app/customer_session_service.py（新增：login/heartbeat/logout 状态机+租约常量+脱敏+事件写入）、server/app/customer_session_routes.py（新增：三路由+Bearer 设备/会话双层鉴权+幂等信封+login:ip 限流+稳定错误码）、server/app/main.py（挂载 customer_session_router）、server/app/security_rate_limit.py（login_ip_limit() 对齐 _positive_int_env 先例，评审 P3 修复）、server/tests/test_customer_sessions.py（新增 35 用例：29 fail-first+6 评审回归锁定）、docs/evidence/T19-EVIDENCE.md、任务与证据账本
失败测试或回归锁定：35 例（29 fail-first+6 评审回归锁定）——模块单元 3（90 秒租约常量冻结/设备名脱敏 2 含空名与短名）+login 契约门 5（Bearer 必需/未知凭据/已释放凭据 401 DEVICE_REVOKED/幂等键必需/login:ip 限流 429+Retry-After）+§12.3 状态机 5（同设备有效 token 续租 200 同 token 同 epoch 同 session_id/同设备无 token 恢复 epoch+1 旧 token 401 SESSION_REPLACED/异设备在线 409 脱敏提示零库变更且全名不出现零 LOGIN 事件/租约过期接管 TIMEOUT actor NULL 旧 token 401/无行防御 epoch-1）+幂等信封 3（丢响应重放同 token 同 epoch 零第二 LOGIN/同键异参 409/业务 409 不烧键过期后同键成功）+heartbeat 5（Bearer 必需/续租 epoch 不变 session_id 对齐 DB 行/伪造 token 401 SESSION_REPLACED/过期租约 401 SESSION_EXPIRED 不复活无事件/logout 后 401）+logout 6（幂等键必需/租约置过去+LOGOUT 事件/对方设备立即可登录/丢响应重放 204+REPLAY 头无第二 LOGOUT/接管后迟到 logout 401 不触碰新会话 lease 不变/过期租约 401）+并发 1（双线程 lapsed 起单赢家 201+409 epoch 恰 2 单当前设备 lease 未来）+红线 1（事件表无明文凭据）+评审回归锁定 5（login:ip env 非正值/乱码回落安全默认 10 而合法 7 生效/完全校验幂等重放零限流预算——预算耗尽→新键 429→同键重放仍还原 201/缺设备域密钥 heartbeat 503 SESSION_SERVICE_UNAVAILABLE/用户驱动事件记录 actor_user_id 仅 TIMEOUT 无 actor/缺行并发双首写均 201 恰一条 LOGIN——输家重读赢家行重驱状态机永不 500/信封 recovery 窗口判定用事务内 PG 时钟——进程时钟偏移十年仍可重放密封 201，PR #51 connector P2）
实现结果：§12.3 单在线会话落地（029 结构，无新迁移）：login 按认证 user_id 加 FOR UPDATE 行锁（修复了无 user_id 过滤锁错行的缺陷）后判定续租（同 token 同 epoch 200）/恢复（epoch+1 新 token）/接管（epoch+1+系统 TIMEOUT 事件绑定旧会话列）/冲突（409 脱敏设备名+剩余租约零写入，永不静默踢人）；heartbeat 跨密钥版本 digest 探测定位行锁，epoch 不变续租，伪造与被替换 token 统一 401 SESSION_REPLACED（无预言机），lapsed 401 SESSION_EXPIRED 不复活；logout 复用 T16 unbind 模式 GREATEST(now, created_at+1µs) 全微秒精度事务时钟（PR #47 P2 教训）+reason=user_logout LOGOUT 事件，槽位立即释放；login/logout 必带幂等键：鉴权前重放探测（unbind 先例）、scope 跨版本探测（enroll 先例）、业务 409 随事务回滚不烧键、密封 payload 携带 outcome 还原 200/201；login:ip 共享限流（T15 计数器，默认 10/窗口，429+Retry-After）；错误码对齐 §13.2+T16 先例；密钥/PG 故障 503 fail-closed（不误报 401 触发客户端擦凭据）；CodeReview 独立评审（REQUEST_CHANGES：1 P1+2 P2+5 P3）逐条实质修复：三路由业务事务内采样 SELECT now() 以 now= 传入（SES-01 时钟纪律，unbind/激活先例，P1）、完全校验的重放探测先于 login:ip 限流（activate T15 评审规则）、缺行防御首写 INSERT ON CONFLICT (user_id) DO NOTHING 输家重读赢家已提交行重驱状态机、用户驱动 LOGIN/HEARTBEAT/LOGOUT 事件记录 actor_user_id、login_ip_limit() 对齐 _positive_int_env 语义
验证命令与通过数：专项 35 passed；全量 958 passed 零回归（T18 合并后基线 923+新增 35；2 个既存 warning：httpx deprecation+Windows GBK subprocess reader）；ruff/format/mypy 全绿（160 files formatted，64 source files typed）；CodeReview 评审 REQUEST_CHANGES（1 P1+2 P2+5 P3）逐条实质修复含 5 例回归锁定+PR #51 connector P2（信封 recovery 窗口时钟）修复含 1 例回归锁定后专项 35+全量 958 复确认；开发中真实 PG 运行揭出的缺陷（行锁缺 user_id/heartbeat 整秒截断违反 CHECK/GREATEST 类型错位/TIMEOUT 事件绑定列错位）均修复并记入 T19-EVIDENCE.md
证据层级：AUTOMATED_VERIFIED
安全与可观测性：session token 只以 keyed digest 过库（跨版本探测）；409 脱敏（首两字符+**，空名保持空）；用户驱动 LOGIN/HEARTBEAT/LOGOUT 事件记录 actor_user_id，仅系统 TIMEOUT 无 actor；事件表 append-only（029 触发器）且测试锁定无明文凭据；信封 AEAD 密文（AAD 绑定 operation/scope/key_digest）+request_hash 用 sha256 替代明文 secret；租约判定与信封 recovery 窗口共用事务内 PG 时钟（客户端本地时间非真源；connector P2 回归锁定证明进程时钟偏移十年仍可重放）；伪造与被替换 token 统一 401 无预言机；密钥配置故障 503 fail-closed
迁移与回滚：无新迁移（029 已预留全部结构）；回滚=还原代码（会话行可保留，租约到期自然释放）
外部授权记录：无；未调用真实 ZPay/COS/付费 Provider/对外发码/灰度/公网发布
未测试项：显式原子 switch 与 epoch fencing（T20）；业务写路由事务内 fencing（T21）；客户端 30 秒心跳合同与 OpenAPI 重新生成（T28）；多 API 实例同租约竞态进程级证明（PG 行锁语义覆盖，T13 100 并发先例）；STAGING/REAL_CHAIN/PRODUCTION
Lore 提交 SHA：见 PR squash 合并 SHA
```

## T20 — Explicit Atomic Switch & Session-Epoch Fencing (SES-02/SES-03)

| Field | Value |
| --- | --- |
| **Task ID** | T20 / SES-02 / SES-03 |
| **Owner / Reviewer** | Backend (Agent) / PR #52 chatgpt-codex-connector review (1 P1 + 2 P2, all substantively fixed with 4 regression locks: the code-status gate FOR UPDATE serialization, the clock_timestamp() post-lock lease verdict, and the expected_lease_until snapshot re-comparison) + security self-review |
| **Branch / Base SHA** | feat/customer-v3-t20-session-switch-fencing / base 939c305 (main, T19 PR #51 squash) |
| **Date** | 2026-08-23 |
| **Verified Implementation SHA** | PR squash merge result (see docs/evidence/T20-EVIDENCE.md) |
| **Upstream Spec Sections** | Task list §4 T20, §12.3 SES-02/SES-03; code checklist §9.2 (frozen customer_auth.py), §3.3 (frozen test_customer_fencing.py); dev doc §12.3 fifth line (explicit switch), §12.4 (in-transaction fencing), §6.1 API table, §6.3 idempotency, §13.2 error codes; acceptance spec §2.3 / §3.4 |
| **Failure Test or Regression Lock** | 36 cases (19 in test_customer_sessions.py + 17 in test_customer_fencing.py): switch cases 13 (explicit atomic takeover of a live other-device lease — SWITCH event + epoch bump + fresh token committed together, the displaced token fenced everywhere; same-device renewal only; same-device recovery epoch+1; lapsed takeover with TIMEOUT event; missing-row establish; missing Bearer → 401; unknown credential → 401; missing Idempotency-Key → 400; lost-response replay returns the same token/epoch; switch draws the login:ip budget — spent budget → 429 Retry-After; two concurrent switches serialize to one winner; no wallet charge; suspended code → 403 CODE_SUSPENDED) + code-gate/revocation-propagation 6 (a suspended/revoked code never establishes a session; the verifier rejects a suspended code / a revoked code / a released device even with a live lease; the admin suspend/revoke paths propagate the session revocation in the same transaction) + fencing 13 (minimal six-field context; unknown token; replaced token after switch; lapsed lease; logged-out session; suspended code with a live lease; revoked code with a live lease; released device with a live lease; expected session_id / epoch / device / user re-comparison each fenced; never leaks the token or digest) + PR #52 review regression locks 4 (the matching expected lease snapshot still verifies; a changed expected lease → SESSION_REPLACED even with the epoch unchanged; a verifier that waits on the row lock past a 2 s lease answers SESSION_EXPIRED — the clock_timestamp() verdict; a concurrent write to the code row blocks on the gate's FOR UPDATE until establishment commits — the code-status serialization) |
| **Implementation Result** | The explicit atomic switch lands as `POST /api/customer/sessions/switch`: `switch_session` drives the §12.3 state machine with `takeover=True` — only the explicit confirmed client flow reaches the takeover branch, a plain login still answers 409 (the no-auto-kick red line), and the SWITCH event + epoch bump + fresh token commit in one transaction so the displaced token answers 401 SESSION_REPLACED on every API instance; the switch reuses the login idempotency machinery with its own `session_switch` envelope operation (distinct AAD) and draws the same login:ip budget (not bypassable by switching). The SES-03 propagation generalizes the T16 session-revocation core into `customer_session_service.revoke_session` (epoch bump + GREATEST lease pull + reason-tagged LOGOUT event; device-scoped for the T16/T18 unbind/revoke delegation, user-wide for the admin code suspend/revoke paths which now call it so a disabled account loses its session in the same transaction) plus the code-status gate (403 CODE_SUSPENDED/CODE_REVOKED on login and switch — a disabled account never establishes a session). The new `customer_auth.verify_session_context` is the in-transaction fencing verifier for the T21 write routes: it row-locks the live session, re-compares the request's expected user/device/session/epoch against the row (a switch in between fences the stale write), judges the lease on the transaction's PostgreSQL clock, and re-checks the activation-code and device status (defence in depth), answering stable SESSION_REPLACED / SESSION_EXPIRED; the returned `CustomerSessionContext` is exactly six fields and never carries a credential |
| **Verification Command and Pass Count** | pytest tests/test_customer_sessions.py tests/test_customer_fencing.py → 71 passed (54 session + 17 fencing incl. the 4 PR #52 locks); full suite → 994 passed zero regression (990 + 4 review locks); ruff check → all green; ruff format --check → 162 files already formatted; mypy app → success, 65 source files |
| **Evidence Level** | AUTOMATED_VERIFIED |
| **Security and Observability** | login/switch share the code-status gate (a suspended/revoked account never establishes a session); the revocation paths propagate through the shared revoke_session core (device unbind/revoke and admin code suspend/revoke) with epoch bump + past lease + LOGOUT event naming the acting user — server-side revocation, never a client-side token wipe; the fencing verifier serializes switch/takeover/revocation writes with a row lock and re-checks the authority chain in-transaction; expected_* re-comparison blocks stale writes (epoch can never come back); tokens reach the database only as keyed digests and the verifier context never carries credential material; every new lease/revocation judgment uses the in-transaction PostgreSQL clock (SES-01); stable error codes (401 SESSION_REPLACED/EXPIRED, 403 CODE_SUSPENDED/REVOKED, 409 OTHER_DEVICE_ONLINE, 429 RATE_LIMITED, 503 fail-closed) |
| **Migration and Rollback** | No new migration — revision 029 (T13) already provides customer_session_state / customer_session_events / customer_idempotency_envelopes; rollback = code revert (session rows may remain; lapsed leases release naturally) |
| **External Authorization Record** | None; no real ZPay/COS/paid provider/external codes/gray release/public launch |
| **Untested Items** | the fencing verifier wired into the business write routes (T21 SES-04/SES-05); the client switch-confirmation flow and OpenAPI regeneration (T30/T28 frontend gates); process-level proof of the multi-API-instance switch race (the PG row-lock semantics cover it, the T13 100-concurrency precedent); STAGING/REAL_CHAIN/PRODUCTION |
| **Lore Commit SHA** | PR squash merge SHA |

### T20 Section 14 Ledger Record

```text
任务/工作包：T20 / SES-02、SES-03
Owner / Reviewer：后端（Agent 执行）/ PR #52 chatgpt-codex-connector 评审（1 P1+2 P2，逐条实质修复含 4 例回归锁定：码状态门 FOR UPDATE 串行化建立/租约判定用 clock_timestamp() 锁后实际时钟/expected_lease_until 快照重比对）+ 安全自评审（结论见 commit message 与证据账本）
分支 / 基线 SHA：feat/customer-v3-t20-session-switch-fencing / 基线 939c305（main，T19 PR #51 squash）
上游规格段落：客户版任务清单 V3 §4 T20、§12.3 SES-02/SES-03；代码开发清单 V3 §9.2 customer_auth.py 冻结名、§3.3 test_customer_fencing.py 冻结名；激活码开发文档 §12.3 第五行显式 switch、§12.4 事务内 fencing、§6.1 API 表、§6.3 幂等、§13.2 错误码；测试与验收规格 §2.3/§3.4
改动文件：server/app/customer_session_routes.py（switch 端点+operation/now 双参数信封重放+码状态门 403）、server/app/customer_session_service.py（switch_session=login_session takeover=True+revoke_session 通用化 T16 吊销核心）、server/app/customer_auth.py（新增：verify_session_context 事务内 fencing 校验器）、server/app/customer_device_service.py（_revoke_session_riding_device 委托 revoke_session）、server/app/admin_activation_routes.py（suspend/revoke 调 revoke_session 传播+_transaction_now_iso）、server/tests/test_customer_sessions.py（+19 用例）、server/tests/test_customer_fencing.py（新增 17 用例：13 原始+4 PR #52 评审回归锁定）、docs/evidence/T20-EVIDENCE.md、任务与证据账本
失败测试或回归锁定：先红后绿 36 例——switch 13（显式原子顶替：SWITCH 事件+epoch bump+新 token 单事务/同设备续租/同设备恢复 epoch+1/过期接管 TIMEOUT 事件/缺行建立/Bearer 必需/未知凭据 401/幂等键必需 400/丢响应重放同 token 同 epoch/共享 login:ip 预算 429 Retry-After/双设备并发串行化单赢家/零钱包扣费/停用码 403 CODE_SUSPENDED）+码状态门与撤销传播 6（停用/撤销码不建立会话/验证器拒停用码、撤销码、释放设备即使租约存活/管理 suspend/revoke 同事务传播会话失效）+fencing 13（最小六字段上下文/未知 token/switch 后旧 token 被拒/租约过期/logout 后/停用码活租约/撤销码活租约/释放设备活租约/expected session_id、epoch、device、user 四类二次比对各 1/不泄漏 token 或 digest）+PR #52 评审回归锁定 4（expected_lease_until 匹配快照通过/不匹配快照 SESSION_REPLACED/锁等待跨租约后 clock_timestamp 判定 SESSION_EXPIRED/码状态门 FOR UPDATE 使并发写码行阻塞到建立提交）
实现结果：显式原子 switch 落地（switch_session 复用 §12.3 状态机 takeover=True：仅显式确认流程进入顶替分支，普通 login 保持 409 无自动踢人；SWITCH 事件+epoch bump+新 token 单事务原子提交，旧 token 全实例 401 SESSION_REPLACED）；switch 幂等信封独立 session_switch operation（AAD 区分）、重放探针先于鉴权先于限流、共享 login:ip 预算（switch 不能绕过限流）；码状态门（login/switch 403 CODE_SUSPENDED/CODE_REVOKED，停用账户不建立会话）；撤销传播（T16 吊销核心通用化为 revoke_session：device_id 限定设备解绑/撤销委托，None=用户全量，管理端 suspend/revoke 同事务调用使 session 立即失效，服务端撤销而非客户端删 token）；事务内 fencing 校验器 customer_auth.verify_session_context（行锁+expected_* 二次比对+PG 时钟租约+码/设备状态 defense-in-depth 复查，稳定 SESSION_REPLACED/SESSION_EXPIRED，CustomerSessionContext 仅 6 字段最小权限面）；全部新判定用事务内 PG 时钟（SES-01）
验证命令与通过数：专项 71 passed（54 session+17 fencing）；全量 994 passed 零回归（990+4 例 PR #52 评审回归锁定）；ruff/format/mypy 全绿（162 files formatted，65 source files typed）
证据层级：AUTOMATED_VERIFIED
安全与可观测性：login/switch 共享码状态门（停用账户不建立会话）；撤销路径经共享 revoke_session 核心传播（设备解绑/撤销与管理码 suspend/revoke）——epoch bump+租约拉过去+LOGOUT 事件记 actor，服务端撤销而非客户端删 token；fencing 校验器行锁串行化 switch/接管/撤销写入并在事务内复查权威链；expected_* 二次比对拦截迟到写（epoch 永不回跳）；token 只以 keyed digest 过库，校验器上下文永不携带凭据；租约/吊销判定全部用事务内 PG 时钟（SES-01）；稳定错误码（401 SESSION_REPLACED/EXPIRED、403 CODE_SUSPENDED/REVOKED、409 OTHER_DEVICE_ONLINE、429 RATE_LIMITED、503 fail-closed）
迁移与回滚：无新迁移（029 已预留全部结构）；回滚=还原代码（会话行可保留，租约到期自然释放）
外部授权记录：无；未调用真实 ZPay/COS/付费 Provider/对外发码/灰度/公网发布
未测试项：fencing 校验器接入业务写路由（T21 SES-04/SES-05）；客户端 switch 确认流程与 OpenAPI 重新生成（T30/T28 前端门禁）；多 API 实例同 switch 竞态进程级证明（PG 行锁语义覆盖，T13 100 并发先例）；STAGING/REAL_CHAIN/PRODUCTION
Lore 提交 SHA：见 PR squash 合并 SHA
```

## T23 — Audited Admin Adjustments (BILL-02)

| Field | Value |
| --- | --- |
| **Task ID** | T23 / BILL-02 |
| **Owner / Reviewer** | Backend (Agent) / CodeReview sub-agent pass (0 P1/0 P2/4 P3, all substantively fixed with regression locks) + PR #54 chatgpt-codex-connector pass (1 P2, fixed with a regression lock) + security self-review |
| **Branch / Base SHA** | feat/customer-v3-t23-admin-adjustment / base af0308f (main, T20 PR #52 squash) |
| **Date** | 2026-08-24 |
| **Verified Implementation SHA** | PR squash merge result (see docs/evidence/T23-EVIDENCE.md) |
| **Upstream Spec Sections** | Task list §5 T23, §12.5 BILL-02; code checklist §9.1/§9.3 (frozen admin_customer_routes.py); dev doc §15 admin write contract; acceptance spec zero-ledger-difference |
| **Failure Test or Regression Lock** | 24 red→green cases: schema shape + FK-unique audit-order link 2; revision 039 CHECK constraints (four violating shapes raise CheckViolation from PostgreSQL itself) 1; append-only UPDATE/DELETE/TRUNCATE all raise RaiseException with the survivor count intact 1; the happy path asserting all four tables (PAID admin_adjustment order with no third-party trade number + exactly one CHARGE row + wallet increment + the audit row naming the real admin) 1; INTERNAL pricing scope for an activation-less user 1; the write contract 5 (missing Idempotency-Key 400 / missing confirm 400 CONFIRMATION_REQUIRED / blank reason 400 REASON_REQUIRED / auditor write 403 AUDITOR_READ_ONLY while read stays 200 / anonymous write 401); business validation 4 (non-positive credits / int4-overflowing credits / unknown user 404 / wallet-less user 404); invalid source-document enum + blank ref 400 1; unit-price snapshot frozen on the order across a mid-test settings change 1; idempotency 4 (same key + same params replays the sealed 201 with X-Idempotent-Replay and charges exactly once / same key + different params 409 IDEMPOTENCY_CONFLICT / same key against a different target user 409 — the path-params-in-fingerprint PR #43 lesson / a failed 404 write rolls the placeholder back so the key stays reusable); zero ledger difference (summed CHARGE deltas reconcile the wallet balance exactly) 1; SQLite/missing-DSN fail-closed 503 ADJUSTMENT_SERVICE_UNAVAILABLE 1; the audit listing with pagination 2. Plus 5 review locks: the half-committed placeholder answers 409 not 500 (sub-agent P3 R1); the response balance is the post-UPDATE row via RETURNING (P3 R2); SUSPENDED prices as CUSTOMER_STANDARD while REVOKED does not (P3 R3); the RESTRICT FKs refuse every cascade delete with the audit row surviving (P3 R4); a wallet parked at the int4 ceiling plus credits=1 answers a stable 400 with the transaction fully rolled back — no balance/order/CHARGE/audit residue (PR #54 connector P2) |
| **Implementation Result** | The audited admin adjustment lands as one atomic PostgreSQL transaction: a `provider='admin_adjustment'` `status='PAID'` recharge order (revision 026 shapes — created PAID by double confirmation, no third-party trade number, paid_at from the transaction clock), a wallet `CHARGE` ledger row (task_id/billing_round NULL, idempotency key `admin_adjustment:charge:{order_id}`), the atomic wallet increment, and one append-only `admin_adjustments` audit row naming the real acting administrator — all four writes commit or roll back together, so no balance mutation can exist without its ledger row (禁止直接改余额). Amount discipline: amount_fen = credits × internal_base_unit_price_fen frozen on the order (charged == base, PRICE-01 holds by construction); the min/step ladder deliberately does not apply (revision 026 scopes it to zpay orders; an adjustment's amount is defined by its source document); an int4 overflow guard refuses credits whose derived amount exceeds 2^31-1 before the INSERT, and the atomic wallet increment carries the int4 ceiling bound in its WHERE clause (`available_credits <= 2147483647 - credits` — the PR #54 connector P2 fix) so a wallet-side overflow answers a stable 400 instead of a PostgreSQL NumericValueOutOfRange 500, with the wallet-still-there vs vanished distinction preserved (400 vs 404). Pricing scope inferred from the activation binding (CUSTOMER_STANDARD vs INTERNAL). The route runs behind the T09 admin gate (AdminWriter; auditors read-only) and the T12 idempotency snapshot layer: canonical route + path params + body fingerprint (a key replayed against a different target user answers 409, never a silent cross-resource replay), same-key replay of the sealed response, business failures rolling the placeholder back. Revision 039 adds the append-only `admin_adjustments` table (source-document enum + non-blank CHECKs, one unique audit row per order, rewrite-refusing trigger, the shared 036 TRUNCATE guard, and a lineage-preserving downgrade guard). The audit-trail listing serves operators and auditors with bounded pagination. SQLite/missing DSN answers 503 fail-closed; every timestamp samples the in-transaction PostgreSQL clock (SES-01) |
| **Verification Command and Pass Count** | pytest tests/test_admin_customer_routes.py → 29 passed (24 red→green + 5 review locks); full suite → 1023 passed (994 base + 29 new, zero regression); ruff check → all green; ruff format --check → 165 files already formatted; mypy app → success, 66 source files |
| **Evidence Level** | AUTOMATED_VERIFIED |
| **Security and Observability** | every adjustment passes the T09 admin-session gate with the real actor persisted in the append-only audit row (auditors 403 AUDITOR_READ_ONLY); the §15 write contract fully enforced (Idempotency-Key / confirm / reason / request id); the idempotency snapshot layer blocks same-key cross-resource replays (409 on different params or a different target user — the PR #43 lesson regression-locked); balance changes are atomically bound to CHARGE evidence with the zero-ledger-difference assertion; admin_adjustments is triple append-only (row trigger + shared 036 TRUNCATE guard + downgrade lineage guard); amount overflow and source-document shape rejected at the route (400) with the PG CHECK constraints as defense in depth; no secrets in logs |
| **Migration and Rollback** | New revision 039_admin_adjustments (down_revision 038); the downgrade refuses once any audit row exists (operator lineage must survive rollbacks); a rollback requires manually exporting the audit trail first |
| **External Authorization Record** | None; no real ZPay/COS/paid provider/external codes/gray release/public launch |
| **Untested Items** | the T33 management page (the read endpoint is ready); the joint real-chain reconciliation of ZPay recharges and adjustments (BILL-01/BILL-02 real-chain work rides with T35+); STAGING/REAL_CHAIN/PRODUCTION |
| **Lore Commit SHA** | PR squash merge SHA |

### T23 Section 14 Ledger Record

```text
任务/工作包：T23 / BILL-02（账务/管理）
Owner / Reviewer：后端（Agent 执行）/ CodeReview 子代理评审（0 P1/0 P2/4 P3 逐条实质修复含 4 例回归锁定：半提交占位符 409 而非 500/响应余额用 RETURNING 后的真实行/SUSPENDED 计入当前绑定定价/039 FK 全 RESTRICT）+ PR #54 connector 评审（1 P2 已实质修复含回归锁定：钱包余额 int4 溢出——amount_fen 检查只看 credits×单价看不到 available_credits+credits 侧溢出，PG 抛 NumericValueOutOfRange 变 500；修复为原子增量 UPDATE 的 WHERE 带界 available_credits <= 2147483647 - credits，钱包仍在但越界答稳定 400，钱包消失仍 404，锁定测试 test_wallet_balance_overflow_is_rejected_not_500 断言余额/订单/CHARGE/审计零残留）+ 安全自评审
分支 / 基线 SHA：feat/customer-v3-t23-admin-adjustment / 基线 af0308f（main，T20 PR #52 合并后）
上游规格段落：客户版任务清单 V3 §5 T23、§12.5 BILL-02；代码开发清单 V3 §9.1/§9.3 admin_customer_routes.py 冻结名；激活码开发文档 §15 管理写契约；测试与验收规格账本差额为零
改动文件：server/migrations/versions/039_admin_adjustments.py（新增：append-only 审计表+trigger+036 共享 TRUNCATE guard+血统保 downgrade）、server/app/admin_customer_routes.py（新增：调账创建+审计列表，原子四写：PAID order+CHARGE+钱包增量+审计行）、server/app/main.py（挂载路由）、server/scripts/reconcile_customer_billing.py（PG_ONLY_TABLES 加 admin_adjustments——T07 导入对账契约适配，T18 先例）、10 个迁移测试文件 head 断言 038→039（每迁移标准维护，22 处；038 guard 消息匹配与 validate_revision_pair 字面参数保持不动）、server/tests/test_admin_customer_routes.py（新增 29 用例：24 红→绿 + 5 评审回归锁定）、docs/evidence/T23-EVIDENCE.md、任务与证据账本
失败测试或回归锁定：先红后绿 24 例（schema 形状+唯一索引 2；CHECK 约束四形状 CheckViolation 1；append-only UPDATE/DELETE/TRUNCATE RaiseException+幸存计数 1；成功流四表断言 1；内部 scope 1；写契约 5：幂等键/确认/reason/auditor 只读/未认证；业务校验 4：非正数/int4 溢出/未知用户/无钱包；来源单枚举+空 ref 1；价格快照冻结 1；幂等 4：同键重放单次入账/同键异参 409/同键异目标 409/失败不烧键；账本差额为零 1；fail-closed 503 1；审计列表+分页 2）+ 评审回归锁定 5 例（半提交占位符 409 非 500；响应余额为 RETURNING 后真实行；SUSPENDED 计入/REVOKED 不计入当前绑定定价；RESTRICT FK 元数据+删除拒绝+审计行幸存；PR #54 connector P2 钱包余额 int4 溢出拒 400 非 500——余额停在 2147483647 加 credits=1 答稳定 400 且事务完整回滚零残留）
实现结果：后台调账作为单个原子事务落地（双确认+来源单+幂等快照+真实 actor 审计行四写同事务提交或回滚）；金额纪律 credits×内部单价快照冻结（PRICE-01 由构造成立，min/step 仅管 zpay 不适用调账——026 约束口径）；int4 溢出应用层防护；pricing_scope 按 027 当前绑定口径推导（ACTIVE/SUSPENDED 计入，REVOKED 仅审计）；钱包增量用 RETURNING 后真实行；admin_adjustments append-only（039 trigger+036 共享 TRUNCATE guard+全 RESTRICT FK+downgrade 血统保护）；审计列表供 operator/auditor（auditor 只读经 T09 门）；缺 PG 配置 503 fail-closed；SES-01 全部时间戳用事务内 PG 时钟
验证命令与通过数：专项 29 passed（24 红→绿 + 5 评审锁定）；全量 1023 passed（994 基线+29 新增，零回归；2 警告为既有环境噪声）；ruff/format/mypy 全绿（165 files formatted，66 source files typed）；npm run check 全仓门禁绿（secret/client 324/e2e/tauri/server）
证据层级：AUTOMATED_VERIFIED
安全与可观测性：调账永远经 T09 admin session 门（真实 actor 写入审计行；auditor 403 AUDITOR_READ_ONLY）；§15 写契约全量执行（Idempotency-Key/confirm/reason/request id）；幂等快照层同键异参/异目标 409 防止跨资源重放（PR #43 教训回归锁定）；余额变更与 CHARGE 凭据原子绑定+账本差额为零断言（禁止直接 UPDATE 余额）；admin_adjustments 三重 append-only 防护；金额溢出与来源单形状在应用层 400 拒绝（PG CHECK 为纵深防御）；密钥/凭据不入日志
迁移与回滚：迁移 039_admin_adjustments（down_revision=038）；有审计数据时 downgrade 拒绝（保操作员血统）；回滚需先人工导出审计
外部授权记录：无；未调用真实 ZPay/COS/付费 Provider/对外发码/灰度/公网发布
未测试项：T33 管理页面（读端点已就绪）；真实 ZPay 续充与调账的联合对账（BILL-01/BILL-02 真实链路随 T35+）；STAGING/REAL_CHAIN/PRODUCTION
Lore 提交 SHA：见 PR squash 合并 SHA
```

## T28 — Customer API Adapter from the Regenerated OpenAPI Contract (FE-01)

| Field | Value |
| --- | --- |
| **Task ID** | T28 / FE-01 |
| **Owner / Reviewer** | Frontend (Agent) / CodeReview sub-agent pass (1 P1 + 3 P3: the P1 and two P3s substantively fixed with regression locks, one P3 confirmed no-change) + chatgpt-codex-connector PR #55 pass (4 P2, all substantively fixed with regression/contract locks — C1 suspended-code event isolation, C2 X-Request-Id end-to-end, C3 login/switch 200 renewal declaration, C4 enroll phantom-200 removal) + security self-review |
| **Branch / Base SHA** | feat/customer-v3-t28-customer-api-adapter / base b3fe6d3 (main, PR #53 merged) |
| **Date** | 2026-08-24 |
| **Verified Implementation SHA** | PR squash merge result (see docs/evidence/T28-EVIDENCE.md) |
| **Upstream Spec Sections** | Task list §6 T28, §12 FE-01, §10.1 (three lifecycle events); code checklist FE-01 file mapping; dev doc §6.1 API table, §6.3 idempotency, §7 security boundary (explicit credential passing), §3.3 (OTHER_DEVICE_ONLINE masked hint) |
| **Failure Test or Regression Lock** | 24 cases: drift lock 1 (the eight customer schemas locked by typed literals — any regeneration that changes a field set fails `tsc -b`, so hand-written shapes cannot drift into integration) + request-shape 10 (activate carries the idempotency key and no bearer; login with the device credential reports the 200-renewed/201-established outcome; renew+replay reads X-Idempotent-Replay; switch rides the same lane; heartbeat sends the session credential and no idempotency key; logout with key; device listing; unbind; enroll surfaces both pairing outcomes 202-pending/201-consumed; approve with the first device's credential) + error-state 9 (the login conflict carries its masked device hint + slot + lease expiry; the idempotency conflict stays separate from the device conflict; the rate limit keeps its Retry-After seconds; 503 → service-unavailable; the 403 code-status gate; transport network/timeout — no status to derive from, decided at construction; a non-JSON body falls back to determinate unknown without hiding the status; the three 401 lifecycle outcomes dispatch their three dedicated events; a mere invalid credential dispatches nothing) + review locks 2 (the 400 anti-enumeration rejections ACTIVATION_UNAVAILABLE/PAIRING_UNAVAILABLE stay bad-request — never the outage state, the R1 P1 lock; USER_ALREADY_ACTIVATED/DEVICE_SLOTS_FULL group under determinate conflict) + PR #55 locks 2 (a suspended 403 CODE_SUSPENDED dispatches no event while 403 CODE_REVOKED still fires revoked — the reversible/permanent split; every request stamps X-Request-Id, the error retains it, an explicit id travels through) |
| **Implementation Result** | The customer lane lands as the fourth transport lane in api.ts next to the three internal ones, every type cut from the regenerated generated/api.ts (194KB→291KB, 126 paths/130 schemas — the 2026-08-20 artifact predated T13/T17/T19/T20/T23, so none of the eight customer schemas existed). The server side fills the T17 enroll OpenAPI gap: DeviceEnrollPendingResponse (202) and DeviceEnrollConsumedResponse (201) declared through the responses= parameter for the client types only, while the route keeps its raw JSONResponse answers (the two bodies differ by design; the models never gate the runtime); three contract tests (enroll/sessions/activate) refuse a silent response_model drop. 401/403/409/429/idempotency errors all resolve to exactly one of the 18 CustomerApiErrorKind values (code-exact matches first, status fallback second; transport failures carry a private transportKind decided at construction) — the UI never parses a raw status line. OTHER_DEVICE_ONLINE carries the masked device hint + slot + lease expiry; RATE_LIMITED keeps Retry-After. The three §10.1 lifecycle events (expired/replaced/revoked) replace the internal lane's single SESSION_EXPIRED_EVENT so the workspace shows the sentence matching what actually happened; only lifecycle terminal states dispatch — a mere DEVICE_CREDENTIAL_INVALID never tears down the UI. Credentials travel as an explicit discriminated union ({kind:"device"}|{kind:"session"}) — no global plaintext variable (dev doc §7); desktop persistence is T29's Tauri layer. Idempotency per contract: activate/login/switch/logout/unbind/enroll carry Idempotency-Key, heartbeat (naturally idempotent) does not; X-Idempotent-Replay is read into a replayed flag; login surfaces 200-renewed vs 201-established; enroll returns the 202-pending/201-consumed discriminated union |
| **Verification Command and Pass Count** | vitest src/customerApi.test.ts → 24 passed (20 red→green + 2 sub-agent locks + 2 PR-review locks); client full vitest → 348 passed (324 base + 24 new, zero regression); server PG three-file sweep (devices/sessions/activation) → 128 passed incl. the 3 new contract locks (assertions extended: login/switch 200 $ref, enroll no-200); full suite → 1026 passed (1023 base + 3 new, zero regression; the 2 warnings are the pre-existing environment artifacts); ruff check → all green; ruff format --check → 165 files already formatted; mypy app → success, 66 source files; npm run check → green (secret scan / client biome+tsc+vitest 348 / e2e / tauri cargo / server gates) |
| **Evidence Level** | AUTOMATED_VERIFIED |
| **Security and Observability** | credentials never land in a global variable or Web Storage (explicit arguments, dev doc §7; desktop persistence is T29's scope); the error envelope parser never hides the HTTP status (a non-JSON body still carries status into the determinate unknown state); the anti-enumeration 400s stay user-fixable (bad-request) instead of masquerading as outages — the R1 P1 fix with its regression lock; lifecycle events fire only on true session-terminal outcomes, so an input error never tears down the workspace, and a reversible CODE_SUSPENDED never fires the revoked event (PR #55 C1 — the device credential survives an admin's suspension window); every request carries X-Request-Id and CustomerApiError retains it for the §13.2 IDEMPOTENCY_CONFLICT report (PR #55 C2); path parameters pass through encodeURIComponent (the R2 P3 fix); no secrets or credentials in logs or fixtures |
| **Migration and Rollback** | No new migration (the enroll responses= declaration is OpenAPI-documentation-only; runtime behaviour unchanged); a rollback is a code revert |
| **External Authorization Record** | None; no real ZPay/COS/paid provider/external codes/gray release/public launch |
| **Untested Items** | Desktop credential persistence and restart recovery (T29); the second-device pairing/conflict/switch UI flows (T30); device management and heartbeat interactions (T31); the full browser E2E chain (T34); STAGING/REAL_CHAIN/PRODUCTION |
| **Lore Commit SHA** | PR squash merge SHA |

### T28 Section 14 Ledger Record

```text
任务/工作包：T28 / FE-01（客户前端）
Owner / Reviewer：前端（Agent 执行）/ CodeReview 子代理评审（1 P1 + 3 P3：P1 与 2 P3 逐条实质修复含 2 例回归锁定，1 P3 确认无需修改）+ chatgpt-codex-connector PR #55 评审（4 P2 逐条实质修复：C1 CODE_SUSPENDED 移出 revoked 事件道——可逆暂停不清设备凭据；C2 X-Request-Id 全链路并保留在 CustomerApiError.requestId；C3 login/switch 契约补 200 续期响应模型；C4 enroll 幽灵 200 消除——status_code=202 对齐）+ 安全自评审
分支 / 基线 SHA：feat/customer-v3-t28-customer-api-adapter / 基线 b3fe6d3（main，PR #53 合并后）
上游规格段落：客户版任务清单 V3 §6 T28、§12 FE-01、§10.1（三事件拆分）；代码开发清单 V3 FE-01 文件映射；激活码开发文档 §6.1 API 表、§6.3 幂等、§7 安全边界（凭据显式传参，禁止全局明文变量模拟持久会话）、§3.3（OTHER_DEVICE_ONLINE 掩码提示）
改动文件：client/src/generated/api.ts（再生成 194KB→291KB，126 paths/130 schemas，8 个 customer schema 进入契约）、client/src/api.ts（新增 customer 车道 ~500 行：CustomerCredential 判别联合+CustomerApiError 18 kind+三生命周期事件+requestCustomer 传输层+9 个 API 函数）、client/src/customerApi.test.ts（新增 22 用例）、server/app/customer_device_routes.py（enroll 双响应 OpenAPI 契约：responses= 参数文档化 202/201 双模型，运行时仍裸 JSONResponse）、server/tests/test_customer_devices.py / test_customer_sessions.py / test_customer_activation.py（3 个 OpenAPI 契约锁定测试）、docs/evidence/T28-EVIDENCE.md、任务与证据账本
失败测试或回归锁定：先红后绿 20 例（漂移锁 1+请求形状 10+错误状态 9）+ 评审回归锁定 2 例（400 反枚举不进 outage 保持 bad-request；USER_ALREADY_ACTIVATED/DEVICE_SLOTS_FULL 归确定 conflict）+ PR #55 锁定 4 例（403 CODE_SUSPENDED 不派发事件且 CODE_REVOKED 仍派 revoked；X-Request-Id 每请求携带+错误保留+显式透传；服务端契约断言扩展 login/switch 200 $ref 与 enroll 无 200）
实现结果：customer API/error/credential adapter 全部从再生成 OpenAPI 契约切出（漂移锁让字段漂移在 tsc 编译期失败）；enroll 补齐 202/201 双响应模型（T17 遗留 OpenAPI 空缺，运行时零变化）；401/403/409/429/幂等错误全部进入 18 个确定 CustomerApiErrorKind（UI 永不解析裸状态行）；三事件拆分（§10.1）仅生命周期终态派发；凭据显式判别联合传参（§7 红线）；幂等语义按契约逐端点执行，X-Idempotent-Replay 读为 replayed，login 200/201 与 enroll 202/201 双判别联合
验证命令与通过数：client 专项 24 passed（20 红→绿+2 评审锁定+2 PR #55 锁定）；client 全量 vitest 348 passed（324 基线+新增，零回归）；服务端 PG 三文件 128 passed（含 3 新契约锁，断言扩展覆盖 200/无 200）；全量 pytest 1026 passed（1023 基线+3 新增，零回归；2 警告为既有环境噪声）；ruff/format/mypy 全绿（165 files formatted，66 source files typed）；npm run check 全仓门禁绿（secret/client biome+tsc+vitest/e2e/tauri/server）
证据层级：AUTOMATED_VERIFIED
安全与可观测性：凭据永不落全局变量或 Web Storage（显式参数传递）；错误信封解析不吞 HTTP 状态；反枚举 400 保持用户可修正不误导为服务中断（P1 修复+回归锁定）；生命周期事件只在会话真正终态派发，可逆 CODE_SUSPENDED 不触发 revoked（PR #55 C1）；X-Request-Id 每请求携带且保留在错误对象上供 §13.2 上报（PR #55 C2）；路径参数 encodeURIComponent；无密钥/凭据入日志或夹具
迁移与回滚：无新迁移（OpenAPI 文档面变更，运行时行为不变）；回滚即还原代码
外部授权记录：无；未调用真实 ZPay/COS/付费 Provider/对外发码/灰度/公网发布
未测试项：凭据桌面持久化与重启恢复（T29）；第二设备配对/冲突/切换 UI（T30）；设备管理/heartbeat 交互（T31）；浏览器全链路 E2E（T34）；STAGING/REAL_CHAIN/PRODUCTION
Lore 提交 SHA：见 PR squash 合并 SHA
```
