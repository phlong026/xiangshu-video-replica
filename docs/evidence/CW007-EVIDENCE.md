# CW-007 — PG 测试隔离、客户种子与缺库硬门

## 任务信息（§14 模板）

| 字段 | 值 |
| --- | --- |
| 任务/工作包 | CW-007 补齐 PG 测试隔离、客户种子与缺库硬门 |
| Owner / Reviewer | Owner：ZCode 代理（hlong026 会话，2026-09-09）；Reviewer：独立复核子代理（结论见 §6）+ PR review |
| 分支 / 基线 SHA | `feat/customer-v3-cw007-pg-test-foundation`；原始开发基线 `origin/main@b211095`，2026-09-09 rebase 整合到 `origin/main@ce40db5`（drift #100/#101/#102 前端+viral 收编后），整合提交 `e3613ad` |
| 上游规格段落 | V3 清单 §4 CW-007；保留验收底线全部条目 |
| 改动文件 | 新增 `server/tests/pg_test_kit.py`、`server/tests/test_pg_test_kit.py`（已登记文件映射 `docs/客户版代码开发清单-V3.md` CW-007 增量节）；迁移 28 个既有 PG 测试文件至硬门（清单见 §2；含独立评审 P0 指出的 test_internal_access_tokens.py）；`AGENTS.md` 验证口径同步；**ce40db5 整合扩展**：`test_postgres_migrations.py` HEAD_REVISION 076→080 + `test_viral_refresh.py`（#102 新增）缺库 skip→硬门 |
| 失败测试或回归锁定 | 新增 `test_pg_test_kit.py` 9 用例即本项回归；既有 27 套件在迁移前后均保持绿（§3） |
| 实现结果 | §2 交付明细；§3 验证结果 |
| 验证命令与通过数 | 见 §3（原基线 kit 9/9、迁移套件 798 用例 0 fail 0 skip、硬门行为 fail/skip 双模式验证）+ §3.1 整合复验（ce40db5：专项 186 + 全量 `npm run check` server 2117 passed 0 failed / client 1247） |
| 证据层级 | AUTOMATED_VERIFIED（真实 PG16 fixture 上执行） |
| 安全与可观测性 | 测试库 allowlist 拒绝非测试库/系统库（postgres/template*），清理永远不会触碰业务库 |
| 迁移与回滚 | 纯测试基建 + 文档；回滚 = revert 本分支，不影响生产行为 |
| 外部授权记录 | 无 |
| 未测试项 | Windows/CI 上的 Docker PG service 组合由 CI 门禁验证（本地为 macOS Docker） |
| Lore 提交 SHA | 本 PR squash 后回填 |

## 2. 交付明细（对照 CW-007 保留验收底线）

| 验收要求 | 实现 |
| --- | --- |
| 无 PG/错 DSN/不安全目标时 preflight 非 0 失败，不 skip 或回退 SQLite | `pg_test_kit.require_pg_or_explicit_skip()`：不可达即 `pytest.fail`（进程退出码 1）；`VIDEO_REPLICA_TEST_ALLOW_PG_SKIP=1` 为**显式开发者快速通道**（skip 且永不作为验收证据）。kit 自身无任何 SQLite 回退路径 |
| 测试 host/库名前缀及范围明确，清理拒绝非测试库 | `RECORDED_TEST_DATABASES` allowlist + `assert_safe_test_database()`：create/drop 仅接受登记的 `*_test` 库名；拒绝 `postgres`/`template0/1` 与未登记名（验收用例 `test_create_and_drop_refuse_unregistered_names`） |
| 至少 2 客户 3 设备可重复建种 | `seed_customer_scenario()`：cust-a/cust-b + 3 BOUND 设备（A-slot1/A-slot2/B-slot1）+ 2 码 + 2 钱包；TRUNCATE 后重插，幂等（`test_seed_is_repeatable_two_customers_three_devices`） |
| 连续 2 次及中断后重跑一致 | 种子连续两次 facts 相等；中断模拟（种子后插 ghost 行 → 重新 create wipe → 重种）facts 恢复标准态（`test_rerun_after_interrupted_create_is_consistent`） |
| 2 个独立专项不互删/串数据 | 双库 alpha/beta 并存：alpha 写入对 beta 不可见、beta 种子不受影响（`test_independent_suites_do_not_cross_talk`） |
| 共享全量 fixture 只允许 1 个 suite 运行 | `shared_suite_lock()` 文件锁；互斥性测试 `test_shared_suite_lock_is_exclusive` |
| 移除当前业务缺 PG skip | 28 个 PG 测试文件的全部缺库 skip（`pytest.skip(SKIP_REASON)` 17 处、字面量 5 处、模块级/装饰器 skipif 5 文件、A1 lane 字面量 1 文件）替换为运行期硬门调用/usefixtures 守卫；收集期 `skipif(pg_reachable())` 探测形态全部清除；20 处双重探测包裹与 19 处死代码 `_pg_available` 一并清理 |

已知边界（登记，不冒充完成）：`conftest.py` 的开发身份 autouse fixture 保留（客户数据库测试的身份合同收敛归 CW-026/CW-041）；`test_security_contracts.py`（secret-scan 脚本存在性）与 `test_source_frames.py`（ffmpeg）的 skipif 与 PG 无关，保留；4 个历史套件库文名缺 `_test` 后缀（t11_activation_code_service、t34_chain_e2e、t13c_customer_activation_concurrency、t22r_customer_recharge），改名与 kit 助手收编登记为后续 CW 欠账（kit 内注释已标注）——评审 P1-3。

## 3. 验证结果（本地 PG16 fixture，`TEST_POSTGRESQL_URL=postgresql://testuser:testpass@localhost:5433/customer_v3_test`）

| 验证 | 命令/场景 | 结果 |
| --- | --- | --- |
| kit 验收测试 | `pytest tests/test_pg_test_kit.py -q` | **9 passed** |
| 迁移套件回归（有 fixture） | 分四批跑全部 27 个迁移文件 + kit | **798 passed, 0 failed, 0 skipped**（128+281+292+92+5） |
| 评审修复后复验（含第 28 文件） | kit + internal_access_tokens + fencing + admin_auth + activation_code_routes | **189 passed**（修复后）+ kit 复验 9 passed；随后 npm run check 全量复核 |
| 硬门：缺库必须失败 | `TEST_POSTGRESQL_URL=…:5999/dead pytest tests/test_customer_queue_fairness.py` | 12 errors（fixture 内 `pytest.fail`），**进程退出码 1** |
| 硬门：错误目标库名被拒 | gate 内 `assert_safe_test_database`：非登记 `*_test` 库名在连通性检查前即拒绝（评审 P1-2 落实） | 见 kit 源 `require_pg_or_explicit_skip` |
| 硬门：显式 opt-in 可跳过 | 同上 + `VIDEO_REPLICA_TEST_ALLOW_PG_SKIP=1` | 12 skipped，退出码 0（开发者快速通道） |
| 静态门 | `ruff check tests --fix`、`ruff format tests`、`py_compile` 全部改动文件 | 0 error |
| 全量门禁 | `npm run check`（本任务唯一一次全量 pytest，2026-09-09 本地） | **1926 passed, 1 warning, 0 failed**（765.84s）；secret 扫描/client biome+tsc+vitest/e2e/tauri/ruff/mypy 全部通过，exit 0 |

### 3.1 整合复验（rebase 到 ce40db5 后，2026-09-09 本地重跑，Node 24 + PG16 fixture）

| 验证 | 命令/场景 | 结果 |
| --- | --- | --- |
| 冲突解决正确性 | `test_postgres_migrations.py`（HEAD_REVISION 076→080，9 处断言 + 模块级 autouse 硬门） | **24 passed** |
| viral 整合扩展 | `test_viral_refresh.py`（#102 新增 PG 用例迁硬门，真实建库跑通非 skip） | **5 passed** |
| kit rebase 后完好 | `test_pg_test_kit.py` | **9 passed** |
| 自动合并重叠文件 | activation_code_schema + admin_dashboard + admin_session + customer_devices + sqlite_to_postgres | **148 passed** |
| 静态门 | server `ruff check` / `ruff format --check` / `mypy app` | All passed / 289 formatted / 104 files no issues |
| **全量门禁（ce40db5 基线）** | `npm run check`（Node 24，唯一一次全量 pytest，770.76s） | **server 2117 passed, 0 failed；client 1247 passed（78 files）；secret/biome/tsc/cargo fmt+check/ruff/mypy 全绿** |

> 全量数字从原基线 1926→2117（+191）来自 ce40db5 相对 b211095 新增的 viral 模块与 fp 前端测试；CW-007 增量本身零回归。全仓 PG 缺库 skip 复扫为零（含 drift 新增文件 test_viral_refresh.py）。

## 4. 逐套件迁移清单（27 文件）

SKIP_REASON 型→硬门调用：activation_code_routes、activation_code_service、admin_activation_routes、admin_audit_routes、admin_customer_routes、admin_session_routes、customer_activation、customer_chain_e2e、customer_devices、customer_fencing、customer_idempotency、customer_queue_fairness、customer_recharge、customer_security、customer_sessions、queue_load_10k、worker_crash_recovery（17）；字面量型：admin_dashboard_routes、admin_profit_routes、admin_rate_routes、operation_costs、ops_alerts（5）；模块级/装饰器 skipif→autouse/usefixtures 守卫：activation_code_schema、postgres_migrations（模块级 autouse）、admin_auth（usefixtures+fixture 内）、db_pg（usefixtures×2）、sqlite_to_postgres（pg_only→usefixtures）（5）。

## 5. RED→GREEN 记录

- RED 阶段：`test_pg_test_kit.py` 初版即按验收断言编写（allowlist 拒绝、种子幂等、锁互斥在实现缺陷时失败——实际开发中捕获 seed 键派生缺陷 `cw007-code-E` 冲突与 DSN 凭据不符两例，修复后转绿）。
- 迁移无行为变更：既有断言逐字未动，仅 skip 机制替换；798 用例全绿证明等价。

## 6. 独立复核

独立评审子代理对 kit/测试/迁移 diff 做行为与合同复核，发现 P0×1 / P1×2 / P2×7，全部在本分支落实：

- **P0-1**：`test_internal_access_tokens.py`（A1 安全 lane）仍保留缺库静默 skip——已迁移至硬门，本文件计入 §4 清单（共 28 个）。
- **P1-2**：gate 原不校验目标库名，allowlist 安全声明被高估——`require_pg_or_explicit_skip` 现于连通性检查前执行 `assert_safe_test_database(库名)`。
- **P1-3**：4 个历史库名缺 `_test` 后缀无法入 allowlist——已在 kit 注释与"已知边界"登记为后续 CW 欠账。
- **P2**：5+19 处死代码 `_pg_available` 删除、20 处双重探测统一为直接 gate 调用、`pg_reachable` 失败附带异常类型摘要、seed 返回实测计数（不再硬编码）、空 TYPE_CHECKING 脚手架删除、注释矛盾修正、fcntl 平台边界注明。
- 复核其余结论：硬门语义正确、无 SQLite 回退、种子与 schema 约束逐条吻合、迁移无行为改动、CI（Linux+PG16 service）不受影响。

## 7. 签认记录

| 角色 | 结论 | 日期 |
| --- | --- | --- |
| 测试负责人 | 待签认 | — |
| 后端负责人 | 待签认 | — |
