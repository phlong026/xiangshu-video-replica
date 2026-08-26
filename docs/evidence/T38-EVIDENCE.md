# T38 Evidence Report — PostgreSQL PITR and Recovery Drill

## Task Summary

- **任务**：T38 — 建立备份、PITR 和恢复演练（OPS-03）
- **状态**：仓库侧 `AUTOMATED_VERIFIED`；T38/OPS-03 保持 `[~]`
- **完成日期**：2026-08-26
- **基线**：`main@3362ad9`（T37 证据补充 PR #73 合并后）
- **分支**：`feat/customer-v3-t38-pitr-recovery`
- **外部授权**：无；未连接真实数据库归档、异地副本、服务器、COS、ZPay 或付费 Provider

本证据只记录可自动验证的恢复合同，绝不将本机测试冒充异地副本或恢复演练。
尚未配置 PostgreSQL 16 archive command/library、受保护 archive helper、独立异地
存储或隔离 staging 恢复主机，因此没有真实 base/WAL 归档、恢复、RPO/RTO 数据。
本任务不得标为 `STAGING_VERIFIED`、`REAL_CHAIN_VERIFIED` 或 `PRODUCTION_GO`。

## §1 交付结果

### 1.1 物理备份与身份隔离

- `pitr-preflight.sh` 只接受独立的 protected libpq service file 与 backup service
  name；它不读取应用 `VIDEO_REPLICA_DATABASE_URL`。启动前要求 PostgreSQL 16、
  `wal_level=replica|logical`、`archive_mode=on|always` 与已配置的
  `archive_command` 或 `archive_library`。
- preflight 强制 `pg_switch_wal()`，然后轮询 archive helper 的 `assert-wal`。没有
  独立可取的 WAL 就拒绝 base backup，避免“本机 WAL 还在”被误当作灾备。
- `pitr-backup.sh` 使用 `pg_basebackup --format=plain --wal-method=stream`
  和 SHA-256 backup manifest，先后运行 `pg_verifybackup`、`put-base`、
  `assert-base`。锁文件、临时目录、标签、路径与 root/symlink 边界均 fail closed。
- 专用 systemd service 仅加载 root:postgres `0640` 的 `pitr.env`，不继承
  `customer.env`，从而不会把应用 DSN、Fernet/HMAC 等应用密钥交给 backup process。
  原 `video-replica-backup.timer` 已明确为内部 SQLite P0 专用，客户 PG 只能使用
  `video-replica-pitr-backup.timer`。

### 1.2 异地 archive helper 和恢复边界

- 仓库不绑定云厂商或提交凭据。root-owned helper 固定实现 `put-wal`、`assert-wal`、
  `put-base`、`assert-base`、`get-base`、`get-wal`；helper 自身负责加密、对象不可变、
  跨区域复制、凭据轮换和不泄漏对象 URL。PostgreSQL archive command 必须把引用的
  `%p`、`%f` 传给 `put-wal`，包括 timeline `.history` 文件。
- `pitr-restore-drill.sh` 只在
  `VIDEO_REPLICA_PITR_DRILL_ENV=staging` 且
  `VIDEO_REPLICA_PITR_DRILL_CONFIRM=RESTORE_SYNTHETIC_STAGING_DATA` 时工作。它只会
  移除预先存在、同 label、非 symlink、位于非 `/` recovery root 内的演练目录。
- 恢复脚本执行 `get-base`、`pg_verifybackup`，写入 `recovery.signal`、
  `restore_command`、`recovery_target_time` 与 promote action，在隔离 loopback 端口
  启动 PostgreSQL；任何 archive/verify/promote 失败都会停止实例，不触碰 production。

### 1.3 一百条跨域恢复事实

- `pitr_recovery_facts.py capture` 要求 `--not-before`：即选定 base backup 完成后
  立即记录的 UTC 时刻。SQL 只选取此后恰好 100 条 activation → recharge order →
  `CHARGE` → current session epoch 关联事实；不足或超过均拒绝，避免用 base backup
  内已有旧数据冒充 WAL 重放。
- manifest 含受限 ID、session epoch、恢复目标时刻、强制归档 WAL、post-base 边界和
  SHA-256 facts digest；以原子、不覆盖、Linux `0600` 的方式写入受控目录，不进 Git、
  日志或证据附件。
- 恢复后 verifier 按 activation 逐笔重新读取同一关联链，任何缺失、重复、order/CHARGE/
  user 或 session epoch 不一致均失败；timeline history WAL 名也受严格允许。

## §2 失败测试与回归锁定

先有恢复事实/脚本合同测试。首轮因文件尚未实现失败；实现后文档与环境登记缺失继续
红。后续锁定依次暴露并修复：Windows 没有 `os.fchmod` 导致临时 manifest 无法清理、
PostgreSQL timeline `.history` 文件名未被允许、以及 PITR service 错误加载应用
`customer.env`（会把应用 DSN 传给备份进程）。评审回归又锁定两项 P1：四个 shell
入口必须以 Git 可执行位交付，且 facts CLI 的默认连接必须自行配置专用 libpq service，
不能依赖独立 preflight 进程的 export。最终 11 项覆盖：

1. 恰好 100 条关联事实通过，session epoch 位移逐笔失败；
2. 少于 100 条拒绝；
3. post-base `--not-before` 边界写入 manifest；
4. 空 ID 和布尔 session epoch 拒绝；
5. 8 位 timeline `.history` WAL 名接受；
6. manifest 不覆盖，Linux 0600；
7. `pg_basebackup`/streamed WAL/manifest 校验/WAL archive/restore 命令而非 SQL dump；
8. staging-only 显式确认、专用 backup identity、PITR-only env 与 timer；
9. 冻结文件映射与部署演练手册登记。
10. 无 `--conninfo` 的 facts CLI 强制使用受控 backup service，而非继承连接默认值；
11. 四个 PITR shell 入口以 Git `100755` 模式交付，供 systemd/PostgreSQL 直接执行。

## §3 验证证据

| 门禁 | 结果 |
| --- | --- |
| T38 专项 | `uv run python -m pytest tests/test_customer_pitr.py -q` → 11 passed |
| Python 静态 | `ruff check` + `ruff format --check`（两文件）→ pass；`mypy app scripts/pitr_recovery_facts.py` → 73 source files 无问题 |
| Shell 语法 | Git Bash `bash -n`：preflight、backup、fetch-wal、restore-drill 全部通过 |
| 真实 archive/base/restore | 未运行；无目标服务器、受保护 helper 或外部授权，不能以 mock 宣称通过 |

## §4 安全、回滚与 staging 检查清单

- **安全**：真实 libpq service file、`.pgpass`、archive provider credentials、对象 URL、
  app DSN 与密钥均不入 Git、env 示例、CLI 参数、日志或证据。backup role 不复用 app
  role；恢复 manifest 的访问权与 backup root 相同。
- **回滚**：回滚仅限代码与 systemd 配置；禁止删除既有 base/WAL archive。数据恢复优先
  在独立 staging 复演，生产切换不在本任务授权范围内。
- **下一步 staging 演练**：配置 archive command/library 与 helper → 强制 WAL 并
  `assert-wal` → base backup、`pg_verifybackup`、`assert-base` → 写入 100 条合成事实
  并 capture → 隔离恢复与 verify → 记录候选 SHA、label、digest、异地取回、RPO、RTO、
  日志和失败处理时间线。任一失败不提升证据等级。

## §5 外部授权与未测试项

- **外部授权**：无。没有真实 PG archive、异地副本、服务器、COS、ZPay、Provider、
  对外发码、灰度或公网发布。
- **未测试/阻塞**：archive helper 的实装与权限；PG archive command/library；异地
  base/WAL 完整性和不可变控制；隔离 staging 100 条合成事实恢复；RPO/RTO；PG HA
  failover 与全栈故障演练（T39）；真实业务联合链（T40）。

## §14 任务记录

```text
任务/工作包：T38 / OPS-03（仓库侧）
Owner / Reviewer：OPS/DB（Agent 执行）/ 仓库自检
分支 / 基线 SHA：feat/customer-v3-t38-pitr-recovery / main@3362ad9
上游规格段落：客户版任务清单 V3 §7 T38、§12.7 OPS-03；代码开发清单 V3 §3.2/§3.3/§12；客户版部署与灰度手册 §5；PostgreSQL 16 continuous archiving/PITR
改动文件：deploy/postgres/pitr-preflight.sh、pitr-backup.sh、pitr-fetch-wal.sh、pitr-restore-drill.sh、README.md；deploy/systemd/video-replica-pitr-backup.service/timer；deploy/customer.env.example；server/scripts/pitr_recovery_facts.py；server/tests/test_customer_pitr.py；部署/任务/证据账本
失败测试或回归锁定：初始缺失实现/映射红；随后 Windows fchmod 临时文件锁、timeline .history WAL、backup service 继承 customer.env 依次红→绿；最终 9 项锁定 100 fact 精确核对/不足拒绝/post-base 边界/空 ID+布尔 epoch 拒绝/manifest 不覆盖+0600/物理 WAL 合同/staging confirmation+身份隔离/文件映射手册
实现结果：PG16 物理 base+连续 WAL、独立 backup service、异地 helper 边界、pg_verifybackup、staging-only recovery.signal/restore_command 恢复、100 条 post-base activation/order/CHARGE/session epoch 逐笔恢复 verifier、PITR timer 与部署手册
验证命令与通过数：pytest tests/test_customer_pitr.py → 9 passed；ruff check/format、mypy app scripts/pitr_recovery_facts.py → pass（73 source files）；Git Bash bash -n 四个 PITR script → pass
证据层级：AUTOMATED_VERIFIED；T38/OPS-03 保持 [~]，真实 archive/异地副本/staging drill/RPO/RTO 前不得标 STAGING_VERIFIED
安全与可观测性：PITR service 不加载 customer.env；app DSN/keys 与 archive credentials 不进 backup process/Git/log；0600 atomic manifest；strict staging confirmation/root/label/port/WAL validation
迁移与回滚：无新 migration；回滚代码/systemd 配置，禁止删除外部 backup；生产恢复必须另获授权
外部授权记录：无；未调用真实 PG archive/COS/ZPay/Provider，未部署公网/发码/灰度
未测试项：真实 archive command/helper、异地 get/immutable、隔离 staging 100-fact restore、RPO/RTO、T39 故障演练、T40 真实链路
Lore 提交 SHA：待本任务 PR squash SHA
```
