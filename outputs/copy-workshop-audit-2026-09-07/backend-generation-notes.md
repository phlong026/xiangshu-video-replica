# 文案工坊后端生成与恢复链审查备忘

审查日期：2026-09-07。范围：音频提文案、AI 普通改写、按人物 IP 改写的路由、权限、任务持久化、Worker、ASR/LLM 适配、异常恢复及相关迁移。只读业务源码；未修改业务代码、未提交、未调用真实 Provider。本文是主报告的后端专项输入，不代表整模块验收。

## 1. 结论

后端具有真实服务适配代码，普通改写与按 IP 改写并非占位；上传视频提文案也有抽音频、临时存储、ASR、结果存储的实现。但不能据此判定客户版端到端完成：**标准 PostgreSQL Worker 没有消费音频提文案任务**。另外，ASR 的并发互斥、租约写回、异常归类、任务恢复与清理仍有确定缺口。改写结果目前主要依赖提示词约束，缺少输出完整性及内容质量的程序校验。

## 2. 按实际执行顺序读取的源码与能力

下表所有路径相对仓库 `/Users/honor.pei/Documents/订单项目/乡墅爆款短视频复刻`；后续问题条目给出完整路径和关键行。

| 步骤 | 文件与行 | 已有业务行为 | 审查判断 |
|---|---|---|---|
| 提取路由挂载 | `server/app/main.py:56,328`；`script_from_audio_routes.py:24–80` | POST 项目提取、GET 任务、GET 项目最新任务；202 返回 | 已实现 |
| 改写路由 | `generation_routes.py:150–237` | POST 项目改写、GET 任务、GET 最新；最新查询支持 all/identity/none | 已实现 |
| 客户写权限 | `customer_fence.py:364–384`；`script_from_audio.py:151–163`；`script_rewrite.py:279–290` | 客户写事务有会话 fencing，均检查项目访问权并拒绝 auditor 写入 | 已实现，真实登录角色验收另计 |
| 输入约束 | `script_from_audio.py:50–54,164–176`；`script_rewrite.py:56–61,90–100` | ASR 要 source_asset_id 且资产属于项目；改写 1–20000 字符且拒绝纯空白 | 部分；ASR 无资产媒体类型/大小硬校验 |
| 服务配置 | `asr.py:294–350`；`script_rewrite.py:238–267` | 服务端加密配置读取；ASR 配置 DashScope、改写配置 DeepSeek；API 入队前检查，Worker 再解析；任务不存 key | 已有真实适配，配置可用与付费成功未验 |
| IP 选择与快照 | `script_rewrite.py:112–235,295–320` | 本人名下未归档 identity、基础 persona；冻结六个人物字段及 revision，生成 hash，和文本一起进入幂等指纹 | 已实现，不读取不断变化的人物档案来替代排队时快照 |
| 改写入库互斥 | `script_rewrite.py:322–432`；迁移 `048:80–92`、`074:18–38` | 幂等键冲突、活跃项目 partial unique、并发 INSERT 冲突后再比较请求，任务结果与 IP 快照落库 | 较完整 |
| ASR 入库互斥 | `script_from_audio.py:177–266`；迁移 `068:78–93` | 普通串行请求可幂等/互斥；有幂等 unique，但无活跃项目 partial unique | 部分，见 BG-02 |
| 改写领取/恢复 | `script_rewrite.py:451–562,580–649` | SKIP LOCKED 领取、5 分钟租约；未发出恢复 PENDING、已发出过期标不确定；写回校验 RUNNING/locked_by | 已实现基本恢复机制，无服务商对账/取回结果入口 |
| ASR 领取/恢复 | `script_from_audio.py:269–399,437–506` | 20 分钟租约、过期区分未提交/已提交；但开始/成功/失败 UPDATE 只按 id | 部分，见 BG-03/BG-04 |
| SQLite Worker | `generation_worker.py:415–475` | 同一循环处理改写和 ASR，完成后存结果 | 有执行链，专项测试使用这一分支 |
| PG Worker | `generation_worker.py:890,1058–1097,1382–1395,1457–1487` | 改写有消费；改写后直接到 reconcile，没有 ASR 领取/执行 | ASR 客户主链断开，见 BG-01 |
| ASR 原文件处理 | `script_from_audio.py:344–434`；`media_tools.py:29–99` | 读取存储原视频；ffmpeg→16kHz 单声道低码率 AAC/M4A；ffprobe 时长；上传临时对象；30 分钟签名 URL；ASR；finally 删除对象，本地临时目录释放 | 正常结束路径实现，进程崩溃/删除失败没有复删闭环 |
| ASR 服务调用 | `asr.py:138–267` | ≤300s Flash 同步；更长提交异步任务→轮询→下载转写 JSON；输出全文/时长 | 真实 HTTP 实现；仅保留全文，未保留词级时间戳，语言字段固定 zh |
| LLM 服务调用 | `script_rewrite.py:734–820` | OpenAI 兼容 chat/completions；120s 超时；固定 temperature=1.3/max_tokens=2048；记录 provider/model | 真实 HTTP 实现；未检查 finish_reason/内容类型/长度等 |
| 结果边界 | `script_from_audio.py:437–465`；`script_rewrite.py:580–605` | 生成结果存任务 result_json，不直接写项目 script 版本或 confirmed 草稿 | 合理：生成候选与客户终稿应分开 |
| 费用边界 | `docs/文案工坊优化-C7草稿库与ASR文案链路设计-2026-09-06.md:117–119` | 已明确 ASR/解析客户计费、单价/配额另立项，本次能力接通不默认向客户计费 | 不将“没有钱包扣费”误判为本轮功能缺陷；真实供应商仍可能产生费用 |

## 3. 确定问题与客户影响

### BG-01 / P1：客户 PostgreSQL Worker 不处理音频提文案

- 启动按数据库模式选 Worker：`/Users/honor.pei/Documents/订单项目/乡墅爆款短视频复刻/server/app/generation_worker.py:1457`；PG 一次执行调用 `run_pg_worker_round`（1478），循环执行 `run_forever_pg`（1487）。PG round 指向 `run_pg_worker_once`（1389）。
- PG 函数的改写执行位于 1058–1095，1096 立即进入 reconcile；整个文件 `acquire_script_from_audio_task(...)` 实际调用仅在 SQLite 通用循环 445 行。
- API POST 已正常入 `script_from_audio_tasks` 为 PENDING（`script_from_audio.py:221–235`），GET 也只读任务。没有旁路后台队列消费者。
- 客户影响：完成上传后点击“提取文案”，服务端可能已受理但永远不生成，刷新/最新任务查询仍 PENDING；后续文案工坊没有原文可用。不能用 SQLite 测试成功证明客户 PG 运行已闭环。
- 验收：对真实 PostgreSQL fixture 建任务，直接跑生产使用的 `run_pg_worker_once`，使用假的存储/ASR 确认任务 SUCCEEDED；再做独立 Worker 进程、重启恢复和真实服务验收。

### BG-02 / P1：ASR 项目互斥只有先查后插，数据库未兜底

- `/Users/honor.pei/Documents/订单项目/乡墅爆款短视频复刻/server/app/script_from_audio.py:201` 先查活跃任务，221 执行 INSERT ON CONFLICT DO NOTHING。
- `/Users/honor.pei/Documents/订单项目/乡墅爆款短视频复刻/server/migrations/versions/068_script_from_audio_tasks.py:84` 只有 unique(project_id,idempotency_key)，没有对同项目 PENDING/RUNNING 的 partial unique。不同幂等键并发请求可以同时通过先查。
- 对比改写迁移 `/Users/honor.pei/Documents/订单项目/乡墅爆款短视频复刻/server/migrations/versions/048_async_script_rewrite_tasks.py:87` 已建活跃项目 partial unique，且改写域冲突回读后重验 hash；ASR 在 241–249 的回读甚至没有重验 request_hash。
- 客户影响：重复点击/两窗口/重试请求可能产生并行 ASR、重复供应商调用；相同原视频临时 key 按项目+内容 hash 共享（script_from_audio.py:407），也有相互清理对象的风险。
- 本轮隔离 SQLite 临时库探针确认：当前迁移允许同项目两个不同幂等键 PENDING 行同时存在。此项是数据库约束缺失的执行证据，并非已经完成真实 PG 并发时序压测。

### BG-03 / P1：ASR 旧租约可以覆盖已回收终态

- `/Users/honor.pei/Documents/订单项目/乡墅爆款短视频复刻/server/app/script_from_audio.py:393` 标记已提交、451 成功写回、489 失败写回均只 `WHERE id = %s`；未检查 status、locked_by、attempt，也不校验 rowcount。
- 同文件 291–297 会将过期且已提交任务变成 SUBMISSION_UNCERTAIN 并清理锁。但旧 Worker 回来仍可将该行写成 SUCCEEDED/FAILED，甚至覆盖新持有者的状态。
- 本轮零 Provider 探针实测：先领取→人为过期→第二 Worker 回收成 SUBMISSION_UNCERTAIN→用旧 lease 完成，状态被覆盖为 SUCCEEDED。
- 客户影响：页面状态可能反复变化、错误任务被当完成、运维对账状态被晚到结果覆盖。需要使用与改写一致的 CAS，并核对 attempt/租约所有权。

### BG-04 / P1：ASR 长任务没有可恢复的服务商回执，且超时与提交阶段归类不准确

- `/Users/honor.pei/Documents/订单项目/乡墅爆款短视频复刻/server/app/asr.py:187–190` 异步 task_id 仅为局部变量，拿到回执后不写任务表；任务表迁移 068 没有 provider_task_id。崩溃后无法继续轮询原任务。
- `asr.py:212–238` 在本进程循环 sleep+HTTP；默认 90 次，间隔 2s，单轮请求 timeout 15s（31–33、221）。慢请求尾部理论可超过 20 分钟租约（`script_from_audio.py:45`）；Worker 没有为该任务续租。它还占用整个同进程 worker 轮次，其他类型要等本轮执行结束。
- `/Users/honor.pei/Documents/订单项目/乡墅爆款短视频复刻/server/app/generation_worker.py:454–459` 在读原视频、抽音频、上传临时对象之前已标记 provider_started。纯本地/存储失败也可能误判成服务商提交不确定。
- `asr.py:98–99,238` 将网络错误/轮询超时统一抛 AsrProviderError；`script_from_audio.py:478–480` 又统一当 FAILED，而非保留“可能已受理”的不确定性。retryable=false 与“请稍后重试”的文案也可能矛盾。
- 客户影响：长音频/网络抖动时等待结果难以恢复；重复新建任务可能再次调用服务商。应区分 PREPARING、已取得回执后轮询、结果下载失败等阶段；有回执就取回原任务结果，而非笼统重做。
- 此处描述的是源码可推出的运行机制和故障风险；本轮没有模拟 20 分钟真实超时，也没有付费请求。

### BG-05 / P2：ASR 未配置时返回通用 500，客户看不到设计承诺的配置提示

- `/Users/honor.pei/Documents/订单项目/乡墅爆款短视频复刻/server/app/script_from_audio.py:166` 入队直接 `get_asr_provider`。
- `/Users/honor.pei/Documents/订单项目/乡墅爆款短视频复刻/server/app/asr.py:310–315` 配置不可读/缺 key 抛 AsrProviderError（RuntimeError），路由不转为业务 HTTPException。
- `/Users/honor.pei/Documents/订单项目/乡墅爆款短视频复刻/server/app/main.py:143–145` 只有 HTTPException 和通用 Exception 处理器；后者 `/server/app/ops_metrics.py:465–467` 返回纯文本 Internal Server Error。
- 与改写不同，改写缺服务会明确返回 503+DEEPSEEK_NOT_CONFIGURED（script_rewrite.py:251–261）。客户不能据 ASR 500 判断是缺配置还是文案内容问题。此项为源码链确认，未发起真实配置端点变更。

### BG-06 / P2：LLM 截断结果与 null 被当成成功正文

- `/Users/honor.pei/Documents/订单项目/乡墅爆款短视频复刻/server/app/script_rewrite.py:803` 使用 `str(content).strip()`，`content=null` 变成非空字符串 `None`，通过 812 行的非空检查。
- 同文件仅提取 message.content，不检查 finish_reason；固定 2048 output tokens（35,757），但请求允许 20000 字符（59），长稿完整性存在结构性缺口。
- 本轮通过替换 urlopen 为内存响应复现：null→返回 `None`；finish_reason=length 的“这是一段未写完的文案”→直接返回成功结果。真实网络调用数为 0。
- 客户影响：文案工坊可能展示残稿/无意义文本为已完成二创，随后被客户确认或流入其他模块。最少应保证输出是非空字符串、完成原因可接受、输入输出长度有合理门槛，再谈内容质量。

### BG-07 / P2：“临时音频即删”只覆盖正常退出 finally，缺少崩溃与清理失败补偿

- `/Users/honor.pei/Documents/订单项目/乡墅爆款短视频复刻/server/app/script_from_audio.py:428–433` 仅 finally 尝试删除；失败只 warning，没有重试任务/清理账本。
- 任务迁移中的 `audio_object_key`（`068_script_from_audio_tasks.py:45`）没有业务代码写入/读取；进程被杀后 finally 不运行，重启没有依此字段找到残留对象清理。
- 与设计文档 108–110 行“删除失败告警并复删”不完全一致。客户影响是残留临时音频与存储占用；正常成功/异常捕获路径的即删不等于任意崩溃都无残留。

### BG-08 / P2：ASR 源资源与输出契约仍窄于页面/设计的可能预期

- `/Users/honor.pei/Documents/订单项目/乡墅爆款短视频复刻/server/app/script_from_audio.py:167–175` 只校验资产存在且属于项目；未约束视频/音频 kind/content_type。46 行定义的 2GB max 常量全仓仅该处出现，未参与执行；406 行一次性将完整原视频读为 bytes 后再 hash、落临时盘。
- `asr.py:179–183,263–267` language 固定 zh；只留全文/时长，不留句子和词级时间戳。设计文档 10 行提到词级时间戳，但现有 ScriptFromAudioResult（script_from_audio.py:57–62）没有相应字段。
- 客户影响：错误类型资产在后台才失败；大资源增加内存压力。全文提取场景可用，但不能据此承诺带时间轴的精确字幕/多语识别元数据已实现。

## 4. 从客户写稿逻辑评价改写能力

### 已有且合理的部分

1. 不要求先生成视频镜头才可提交改写；只要已有项目及文本即可异步改写。ASR 和改写结果都停留候选态，由前端显式确认后再向项目正式脚本发布，这条边界合理。
2. 普通改写和按 IP 改写共用实现，避免两套不一致逻辑。IP 身份取本人有效档案，排队时冻结快照，Worker 不受随后改档影响，便于追溯“此版本根据哪个人物定位生成”。
3. 普通 system prompt（script_rewrite.py:45–53）已有：保留核心信息/节奏/镜头数量/信息密度、总字数误差不超过 20%；避免连续 8 字相同；新开场钩子；口语化短句；使用原文语言；只输出纯正文。
4. 选 IP 时附加 display_name、role、service_scope、target_audience、expression_style、profile_version，并明确档案仅约束表达/称谓/定位/受众，不得虚构经历、案例、资质、数据、效果保证、服务承诺（199–205）。

### 尚不足以称为“最佳业务步骤”的边界

1. **脱离项目的文案创作不完整。** 请求只有 `/projects/{project_id}/script-rewrite`；没有 `/studio/drafts/.../rewrite` 或无 project_id 改写入口。客户从人物库/空白工坊手工写一篇文案，应不必为了调用改写先构造视频项目。具体前端是否自动建项目，由主报告核对。
2. **缺少创作目标字段。** 请求模型只有 text、identity_id、idempotency_key；没有视频目标时长、平台、营销目标、主题、CTA、自我介绍要求、必须保留事实/必须删除内容等参数。人物画像只能影响表达，不能替代当前这篇稿的目的。
3. **“镜头数量”与文本单字段契约并不对齐。** 后端没有结构化镜头/段落输入，只给一段全文，无法程序证明镜头数量保持一致。对口播成片更有用的字数→预计时长、停顿/段落、超长提示也未在生成后校验。
4. **提示词约束不等于验收。** 字数±20%、连续8字、新钩子、原事实、禁止乱承诺均没有后置检查；普通无 IP 分支连“不得虚构经历/服务承诺”的额外边界也不会加入。程序成功只说明拿到文本，不能证明稿子事实准确、表达适合这个客户、可直接发布。
5. **缺少可操作恢复。** GET/latest 有利于刷新恢复，但没有完整 ASR 回执恢复、取消、人工核查后继续获取结果的专用端点。应让客户能看清“排队、处理中、需恢复、确实失败”，并能在不丢原文/不重复提交的前提下继续。

建议顺序：明确素材来源/自己输入→提取或整理原文→核对核心事实→可选人物 IP 与创作目标→生成候选→对照原文编辑/预览预计时长→确认终稿→按终稿目的进入数字人口播或视频创作。后端应把生成候选、草稿保存、终稿发布、下游任务引用区分清楚；当前已经有一部分这种边界，但源头与恢复链还未闭环。

## 5. 本轮验证与未验证

- 已逐段阅读上述主链源码与迁移，检查唯一入口、Worker 模式分派、任务状态与错误类型。
- 隔离临时 SQLite 数据库，运行当前迁移后验证：同项目可同时插入两个不同幂等键 PENDING；过期旧租约可以把已回收的 SUBMISSION_UNCERTAIN 改成 SUCCEEDED。临时库随进程结束清理。
- 内存 HTTP 响应探针验证：DeepSeek content=null、finish_reason=length 均被当正文接受。
- 本专项没有重复跑现有测试；现有测试的本轮执行由另一审查分工统一负责。已读到 `test_script_from_audio.py:25,43–48,191–226` 使用 SQLite、fake ASR 和 stub 媒体工具；这不覆盖生产 PG Worker。`test_asr_provider.py` 使用 StubTransport，不是实账号结果。
- 未连接真实 Provider、未验证真实收费、未变更任何服务凭据、未使用生产 COS、未启动全量 PG 测试，也未将现有测试声明为真实链路验收。
- 官方接口文档核对支持现有 ASR Flash endpoint 与 `output.text` 解析方向；没有据此发现接口地址错误。参考：[阿里云 Fun-ASR Flash API](https://help.aliyun.com/zh/model-studio/non-real-time-speech-recognition-for-fun-asr-flash)、[Fun-ASR 异步录音识别 HTTP API](https://help.aliyun.com/zh/model-studio/fun-asr-recorded-speech-recognition-http-api)。官方文档只能校对接口契约，不能证明当前配置可调用成功。

## 6. 记忆使用说明（供主报告引用整理）

快速记忆检索只用于确定“草稿独立域、ASR sourceAssetId、确认后发布”的既有审查方向，所有当前实现结论均重读源码。

- `MEMORY.md:90–98`：文案工坊/ASR 历史主题定位。
- `MEMORY.md:143–145`：草稿/生成结果边界、真实链路证据边界。
- 对应本段历史资源为 `extensions/skysight/resources/2026-09-06T06-00-00-YAKE-6h-memory-summary.md`，registry 中未给出 rollout UUID，本轮未另读该资源。
