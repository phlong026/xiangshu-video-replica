# T36 Evidence Report — 类生产拓扑仓库交付

## Task Summary

- **任务**：T36 — LB、双 API、四 Worker、PostgreSQL HA、私有 COS staging
- **状态**：仓库侧 `AUTOMATED_VERIFIED`；父任务与 OPS-01/COS-01 保持 `[~]`
- **完成日期**：2026-08-26
- **基线**：`main@2a298470e20a4cd14d7fd39e83e701468e9978fb`（T35 PR #70 squash）
- **分支**：`feat/customer-v3-t36-staging-topology`
- **独立复审**：`APPROVE`，0 Critical / 0 High / 0 Medium

本证据只关闭仓库可交付部分。当前没有目标服务器、域名/TLS、PostgreSQL
HA 服务、私有 COS bucket 或真实账号授权，因此没有启动真实双 API/四 Worker，
没有执行 PG failover、节点替换或 COS put/copy/delete，也没有真实客户端 IP 经
LB 的限流桶证据。本任务不能标记 `STAGING_VERIFIED`，更不能提升到
`REAL_CHAIN_VERIFIED` 或 `PRODUCTION_GO`。

## §1 交付结果

### 1.1 API/Worker 启动与健康语义

- 保留兼容 `/health`，新增 `/live`（只证明进程存活）和 `/ready`（依赖感知）。
- 客户生产启动门先验证 PostgreSQL round-trip，再从 PostgreSQL 解密共享设置，
  要求 `active_storage_provider=cos` 与 COS 四项必填配置完整，并对私有 bucket
  执行只读 HEAD。不存在探针对象的 404 仍表示可达；鉴权、网络、region/bucket
  错误均 fail-closed。
- API lifespan、systemd `ExecStartPre` 与直接 Worker 入口使用同一启动门。
  缺失/错误/不可达时 API/Worker 不进入可用状态；运行中依赖故障使 `/ready`
  返回不含内部错误或凭据的通用 503。
- PG readiness 要求 `SHOW transaction_read_only = off`；HA 端点误指只读副本或
  failover 尚未提升主库时，API/Worker 与 `/ready` 均 fail closed，不会在业务
  写入和队列 claim 阶段才暴露故障。
- 内部 SQLite/P0 路径仍返回 `database=internal, storage=local`，原 `/health`
  行为没有改变。

### 1.2 双 API、四 Worker 与可信代理

- `video-replica-api@.service` 以端口为实例参数，手册固定启用 8001/8002；
  Uvicorn 绑定 loopback 且强制 `--no-proxy-headers`。
- `video-replica-worker@.service` 以 1–4 为实例，worker id 为
  `%H-worker-%i`；不 `Requires`/`PartOf` API，API 重启不会连带杀 Worker。
- Nginx upstream 包含两个 API，TLS 默认虚拟主机拒绝未知 SNI；客户 Web 与
  `/admin` 共用 `client/dist` 和 HTTPS origin，`/api/*` 转发到 API。
- Nginx 对兼容 `/health` 使用 exact-match API 反代，避免 SPA fallback 返回
  `index.html` 假装健康 JSON；其 trusted-header 合同与无依赖 `/live` 一致。
- 公开 origin 固定标准 HTTPS 443，构建门和服务端同时拒绝非标准
  端口，避免 Nginx `$host` 与应用 authority 判定分叉。
- 最后一跳覆盖 Host、`X-Forwarded-Proto=https` 与单值
  `X-Forwarded-For=$remote_addr`，不使用 `$proxy_add_x_forwarded_for`；未启用
  `non_idempotent` 重试，代理不会重放付费 POST/PATCH/PUT。业务 `/api/` 仅把
  `error/timeout` 计为进程故障；Provider/存储等路由级 502/503/504 不会误摘除
  健康 API，状态型摘除只由私有 `/ready` 负责。
- `/ready` 在 Nginx 仅向同机监控开放；`http_503` + `max_fails=1`
  使一次失败探测立即把对应 API 从普通 upstream 摘除 30 秒，
  upstream `zone` 使该失败状态在所有 Nginx worker 间共享，
  避免用公网请求占满线程池，也不让已知不就绪节点继续接收业务。该 location
  与 `/live` 使用 operational-only 的 `127.0.0.2` XFF 哨兵，避免同机监控的回环地址与
  Nginx→Uvicorn peer 相等而被防重写门误拒；业务路由仍传真实客户端地址。

### 1.3 PostgreSQL 与运维边界

- `deploy/postgres/migrate.sh` 要求 PG DSN、`set -euo pipefail`，并在唯一指定
  迁移主机使用 non-blocking `flock` 串行 Alembic `upgrade head`；脚本先复用
  客户生产数据库门，SQLite、缺失 TLS sslmode 或可明文降级的 DSN 会在 Alembic
  触碰目标库前失败。
- 对完整迁移后仍满足 pristine-state 合同的新库，提供显式确认的 one-shot 初始化
  命令：在 SERIALIZABLE 事务和 PG advisory lock 下确认用户、钱包、Provider/COS、
  审计与 runtime 默认值均未被业务写入，再创建首位 admin/钱包、加密 COS 配置、
  runtime 选择和无 secret 审计，并只输出 15 分钟单次 exchange credential；任一
  半初始化或已有用户状态均拒绝，因此不能用作通用提权或设置覆盖入口。真实 PG16
  pristine 空库迁移后已验证该路径及第二次调用拒绝语义；手册在输出重定向前显式
  创建 `0700 root:root` 的 `/run/video-replica`，不依赖重启后已消失的临时目录。
- PostgreSQL 16 文档要求 HA 主端点、私网/TLS、最小权限、连接预算和禁止公网；
  客户生产启动门拒绝缺失 `sslmode` 和可降级为明文的
  `disable/allow/prefer`，模板固定 `verify-full` + CA/主机名校验；T38 前不宣称
  备份/PITR 已验证。
- 部署手册给出管理端同源静态部署、COS 启动前受控配置、ready/live、滚动发布、
  Worker 逐个替换和前向修复优先的回滚流程。
- 客户生产人物图片缓存写入确定性的私有 COS `character-cache/` 对象；缓存
  POST 只在短 PG 事务内完成 fencing、资产授权与加密存储配置加载，事务退出后
  才执行 COS HEAD/源 GET/PUT；签名 URL 在任意 API 副本都从同一共享对象读取，
  公开 GET 同样在 COS 网络读取前释放 PG 连接。内部 SQLite 单机版继续使用原本
  的本地文件缓存，不改变 P0 行为。

### 1.4 客户云 Windows 构建

- 内部默认构建继续启用 `local-sidecar`。
- 客户目标同时执行 `cargo check --no-default-features` 与
  `tauri build --config src-tauri/tauri.customer.conf.json ... -- --no-default-features`；
  客户专用配置把 `bundle.resources` 清空，不打包内部版的
  `start-backend.bat/.sh`，移除 CSP 中的本地 API origin，并为客户版
  单独冻结 product name、application identifier、publisher 和开始菜单目录，
  防止与内部版覆盖安装或串联升级。构建前要求
  `VITE_API_BASE_URL` 是无凭据、无 path/query/fragment、可路由且非 loopback 的
  HTTPS origin；IPv4/IPv6 unspecified、link-local、multicast、reserved/documentation
  literal（含 mapped IPv4）均 fail closed，避免“没有 sidecar 但仍嵌入本机或
  不可达地址”的不可用假构建。
- 客户配置覆写完整主窗口并把启动 URL 固定为 `customer`，编译后 WebView 直接
  进入 `/customer` 激活/设备登录车道，不会落到默认路径的内部工作台。
- Windows CI 保留默认内部版 NSIS 构建与
  `unsigned-windows-nsis-${{ github.sha }}` 产物，再隔离输出并构建客户版
  `unsigned-customer-cloud-windows-nsis-${{ github.sha }}`。客户版使用 `.invalid`
  地址只验证合同，产物明确是 `UNSIGNED_INTERNAL_TEST`，不能用于 staging。
- 本机真实 release/NSIS 构建成功；编译后的 app binary 不包含
  `VIDEO_REPLICA_BOOT_COMMAND`、`start-backend.bat` 或 `127.0.0.1:8000` 字符串。
  Windows CI 进一步用 7-Zip 检查客户 NSIS payload，发现任一
  `start-backend.bat/.sh` 即失败。

## §2 失败测试与回归锁定

先提交 `test_customer_ha_smoke.py`，首轮 7 项全部失败（缺 endpoints、PG/COS
gate、Nginx/systemd/PG/docs 文件）。实现与评审修复后扩展为 23 项，其中基础
交付的 10 项为：

1. `/live` 与内部 `/ready` 契约；
2. 缺 COS 时启动门失败；
3. PG 强制 TLS + COS 配置与只读 HEAD 成功；
4. `/ready` 503 不泄漏底层 DSN/secret；
5. 直接客户 Worker 在 COS 不可用时不进入循环；
6. Nginx 两 upstream、SPA、转发头覆盖与重试边界；
7. API/Worker systemd 实例合同和独立生命周期；
8. maintenance 单 owner 与 PG migration `flock`；
9. 部署/证据/PG HA 文档分级；
10. 客户云 no-sidecar、HTTPS origin 与 CI artifact 合同。

PR connector 最后两项 P1 另以红测试锁定：迁移脚本必须在 Alembic 前执行生产
PG/TLS 校验；空库 one-shot 必须原子初始化首位 admin 与加密 COS、已有用户时
拒绝；部署手册必须先迁移再初始化。另在独立临时数据库完成真实 PostgreSQL 16
全迁移与 schema/约束/加密/二次调用回归，不以 mock 冒充可执行首装路径。

最终发布合同复审又锁定客户桌面启动路由、兼容 `/health` exact proxy，以及
`tauri.customer.conf.json`/`require_customer_api_base.mjs` 在冻结文件映射中的
登记。两项合同测试先红后绿；项目锁定 Tauri 2.11.4 CLI 完成客户配置解析、Web
构建和 `--no-default-features` 无打包桌面编译，生成 binary 中含客户 main-window
`customer` URL 配置。

全量门禁发现既有 `test_build_contracts.py` 仍断言 Windows job 必须构建默认
sidecar NSIS；T36 已有意把该 job 改成客户云目标，因此更新回归合同，专项
18/18 通过（Git Bash 下无 skip）。

## §3 验证证据

| 门禁 | 结果 |
| --- | --- |
| T36 首轮失败基线 | 7 failed（预期红） |
| T36 最新专项 | 23 passed |
| build-contract + T36 复验 | 18 passed，0 skipped |
| PG 客户生产 TLS 启动门 | 13 passed（含 8 项 sslmode 配置锁 + 1 项真实 API lifespan 锁） |
| PR 评审修复回归 | 94 passed，38 skipped（含同机监控哨兵；本机未连 PG fixture 的预期跳过） |
| PG 连接/DSN 脱敏复验 | 11 passed（含 T36 专项） |
| settings/storage/T36 相关回归 | 78 passed |
| admin auth/customer security/T36 | 75 passed / 36 skipped（PG fixture 未启动的早期专项；后续全量已在 fixture 下覆盖） |
| 最新评审修复切片 | 136 passed / 2 skipped（PG fixture 已启动；含 admin auth、customer security、T36、build contract 与真实空库首装） |
| PG16 空库首装集成 | 1 passed：完整 Alembic head 后真实写入 admin/钱包/加密 COS/runtime/audit，第二次调用拒绝 |
| 客户启动路由/health/冻结映射/origin guard | 40 passed / 2 skipped；项目锁定 Tauri 2.11.4 客户配置解析 + Web build + no-default-features app build passed |
| PG writable + T36/构建/迁移复验 | 86 passed / 2 skipped；真实 PG fixture 验证 primary read-write gate 与既有迁移/bootstrap 回归 |
| 人物/RBAC/T36/构建多实例复验 | 122 passed / 2 skipped；共享 COS 人物缓存跨两个不同本地目录读取成功，事件序列锁定 PG 退出早于 COS HEAD/源 GET/PUT，内部本地缓存回归保持通过 |
| 生成/设置/DB 相关专项 | 169 passed / 10 skipped；唯一 PG fixture 连接超时在 fixture 启动后单例复验通过，不是代码失败 |
| Secret scan | Pass：无运行时硬编码凭据 |
| Client | 47 files / 513 tests passed |
| E2E Biome | 14 files passed |
| Default Tauri check | Pass |
| Customer Tauri check | Pass：`--no-default-features` |
| Customer unsigned NSIS | Pass：`短视频复刻客户云工作台_0.1.0_x64-setup.exe`，1,219,752 bytes；SHA256 `5CEBDAFC4F64DB49A6DCD74C818B422F5D51D3484F656156379A62C9BEA8B6E8`；客户配置排除内部 launcher 并分离应用身份后重建；`.invalid` 合同产物，未提交 |
| Ruff / format / mypy | Pass；181 files formatted / 71 source files typed |
| Server full pytest | 单次全量：1200 passed + 1 stale build-contract failed，16 warnings，1169.23s；失败断言修复后其所在两文件 18/18，按“全量只跑一次”规则未重复第二轮全量 |
| Independent review | APPROVE，0 Critical / 0 High / 0 Medium |
| PR connector review | 阻塞式 readiness、PG 事务/primary 可写边界、COS bucket/共享人物缓存与缓存 I/O 前释放 PG、客户 NSIS 资源/安装身份/启动路由、origin 端口及 non-destination literal 一致性、私有 ready/upstream 摘除、业务 503 不误摘 API、兼容 health exact proxy、冻结文件映射、Alembic 前生产 DSN 校验、空库首装路径及 ephemeral `/run` 输出目录均已实质修复；最终复审由 PR 留痕 |

上述分段证据证明当前 1201 个收集项中的唯一失败已修复；PR CI
负责最终的完整 Linux 质量门和 Windows NSIS payload 解包证据。本地
缺少 7-Zip，因此只记录真实 NSIS 构建与哈希，不用静态合同冒充解包证据，
也不用仓库级结果冒充真实 staging。

## §4 安全、迁移与回滚

- **Secret**：真实 DSN、COS key、激活码、设备/session token、签名 URL 均未写入
  仓库、测试夹具或证据；示例仅含不可路由/占位值。
- **代理**：Nginx 只覆盖转发头；API 保留 T35 原始 peer 校验；所有 Uvicorn
  产品入口保持 `--no-proxy-headers`。
- **COS**：readiness 仅执行 HEAD，不写/删对象；完整私有桶最小权限与归档链
  留到 T40 经授权执行。
- **数据库迁移**：本任务没有新增 Alembic revision；迁移脚本在 Alembic 前调用
  与运行时相同的生产数据库校验，生产 PG DSN 必须选择
  `require/verify-ca/verify-full` 之一，部署模板使用 `verify-full` + 只读 CA，避免
  libpq `prefer` 降级明文；部署脚本只串行执行既有 `upgrade head`。空库首装输入
  与一次性输出按 root-only 临时文件处置，不进参数、日志或仓库。回滚优先上一
  应用 SHA/前向 schema 修复，不在新旧应用并写时 downgrade。
- **桌面构建**：本地 unsigned `.invalid` artifact 仅作合同验证，不发布、不上传。

## §5 外部授权与未测试项

- **外部授权**：无；没有使用真实服务器、域名/TLS、COS、ZPay 或付费 Provider，
  没有对外发码、灰度或公网发布。
- **未测试/阻塞**：真实 LB 双 API、四 Worker 跨故障域；PG HA failover；私有
  COS put/head/get/copy/archive/delete 与最小权限；节点替换；不同真实客户端 IP
  的 PG 限流桶；真实 staging Windows origin 与签名 installer。
- **下一门**：用户最后集中提供目标服务器/域名/证书/PG16 HA/COS 与真实账号
  授权后，按部署手册执行并把 OPS-01/COS-01 提升为 `STAGING_VERIFIED` 或
  `REAL_CHAIN_VERIFIED`；在此之前保持 `[~]`。

## §14 任务记录

```text
任务/工作包：T36（仓库侧）/ OPS-01（部分）/ EXT-01（COS 部分）/ COS-01（部分）/ DESK-02
Owner / Reviewer：OPS/DB/后端/Tauri（Agent 执行）/ 独立 Code Reviewer（APPROVE，0C/0H/0M）
分支 / 基线 SHA：feat/customer-v3-t36-staging-topology / main@2a298470e20a4cd14d7fd39e83e701468e9978fb
上游规格段落：客户版任务清单 V3 §7 T36、§12.5 COS-01/EXT-01、§12.6 DESK-02、§12.7 OPS-01、§13–§16；代码开发清单 V3 §11.1/§12；测试与验收规格 V3 §1/Gate B
改动文件：bootstrap/main/generation_worker；test_customer_ha_smoke/test_postgres_migrations/test_build_contracts；customer env；Nginx、API/Worker systemd、PG migration/README；customer Tauri config/build guard/CI；冻结文件映射、部署与证据模板；T36 证据/任务账本
失败测试或回归锁定：7 项先红；T36 最新 23 项；build-contract + T36 基础合计 18/18；最新评审修复切片 136 passed/2 skipped；真实 PG16 空库首装 1 passed；覆盖 PG TLS/primary writable/COS HEAD gate、ready/live 同机哨兵、业务 status 不误摘 API、direct Worker、proxy/systemd、Alembic 前生产 DSN 门、空库原子首装、半初始化库拒绝矩阵、跨副本共享人物缓存及 PG-exit→COS HEAD/GET/PUT 顺序、内部/客户双构建、non-destination origin 与客户 NSIS payload 合同
实现结果：仓库形成可部署的 LB+双 API+四 Worker 形状；API/Worker PG（强制 TLS sslmode、primary writable，模板 verify-full + CA）+私有 COS fail-closed；业务路由只因 transport failure 摘除 API，客户人物缓存使用共享 COS 且缓存网络 I/O 不占用业务 PG 连接；同源 Web/admin；可信代理覆盖头且 health 不落 SPA；单主迁移锁且 Alembic 前校验目标；空库 one-shot 原子创建首 admin/钱包/加密 COS/runtime/audit 并拒绝半初始化库；客户云无 sidecar、无内部 launcher、强制可路由非 loopback HTTPS origin 且直接启动 `/customer`；滚动/回滚/证据手册就绪
验证命令与通过数：client 513；T36 最新 23；build+T36 18；PG 客户生产 TLS 门 13（8 项 sslmode 配置锁 + 1 项真实 API lifespan 锁）；最新评审回归 136 passed/2 skipped；真实 PG16 空库首装 1 passed；客户启动/health/map/origin 40 passed/2 skipped；PG writable/T36/build/migration 86 passed/2 skipped；人物/RBAC/T36/build 122 passed/2 skipped；锁定 Tauri CLI app build pass；settings/storage/T36 78；Ruff/format/mypy/Tauri pass；customer NSIS build pass + SHA256；server 单次全量 1200 pass/1 stale contract，修复后相关专项全绿；独立复核 0C/0H/0M；PR connector 意见均已修复
证据层级：AUTOMATED_VERIFIED；T36/OPS-01/COS-01 保持部分完成，不高于 AUTOMATED_VERIFIED；DESK-02 [x]
安全与可观测性：无真实 secret；COS HEAD 只读；503/日志不泄漏底层错误；proxy overwrite-only；T37 结构化日志/指标/P1 告警尚未开始
迁移与回滚：无新 revision；flock 单迁移主机在生产 PG/TLS 校验后执行既有 Alembic head；迁移后空库用拒绝非空目标的 one-shot 初始化；滚动 API/Worker 和应用回滚手册已写；真实恢复/PITR 属 T38
外部授权记录：无；未调用真实 COS/ZPay/Provider，未部署公网，未发码/灰度
未测试项：真实 TLS/LB/双 API/四 Worker/PG HA/COS/节点替换/客户端 IP 桶；真实 Windows staging origin/签名/实机
Lore 提交 SHA：见本任务 PR squash SHA
```
