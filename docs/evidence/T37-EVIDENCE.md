# T37 Evidence Report — 结构化日志、指标与 P1 告警

## Task Summary

- **任务**：T37 — 建立结构化日志、指标和 P1 告警
- **状态**：仓库侧 `AUTOMATED_VERIFIED`；父任务、OPS-02、EXT-02 保持 `[~]`
- **完成日期**：2026-08-26
- **基线**：`main@bb545ca`（T36 PR 合并后）
- **分支**：`feat/customer-v3-t37-observability`
- **外部授权**：无；未连接真实服务器、告警平台、COS、ZPay 或付费 Provider

本证据只关闭仓库可自动验证部分。当前没有目标服务器上的集中日志/指标平台、
外部 P1 接收器、PostgreSQL HA 监控角色或真实外部账号，因此没有执行真实
fired/resolved 通知演练，也没有验证 PG 复制/慢查询/备份指标和 Provider/COS/ZPay
费用全链。证据不能提升为 `STAGING_VERIFIED`、`REAL_CHAIN_VERIFIED` 或
`PRODUCTION_GO`。

## §1 交付结果

### 1.1 结构化请求日志与追踪

- 所有 HTTP 请求统一生成或保留经长度/控制字符校验的 `X-Request-Id`，响应和
  完成日志使用同一 id；幂等重放优先沿用已提交操作的持久 request id。
- 完成日志为单行 JSON，固定包含 request id、method、路由模板、operation、
  status、latency 和 result code；管理/客户认证依赖、fencing 与通用 owner/RBAC
  拒绝会写入真实业务结果码；共享 legacy `HTTPException` 和直接返回的入口/健康
  拒绝也不会退化成泛化的 HTTP 状态码。
- 业务关联字段只允许显式白名单：actor/user/device/session/session epoch/task/
  order/activation-code mask。task/order 路径参数由路由模板自动提取；激活成功只
  记录掩码码。接口不能传任意字典，因此请求体、token、签名 URL、Provider 原始
  payload 不会误入公共请求日志。
- Alembic in-process 配置保留既有应用 logger，避免迁移测试/管理工具静默关闭
  request/fencing 审计日志。

### 1.2 私有进程指标

- `/metrics` 不进入 OpenAPI；必须读取 release 目录外的 Bearer token file，鉴权
  在 readiness PG/COS 探针之前完成，错误 token 不能放大依赖负载。
- Nginx 仅向 loopback 暴露 `/internal/metrics/api-1` 与 `api-2`，分别直连两个 API；
  保留 canonical Host/HTTPS 和 operational-only XFF 哨兵，不能只抓 LB 聚合结果。
- exporter 提供按路由模板/状态聚合的请求数与耗时、ready gauge、session fencing
  reject counter 和 fencing verification wait count/total；不使用 request/user 等
  高基数标签。任意未匹配路径归入 `UNMATCHED`，未知 HTTP 扩展方法归入 `OTHER`，
  攻击者输入不能扩张指标序列。

### 1.3 单所有者 P1 异常探针

- systemd timer 每分钟在一个 OPS 主机运行；PostgreSQL transaction advisory lock
  拒绝误启的第二所有者。每条查询均有 statement timeout、row limit，并只读取
  应用表，不要求 `pg_monitor` / `pg_stat_*` 权限。
- 覆盖：同一 epoch 双设备 heartbeat、重复 CHARGE、重复 provider trade no、
  PAID 无 CHARGE、CHARGE 来源/金额/用户不一致、钱包与流水不一致、旧 session
  fencing 拒绝、旧 session 写入实际提交、跨项目/资产访问拒绝、队列饥饿、终态
  任务悬挂 reserve，共 11 类固定 P1 告警。
- 每次成功 fenced 写在业务事务内尝试写入一条 expected/verified epoch 事实；同一
  session/epoch 组合去重，避免按请求无限增长。如果 verifier 回归导致旧写成功，
  不一致事实与业务写同生共死并持续触发 `stale_write_committed`。事实只含 request id、
  摘要和 epoch，不含 user/device/session 明文，且禁止 UPDATE/DELETE/TRUNCATE。
- 双在线按 `(user_id, session_epoch)` 分组，合法 switch 前后的旧/新 epoch 不会
  被误报；跨用户信号只统计既有 `security.project_denied` / `security.asset_denied`
  审计事实，不读取 entity/metadata，也不把标识输出到告警。
- 状态文件原子替换且权限私有；journal 只输出状态边沿 `fired` / `resolved`，内容
  只有固定 alert 名、P1、检查时间和数量。驱动异常只输出固定事件和异常类型，
  不输出 DSN/查询值。

### 1.4 迁移与管理员入口加固

- revision 042 为 session event、recharge status、wallet transaction、wallet cursor
  与 audit action/time 增加探针入口索引；扩展安全事实维度为
  `admin:exchange:ip` 与 `session:fencing`，并增加成功写事务 epoch 去重证据表及
  mismatch 部分索引。
- 管理员一次性 exchange credential 在解析前先消费 PostgreSQL 共享 IP 摘要预算；
  多 API 共用限制，原始 IP 不入审计，失败与 429 均留 append-only 事实。
- 旧 session 的写事务被 fencing 拒绝时追加摘要化安全事实。该审计 hook 或本地
  metrics/log 失败都不能覆盖原 401，也不会输出底层异常消息。
- 042 downgrade 在上述新维度或成功写事务表已有 append-only 证据时拒绝，避免
  丢失审计事实；无证据时按逆序删除表/索引并恢复旧约束。SQLite→PG 导入/对账
  把该表登记为 PG-only：空表允许，非空即视为目标已有分歧状态并 fail closed。

## §2 失败测试与回归锁定

先红后绿覆盖：request-id 控制字符/超长替换、显式日志字段白名单、路由 task/order
提取、权限/legacy 异常/直接入口拒绝业务码、metrics 鉴权先于 readiness、固定
`UNMATCHED` 路径与 `OTHER` HTTP 方法标签、双实例私有 Nginx/systemd 合同、
fencing 指标/日志/持久事实、observability hook 故障不改变业务 401、探针查询面、
成功旧写与 epoch mismatch 事实同事务提交、双在线 epoch 防误报、跨用户拒绝只读
固定审计字段、transition-only 状态机、私有原子状态文件、告警输出脱敏、advisory
lock 丢失零副作用、全查询在真实 PG16 fresh head 执行、管理员共享 IP 预算以及
042 upgrade/append-only/downgrade guard。

开发期组合回归完成 253 passed；随后增加的 request-state 白名单、无效重放
request-id 与未匹配路由固定标签三项安全锁使 ops 专项增至 17 passed。收尾
唯一一次全仓门禁完成 1273 passed；独立复审发现的 1 个 Medium（已验证凭据被
角色/nonce 检查拒绝时失败事实低报）已先红后绿修复，完整 admin auth 72
passed。PR #72 connector 首轮 2 P1 + 2 P2 也已先红后绿修复：任意 404 路径不再
扩张指标标签、钱包一致性先检查异常再限制输出、fencing TEXT 截止时间与 ISO
事实统一使用 `T` 分隔符、队列候选在数量上限前先确认存在超时任务；相关告警与
迁移切片 13 passed。第二轮 2 P1 + 1 P2 已继续先红后绿修复：未知 HTTP 方法固定
归入 `OTHER`、成功旧写产生持久 mismatch 事实并由第 11 类 P1 查询消费、legacy
异常和入口直接响应记录真实业务码。最新 ops 20、alerts/042 14、fencing 55、
customer security 36、health 6、SQLite→PG 35 passed，mypy 全绿；connector 最终
复审与 PR CI 仍待完成，不在本证据中提前声明通过。

## §3 当前验证证据

| 门禁 | 结果 |
| --- | --- |
| T37 ops metrics | 20 passed（含未匹配路径/未知方法低基数锁及真实业务结果码） |
| T37 cluster alerts + migration slice | 14 passed（真实 PG16 fixture；含 11 类告警、成功旧写持久事实与 append-only 锁） |
| customer fencing | 55 passed（真实 PG16 fixture；成功写 epoch 事实、证据故障业务回滚与模拟 verifier 回归） |
| customer security / health | 36 / 6 passed |
| SQLite→PG 导入与对账 | 35 passed（新增 PG-only 表空/非空边界） |
| 管理员 exchange 共享限流 | `test_admin_auth.py` 72 passed |
| admin 关联回归 | 93 passed（既有 marker warning，不是失败） |
| 激活/设备/充值/管理/T37 组合 | 253 passed |
| Secret / client / E2E / Tauri | Pass；client 47 files / 513 tests；E2E Biome 14 files；Cargo fmt/check pass |
| Ruff / format / mypy | Pass；186 files formatted；72 source files typed |
| 全仓 `npm run check` | Pass；server 1273 passed / 16 existing warnings，1013.27s；这是本任务唯一一次完整 pytest |
| 复审 | 独立复审 `APPROVE`：首轮 0C/0H/1M，修复后 0C/0H/0M；PR #72 connector 两轮共 4 P1 + 3 P2 均已回归锁定并修复，最终复审待新提交触发 |

## §4 安全与可观测边界

- metrics token、Authorization、激活码明文、设备/session credential、签名 URL、
  DSN、Provider payload 不进入仓库或日志；指标没有用户级高基数标签。
- 管理员 exchange 按摘要化 ingress client IP 共用 PG budget；应用信任的 client IP
  仍来自 T35/T36 已验证的代理边界，不直接解析任意 XFF。
- anomaly probe 的 counts 是告警信号，不是账务修复器；它不修改钱包、订单、队列
  或 session。钱包/队列候选扫描为有界抽样，完整逐笔恢复/对账属于 T38/T39。
- 当前没有外部告警接收器。只有 journal 时不得开放灰度；真实 fired/resolved、
  unit failure 和 timer missed-run 通知必须在候选环境留证。

## §5 外部授权与未测试项

- **外部授权记录**：无；未使用真实 COS/ZPay/Provider，未部署公网、发码或灰度。
- **未测试项**：集中日志与指标抓取；外部 P1 fired/resolved 接收与值班升级；PG
  connection/lock/slow query/replication lag；backup/PITR；Provider/COS/ZPay 的
  provider task id、费用和统一脱敏错误；真实多实例异常注入与容量基线。
- **后续归属**：PG/备份进入 T38，故障与告警演练进入 T39，外部调用真实链与费用
  字段进入 T40。按用户要求，真实账号集中到 T40 前最后确认。

## §14 任务记录

```text
任务/工作包：T37（仓库侧）/ OPS-02（部分）/ EXT-02（部分）
Owner / Reviewer：OPS/后端（Agent 执行）/ 独立复审 APPROVE（首轮 0C/0H/1M；Medium 修复后 0C/0H/0M）+ PR #72 connector（两轮共 4 P1/3 P2，均已先红后绿修复；最终复审待触发）
分支 / 基线 SHA：feat/customer-v3-t37-observability / main@bb545ca
上游规格段落：客户版任务清单 V3 §7 T37、§12.6 EXT-02、§12.7 OPS-02；测试与验收规格 V3 §8.3；代码开发清单 V3 §3.1–§3.3/§12
改动文件：server/app/ops_metrics.py、main.py、customer_fence.py、permissions.py、admin_auth/customer session/device/activation/recharge routes、security_rate_limit.py；server/scripts/check_ops_alerts.py、reconcile_customer_billing.py；migration 042 + Alembic logger fix；T37/关联回归测试；customer env、Nginx、ops-alert systemd；部署手册、冻结文件映射与任务/证据账本
失败测试或回归锁定：request id/structured fields/result code/secret redaction/private metrics；任意未匹配路径统一 `UNMATCHED`、任意扩展 HTTP 方法统一 `OTHER`；legacy/入口拒绝真实业务码；fencing metrics+durable audit+hook failure isolation；fencing ISO `T` 事实与截止时间同序比较；成功写事务 expected/verified epoch 同事务事实；11 类 cluster anomaly query、epoch anti-false-positive、cross-user denial source、钱包不一致与队列超时任务均在输出上限前完成异常过滤、bounded/app-table-only SQL、transition state/state-file/advisory owner；admin exchange PG-shared rate budget；042 index/append-only/downgrade guard；fresh PG16 head execution
实现结果：HTTP 单行结构化日志和安全字段白名单；双 API 私有 Prometheus exporter；fencing reject/wait 指标；单所有者 PG anomaly probe + fired/resolved 私有状态；P1 面覆盖双在线/资金/账本/旧写拒绝与实际提交/越权拒绝/队列/悬挂 reserve；admin exchange 多实例共享限流；部署与接收器演练手册
验证命令与通过数：T37 ops 20；alerts+042 slice 14（真实 PG16 fixture）；customer fencing 55；customer security 36；health 6；SQLite→PG 35；admin auth 72（含复审修复）；admin related 93；受影响组合 253；client 513；Cargo pass；ruff/format/mypy pass（186/72）；唯一全量 server 1273 passed/16 existing warnings；独立复审最终 APPROVE、0C/0H/0M；PR #72 connector 两轮 4 P1/3 P2 均已修复，最终复审待触发
证据层级：AUTOMATED_VERIFIED（仓库侧）；T37/OPS-02/EXT-02 [~]，不得提升 STAGING
安全与可观测性：日志白名单且无请求体/credential/provider payload；metrics Bearer file + loopback；PG advisory single owner；counts-only P1 output；真实集中平台/接收器/PG监控/外部费用链未冒充完成
迁移与回滚：042 PG-only 追加索引、安全维度与成功写事务 epoch 去重事实；存在新 append-only security/write evidence 时 downgrade fail closed，否则逆序回退；T07 导入对账登记 PG-only 表；应用回滚优先前向修复并保留审计事实
外部授权记录：无；未调用真实 COS/ZPay/Provider，未公网部署/发码/灰度
未测试项：集中平台与 fired/resolved 演练；PG connection/slow/replication/backup；真实外部调用 task id/费用；staging/real-chain/production
Lore 提交 SHA：见本任务 PR squash SHA
```
