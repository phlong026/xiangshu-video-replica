# 文案工坊专项测试证据与覆盖缺口

审查日期：2026-09-07。审查范围：当前工作树的文案工坊上下游测试、持久化/改写/ASR 后端合同及所选脚本交接测试。遵循当前 `AGENTS.md` 与 `andrej-karpathy-skills` 的实证审查原则，本次未改业务代码或测试代码。

## 1. 本次执行结果

基线：分支 `fix/frontend-completion-no-confirmation-20260907`、HEAD `f3ae7e9`，工作树存在其他任务的未提交变更；结果针对执行时工作树，不代表 HEAD 单独检出、CI 或部署环境。

| 组别 | 范围 | 结果 | 时间 | 原始日志 |
| --- | --- | --- | --- | --- |
| 前端页面与状态 | ContentPages、CreationPages、StudioWorkspace、state、live、MainPages 共 6 个文件 | 186 passed；无失败/skip | 16.65s | `test-frontend.log` |
| 前端 API 与旧改写工作区 | api、GenerationComposer、ScriptEditor 共 3 个文件 | 128 passed；无失败/skip | 2.80s | `test-frontend-api-legacy.log` |
| 后端核心合同 | script_rewrite、script_from_audio、studio_drafts、asr_provider 共 4 个文件 | 45 passed；无失败/skip；1 warning | 49.09s | `test-backend-core.log` |
| 后端脚本交接 | test_generation 中所选 6 个测试节点 | 6 passed；无失败/skip；1 warning | 4.83s | `test-backend-script-handoff.log` |

合计 365 项通过（前端 314、后端 51），无失败、无 skip。数字包含相关页面和下游合同的专项测试，不代表 365 个独立文案工坊客户验收场景。

首轮前端日志出现 jsdom 的 `Not implemented: Window's scrollTo() method`，未引发测试失败。后端 warning 是 Starlette 对 TestClient 使用 httpx 的弃用提示，未引发测试失败。本次未安装或升级依赖。

### 精确命令（均在仓库根目录）

```bash
npm run test --workspace client -- src/studio/ContentPages.test.tsx src/studio/CreationPages.test.tsx src/studio/StudioWorkspace.test.tsx src/studio/state.test.ts src/studio/live.test.ts src/studio/MainPages.test.tsx

npm run test --workspace client -- src/api.test.ts src/GenerationComposer.test.tsx src/ScriptEditor.test.tsx

env -u VIDEO_REPLICA_DATABASE_URL -u TEST_POSTGRESQL_URL VIDEO_REPLICA_CUSTOMER_PRODUCTION=false uv --cache-dir .uv-cache run --project server --locked python -m pytest --rootdir server server/tests/test_script_rewrite.py server/tests/test_script_from_audio.py server/tests/test_studio_drafts.py server/tests/test_asr_provider.py -q -ra

env -u VIDEO_REPLICA_DATABASE_URL -u TEST_POSTGRESQL_URL VIDEO_REPLICA_CUSTOMER_PRODUCTION=false uv --cache-dir .uv-cache run --project server --locked python -m pytest --rootdir server server/tests/test_generation.py::test_script_maps_spoken_text_to_shots_without_deleting_user_text server/tests/test_generation.py::test_generation_workflow_exposes_runtime_limits_and_script_staleness server/tests/test_generation.py::test_new_analysis_makes_the_existing_shot_card_and_script_stale server/tests/test_generation.py::test_prompt_compile_rejects_a_superseded_script server/tests/test_generation.py::test_prompt_preview_compiles_the_latest_script_and_shot_card_without_persisting server/tests/test_generation.py::test_prompt_preview_falls_back_to_the_original_script_from_the_analysis -q -ra
```

未执行全量 pytest、`npm run check`、共享 PostgreSQL fixture 启动/重置、真实付费 Provider、生产 COS 或公网发布。

## 2. 证据边界

- 新页面测试主要通过 `vi.mock('./context')` 或 mock `studio/live` 驱动组件，验证渲染、状态更新、函数调用和导航合同；未将浏览器与真实 FastAPI 同时串起来。
- `StudioWorkspace.test.tsx:14` 起 mock `live`，包括 `publishScriptVersion`、`persistCloudDraft`、`persistSavedScript`、`extractScriptFromUpload`。确认终稿测试中“发布成功”只是 mock 返回成功，不能覆盖服务端前置依赖缺失。
- `GenerationComposer.test.tsx:210`、`:273`、`:351` 的 AI 改写测试验证旧工作区：入队后释放 busy、人物切换隔离、回到项目恢复任务。不能把这些结果移植为新 CopyPage 自动恢复已验证。
- `server/tests/test_script_rewrite.py:24`、`test_script_from_audio.py:30`、`test_studio_drafts.py:29` 均使用每测试独立临时 SQLite 数据库和 TestClient，身份采用 `X-Dev-User-Id`。`server/tests/conftest.py:8` 强制 development 身份模式并禁用本机密钥存储。
- 改写测试注入 `_request_deepseek`/`urlopen`，使用测试配置，不调用 DeepSeek。转写 Worker 使用 FakeStorageAdapter、fake ASR，且 `test_script_from_audio.py:124` 注入假 ffmpeg/ffprobe；`test_asr_provider.py:33` 注入 StubTransport，未访问网络。
- 所选脚本交接测试有已构造的项目/镜头卡数据，主要验证 `/scripts`、`/scripts/latest` 与 Prompt 之间的业务合同，不能证明“纯提取文案、从未拆解分镜”的来源也可成功发布脚本。
- 本次专项全绿只能证明上述自动化合同；未做客户登录态 PostgreSQL、本机真实媒体转写、真实 Provider 质量/计费或客户浏览器逐步 UAT。

## 3. 按客户流程拆解测试覆盖

| 客户操作/异常 | 已有证据 | 仍缺的验证 | 判断 |
| --- | --- | --- | --- |
| 工作台上传后提取文案进入工坊 | `MainPages.test.tsx:704` 验证提取入口调用，`:727` 覆盖恢复来源；`StudioWorkspace.test.tsx:537` 验证提取成功回填并导航，`:565` 验证 ASR 错误留页，`:583` 验证缺资产提示 | 真实上传→ASR→完整文本回填；提取期间切换来源 A/B 或路由，迟到结果是否覆盖新草稿 | 页面 mock happy path 已覆盖；真实接口交接和竞态未覆盖 |
| 爆款详情“提取文案” | `ContentPages.test.tsx:228` 验证先调用媒体准备、`sourceId` 交接、导航 copy；`:266` 验证媒体准备失败留页 | 该测试返回的是 audio URL，不包含 transcript；未断言 ASR 提交、转写结果或原文完整性；“媒体就绪”不能证明“文案已提取” | 已测备料及跳转，未测提取链路 |
| 来源/项目切换 | `state.test.ts:25` 覆盖 projectId 变化清空旧文本；`:98` 覆盖项目导入清理返回上下文；`CreationPages.test.tsx:1612`、`:1690` 覆盖复刻页来源竞态 | 仅 sourceId/selectedVideoId 变化、projectId 未变或未清除时，新 CopyPage 是否保留旧文案/旧项目；文案任务迟到响应 | 共享状态/复刻页有保护测试，新工坊特有来源切换缺口 |
| AI 二创改写 | `test_script_rewrite.py:179` 验证入队→Worker→结果→latest；`:280` 验证提交后超时不自动重调；`:239` 验证 lease 过期转不确定 | 新 CopyPage 按钮到真实请求、生成期间继续编辑或重新选来源、空原文/无项目来源、配置错误 UI、重新进入工坊恢复 | 后端领域已覆盖，新页面直接行为缺测试 |
| IP 选择及隔离 | `CreationPages.test.tsx:364` 验证打开人物选择器；`state.test.ts:152` 验证声音/分身与报价失效；`test_script_rewrite.py:331`、`:501`、`:553` 验证 IP 快照和隔离；旧 `GenerationComposer.test.tsx:273` 验证旧人物迟到结果不回填 | 新 CopyPage 同项目换 IP 的异步回填、确认状态/已有稿是否匹配新人物 | 服务端与旧组件已覆盖，新组件仍需专门测试 |
| 手改二创稿后改写结果迟到 | 共享 draft 编辑标记：`state.test.ts:13`；复刻恢复保留手改/主动清空：`CreationPages.test.tsx:1378` | 新 CopyPage/StudioWorkspace：请求开始后用户编辑，完成响应是否无条件覆盖；点击保存/确认后旧改写任务是否覆写终稿 | 没有直接防丢稿回归测试，优先补 |
| 草稿自动保存与重启恢复 | `StudioWorkspace.test.tsx:422` 验证启动恢复云草稿和文案列表；`:456` 验证防抖保存；`live.test.ts:254` 验证兼容旧草稿编辑标记；后端 `test_studio_drafts.py:96` 验证 roundtrip，`:116` 验证 revision 递增 | 网络失败/重试、编辑后 2 秒内关闭刷新、恢复响应迟到覆盖新输入、并发保存乱序、多端 last-write-wins、会话换用户时隔离 | 正常保存恢复有覆盖，丢稿边界缺测试 |
| 保存版本/我的文案 | `StudioWorkspace.test.tsx:484` 验证 persistSavedScript 调用；`test_studio_drafts.py:202` 验证相同 script_id 覆盖更新，`:230` 验证用户隔离，`:252` 验证只列最新 50 条 | “保存版本”是否应追加历史而非同 ID 覆盖；我的文案套用后来源、项目、IP 是否一并回填；超过 50 条可发现性；列表读取失败 | 已测当前 upsert 合同，但未证明符合客户的版本历史预期 |
| 确认终稿 | `StudioWorkspace.test.tsx:501` 验证有 projectId 时调用 publishScriptVersion；`:524` 验证无 projectId 不发布脚本 | 发布失败时 confirmed 回滚、无镜头卡来源的服务端拒绝、连续点击和任务迟到、多次确认重复版本、保存/发布部分成功 | 当前测试 mock 成功，关键失败与前置条件缺口 |
| 终稿→数字人口播→返回修改 | `CreationPages.test.tsx:288` 只断言 `navigate('oral', {returnTo:'copy'})`；`:1000` 验证口播稿只读并返回工坊；`state.test.ts:191` 验证 text 模式要求确认终稿、请求不带音频 | 切入实际口播页后真实草稿一致、失效人物/声音、修改后重新确认、后端口播调用结果 | 导航/状态合同已测，端到端链路未测；不能仅凭测试标题“保留同一草稿”推定完整验证 |
| 终稿→视频复刻/Prompt | `state.test.ts:71` 验证同项目返回保留本地稿；`CreationPages.test.tsx:1245`、`:1378` 验证恢复不覆盖本地稿；后端 `test_generation.py:1841` 验证全文与逐镜头映射，`:1875`、`:1939` 验证脚本过期 | 新 CopyPage “用于视频复刻”按钮到下游全流程；所选稿与项目镜头映射/Prompt最终正文一致；不含分镜的纯文案应走何分支 | 下游组件与脚本合同有验证，新页面交接未全链串测 |
| 后台任务恢复 | `api.test.ts:1829` 验证改写共享轮询，`:1876` 验证 IP 作用域 latest；旧 `GenerationComposer.test.tsx:351` 验证回项目恢复 | 新 CopyPage 改写/ASR 任务在刷新/重启后的自动恢复、任务中心发现、失败重新提交/取消、结果已经完成但前端未回填 | 后端/latest和旧工作区有测试，新工坊用户恢复链缺测试 |
| 权限/错误 | `test_script_rewrite.py:157` 验证其他项目和 auditor 禁止；`:481` 验证外人 IP 不可枚举；`test_script_from_audio.py:166`、`:175` 权限；`test_studio_drafts.py:176`、`:181` 角色和未登录；`test_asr_provider.py:122`、`:144`、`:153`、`:160` 失败/超时/凭据/未配置 | 客户 session 被替换、过期、冻结期间提交新工坊写入；PG 事务 fencing；前端错误恢复与重试可达性 | development身份权限有验证，不能替代客户 session 授权验收 |

## 4. 特别容易被“测试全绿”掩盖的结论

1. **新旧界面混算覆盖率。** 新 CopyPage 的直接文案测试仅见用于口播导航、打开人物选择器；大部分改写恢复测试属于旧 GenerationComposer。需要按页面对应测试核对，不能按“仓库有 rewrite 测试”认定新页已验收。
2. **mock 绕过前置依赖。** 确认终稿测试直接 mock 发布成功；服务端脚本测试直接预置镜头卡。两组都绿，仍可能没有任何测试覆盖“ASR 提取成功，但没有 analysis/shot card 的项目确认终稿”。
3. **保存版本测试锁定了覆盖更新。** `test_studio_drafts.py:202` 明确断言同 script_id 保存两次后仅 1 行且 version 变为 3。这证明 upsert 实现符合当前合同，不证明客户能找回旧版。
4. **任务持久化不等于新页面可恢复。** 后端 latest 和共享 poller 已测；新文案工坊是否在重进时调用恢复入口，需要独立页面测试。
5. **客户写入 fencing 覆盖名单尚未包含新增文案端点。** `server/tests/test_customer_fencing.py:988` 覆盖 `/scripts`；`:1002` 参数化的 `_GATED_WRITE_ROUTES` 中未检出 `script-rewrite`、`script-from-audio`、`/studio/drafts`、`/studio/saved-scripts`。这只是覆盖缺口结论，不据此认定源代码缺少鉴权。

## 5. 建议追加的最小验收集

| 优先级 | 用例 | 成功条件 |
| --- | --- | --- |
| P1 | 爆款 A 提取文案→工坊 | 原文来自实际转写，来源 A 的媒体/项目/文本一致，可读错误可重试 |
| P1 | 上传后只做 ASR、未拆解分镜→确认终稿→口播 | 能确认并进入口播；如需依赖，UI 清楚引导且不提前显示确认成功 |
| P1 | 草稿 A 保存期间写 B；旧请求后返回 | 重载仍保留 B，不被旧请求/旧自动保存覆盖 |
| P1 | AI 改写期间手改/清空/确认终稿 | 不无提示覆盖最新人工内容或已确认稿 |
| P1 | 改写/ASR 期间切来源、换项目、换 IP | 迟到结果仅属于原任务，可找到且不污染当前草稿 |
| P1 | 关闭工坊/刷新/应用重启后恢复任务 | 同一任务继续展示进度或取回完成结果，无额外付费调用 |
| P1 | 确认终稿的草稿保存成功、项目脚本发布失败 | confirmed 状态与错误提示一致，下游无法消费不存在的项目脚本版本 |
| P1 | 从“我的文案”套用另一个来源文案→口播/复刻 | 来源、项目和 IP 绑定符合显式合同；不把新文本静默写到旧项目 |
| P1 | 客户 token 已过期/被替换/冻结后调用四类文案写入接口 | PG 拒绝写入且无 Provider 副作用；具备对应参数化回归测试 |
| P2 | 保存多版并回退 | 版本历史可见可回滚，或 UI 明确只保存当前版本 |
| P2 | 保存失败与 2 秒内关闭窗口 | 用户能判断是否保存成功，重新打开可恢复最后有效编辑 |
| P2 | 51 条以上“我的文案” | 用户能查到更早内容，或有明确数量上限说明 |

这些是根据现有测试边界得到的待验收项，不是本次已证实的全部产品缺陷；最终缺陷等级需结合主报告的源代码链路证据。
