# Customer Edition Task Evidence Record V3

> Note: This file is the evidence ledger for `docs/customer-task-list-V3.md`; each task closure must record details per Section 14 template. The task list remains the single source of truth for status.

## T01 — Freeze V3 Main Specifications

| Field | Content |
| --- | --- |
| **Owner** | Architecture/Product |
| **Reviewer** | (N/A - spec freeze doesn't require independent reviewer) |
| **Branch / SHA** | `feat/customer-v3-t01-freeze-spec` / (TBD after commit) |
| **Upstream Spec Sections** | `docs/customer-development-plan-V3.md` §1; `docs/customer-task-list-V3.md` Header & Table T01 |
| **Files Changed** | - Update `docs/customer-task-list-V3.md` Header status<br>- Update Task Table T01 status `[~]`→`[x]`<br>- Add `docs/客户版任务证据记录-V3.md` (this file as ENG version) |
| **Failure Test or Regression Lock** | N/A for spec freeze tasks |
| **Implementation Result** | User session confirmed V3 execution plan and boundaries; frozen downstream design dependencies on file mapping and document structure |
| **Verification Command and Pass Count** | N/A |
| **Evidence Level** | `CODE_PRESENT` (here refers to documentation freeze) |
| **Security and Observability** | N/A |
| **Migration and Rollback** | R0 preserves current internal P0 release/tag; V3 branch evolves independently |
| **External Authorization Record** | None |
| **Untested Items** | N/A |
| **Lore Commit SHA** | (TBD) |

### Acceptance Evidence

#### Development Plan Conclusion Consistency

- Plan §1 states clearly: "This is not adding a few pages on top of the existing system. This scan identified 33 modules directly depending on `sqlite3` in the current runtime layer... Customer edition must complete PostgreSQL migration first, then build activation codes, device/session, fair queueing, and multi-instance"
- Effort model: 95–165 person-days base effort → risk-adjusted 110–185 person-days management; recommended configuration: 2 backend + 1 frontend/Tauri + 1 QA + 0.5–1 OPS
- Lane division: A(DB/billing)/B(device/auth)/C(worker/queue)/D(customer frontend)/E(security/deployment)
- Milestones M0–M6 clearly defined, especially M0/M1 exit gates constraining subsequent feature development order

#### Unique File Mapping Frozen

Per unique implementation file mappings frozen in `docs/customer-code-list-V3.md` §3:

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

1. **Status sync**: Only update task status (`[ ]/[~]/[x]/[!]`) in `docs/customer-task-list-V3.md`
2. **Evidence registration**: Detailed evidence for each task registered in corresponding section of this file
3. **SHA recording**: Complete Lore commit SHA recorded in both task list and this file
4. **Blocking markers**: Tasks requiring external authorization/resources marked with `[!]` and documented blocking items
