# T39 Evidence Report — Staging Fault Drills

## 基本信息

| 字段 | 内容 |
| --- | --- |
| 候选 SHA / PR | `<待 staging 窗口冻结>` / `<待填>` |
| 环境 / 时间窗 | `NOT_RUN` / `<待填>` |
| Owner / Reviewer | `QA/OPS` / `<独立复核待填>` |
| 配置版本 / 迁移 head | `<待填>` / `<待填>` |
| Web / Windows 产物 SHA256 | `<待填>` |
| 外部授权记录 | `无；不执行真实 ZPay、COS 或付费 Provider` |
| 未测试项 | `真实双 API/四 Worker/PG HA/stub 故障注入和外部告警接收均未执行` |

## Task Summary

- **任务**：T39 — 执行 API/Worker/PG/COS/ZPay/Provider 故障演练（OPS-04）
- **状态**：演练操作单与回归锁为 `AUTOMATED_VERIFIED`；T39/OPS-04 保持 `[~]`
- **完成日期**：2026-08-26（仓库侧准备）
- **基线**：`main@3245c6f`（T38 合入后）
- **分支**：`feat/customer-v3-t39-fault-drills`

本文件不是 staging 演练成功记录。当前没有已授权的目标服务器、LB、双 API、四
Worker、PG HA 或受控依赖 stub，故所有实际演练均为**未执行**。不得以此文件宣称
`STAGING_VERIFIED`、`REAL_CHAIN_VERIFIED` 或 `PRODUCTION_GO`。

## §14 任务记录

- **上游规格**：任务清单 §7 T39、§12.7 OPS-04；测试与验收规格 §8.2；部署手册
  §6.1；T26 的 `SUBMISSION_UNCERTAIN` crash-recovery 合同；T38 的 100 条 PITR
  recovery-facts 合同。
- **变更文件**：`docs/客户版部署与灰度手册.md`、`docs/客户版任务清单-V3.md`、
  `docs/CUSTOMER-TASK-EVIDENCE-V3.md`、本证据文件、
  `server/tests/test_customer_ha_smoke.py`。
- **实现结果**：手册把 API 双成员 kill、四 worker 的 idle/`SUBMITTING` 两个 crash
  时点、PG HA failover、100 条事实核对、受控 stub 暂时失败、RTO/RPO、T37 告警收发、
  No-Go 与回滚约束为同一候选 SHA 的 staging 操作。`SUBMITTING` 任务过期后必须停在
  `SUBMISSION_UNCERTAIN` 人工对账，替代 worker 只可处理后续任务，禁止重发可能付费
  的 Provider POST。PG 核对显式加载受控 backup libpq service，不继承 shell 默认或
  application DSN。
- **验证命令**：`uv run python -m pytest tests/test_customer_ha_smoke.py -q` → 36
  passed；`uv run python -m pytest tests/test_customer_pitr.py tests/test_customer_ha_smoke.py -q`
  → 47 passed；变更测试 Ruff/format 与 `mypy app` 通过。PR #75 的 Secret scan、Linux
  quality gate 与 Windows Tauri/NSIS 均通过。
- **安全与可观测性**：不记录 service file 内容、DSN、账号、对象 URL、任务原始数据或
  Provider 响应；pitr service 的路径与名称不是凭据。故障时要保存 T37 的脱敏
  fired/resolved 接收时间与不变量摘要，而非令牌或请求体。
- **迁移与回滚**：无 Alembic revision。任一不变量失败即停止扩大，从 LB 摘除变更成员，
  保留 append-only 审计事实，按部署手册第 8 节回滚；绝不直接改钱包余额、账本或
  `SUBMISSION_UNCERTAIN` 任务状态。
- **外部授权记录**：无；未执行 staging fault injection，亦未调用真实 ZPay、COS 或付费
  Provider。
- **未测试项**：实际双 API/四 Worker/PG HA/stub 故障、RTO/RPO 与外部告警接收仍需
  经授权的 staging 窗口。
- **Lore Commit SHA**：`ecf84c392675c4db97dd5d9847173783fcee7df2`（PR #75 squash）。

## CODE_PRESENT

- [x] T39 操作单限定在真实 staging，要求同一候选 SHA、回滚 SHA 和受控窗口。
- [x] API/Worker kill、PG failover、受控 stub 故障、停止条件和证据字段均有明确边界。
- [x] 明确拒绝真实 ZPay、COS、付费 Provider；真实调用留给获得授权后的 T40。

结论：`PASS`。

## AUTOMATED_VERIFIED

- [x] `test_t39_fault_drill_runbook_requires_staging_guards_and_business_proof` 先红后绿，
  锁定两 API、四 Worker、`systemctl kill`、100 条恢复事实、受控 stub、同幂等键、
  `RTO <= 5 分钟`、`RPO=0` 与真实外部调用禁止项。
- [x] 该测试只验证仓库文档合同，不把自动化测试伪装成 fault injection。

结论：`PASS`。

## STAGING_VERIFIED

- [ ] LB 后两 API 逐一 kill/recover，并保留两设备单在线连续服务证据。
- [ ] 四 Worker 逐一 kill/recover，且同一业务动作无重复 Provider POST、重复付费或账本漂移。
- [ ] PostgreSQL HA failover 后，100 条 activation/order/CHARGE/session epoch 逐笔核对通过。
- [ ] COS/Provider/ZPay 受控 stub timeout/5xx/断连保持同一幂等键与状态机。
- [ ] RTO、RPO、T37 外部 fired/resolved、回滚和独立复核均已记录。

结论：`NOT_RUN`。

## REAL_CHAIN_VERIFIED

- [ ] 不适用；真实 ZPay、COS 与付费 Provider 需要 T40 的明确授权。

结论：`NOT_RUN`。

## PRODUCTION_GO

- [ ] 不适用；T39 未完成 staging 演练，且 T40–T42 尚未开始。

结论：`NO-GO`。

## 异常与回滚记录

| 时间 | 注入/事件 | 预期 | 实际 | 不变量 | 处置/回滚 | Owner |
| --- | --- | --- | --- | --- | --- | --- |
| `<待 staging>` | `<API/Worker/PG/stub>` | `<待填>` | `未执行` | `<待填>` | `<待填>` | `<待填>` |

## 签署

| 角色 | 结论 | 姓名/标识 | 时间 |
| --- | --- | --- | --- |
| Release | `NOT_RUN` | `<待填>` | `<待填>` |
| 业务/账务 | `NOT_RUN` | `<待填>` | `<待填>` |
| 技术/OPS | `NOT_RUN` | `<待填>` | `<待填>` |
| Independent Verifier | `NOT_RUN` | `<待填>` | `<待填>` |
