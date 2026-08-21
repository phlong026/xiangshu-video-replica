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
