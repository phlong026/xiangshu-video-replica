# T32 — Admin Activation Code Management Frontend (ADM-01)

## Task Information

| Field | Value |
| --- | --- |
| **Task ID** | T32 / ADM-01 |
| **Owner** | Frontend/Admin (Agent) |
| **Reviewer** | session-internal code review + security review |
| **Branch / Base SHA** | `feat/customer-v3-t32-admin-frontend` / base `83a5bb2` (T12, PR #43 squash) |
| **Date** | 2026-08-23 |
| **Evidence Level** | `AUTOMATED_VERIFIED` (component tests + full workspace gate) |

## Exit-Gate Verification

Task exit gate: *admin cookie/CSRF 有效；操作有原因、确认、结果 request id 和审计入口*；ADM-01
No-Go red line: *管理 secret 不得进入浏览器持久存储* — all delivered and locked by automated tests:

```bash
$ npx vitest run src/api.admin.test.ts src/admin src/AdminApp.test.tsx
src/api.admin.test.ts (9 tests)                       # adapter: exchange/write contract/idempotency/error envelope/no-storage
src/admin/DeliveriesPage.test.tsx (6 tests)           # deliver: reason+confirm+request id, optional refs, 401, read-only
src/AdminApp.test.tsx (5 tests)                       # + activation tab reachable at the sign-in gate
src/admin/ActivationCodeBatchesPage.test.tsx (6 tests)# create/generate/one-time download/409 replay/401/read-only
src/admin/AdminActivationSection.test.tsx (7 tests)   # session lifecycle: gate, exchange, refresh-recovery, auditor, logout, sub-nav
src/admin/ActivationCodesPage.test.tsx (7 tests)      # list masked-only, filters, suspend matrix, reason gate, 401, read-only
Test Files  6 passed (6)   Tests  40 passed (40)
```

Key red-line locks:

- **CSRF token never hits persistent storage** — `api.admin.test.ts` spies on
  `Storage.prototype.setItem` during exchange and asserts it is never called; the
  token lives in a module-scoped variable only, so a page refresh downgrades the
  UI to read-only (`AdminActivationSection` "readonly" phase) until a fresh
  exchange. The HttpOnly cookie itself is browser-managed and never readable
  from JS.
- **Every write carries reason + confirm + Idempotency-Key + X-Admin-CSRF**
  (T12 §15 contract) — asserted byte-exact on the request body
  (`JSON.stringify({ ...fields, confirm: true, reason })`) in adapter and
  component tests for create/generate/download/deliver/suspend.
- **One-time plaintext download** — the plaintext codes render once in the
  result panel and the server-side 409 `EXPORT_ALREADY_DOWNLOADED` maps to a
  deterministic Chinese message; a second reveal is impossible because the
  component drops the export block after download.
- **401 mid-session** collapses every page back to the sign-in gate via
  `onSessionExpired`.

## Implementation Notes

- `client/src/api.admin.ts` (new, 458 lines): dedicated control-plane adapter
  (does **not** reuse the internal-P0 `requestControl` from `api.ts`, which has
  no credentials/CSRF). Plain `Record<string, string>` headers so tests can
  assert on them; `credentials: "include"`; `AbortController` timeout;
  `crypto.randomUUID` idempotency keys (UUID fallback included); error envelope
  (`detail.code`/`detail.message`) mapped to deterministic Chinese messages
  (`EXCHANGE_CREDENTIAL_INVALID`, `EXPORT_ALREADY_DOWNLOADED`,
  `CODE_TRANSITION_INVALID`, `ADMIN_SESSION_INVALID`, `AUDITOR_READ_ONLY`, …).
- `client/src/admin/AdminActivationSection.tsx` (new, 230 lines): the T09
  session facade — one-time credential exchange, session bar (display name +
  role label), sign-out (DELETE + clear), refresh-recovery to read-only
  ("会话令牌已随页面刷新丢失…"), auditor read-only notice, and the three-page
  sub-navigation. `readOnly = refresh-drop || auditor`.
- `ActivationCodeBatchesPage` / `ActivationCodesPage` / `DeliveriesPage`: the
  three frozen §4.2 pages. All list views show **masked codes only**; result
  values (batch id, export id, request id, delivery id) are wrapped in `<code>`
  elements for unambiguous test/DOM semantics. `ActivationCodesPage` performs
  local row status refresh after a successful transition and shows
  `已暂停（request id: …）`.
- `AdminApp.tsx`: fourth tab「激活码」rendering `AdminActivationSection` — the
  §10.3 direction of AdminApp becoming a page container (T33 adds the remaining
  four pages).
- File-name note: the three page components and `api.admin.test.ts` are frozen
  names from code checklist §4.2/§4.4. `api.admin.ts` is the implementation
  counterpart of the frozen `api.admin.test.ts` (same convention as T28's
  `api.customer.test.ts`); `AdminActivationSection.tsx` is a new auxiliary
  facade that owns the T09 session lifecycle and conflicts with no frozen name.

## Files Changed

| File | Change |
| --- | --- |
| `client/src/api.admin.ts` | new (458 lines): control-plane adapter, in-memory CSRF, §15 write contract |
| `client/src/api.admin.test.ts` | new (416 lines, 9 cases) |
| `client/src/admin/AdminActivationSection.tsx` | new (230 lines): session facade + sub-navigation |
| `client/src/admin/AdminActivationSection.test.tsx` | new (210 lines, 7 cases) |
| `client/src/admin/ActivationCodeBatchesPage.tsx` | new (352 lines): create/generate/one-time download |
| `client/src/admin/ActivationCodeBatchesPage.test.tsx` | new (295 lines, 6 cases) |
| `client/src/admin/ActivationCodesPage.tsx` | new (272 lines): list + suspend/resume/revoke |
| `client/src/admin/ActivationCodesPage.test.tsx` | new (242 lines, 7 cases) |
| `client/src/admin/DeliveriesPage.tsx` | new (163 lines): deliver |
| `client/src/admin/DeliveriesPage.test.tsx` | new (217 lines, 6 cases) |
| `client/src/AdminApp.tsx` | +「激活码」tab rendering `AdminActivationSection` |
| `client/src/AdminApp.test.tsx` | +401 session branch in the fetch stub + activation-tab gate case |
| `client/src/styles.css` | +admin-session/admin-hint/admin-result/admin-code-list/admin-filters styles |
| `docs/客户版任务清单-V3.md` | T32 + ADM-01 status, header status line |
| `docs/CUSTOMER-TASK-EVIDENCE-V3.md` | T32 evidence ledger entry |
| `docs/evidence/T32-EVIDENCE.md` | this file |

## Regression

```
$ cd client && npm run check
biome check .        → Checked 64 files. No fixes applied.
tsc -b               → no errors
vitest run           → Test Files 29 passed (29)  Tests 360 passed (360)

$ bash scripts/verify_no_secrets.sh → No hardcoded secrets detected
$ npx biome check e2e               → Checked 6 files, clean
$ cargo fmt --check && cargo check --locked (client/src-tauri) → Finished dev profile in 1m 03s
$ uv run ruff check server && ruff format --check server → All checks passed! / 133 files already formatted
$ uv run mypy server/app → Success: no issues found in 56 source files
$ uv run pytest server/tests (zero server changes; baseline regression) → see ledger note below
```

Server and Tauri trees are untouched by T32 (git status lists only
`client/src/**` + docs); their gates were re-run on the worktree to confirm the
merged main baseline (83a5bb2) still passes. The full `pytest` baseline run is
recorded in the ledger; the PG-fixture suites run in CI with the fixture up
(AGENTS.md rule).

## Section 14 Ledger Record

```text
任务/工作包：T32 / ADM-01
Owner / Reviewer：前端/管理（Agent 执行）/ 会话内代码评审 + 安全评审
分支 / 基线 SHA：feat/customer-v3-t32-admin-frontend / 基线 83a5bb2（T12 PR #43 squash）
上游规格段落：客户版任务清单 V3 §6 T32 行、§12.4 ADM-01；代码开发清单 V3 §4.2/§4.4/§10.3；激活码开发文档 §15（管理写合同）、§4.3（AdminApp 页签演进）；测试与验收规格 §2
改动文件：client/src/api.admin.ts（新增 458 行控制面 adapter）+ api.admin.test.ts（新增 9 用例）、client/src/admin/AdminActivationSection.tsx（新增 230 行会话门面）+ 测试 7 用例、client/src/admin/ActivationCodeBatchesPage.tsx（新增 352 行）+ 测试 6 用例、client/src/admin/ActivationCodesPage.tsx（新增 272 行）+ 测试 7 用例、client/src/admin/DeliveriesPage.tsx（新增 163 行）+ 测试 6 用例、client/src/AdminApp.tsx（第四页签「激活码」）、client/src/AdminApp.test.tsx（+401 分支与页签门面用例）、client/src/styles.css（admin 系列样式）、docs/evidence/T32-EVIDENCE.md、任务与证据账本
失败测试或回归锁定：先红后绿——40 个新用例覆盖：CSRF 永不落持久存储（Storage.setItem spy 断言零调用）、写合同逐字节断言（confirm:true+reason 顺序、Idempotency-Key 每写新 UUID、X-Admin-CSRF 头）、明文一次性下载（二次 409 确定性文案、结果块仅渲染一次）、六态转换（暂停矩阵/原因强制/未勾选拒）、发放（可选引用留空则不入 body、409 覆盖文案）、401 全页面回收登录门、刷新后只读降级、auditor 只读、readOnly 按钮禁用/操作列隐藏
实现结果：T09 会话门面（交换/退出/刷新降级/auditor）+ 三冻结页面（批次创建/生成/一次性下载、掩码列表/暂停/恢复/作废、发放）+ §15 写合同全链路（原因/确认/幂等键/CSRF/request id 展示）；明文码仅存在于一次性下载结果面板，不入任何持久存储
验证命令与通过数：vitest admin 目标套件 40 passed；前端全量 360 passed（29 文件）；biome/tsc 全绿；secret 扫描/e2e biome/cargo fmt+check/ruff/format/mypy 全绿（服务端零改动基线回归）
证据层级：AUTOMATED_VERIFIED（组件级自动化验证；真实浏览器×真实服务端联调属 T34 E2E 范围）
安全与可观测性：csrf_token 仅模块内存（No-Go 红线锁定）；HttpOnly cookie 由浏览器管理不可被 JS 读取；列表仅掩码码；明文码仅一次性面板展示且服务端 409 防重放；401 统一回收登录门；所有写操作展示 request id 审计入口
迁移与回滚：纯前端任务，无迁移、无服务端/Tauri 改动
外部授权记录：无；未调用真实 ZPay/COS/付费 Provider/对外发码/灰度/公网发布
未测试项：真实浏览器与真实服务端全链路（T34）；CustomersPage/DevicesPage/SessionsPage/AuditEventsPage（T33）；管理登录凭据真实签发流程（运维手册范围）；STAGING/REAL_CHAIN/PRODUCTION
Lore 提交 SHA：见 PR squash 合并 SHA
```
