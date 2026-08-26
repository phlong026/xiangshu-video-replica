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
