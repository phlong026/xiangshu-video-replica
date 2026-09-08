# 客户云版收敛：数据、部署、CI 与回滚审计

审计日期：2026-09-08。基线：`bffc341`，分支 `codex/customer-cloud-convergence-analysis-20260908`。本附件为只读代码审计及实施建议；没有连接业务数据库、启动 PG fixture、执行迁移、构建安装包或调用真实 Provider。下列源码引用均相对此分析工作树，行号对应本次基线。

## 1. 结论

目标已经有相当一部分基础：客户 Tauri 构建不启动本地后端，PG 是客户运行真源，私有 COS 承担持久资产，API 与 Worker 有多实例部署入口。收敛的主要工作是把客户路径升为唯一默认路径，剥离内部身份、本地 SQLite、持久本地资产及 sidecar 的运行分支，并补齐测试和部署交付。

不能按 `internal` 或 `sqlite` 文件名批量删除。`022_internal_billing.py` 创建的钱包、订单和流水仍是客户版底座；`db_portable.py` 的 PG 连接门面仍承载事务及 session fencing 的提交语义；SQLite 也是大量共享业务测试和旧数据迁移的载体。

建议保留一个仓库，交付两个应用制品：客户桌面安装包，以及后端服务镜像/发布包。后端内部仍应分 API、Worker、迁移及运维进程；“一个后端服务”不应被解释为将异步生成塞回 API 进程。仓库外的 PostgreSQL、COS、密钥存储与反向代理仍是运行依赖。

## 2. 数据库真实边界

| 代码 | 已存在的行为 | 收敛建议 |
|---|---|---|
| `server/app/db.py:13-46` | SQLite 文件连接、WAL、外键、busy timeout；初始化自动 Alembic upgrade | 移出在线运行路径。若确有旧 SQLite 数据待迁，暂留为一次性迁移工具依赖，最后再归档 |
| `server/app/db_pg.py:122-154` | URL 优先，既可选择 PG 也可 SQLite；无 URL 可回落 DB_PATH | 服务端唯一接受 PG；去掉在线 SQLite 选择和无配置的内部回落 |
| `server/app/db_pg.py:157-192` | 仅在 CUSTOMER_PRODUCTION 开关打开时强制 PG、拒绝 DB_PATH、校验 TLS | 将“客户产品必须 PG”提升为无条件产品约束；开发与生产差异只管理外部依赖及安全环境，不再代表另一产品 |
| `server/app/db_portable.py:3-18` | 一套 PG SQL，SQLite 使用有限翻译器 | 先保留业务门面名称，删 SQLite 翻译和后端时避免同步大面积重命名 |
| `server/app/db_portable.py:320-439` | PG 下 commit/rollback/transaction 不擅自提交，由外层 fenced transaction 负责；也容忍历史 BEGIN IMMEDIATE/PRAGMA | 删除 SQLite 时必须保留 PG 外层提交权。逐调用者清理历史语句后才删 no-op 兼容，不能把 conn.commit() 改成真提交 |
| `server/app/bootstrap.py:579-620` | PG 分支检查 readiness，SQLite 分支初始化本地运行环境 | 唯一保留 PG bootstrap；不要在每个 API/Worker 启动时自动跑结构迁移 |
| `server/app/generation_worker.py:1699-1758` | PG Worker 与 SQLite Worker 在入口分流 | 保留 PG 循环，删除 SQLite round/forever 及入口回退，连带更新测试 |

当前静态规模：`server/app` 顶层 103 个 Python 文件，其中 32 个直接出现 sqlite3 import；11 个出现 SQLite 连接/初始化相关标记，11 个出现 SQLite backend/门面标记。这些集合有重叠，且部分 sqlite3 引用只用于行类型或异常兼容，不代表 32 套独立内部业务。

### 历史迁移必须完整保护

`server/migrations/versions` 本次静态枚举 76 个 Python revision 文件。文件编号存在历史缺号，不能为“整齐”重编号或重接 down_revision。

- `022_internal_billing.py:40-57` 创建 `internal_access_tokens`；同一文件 `59-77` 创建 `wallets`，`79-142` 创建 `recharge_orders`，`144` 起创建 `wallet_transactions`。它是混合职责的历史迁移，不能整体删除。
- `025_postgres_runtime_compatibility.py:1-16,33-45` 修复 022 在 PG 下终结流水唯一索引的条件缺失。证明历史内部命名也参与 PG 空库建表链。
- `026_customer_security_and_billing.py:1-35,106-117` 在同一订单表上扩展激活码/客户充值/管理员调账，并在 PG 上创建管理员会话；SQLite 跳过该扩展。
- `025_postgres_runtime_compatibility.py:49-77` 已有带结算流水时拒绝 downgrade 的保护。因此“upgrade/downgrade 在空库通过”不能等同“真实客户库可任意回退”。
- `server/migrations/env.py:35-48` 读取与运行层相同的 DSN；`59-80` 维护较长的 Alembic revision 列；`84-92` 明确当前 offline SQL 输出不能作为真实可执行迁移脚本。

实施时先保留历史表结构，关闭内部 token 的创建/验证/管理入口并清理消费者。需要物理清理内部专用表时，另起追加迁移，并事先证明客户查询不再 join 它、旧数据已处理、回滚不会复活失效 token。不要为了删代码连带抹去历史账务 `INTERNAL` scope 或旧单据快照。

## 3. 三种数据情形必须分开

### A. 保留现有客户 PG，不导入旧内部数据

这是最少数据风险的默认收敛方向，但必须用实际数据盘点确认是否符合业务需要。保留现有客户 user/device/session/activation/project/asset/wallet/order/ledger，收敛期间只做兼容性追加迁移。内部 SQLite 做私密只读归档，关闭所有写入入口，不混入生产客户库。

### B. 旧 SQLite 要迁入一套全新客户 PG

仓库已有可复用工具，但有严格前提：

1. `server/scripts/sqlite_to_postgres.py:121-155` 要求源与目标都等于当前 release head。旧数据库必须先在受控副本上验证升级，不可直接赌原库可以升级。
2. `158-176` 要求停写维护窗口，并使用 PG 事务 advisory lock。
3. `196-229` 校验表、列、主键和 PG 专用表状态；后者有客户数据时拒绝导入。`server/scripts/reconcile_customer_billing.py:44-99` 维护 PG-only 表/列和种子约定。
4. `536-604` 在一个 PG 事务内导入并对账；完全一致重放可返回 already_reconciled；目标非空且不一致直接拒绝覆盖或合并（`564-568`）。
5. `607-620` 创建只读 SQLite 快照后开始导入；报告只记录计数与摘要，不输出原始业务值。

这个工具复制 DB 行，不负责账户转客户、签发激活码、重建设备登录或将旧磁盘文件搬入 COS。导入成功后的内部身份不能被自动当作有效客户会话。客户资格、旧余额和账户归属应作为独立的业务迁移决策。

### C. 旧 SQLite 要并入已有客户 PG

当前工具明确不支持。不能把空库保护删掉后直接执行。需另行设计按用户/项目选择的导入映射、冲突策略、数据血缘和完整对账：

- 旧用户映射到哪个客户，是否已存在同名/同 ID 用户；不以用户名碰巧相同自动合并。
- 项目、资产、人物版本、任务、草稿和 JSON 中引用的 ID 如何保持关系；旧对象 key 是否与客户 COS 冲突。
- 历史充值单、provider trade number、幂等键、任务计费轮次及流水序号如何保持唯一性与可追踪性。
- 余额若转入，只能经审计化业务单据/完整账务导入表达；不得直接累加 wallets，也不能制造无来源 CHARGE。
- 激活码、设备/session token 不复制为客户登录凭据，旧内部 token 不继续有效。

在现有客户 PG 与内部 SQLite 的实际行数、迁移版本、资产量与使用情况未核实前，无法断言需要 B 还是 C。此不影响先完成产品入口和运行模式收敛的代码计划。

## 4. local://、文件处理与多实例

### 持久资产与临时工作文件是两类东西

`server/app/storage.py:466-558` 的 LocalStorageAdapter 把对象写本机持久目录并签发 local:// URL；`storage.py:207-221` 要求 STORAGE_ROOT。`server/app/media_routes.py:147-169` 在 COS 缺失时仍可回退本地，也可根据旧资产 local URI 找本地 adapter。

客户生产已经拒绝 STORAGE_ROOT（`server/app/bootstrap.py:270-273`），并强制 active_storage_provider=cos、验证配置、执行桶 readiness（`540-575`）。因此数据库记录若仍是 local://，只迁 DB 不搬文件会留下客户无法读取的资产。

现有对账工具检查的是：资产 ID 是否存在、JSON 资产引用是否孤立、行摘要是否一致，以及钱包/订单/计费关系（`server/scripts/reconcile_customer_billing.py:1072-1153`）。它没有遍历真实存储对象、复制文件或校验 COS 中对象内容。因此需要额外资产迁移清单：旧 URI、目标 bucket/key、size、SHA-256、引用计数及复制/校验状态。先复制和逐对象校验，再在事务内替换引用；回滚窗口内保留原文件，不能先删除旧目录。

以下服务器端临时文件能力必须保留：

- `server/app/source_frames.py:146-226` 下载内容后在 TemporaryDirectory 中调用 FFmpeg 抽帧。
- `server/app/media.py:106-142` 用临时文件和 ffprobe 做视频探测。
- `server/app/script_from_audio.py:508-578` 从对象存储读取源视频、在临时目录抽音轨、上传临时音频对象；不确定/等待回执时保留音频，清理前检查 lease。

目标要求是：持久状态和可恢复对象在 PG/COS，进程本地目录只放可重建、受容量与时限控制的临时工作文件。各 API/Worker 不依赖另一进程的 /tmp，也不共享客户桌面的文件路径。systemd `PrivateTmp=true` 与此模型一致。

### Worker 应作为后端进程保留

PG Worker 入口已说明先提交任务领取及 SUBMITTING，崩溃后进入不确定回执核验路径，不能静默重复付费（`generation_worker.py:1710-1717`）；生成、口播、拆解等任务在 `run_pg_worker_round` 内分步处理（例如 `1180-1295`）。删除内部 SQLite Worker 不能顺手删整个 Worker 文件或恢复链。

真实多实例验收应证明：API A 上传/创建，API B 可读，任意 Worker 可领取；杀死领取者后新 Worker 接管/核验不会重复付费；跨进程队列容量、口播/生成共享限制、钱包预留释放和最终归档一致。已有测试入口包括 `test_worker_crash_recovery.py:283,481,700,842`、`test_postgres_migrations.py:1619,1740`，但本次没有执行，不能标记为已通过。

## 5. 客户桌面包现状

| 证据 | 结论 |
|---|---|
| `client/src-tauri/tauri.customer.conf.json:12,23-31` | 客户入口为 customer；resources=[]，文案明确不包含本地 API/Worker |
| `package.json:23` | 客户构建显式加载 customer 配置并使用 --no-default-features |
| `client/src-tauri/Cargo.toml:12-18` | 默认 feature 仍为 local-sidecar，属于仍待收敛的默认路径 |
| `client/src-tauri/src/lib.rs:15-19,119-139` | 本地后端启动和退出清理受 local-sidecar 条件编译控制 |
| `client/src-tauri/src/lib.rs:90-103` | 客户仍需凭据保存、设备实例 ID 和下载操作；这些本地桌面能力应保留 |
| `.github/workflows/ci.yml:201-231` | CI 构建客户包并解包排除两个 start-backend 脚本；没有证明完整包中不存在 Python/FFmpeg 等其它载荷 |

当前客户构建配置没有声明打包 Python、FFmpeg 或 server 目录。未实际构建/解包，不能把配置检查冒充实物检查。内部分发的 `start-backend.bat:33-43` 仍以 uv+Python 开发布局为默认，只留 PyInstaller 覆盖接口；不能据此声称内部安装包已完整自带 Python。

发现文档漂移：`server/app/media_tools.py:3-4`、`client/src-tauri/resources/ffmpeg/README.md:3-6,19-21` 仍称客户 NSIS 分发 FFmpeg，后者还声称 CI 会验证两个二进制；实际客户 resources=[]，当前 CI 只检查启动脚本不在包中。实施时同步更正为“FFmpeg/ffprobe 是后端运行依赖”。`scripts/ffmpeg-minimal` 若仅用于 Windows sidecar 包，应随内部打包链退出；先核对是否另有后台镜像消费者。

新唯一客户安装包需增补实物验收：解包确认无 server/.venv、Python、FFmpeg、SQLite 业务数据库、start-backend 或本地启动命令；在没有 Python/uv/FFmpeg 的干净 Windows 环境安装、启动、登录、上传、预览、生成、下载和卸载。保留签名/升级策略及客户 identifier 的连续性，避免误当成全新产品导致凭据和升级通道丢失。

## 6. 部署制品裁剪与当前交付缺口

### 保留并作为唯一交付主线

- `deploy/customer.env.example:1-21`：PG、私有 COS、HTTPS origin、可信代理及客户安全约束。
- `deploy/systemd/video-replica-api@.service:10-25` 与 `video-replica-worker@.service:10-25`：共享客户 env、独立进程、无本地持久资产目录。
- `deploy/nginx/customer.conf.example:5-18,73-89,122-140`：双 API 入口、ready 私有、业务请求转发、admin 的 Web 静态入口。桌面前端唯一化不等于删除运营管理员后台。
- `deploy/postgres/migrate.sh:15-25`：指定单一迁移主机、host-local flock、先验证客户 PG/TLS、再 upgrade。flock 不是跨主机锁，部署入口要统一。
- `deploy/postgres/pitr-*`、PITR systemd service/timer、maintenance、ops-alerts 及对应脚本：这些属于客户后端运维，不能当内部版删除。

### 内部运行主线可退出，但不应第一步销毁

- `deploy/internal-p0.env.example`、`deploy/nginx/internal-p0.conf.example`。
- `deploy/systemd/video-replica-api.service` 与 `video-replica-worker.service`：没有 @ 的单例单元读 internal-p0.env。
- `deploy/systemd/video-replica-backup.service` / `.timer`：SQLite 日备份；客户使用独立 PITR（见 `deploy/postgres/README.md:79-85`）。
- `scripts/p0_acceptance_evidence.py`、`app/gate1_bootstrap.py`/`gate1_e2e.py` 和内部安装启动脚本：先审查其中通用验证逻辑的消费者，再归档内部流程。
- `app/backup.py`：即使 SQLite 在线日备份退出，它仍被一次性 sqlite_to_postgres 导入工具引用（该脚本 `33`）；旧数据迁移结束前不能直接删除。

### 当前 Git 发布脚本不等于完整后端部署包

`deploy/customer-git-rollout.sh:22-39,124-135,162-176` 依赖现有主机目录、仓库外 compose.yaml、旧镜像及服务用户；Python 依赖变化时拒绝发布，要求另发基础镜像。`202-238` 在旧镜像上覆盖源码与 migrations；当前脚本不是可在空白机器上完整重建环境的 compose/Dockerfile。

建议把客户服务基础镜像和标准编排定义纳入正式交付，API/Worker 共用同一镜像、用不同命令启动，迁移作为一次性部署作业；确认后选择 systemd 或容器为默认文档路径，避免继续维护两套互相漂移的默认部署说明。

运行依赖还有一个小缺口：bootstrap readiness 只检查 ffprobe（`bootstrap.py:550-552`），抽帧和音轨提取实际还要求 ffmpeg。Git rollout 镜像检查了两者（`customer-git-rollout.sh:217-218`），但 systemd 新机路径仅查 `/usr/bin/ffprobe`。后端唯一化时应统一检查两者及所需 codec，不能仅凭 /ready 成功宣告所有媒体流程可运行。

## 7. 测试迁移与 CI 收敛

### 本次静态扫描口径

顶层 `server/tests` 共 92 个 `test_*.py` 文件，AST 计数为 1709 个以 test_ 开头的函数定义。这不是 pytest 收集用例数；参数化会扩展用例，运行时跳过也会改变结果。

SQLite 连接、初始化、Backend 或翻译器任一标记的并集是 46 个测试文件，这些文件共有 892 个 test_ 函数定义。不能把 892 解释为“全是 SQLite 用例”或按比例裁掉；许多文件同时测试共享业务与 PG。分别统计：29 个文件有 sqlite3 import，44 个有 SQLite 连接/初始化标记，43 个有 SQLite 门面标记。29 个文件出现 PG marker 或 TEST_POSTGRESQL_URL 标记。

`server/tests/conftest.py:8-17` 还给全体测试默认开启 development identity header，并禁用本机真实 keychain。收敛时需把客户 PG/客户身份 fixture 建为业务 API 测试主线；操作系统凭据隔离仍保留。不要仅把默认 auth_mode 改成 customer 而不改种子和会话 fixture，否则会产生大面积与真实变更无关的失败。

建议四类处理：

| 类别 | 示例 | 处理 |
|---|---|---|
| 纯内部路径测试 | internal_access_tokens、internal_deployment、local_settings_key、sidecar launcher 合同 | 对应代码退出时删除或移到归档工具；先拆出共享断言 |
| 共享业务测试 | generation、oral、materials、first_frames、script_from_audio、wallet_billing_service | 保留业务用例并迁 PG fixture，覆盖真实约束、事务和租约 |
| 客户安全/并发测试 | activation、devices、sessions、fencing、queue、worker_crash_recovery | 保留，任何跳过都不得视为客户验收通过 |
| 历史兼容/导入测试 | postgres_migrations、migration_dialect_contract、sqlite_to_postgres | 保留空库建表、历史 PG 升级、账务 downgrade 拒绝；SQLite 导入在迁移工具退役前保留 |

PG fixture 不可用时，`test_postgres_migrations.py:129-145` 和 `test_customer_fencing.py:99-105` 会 skip。CI 当前有 PostgreSQL 16 服务（`.github/workflows/ci.yml:43-61`），它应继续成为客户功能验证基座。不要并行运行使用相同测试库名的全量 pytest。

### CI 应只产出客户包，但保留三类门禁

- Secret scan 保留。
- Linux 保留前端 lint/typecheck/unit、Rust 检查与测试、服务端 lint/typecheck/PG 全量、客户浏览器 E2E、web build 与依赖审计；现有职责见 `.github/workflows/ci.yml:112-143`。浏览器 E2E 可作为客户桌面 WebView 业务覆盖，不能因不售浏览器版就删除。
- Windows 删除内部 NSIS 的构建、SHA、归档及“清内部输出再构建客户”步骤（`.github/workflows/ci.yml:170-200`）。客户 NSIS 升为唯一默认构建，保留实物检查、SHA 和归档。
- `package.json:10,13-24` 里 check:tauri 默认仍检查带 sidecar 的 target，tauri:build 默认仍是内部配置。主命令应指向客户配置，最终无需让用户记住 :customer 后缀。
- `server/tests/test_build_contracts.py:105-164` 当前显式要求两种安装包和内部步骤；修改 CI 必须同时改成唯一客户合同，而不是只改 YAML 后跳过失败测试。

## 8. 推荐实施顺序与回滚

1. 冻结基线与保护资料：保存 Git SHA、唯一迁移 head、待保留功能和数据类型清单。仅收集行数/状态/资产摘要，不记录密钥、token 或激活码明文。
2. 建立 PG 客户测试主线：先让原有共享功能在客户身份、PG、对象存储替身下有覆盖，避免删 SQLite 时丢失回归保障。
3. 客户桌面成为唯一默认：裁 sidecar、内部资源与内部 NSIS；保持客户 identifier、凭据与下载能力。此阶段不改真实数据库结构，回滚是恢复旧客户端制品。
4. 准备唯一客户后端路径：先使客户发布固定使用 PG/客户身份，保留 PG 门面和外层提交语义。涉及待迁历史数据时，内部认证、SQLite bootstrap/Worker 和本地资产 fallback 的最终删除须等第 5 步完成，不能先切断读取/导入能力；回滚是部署先前兼容代码，数据库继续 PG。
5. 数据处理独立执行：根据 A/B/C 的实际需要安排；先演练副本。local 文件复制并校验后才改引用，内部写入停掉后不双写。
6. 清理历史运行包和文档：移出内部部署、启动/备份与遗留测试；保留 migration 链和必要导入工具。物理删除专用表安排后续追加迁移，不与 UI/入口删除混为一次大改。
7. 在 staging 用真实多 API/Worker、共享 PG/COS和干净 Windows 客户包验收，再按已授权发布范围推进。真实付费 Provider、生产 COS/迁移和公网发布需单独明确授权。

`customer-git-rollout.sh:64-84` 失败回滚恢复 compose/站点并重启旧镜像，显式保留前向 schema；`184-200` 的 pg_dump 是部署快照，不能替代物理备份+连续 WAL 的 PITR（`deploy/postgres/README.md:36-84`）。所有新增迁移必须兼容上一版代码才能沿用此回滚策略；若删除列/改约束破坏旧代码，需先拆成 expand → 回填/验证 → contract，而非宣传脚本可自动回滚。

已确认订单、计费流水和审计不通过数据库降级或快照回灌静默删除。发生上线后新业务写入，应先停止风险写入、恢复兼容代码；资金差异走审计化补偿。PITR 仅用于经批准的灾难恢复，必须选择恢复点并对上线后数据与外部支付/Provider 回执重新核对。

## 9. 完成判定与本次未知项

代码完成：唯一客户端构建、唯一客户身份与 PG 后端；不存在可用内部登录/sidecar/SQLite 在线入口；管理员运维、钱包、生成、恢复等客户必要能力保留；历史迁移完整；相关静态/单元/PG/CI 检查通过。

数据完成：实际保留对象和账户归属已确认；迁移版本与行摘要、钱包流水、订单、任务、外键/JSON 引用、COS 内容 SHA 一致；无待迁 local://；源快照、对象副本和回滚期明确。

运行完成：干净 Windows 安装包连接指定 HTTPS 后端；跨 API/Worker 读写、双设备会话、旧 session fencing、超时/断连/重启/归档恢复不越权、不重复扣费；实际恢复演练通过并记录证据。

本次没有核实：生产实际 revision/用户与账务规模、内部 SQLite 数量与价值、local 资产数量和磁盘可达性、当前 COS 桶对象、已部署 compose/base image、真实多实例拓扑、付费链路与 PITR 演练。因此只能证明源码能力与裁剪边界，不能声称数据已经迁完、生产已经就绪，也不能给出未经基准测试的并发容量数字。
