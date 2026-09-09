# Studio 分步骤业务链路审计

> 后续校核（2026-09-07 12:17 +08:00）：本附件主体及行号固定到 8d13f60；另一任务随后修改 CreationPages/state/types 与两个测试。12:16 工作区已补“缺失首帧资产的签名读取与集合回填”，并在换项目/人物/目标图时清首帧和确认版本；P1-02 已有局部修复，P1-05 部分修复，旧尾帧/参考图/批次仍残留。该快照 13 文件 277 用例通过（测试前后指纹一致）。以主报告第 0 节、latest-workspace-tests.log 与 latest-state-probes.json 为新状态，不重复实施已补部分；其余基线发现不因该增量自动关闭。12:19 发现另一任务继续修改 CreationPages 及其测试，更晚编辑不在上述验证范围。

审计范围：现有 Studio 创作页、人物页相关交接、任务结果、草稿状态、API 封装，以及它直接复用的三个业务组件。只读分析，未改业务源码，未调用真实生成、支付或数据库。本报告不把单元测试通过等同于真实生成验收。

基准：分支 `feat/unified-frontend-gate-flow-audit-20260907`。进入审计时 HEAD 为 `5b91089`，工作区原有 8 个修改文件由其他会话继续编辑；审计期间该任务将恢复相关修复提交为 **`8d13f60e6316afce544d5961ad169aebab8ef7fa`**。本次验证采用 **2026-09-07 12:05:11–12:05:17 +08:00、HEAD 为 8d13f60 的稳定快照**，测试前后 SHA256 相同，完整清单见 `studio-focused-tests.log`。终版复核时业务源码与该快照完全一致；其后两个测试文件又有新增修改，因此 193 用例结果限定于该次快照，不冒充新增测试结果。本文行号对应业务源码快照，后续编辑可能移动行号。

关键文件 SHA256：

| 文件 | SHA256 |
|---|---|
| `client/src/studio/CreationPages.tsx` | `2baf50bc9cc4eeaa401a2da019017666486cecf0c513fa9d2adc8312a6237d23` |
| `client/src/studio/StudioWorkspace.tsx` | `446060adb0b12291687bd9940829fa9641bd9a8f21ccff15bf95b433f598db56` |
| `client/src/studio/live.ts` | `fca5631c126395be19b562cb90c23c0da47ae6ce892b06fee413b7a5cedd17ab` |
| `client/src/studio/state.ts` | `d48301e101931192862a2b7c175b510f2b9c1ea90660b0bba8b521f455c4efc9` |
| `client/src/studio/PeoplePages.tsx` | `7d23414c5cd89efc7204775873411ee7085b1ed777f9c72677f803303795cb86` |

## 1. 核心结论

现有页面已经接入多项真实 API，但 **“有页面、有按钮、单页测试通过”还不能证明原有复刻业务完整迁移到分步骤流程**。当前存在两条并行生成路径：

```text
复刻页：上传/选项目 → AI 拆解 → shot_cards → 页面 Prompt
                                 ↓
人物置换页：项目人物版本 + 源画面确认 → 人物参考匹配 → 首帧生成/确认
                                 ↓
路径 A：回到复刻页“送生成”
        保存 script → compile → revise? → lock → 项目 generation-batch

路径 B：人物置换页“用于文/图生视频”
        仅交接 firstFrameId → 视频生成页 → independent/video-tasks
        （无项目/分镜/脚本/人物参考版本入参）
```

路径 A 保留项目版本链，但默认反推文本会覆盖服务端编译文本，且没有独立费用确认。路径 B 是独立创作能力，其 API 契约本来就没有复刻版本绑定；现有跳转把用户引到了这条通道。首帧即时资产回填和场景造型选择又存在交接缺口。

因此，建议保留已验证业务组件及服务端版本链，先统一“复刻最终生成”的提交契约，再做内部壳层删除。不能直接删掉所谓旧页，因为人物、源画面、首帧、任务重试等目前仍由它们承载。

## 2. 初始 HEAD、其他任务修复与最终基线的区别

| 能力 | 初始 HEAD 5b91089 已有 | 他会话既存改动，后提交为 8d13f60 | 仍未解决 |
|---|---|---|---|
| 上传创建项目、拆解、人物置换、两种视频生成通道 | 已有 | 未改主 API 链 | 业务交接断点仍在 |
| 复刻页恢复历史分镜 | 仅手动选择已有项目时尝试载入；页面重新挂载不完整 | `CreationPages.tsx:466` 增加挂载恢复，联合读取 shot/analysis/prompt/script，重试错误、请求竞争保护 | 后台拆解任务的恢复与缺失 shot_card 物化未补齐 |
| 本地 Prompt/文案保护 | 只凭非空内容等判断 | `state.ts:120`、`types.ts:183` 加入 `promptEdited/scriptEdited`，覆盖主动清空；云端旧草稿兼容见 `live.ts:905` | 没有为 project/source/person/frame 建立统一完整失效链 |
| 换项目文本清理 | 不完整 | `patchStudioDraft` 换项目清空未显式传入的 Prompt/文案 | 首帧、尾帧、参考图、旧 videoBatchId 仍保留 |
| Prompt 保存竞态 | 简单保存 | 保存版本号、操作序号、迟到响应保护及测试 | 另存模板与项目 active prompt 的恢复关系仍需明确 |

这 8 个原有修改文件是 `CreationPages.tsx/test`、`MainPages.test.tsx`、`live.ts/test`、`state.ts/test`、`types.ts`。它们在审计期间由其他任务提交，并非本次审计实现的功能；最终应标为“8d13f60 已有修复”，不能仍叫未提交修复，也不能归为本次审计开发成果。下文缺口已与 8d13f60 的业务源码复核，仍然存在。

## 3. 各步骤输入、输出与持久化

| 步骤 | 页面与核心位置 | 输入 | 真正输出/持久化身份 | 当前完成层级 |
|---|---|---|---|---|
| 本地上传视频 | `CreationPages.tsx:583`；`live.ts:606` | File | createProject → upload intent → 上传 → complete；得到 `projectId`、`assetId`；草稿设置 sourceId/sourceAssetId/projectId | 真实 API 接线；UI 语义与自动拆解副作用未完全分离 |
| AI 拆解 | `CreationPages.tsx:695`；`api.ts:2677` | projectId + sourceAssetId | analysis task → analysis version → `saveShotCards(analysisVersion.id, shots)` → shot_card version | 真实任务+版本 API；后台完成恢复有缺口 |
| 分镜转 Prompt | `state.ts:219`；`CreationPages.tsx:751` | shot cards + original_script | `buildReplicaPromptText` 生成可编辑文字；先放本地 StudioDraft，再由云草稿保存 | 本地文本转换；不是服务端完整编译 Prompt |
| 源画面提取/确认 | `SourceFrameSelection.tsx:20`、`:72`、`:299`、`:389` | projectId、referenceAssetId、时间点 | source-frame task、source_frames 候选版本、source_frame_selection 版本与资产 ID | 真实 API；支持任务恢复、预览、手工确认、重取 |
| 选择人物/场景版本 | `CharacterSelection.tsx:24`、`:126`、`:278` | projectId + character_version_id | 项目 main character immutable snapshot | 真实 API；但 Studio 人物页交接字段未消费 |
| 匹配人物参考 | `CreationPages.tsx:1209`；`api.ts:3468` | character_version_id + source_frame_selection_version_id | character_reference_selection.id | 真实自动匹配 API；重试按钮失效 |
| 生成人物置换首帧 | `FirstFrameSelection.tsx:375`；`api.ts:3540` | projectId、character_version_id、character_reference_selection_id、模型、quantity | first-frame task → first_frames candidates version + image asset IDs | 真实任务；简化模式不覆盖服务端模板 |
| 确认首帧 | `FirstFrameSelection.tsx:417`；`CreationPages.tsx:1194` | 已查看候选 asset ID、质检结果/覆盖确认 | first_frame_selection 版本；Studio 只回填 firstFrameId/frameConfirmed | 服务端确认已有；即时跨页资产对象未回填 |
| 项目复刻最终生成 | `CreationPages.tsx:822`；`live.ts:1108` | project/shot/script、确认首帧、Prompt、参数 | script version → prompt compile/revise/lock version → generation batch/tasks | 版本链真实接线；默认覆盖编译文本、费用确认有缺口 |
| 文/图/参考独立生成 | `StudioWorkspace.tsx:380`；`api.ts:1706` | prompt、first/tail/reference asset IDs、mode、参数 | 独立 generation batch，任务中心复用 | 真实 API；不等价项目复刻 |
| 任务进度/结果 | `live.ts:523`、`:576`、`:167`；`MainPages.tsx:793` | batch/task ID | 真实任务状态、签名预览/直出结果；失败重试交旧任务组件 | 已接真实查询；新页不是完整任务操作替代品 |

### 3.1 上传与拆解尚未成为两次完全独立的用户动作

`uploadWorkbenchSourceVideo` 明确调用 `completeVideoUpload`，后者命中 `POST /api/assets/{assetId}/complete`（`api.ts:2663`）。后端审计确认该完成流程在 `media.py:478` 自动 enqueue analysis task。新页上传成功却提示“点击启动 AI 拆解”，因此用户可能以为只上传了原视频，后台实际已经进入拆解。

`upload_required === false` 会跳过本次上传/完成调用，不能把所有分支一概认定为新建拆解。对于实际上传完成路径，拆步需要明确后端是否自动排队，而不只是前端把两个按钮分开。

上传取消信号只交给二进制上传；createProject、upload intent、complete 并不共享同一取消检查。当前会丢弃迟到 UI 回调，但不能保证取消会清理已创建项目或取消已提交的后台分析。

### 3.2 AI 拆解结果的两层物化

后台任务写的是 `analysis`；Studio 在 `waitForAnalysisTask` 完成后调用 `saveShotCards`，才形成项目 `shot_card` 版本。页面用 `shotCardVersionId` 决定能否送生成。

刷新或返回页面时，工作区新增恢复逻辑只读取已有 shot cards，并不会在“analysis 已完成、shot cards 尚不存在”时补建 shot_card。假如用户在前端落库前关闭/刷新页面，服务端可能已有分析产物但新页仍显示没有分镜。此时不能简单诱导用户重新付费拆解，应先读取现有 analysis 并补齐缺失派生产物。

拆解任务 ID 没有进入 StudioDraft；ReplicaPage 没有像 SourceFrameSelection/FirstFrameSelection 一样读取 latest task 并重新附着。当前任务中心聚合的是 generation batches 与 oral tasks，不覆盖 analysis/source-frame/first-frame 任务。

拆解完成后 `getLatestProjectAnalysis(...).catch(() => undefined)` 吞掉读取失败，再可能执行 `saveShotCards("", [])`。应保留明确读取失败与可重试状态，避免把“后台成功但结果暂不可读”表现为一次拆解失败。

### 3.3 源画面与生成首帧已有可复用业务保护

源画面组件读取 latest candidates、latest selection、latest task，未完成任务会重新轮询；自动提取只按项目尝试一次，失败保留错误。推荐候选仍需要用户看见预览后确认。可手工输入取帧时间，排队任务可先取消再重取，运行中的任务提示等待。

FirstFrameSelection 会读取候选、历史、已确认版本，检查 stale、当前候选版本及 asset 所属；会重新附着 PENDING/RUNNING/SUCCEEDED task。只有最新可见候选可确认，自动质检不通过时要求第二次明确确认；确认响应使用绑定 key 和生命周期保护。简化模式固定 `gpt-image-2`，不传 UI 默认提示词，由服务端完整组装业务模板。

这些保护是现有依赖，不是可直接当内部版遗留删除的代码。

## 4. 必须先解决的跨步骤缺口

### P1-01：场景照片入口未把选中的人物场景版本带入项目

证据链：

1. `live.ts:1032` 读取 `listCharacterSceneLooks(identityId)`，每套场景只拿 FRONT_FACE 或第一张的 asset ID；返回的 StudioAsset **没有 scene/persona/character_version ID**。
2. `PeoplePages.tsx:504` 的“用于人物置换”仅调用 `patchDraft({ipId: person.id, imageId: asset.id})`，随后跳 `replacement`。
3. `ReplacementPage`（`CreationPages.tsx:1116`）不读取 draft.ipId、draft.imageId、selectedPersonId 或 selectedAssetId；它从 projectId 恢复 CharacterSelection。
4. `CharacterSelection.tsx:77` 恢复该项目原 main-character 快照；项目没选择时在 `:147` 优先基础造型，并自动选择最近发布版本。它不会根据刚点击的场景照片选择 persona。
5. 真实人物参考 POST 使用的是 `characterSelection.character_version_id`（`CreationPages.tsx:1227`），而非入口照片所对应场景版本。

结果：用户在人物 B 的场景 S 点“用于人物置换”，可能仍使用项目原人物 A，或自动使用人物 B 的基础造型。单元测试 `PeoplePages.test.tsx:393` 只断言 patch/navigate，未验证下游实际 `character_version_id`。

建议验收：指定人物 B + 场景 S，从照片入口进入后，界面、项目快照、参考选择、首帧任务输入都必须是同一 scene persona 的 immutable version。发布新造型版本应提示选择，而不能悄悄切换。

### P1-02：确认新首帧后，直接跳视频页可能无法提交

`CreationPages.tsx:1204` 只 patch `firstFrameId` 和 `frameConfirmed`，没有把已生成资产放入 Studio `data.assets` / `data.materials`。`navigate("video")`（`:1399`）不刷新数据。

视频页预览根据这两个集合查 asset（`:1722`）；提交前 `StudioWorkspace.tsx:688` 又要求在同样集合找到首帧，否则提示“请选择首帧图片”。因此在 **本会话刚生成、尚未被主列表加载过** 的首帧情况下，虽然后端确认成功、草稿有 ID，下一步仍缺少资产对象。

限制说明：不是所有历史首帧都失败。若资产原本已在集合内则可用；主动全量刷新，且该资产位于素材页返回的 60 条内，也可能补齐；已成功自动保存后重载，`loadDraftMaterials` 可通过 resolveMaterials 定点恢复。上述补救都不能证明“确认后直接下一步”已完成。

当前 `CreationPages.test.tsx:366` 用 stub 确认首帧并断言 patch/navigate，没有挂载真实 StudioWorkspace 再执行视频提交，所以该用例通过不覆盖此断点。

### P1-03：人物置换最终入口使用独立创作，未保留复刻版本关系

`StudioWorkspace.tsx:389` 的 createIndependentVideoTask 只提交 mode/prompt/asset IDs/参数；API 类型 `api.ts:1691` 没有 projectId、shot_card_version_id、script_version_id、character_version_id 或 first-frame selection version。

后端审计确认 independent 通道仅共用 queue/worker/billing，刻意不施加项目复刻 stale/locked/confirmed 约束。这是独立创作的合法设计，但不能自动当作复刻迁移已完成。最终生成可以保留相同视觉页面，提交必须根据创作来源明确走复刻契约或独立契约。

### P1-04：默认拆解文本覆盖完整编译 Prompt

`runReplicaGeneration`（`live.ts:1131`）先调用服务端 compile，然后只要页面 promptText 非空且与 compiledText 不完全相同，就调用 revise 全量替换。页面默认文本由 `buildReplicaPromptText`（`state.ts:219`）形成，本来就只是一份原片镜头描述，通常不等于服务端完整模板。**即使用户没有编辑，也会走覆盖**；现有 promptEdited 标记没有传给该函数。

后端审计确认 compile 有按输出时长缩放镜头时间、首帧人物/服装不回退、动作与口播时序等约束；revise 保留元数据但全量替换 prompt_text。例：60 秒原片生成 15 秒视频，页面默认文本仍输出源片 0–60 秒时间。

建议验收：默认生成直接使用服务端编译文本；只有明确用户修订才使用自定义语义，且硬约束如何保留必须清楚定义。测试必须检查最终 provider 请求文本，而不只检查“compile/revise/lock 均被调用”。

### P1-05：换项目或人物未统一清空旧首帧与视频上下文

实际执行 `patchStudioDraft` 的纯函数探针（见 `studio-state-probes.log`）证实：

- project-a 改成 project-b：prompt/script 已清空，但 firstFrameId、frameConfirmed=true、tailFrameId、referenceIds、videoBatchId 全保留。
- person-a 改成 person-b：清 voice/avatar 和终稿确认，但旧 firstFrameId/frameConfirmed=true 仍保留。

ReplacementPage 挂载/项目变化、人物回调、源帧回调会局部清首帧（`CreationPages.tsx:1146`、`:1155`、`:1257`），因此走该页能补部分失效。若从复刻页换视频后直接去视频生成、或从其他人物入口跳过置换页，统一 draft 层仍允许沿用旧首帧。Independent 通道又没有项目版本约束，不能依赖旧复刻后端替新页阻止串用。

### P1-06：人物参考匹配失败后的重试按钮未重新触发请求

`CreationPages.tsx:1209` 的匹配 effect 依赖 review/project/characterSelection/sourceFrameSelection/referenceSelection。失败时仅设置 referenceError；`retryReferenceMatch`（`:1279`）只删除 ref 集合中的 key 并清 error。

这些动作不会改变 effect 的任何依赖，在输入稳定时点击“重试匹配人物参考”不产生第二次 POST。失败路径也没有 finally 清 leafBusy；当前清理只在 effect cleanup 发生。建议用明确重试事件调用匹配函数，或加入与请求绑定的重试序号，并按具体操作维护 busy。

### P2-07：分析页离开后的迟到回调保护不完整

`analysisProjectRef.current` 仅在发起分析时赋值，没有在页面卸载/换项目时失效。页面局部禁用“更换来源视频”，但 CreationNavigation/壳层导航不受 analysisBusy 控制。离开 A 项目的拆解页、在其他入口切 B 后，A 的异步回调仍可能执行旧 patchDraft，把 Prompt 写进当前草稿。`restoreOperationRef` 和上传/保存序号不能保护这个分析任务回调。

这是源码路径发现，未运行跨页迟到集成复现。应补“发起→导航→切项目→旧请求返回”的测试，并在写入草稿前检查当前 project/source 与操作 ID。

### P2-08：云草稿能存储，不代表所有步骤可可靠续作

`StudioWorkspace.tsx:231` 编辑后 2 秒防抖保存整个 StudioDraft；`live.ts:935` 固定读取 kind=copy 的云草稿，按用户单行 last-write-wins。挂载恢复可批量补素材，但不保存 analysis task ID、source frame selection version、character reference selection ID、first-frame task ID。上述版本依赖项目组件各自重新查询。

页面卸载会清除防抖定时器，没有 flush；2 秒内关闭页面可能丢最后编辑。“云端保存失败，内容仍在本机”实际上主要是当前 React 内存，没有看到这条链的本地持久化备份。后续应区分已保存/保存中/失败，不应让用户以为关闭后一定可恢复。

### P2-09：缺少同一步骤中的费用确认与稳定重试语义

ReplicaPage“送生成”直接 runReplicaGeneration，没有先取报价并显示确认弹窗；视频生成页虽有报价弹窗，`StudioWorkspace.tsx:1122` 仅按 videoSubmitting 禁用提交，videoQuote=null 时也能点击。页面没有清楚展示复刻最终时长会被 `normalizeCustomerDuration` 从 4–15 秒压为 4/15 两档，例如默认 8 秒变成 4 秒。

两条最终提交均用新的 `crypto.randomUUID()` 作为幂等 key。若请求已被后台接受但客户端收到网络失败，用户再次点击会生成新 key。不能断言每次一定重复计费，但它不具备“同一个不确定提交只查收据/复用 key”的保证。

### P2-10：部分入口仅转移视觉来源标识

`ContentPages.tsx:700/707` 爆款视频“提取文案/复刻”仅设置 sourceId=viral.id，没有把源视频导入项目、生成 projectId/sourceAssetId；已有项目草稿还可能保留旧 sourceAssetId。`MainPages.tsx:206` 链接解析明确提示未接入。

因此“爆款详情看见视频→直接复刻”尚不能当成“已上传并绑定参考资产”的完整入口。应先建立导入/缓存资产及项目绑定，再交接分步创作。

## 5. 主界面公开后，哪些挂载会触发受保护访问或写操作

Studio 当前不是一个只渲染空壳的公开首页。`StudioWorkspace.tsx:247` 挂载读云草稿；`:306` 读能力；`:437` 调 loadStudioData，后者并发读取项目、人物、生成任务、口播任务、素材、统计和数据看板（`live.ts:763`）。正常浏览主界面若要在“打开具体内容”才触发激活，需要先定义公开数据与用户内容的边界，并把受保护的数据请求移到授权后。

尤其人物置换页不能直接用于未激活预览：CharacterSelection 在没有项目角色时会自动选择并 PUT；SourceFrameSelection 在没有候选时会自动提交提取；ReplacementPage 前提齐备会自动 POST 人物参考选择。若门禁仅放在“生成”按钮，这些早期写操作仍会发生。

当前 ReplicaPage 的上传/拆解/送生成只检查 review，没有显式 auditor 判断；ReplacementPage 不把 readOnly 传给三个叶子组件，叶子默认 false。旧 LiveWorkspacePanel 则明确 `readOnly={!canWrite}`。服务端授权仍是最终保障，但统一前端必须补齐页面权限，不应让只读用户点击触发后端拒绝，更不能把 reviewer 示例模式当作用户激活状态。

## 6. 参数、状态与结果的保真情况

| 项目 | 当前行为 | 判断 |
|---|---|---|
| Provider | defaultBatchProvider 默认 metaso；只在 VITE_GENERATION_PROVIDER=fake_h3 时使用 fake | 不存在页面供应商选择；不可据 dev 模式推断不会计费 |
| 输出时长 | 独立生成 4–15 整秒；复刻送生成归一为 4/15 | 两条路径同一个 draft 参数表现不同 |
| 分辨率 | 两路径只保留 768P/2K | 接线明确 |
| 比例 | UI“自动”为中文值，不在支持列表时序列化 adaptive | 有确定回退 |
| 生成数量 | UI 1/2/4；提交按 1/2/4 规范化；能力另约束 max_quantity | 已接能力，但能力读取失败时不会前端 fail-closed |
| 多参考数量 | UI 文案最多 4 张；追加数组没有按 max_reference_images 限制 | 服务端需兜底，前端输入校验不完整 |
| I2V 能力 | requestGeneration 检查 t2v/r2v，未用 i2v_enabled 阻止 I2V | 不能把 capability 请求成功等同完整门禁 |
| 任务类型 | generation batch creation_kind 映射为复刻/独立/置换 | 独立通道生成会显示视频生成，不会保留来源复刻类型 |
| 进度 | Studio 每轮刷新 generation + oral；source/first frame 在各页独立恢复 | 不是全部 N 步任务的统一任务中心 |
| 首帧资产签名 | 叶子组件内预览和失效重签已有 | 签名未随首帧 ID 即时交接到新页 |
| 成片展示 | `loadTaskPreview` 优先 provider direct，再归档资产；任务详情按需回填 | 有真实结果读取；不是一律先 COS 归档 |
| 重试/下载 | 生成批次重试/下载/再创作多通过 openLive(tasks) 交旧 TaskRecordsPanel | 不能删除旧任务组件而假定新页已替代全部操作 |
| 任务恢复 | 主列表 limit 20，无 Studio 分页；更多历史走旧记录入口 | 需要保留完整历史入口或迁移分页 |

## 7. 已执行验证与仍需补充的验收

执行命令：

```bash
npm --prefix client test -- src/studio/CreationPages.test.tsx src/studio/StudioWorkspace.test.tsx src/studio/PeoplePages.test.tsx src/studio/live.test.ts src/studio/state.test.ts src/CharacterSelection.test.tsx src/SourceFrameSelection.test.tsx src/FirstFrameSelection.test.tsx
```

结果：**8 文件，193 用例通过，退出码 0**。第一次 12:03:05 运行；因观察到他会话继续修改 CreationPages，12:05 再跑并记录前后 hash，最后一轮源码没有漂移。测试环境打印 `Window.scrollTo not implemented`，未形成测试失败。上述 tests 全部 mock 相关 API，不调用真实 Provider。

另以 TypeScript transpile + Node VM 执行当前 `state.ts` 导出的纯函数，验证换项目/人物保留旧 frame/context；详见 `studio-state-probes.log`。没有修改测试源码。

下一轮应在修复前补的行为用例：

1. 场景照入口→项目 persona/version→人物参考→首帧请求完全同源。
2. 新生成且未预加载的首帧→确认→直接跳视频页→能预览且提交使用同一 ID。
3. 复刻最终生成保持 project/shot/script/character/source/first-frame 的最新确认链。
4. 默认 Prompt 使用编译产物，60→15 秒时间被缩放，硬约束没有被默认文字覆盖。
5. AI 拆解后台完成但前端中途关闭→重新进入可补齐 shot_card，不重新调用 Provider。
6. 匹配失败→点击重试→真实第二次请求；成功/失败均释放 busy。
7. 任一步骤进行中导航或换来源→迟到响应不得污染新草稿。
8. 未激活首页可浏览；打开具体内容只触发一次激活，并在成功后续接原内容；直接 hash 和自动 effect 不能绕开门禁。
9. 费用未成功读取不能提交；网络结果不确定时复用逻辑提交身份并查询收据。

## 8. 本次审计文件与关键位置索引

| 文件 | 阅读与核对范围 |
|---|---|
| `AGENTS.md` | 项目工作约定、证据与真实调用边界 |
| `/Users/honor.pei/.codex/skills/andrej-karpathy-skills/SKILL.md` | 代码审查原则；本次未实施清理 |
| `client/src/studio/types.ts` | StudioDraft/Asset/Task 数据合同，`:169` 起 |
| `client/src/studio/state.ts` | 完整状态函数、跨项目导入、task snapshot、patch、模式与 Prompt 构建 |
| `client/src/studio/context.tsx` | 工作区上下文及动作合同 |
| `client/src/studio/CreationNavigation.tsx` | 全部步骤页导航 |
| `client/src/studio/CreationPages.tsx` | 复刻/恢复 `:385–1114`、置换 `:1116–1428`、独立视频 `:1710`；口播只核对共享字段边界 |
| `client/src/studio/StudioWorkspace.tsx` | 云草稿 `:231`、报价/提交 `:306–430`、加载/轮询 `:437`、导航/patch `:551`、门禁 `:645`、保存 `:722`、费用弹窗 `:1059`、选择器 `:1250` |
| `client/src/studio/live.ts` | 项目/人物/素材/任务适配器、upload `:606`、cloud draft `:905`、scene `:1032`、项目生成 `:1108` |
| `client/src/studio/PeoplePages.tsx` | 人物定位、基础合成图/场景展示、`:504` 场景照片用于置换的交接 |
| `client/src/studio/MainPages.tsx` | 首页导入与未接入链接、任务列表/详情 `:614/:793`、结果预览/下载/重试/再创作 |
| `client/src/studio/ContentPages.tsx` | 爆款来源交接 `:418/:700/:707`、素材读取与详情预览合同；不将发布模块当本次核心六步审计范围 |
| `client/src/studio/LiveWorkspacePanel.tsx` | 完整复用映射：AnalysisWorkspace、ProjectDetailFlow、CharacterLibrary、TaskRecordsPanel、客户/内部钱包 |
| `client/src/CharacterSelection.tsx` | 完整项目角色恢复/自动选择/发布版本与场景显示合同 |
| `client/src/SourceFrameSelection.tsx` | 候选/任务恢复、提取、取消/重取、确认/预览、时间点 |
| `client/src/FirstFrameSelection.tsx` | 候选/历史/任务恢复、stale、生成/模型/模板、质检覆盖确认、生命周期 |
| `client/src/api.ts` | 对应上传、analysis/shot、角色、源帧、人物参考、首帧、script/prompt、生成、能力/报价端点；不是宣称全文所有 API 审计 |
| `client/src/studio/CreationPages.test.tsx` | 页面及恢复测试、stub 首帧交接边界 |
| `client/src/studio/StudioWorkspace.test.tsx` | 壳层/草稿/独立生成测试 |
| `client/src/studio/PeoplePages.test.tsx` | 场景交接、人物资产测试 |
| `client/src/studio/live.test.ts` | 适配器、生成版本调用顺序、结果与素材恢复测试 |
| `client/src/studio/state.test.ts` | 换人/换项目/终稿/草稿合同测试 |
| `client/src/CharacterSelection.test.tsx` | 自动选择、场景标签、只读合同 |
| `client/src/SourceFrameSelection.test.tsx` | 任务恢复与手工确认合同 |
| `client/src/FirstFrameSelection.test.tsx` | 模板、恢复、stale、质检确认与生命周期测试 |

后端完整语义由并行旧业务/后端审计提供；本报告只在上传副作用、独立通道契约和编译 Prompt 覆盖三处引用其核对结果，最终以总报告的后端文件证据交叉阅读。
