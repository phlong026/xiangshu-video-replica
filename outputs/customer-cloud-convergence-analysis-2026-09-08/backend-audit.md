# 后端收敛审计附件

基线：独立分析工作树，提交 `bffc341`，2026-09-08。范围：`server/app` 认证、路由、业务、计费、Worker、存储。仅源码分析：未改应用、未连生产、未调用 Provider/ZPay/COS、未运行测试。结论为 `CODE_PRESENT`，不代表自动或真实桌面验收通过。

## 一、核心判断

目前是同一个 FastAPI 应用承担内部 SQLite/本地身份/Worker 与客户 PostgreSQL/设备会话/云端 Worker 两种运行模式。`main.py:325-361` 无条件注册业务、客户、管理和旧设置路由。只设置客户环境变量会禁止内部身份，但不等于内部运行与发行内容已经删除。

建议先固定客户唯一入口、补前端到客户 API 的连接，再将后端固定为客户模式，最后删除内部分支。无需重写业务。目标“一个后端服务”可包含同版本的 API 和 Worker 进程，共享 PostgreSQL、服务端配置和私有对象存储。

禁止按名字删除：`internal_billing.py` 是客户共用账务核心；`control_routes.py` 仍服务客户运营；`settings_routes.py` 内辅助函数和 `local_download_signature` 仍被云版直接调用。

## 二、当前身份与数据库调用链

客户激活/设备/会话入口分别由 `activation_code_routes.py`、`customer_device_routes.py`、`customer_session_routes.py` 与对应 service 承担。会话路由（4-40）区分 device token、session token，提供单在线切换、幂等和限流。

业务读取经 `auth.py:82-121` → `authenticate_customer_read_session`（147-179）→ `verify_session_context`；业务写入经 `BusinessDbDep` → `get_business_db`（`customer_fence.py:416-430`）→ `BusinessDb.write`（348-385）→ `fenced_pg_transaction`（182 起）。`customer_auth.py:101-189` 锁定会话行，核对 user/device/session/epoch/lease、激活码 ACTIVE、设备 BOUND，并以锁后的数据库时钟判断过期。收敛必须保留事务内重新验证，不能只在请求入口检查 token。

管理员走 `control_auth.py:73-103` → `admin_auth_routes.get_admin_actor`（732 起）及 `get_admin_writer`（771 起），客户生产已使用逐人会话和 CSRF。支付回调（`payment_routes.py:50-92`）则用验签和请求 PG 事务，不需要客户会话。

`permissions.py:228-274` 保留客户 owner 限制；该权限模块与管理审计角色不能随内部版删除。内部账户 CLI（`internal_accounts.py:90-124`）独立使用 SQLite `--db-path`，正常应用入口没有导入它，但测试仍依赖 create-user/issue-token。它可退出客户发行物，其用户、钱包和历史记录不能一并删除。

## 三、保留、改造、退出清单

| 模块/文件 | 判定 | 具体理由与证据 |
|---|---|---|
| `main.py` | 改造后保留 | 保留客户 ingress、生命周期、异常、CORS、观测与业务路由；退出 loopback 产品分支及旧路由注册。当前边界在 177-278，注册在 325-361 |
| `auth.py` | 改造后保留 | `get_database` 同时提供 SQLite/PG，`authenticate_request` 同时提供内部/客户身份；保留 PG、客户读取身份、通用用户模型，逐项删除内部降级路径 |
| `customer_auth.py`、`customer_fence.py` | 保留，后者减分支 | fencing 本身保留；`BusinessDb.write` 的 SQLite 386-413、`get_business_read_conn` 的 SQLite 440-461 可在 PG 测试完成后退出 |
| `activation_code_*`、`customer_device_*`、`customer_session_*`、`customer_idempotency.py` | 保留 | 客户开通、设备限制、单在线、重复请求保护的专属链 |
| `admin_*`、`control_routes.py` | 保留客户运营用途 | 发码、客户、设备、会话、费率、利润、运行参数、审计、对账、导出均为客户产品运营能力，不是内部本地产品的同义词 |
| `control_auth.py` | 保留客户适配，退出共享身份 | 24-70 是内部代理单账号；73-103 已转接客户逐管理员 session。先把消费者固定到客户适配，后删除旧 token 环境变量及函数 |
| `internal_accounts.py` | 退出产品运行与发行物 | 独立 SQLite 用户/token CLI；测试的 create_user/issue_token fixture 需先替换，历史身份数据仍需审计 |
| `internal_billing.py` | 必须保留，可后续语义重命名 | `generation.py:1786-1801` 统一预扣；`generation.py:5183` 成功结算，另有失败、取消、重试及对账调用；`oral.py:30`、`oral_worker.py:26` 共用口播计费 |
| `wallet_routes.py`、`recharge_routes.py` | 客户入口统一后减旧端点 | 客户已有 `/api/customer/wallet`、`/api/customer/recharge-orders`；旧读接口仍允许客户本人，新旧接口响应并不等价，不能直接粗暴删文件 |
| `payment_routes.py`、`zpay.py`、`zpay_payments.py` | 保留 | 客户充值回调与管理员同步复用；修正 `payment_routes.py:100`“返回内部系统”遗留文案 |
| `settings.py` | 保留服务端仓库，减本地密钥回退 | Provider/ZPay 配置 Fernet 加密持久化（80-138,180-193）；业务、Worker、control 依赖。361-385 本地 OS keystore 回退可退出产品 |
| `settings_routes.py` | 先搬共用能力，再退出旧入口 | `/api/settings` 依赖旧 `SettingsAdmin`（235-247）；`control_routes.py:41-48` 仍导入配置合并、ProviderTester、COS lifecycle 处理 |
| `local_settings_key.py` | 退出客户运行 | 客户启动已要求集中服务端 key，bootstrap.py:300-310；仍被 settings/bootstrap/tests 导入，要先移除消费者 |
| `generation.py`、`generation_routes.py` | 保留业务主链 | 版本、提示词、批次、重试、取消、幂等、结果、账务与队列共用；不能因内部测试多而判断为内部版 |
| `generation_worker.py`、`oral_worker.py` | 保留云端 Worker；退出 SQLite 执行器 | generation_worker.py:1074 起为 PG worker；1681-1758 根据数据库模式分流；客户保留短事务领取/提交/结果落库及续租机制 |
| 分析/人物/首帧/抽帧、口播/独立创作/改写/ASR、studio/materials/viral | 保留客户业务核心，退出旧身份专属入口 | main.py:325-361 注册；PG Worker 调用见第六节，不能按内部测试数量判断归属 |
| `storage.py`、`media.py`、`media_routes.py`、`rbac_routes.py` | 保留云端素材权限和传输；减本地路径 | local adapter、local-objects 路由、进程本地缓存分支可退出；私有云签名、上传完成确认、资产 owner 权限不能删 |
| `ops_metrics.py`、`security_rate_limit.py`、`admin_write_contract.py` | 保留 | 客户限流、追踪、审计及管理写幂等都仍有用途 |

退出文件前必须完成消费者迁移并通过相关测试。

## 四、计费不能按“internal”清除

真实调用链：

```text
POST /api/projects/{project_id}/generation-batches
  generation_routes.py:432-453
  -> BusinessDb.write（客户会话事务）
  -> generation.create_generation_batch
  -> _reserve_generation_credit（1786-1801）
  -> internal_billing.reserve_internal_billing（134 起）
  -> wallets.available_credits/reserved_credits + wallet_transactions.RESERVE

云端 Worker 获得交付结果
  -> generation.finalize_generation_direct_result（5134-5185）
  -> internal_billing.finalize_internal_billing（657-783）
  -> SETTLE / RELEASE 及钱包余额变更
```

预扣和结算未按 customer 分流到另一个钱包服务。该模块还承载口播及悬挂预扣对账，`server/scripts/reconcile_dangling_billing_reservations.py:21` 直接依赖它。

`runtime_settings.internal_base_unit_price_fen` 也不是可直接删除的内部专属字段：`admin_activation_routes.py:199-206` 读取它制作激活批次价格快照；`admin_customer_routes.py:229-266` 读取它作为客户费率默认值；`settings.py:325-354` 提供客户价格覆盖。若希望清理名字，应在后续独立变更中保留历史快照语义、兼容 API 和迁移链，不能随着内部发行物一起 drop。

## 五、已经确认的客户入口衔接风险

### 1. 旧钱包面板与客户充值 API 不等价

结合主分析中的前端调用链，客户工作区可能进入内部 `WalletPanel`。服务端语义已经确认：

| 请求 | 客户生产使用有效客户 session 的静态结果 | 证据 |
|---|---|---|
| `GET /api/wallet` | 可以读本人余额；customer 响应隐藏内部单价、最低充值、步进字段 | `wallet_routes.py:49-88`、`auth.py:115-121` |
| `GET /api/wallet/transactions` | 可以读本人流水，按 actor.id 过滤 | `wallet_routes.py:92-115` |
| `GET /api/recharge-orders`、`/{order_no}` | 可以读本人订单，按 user_id/owner 过滤 | `recharge_routes.py:987-1035` |
| `POST /api/recharge-orders` | 明确拒绝 customer，403 `CUSTOMER_RECHARGE_ROUTE_REQUIRED` | `recharge_routes.py:275-292` |
| `POST /api/customer/recharge-orders` | 正确客户入口，带会话与 Idempotency-Key，使用客户费率链 | `recharge_routes.py:325-355` 及后续处理 |

旧读路由并非对客户都 401；余额可显示也不代表客户钱包已接通。应接客户专用 API，验证费率、充值/二维码、订单查询/关闭、重复请求与会话切换。已有拒绝内部充值路径的测试：`test_customer_recharge.py:265-290`，本次未运行。

### 2. 当前 H3 成片交付已经是 Provider 直链

`generation_worker.py:1048-1061` 在 Provider 成功后调 `finalize_generation_direct_result`；`generation.py:5142-5185` 设置 `DIRECT`、`result_asset_id=NULL`、质检 `NOT_REQUIRED` 并结算，不复制成片到 COS。

这与历史概览“COS 归档质检”不等价。保留当前行为须验证直链可用期及过期恢复；要求永久保存成片到本方 COS 则是额外业务变更。本次未查询 Provider 生命周期，不声称其具体天数。

源参考视频、人物图片、首帧等仍依赖 COS：`media_routes.py:147-155` 选择存储；`bootstrap.py:540-575` 在客户 API/Worker 启动检查 ffprobe、PG、私有 COS readiness。不能因为 H3 结果为 DIRECT 而删除 COS。

### 3. 旧设置路由不能直接删文件

`/api/settings` 使用 `SettingsAdmin`，底层是业务 `AuthenticatedUser` 加 admin role（settings_routes.py:235-247）。客户生产 `auth.py:115-121` 禁止内部 Bearer、使用客户 session；正常客户角色不能通过此 admin gate。客户运营使用 `/api/control/settings` 的逐管理员 session 链。目标应该停止注册旧 `/api/settings`，统一到客户管理配置入口；先移动 `control_routes.py:41-48` 依赖的测试和配置辅助函数，再删除旧路由承载。

### 4. local 字样也可能承载云端功能

`media_routes.py:298-327` 用 `local_download_signature` 验证 user/asset/session_epoch 绑定的授权，`/api/assets/signed-objects/*`（602-609）可代理本地或私有 COS 读取。`rbac_routes.py:175-189` 的客户人物缓存明确使用 COS。清理范围应该是本地持久化实现和备用入口，不能删除共用签名函数、会话撤销传播或所有 cache 函数。

## 六、Worker 范围、队列与计费的差异

`generation_worker.py` 的 PG 路径实际轮询 viral 刷新/导入（1106/1116）、口播（1163）、H3 生成（1203-1205）、人物多视图（1230）、分析（1250）、改写（1301）、ASR（1339）、生成对账（1350）、抽帧（1390）、首帧（1402）、人物组合图（1517）。退出 SQLite Worker 不得误删这些云端入口。

| 任务 | 队列与钱包装配 | 不能扩大声称的范围 |
|---|---|---|
| H3复刻 | `generation.py:4797-4814` 根据 fair_queue 开关选择用户公平队列/全局 FIFO；预扣和结算共用 internal_billing | 生产开关值未核实；不能只因存在T25就宣称当前公平队列已开启 |
| 独立创作 | `independent.py:338` 写同一 generation 批次表，384-390 使用同一秒数预扣和 user_queue_cursor；由同一 H3 Worker 处理 | 复用不是新模式业务/异常测试的替代 |
| 口播 | `oral_worker.py:148-221,300-311` 共享用户并发槽及跨H3/口播并发上限；`oral.py:695` 预扣，Worker 592-598 起以任务 lease/CAS 在事务内结算 | 153-157明确完整跨类型 round-robin 仍待完善；安全并发上限不等于全类型公平排队 |
| 首帧/人物组合图 | `image_tasks.py:483-549` 各表领取/租约；Worker记录 Provider 成本 | 未走 H3 的 user_queue_cursor；成本记录不等于钱包预扣 |
| 改写/ASR | `script_rewrite.py:451-500`、`script_from_audio.py:288-336` 各任务表领取，含提交不明/过期恢复 | 未走 H3 用户公平游标，不能直接套用H3公平性与计费验收 |

客户提交层的 fencing 与后台任务租约是两层控制：`independent_routes.py:32-38`、`oral_routes.py:136-138` 等、`script_from_audio_routes.py:32-34`、`first_frame_routes.py:258-260` 走 `BusinessDb.write`；Worker 执行已接收任务时以自己的任务 lease/CAS 控制回写，而不是把客户活跃 session 当成运行许可。两者应分别测试。

人物模块也有两条入口：客户简化人物在 `simple_character_routes.py` 多处使用 `BusinessDbDep`；旧完整人物版本/发布入口（`character_generation_routes.py`）依赖 `CharacterAdmin`，后者在 `character_identity_routes.py:157-171` 只允许 admin。保留客户人物库不等于保留旧管理员角色的全部产品页面，也不能声称所有人物写路由已有客户 fencing。

H3 的 PG 执行器（`generation_worker.py:830-845,897-952`）保留短事务之间的 Provider 调用、上游 task id 持久化与 `SUBMISSION_UNCERTAIN` 隔离。推荐同一后端代码工程独立启动 API 与 Worker。H3 的这一执行形态、旧 T25 或单一队列测试，不应推广为全部新增模块已完成同等验证。

## 七、建议实施顺序与完成判据

1. 固定客户入口和现有交付行为；补钱包等交叉接线，验证入口 → API → 身份 → 事务 → 结果。
2. 固定客户 PG 后端模式，禁止错误配置自动降级到 SQLite、固定用户、内部 token 或 local storage；测试替身独立保留。
3. 迁移旧路由文件中的共用工具，更新消费者后取消旧设置/内部充值等注册，用路由清单断言证明退出入口不可达。
4. 退出内部账户 CLI、SQLite Worker、桌面后端 bootstrap、本地密钥/存储运行分支；历史数据和迁移按另一附件处理。
5. 单独处理命名债务，避免把 internal_billing 等重命名和大范围删除混在同一次变更。

验收按功能和任务类别分别执行：客户激活/设备/会话；项目/素材隔离与事务内 fencing；管理 session/CSRF/只读/审计；客户费率、充值幂等、回调；H3/独立创作/口播账务与混合排队；图片/改写/ASR等独立任务恢复；COS授权和会话撤销；Provider直链交付。辅助任务不能仅靠旧H3队列测试验收。

已有专项包括 `test_customer_fencing.py`、`test_customer_security.py`、`test_customer_recharge.py`、`test_wallet_routes.py`、`test_worker_crash_recovery.py`、`test_customer_queue_fairness.py`、`test_oral_domain.py`、`test_independent_creation.py` 及图片/ASR/改写测试。名称含 internal 的测试也需按断言保留：`test_internal_access_tokens.py:281` 正在验证客户生产拒绝内部 Bearer。

本次只阅读源码和已有测试断言，未运行测试，未做真实收费、部署或桌面验收。
