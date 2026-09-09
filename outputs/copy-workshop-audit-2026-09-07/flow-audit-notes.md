# 文案工坊上下游与客户步骤审查备忘

审查日期：2026-09-07。范围为当前工作树，不修改业务代码，不提交，不调用付费或生产 API。本文属于源码审查及局部纯函数复现证据，不代表生产验收。控件、持久化和服务端路由详情由主审报告汇总，本文集中记录跨模块交接。

源码绝对根目录：`/Users/honor.pei/Documents/订单项目/乡墅爆款短视频复刻/`。下列定位均相对于此根目录，方便主审合并；核心发现同时附绝对路径定位。

## 1. 总体判断

文案工坊已经具备“编辑稿件 → 选择人物 → 确认终稿 → 带入数字人口播”的基本方向。人物切换或正文编辑后撤销终稿确认，口播页只读展示正文并让修改回到工坊，是合理设计。

但实际的来源接入、IP 二创、复刻使用终稿、历史任务回改和返回路径均有明确断点。当前不能按完整客户业务闭环评价为完成。现状更接近“共享草稿编辑页 + 上传 ASR 接入 + 口播入口”，而不是能够统一承接全部创作来源并向全部下游模块可靠分发的工坊。

## 2. 入口逐项追踪

| 入口 | 前端实际步骤 | 传递字段 | 未交接或风险 | 判断 |
|---|---|---|---|---|
| 工作台直接点“文案工坊” | `MainPages.tsx:565–566` 调 `navigate("copy")` | 保留整个当前草稿 | 没有选择“续写当前稿 / 新建稿”；空态的“打开来源分析”进入旧项目工作区 | 页面可达，原创/选题引导不足 |
| 工作台上传视频后“提取文案” | `MainPages.tsx:237–257` 上传并写项目/资产，`200–217` 调 ASR；`StudioWorkspace.tsx:808–835` 转写后跳转 | 上传阶段有 `projectId/sourceId/sourceAssetId`；成功写 `script.original`、空稿时写 `script.text` | ASR 回调把 `sourceId` 写成项目 ID；异步未校验目标草稿仍相同；转写任务没在任务中心出现 | 主干已接，交接和恢复不完整 |
| 工作台粘贴视频链接 | `MainPages.tsx:205–207` 提示尚未接入 | 无 | 不能解析、落项目或调用 ASR | 明确未接 |
| 爆款详情“提取文案” | `ContentPages.tsx:821` 先 `prepare`，`644–654` 取媒体，`699–704` 跳工坊 | `sourceId=video.id`、`selectedVideoId`、`returnTo=viral-detail` | 媒体 URL/物理资产/项目、转写结果均未交接；没有 ASR 调用；旧稿和旧项目保留 | 断链 |
| 人物详情“去文案工坊创作” | `PeoplePages.tsx:403–407` 先 patch IP 再跳转 | `ipId`、`selectedPersonId` | 未保存的人物定位表单不会自动保存；没有带入来源/选题/正文，也没有设置来源返回路径 | 选择 IP 已接，创作意图交接不足 |
| “我的文案”选择一条 | `CreationPages.tsx:183–194` 只 `patchDraft({script})` | `StudioScript` 六字段 | 仍保留当前来源、项目、IP、音色、分身；列表页不自动切回编辑页 | 容易形成跨稿混合上下文 |
| 口播页“去文案工坊修改” | `CreationPages.tsx:2322` 跳工坊 | 当前草稿保持，`returnTo=oral` | 工坊无返回按钮；只能重新确认后再次“用于数字人口播”，会把返回槽改成 copy | 修改主链可走，返回语义不完整 |
| 任务详情“调整脚本” | `MainPages.tsx:853–864,1070` 调 `recreate("copy")` | 有 `draftSnapshot` 才调用 `draftFromTask` | 真实任务映射不生成 `draftSnapshot`；回退旧任务面板，不能从此按钮回工坊编辑 | 实际历史续作断链 |
| 素材库的视频/音频 | `ContentPages.tsx:1323–1350` 音频直达口播、图片直达复刻等 | 音频 `audioId/ipId`；图片原画面字段 | 当前素材库无“提取文案”路径；没有将已有音频/视频交工坊的按钮 | 若目标为统一工坊来源，则尚缺入口 |
| 选题/原创输入 | `StudioPage`、`StudioDraft` 及 studio 页面未见独立选题或 brief 字段 | 仅自由正文输入 | 目标人群、内容目的、主题、目标时长不构成可提交的创作输入 | 产品改进项，不能冒充冻结需求缺陷 |

## 3. 出口逐项追踪

### 3.1 文案工坊 → 数字人口播

- `CreationPages.tsx:324–329` 要求 `script.confirmed && ipId` 后放行；草稿整体共享，没有复制正文。
- `OralPage` 在 `CreationPages.tsx:2224–2242` 重新确认人物、可用分身、已确认声音和非空终稿。声音/分身未准备好时，停留在口播页补齐，顺序基本合理。
- `state.ts:185–214` 的 `buildOralInput` 再次检验终稿、IP、分身和声音；人物切换会清空音色/分身并撤销终稿确认（`state.ts:153–156`）。这避免把上一人物的声音错误归给新人物。
- 最终 `StudioWorkspace.tsx:355–364` 提交的是 `identityId/avatarId/voiceId/mode/title/scriptText/audioAssetId`，正文来自当前 `state.draft.script.text`，因此口播正向消费正文成立。
- 工坊脚本 ID / 版本 / 项目来源没有进入 `createOralTask` 这次请求。即使口播任务记录保存了正文，后续仍缺从“某版文案”追溯到“哪条生成任务”的明确版本关系。具体服务端保存字段由主审判断。

### 3.2 文案工坊 → 视频复刻

- 工坊页目前只有“用于数字人口播”，没有“用于视频复刻”或“返回当前复刻任务”按钮；客户必须通过侧栏/新建创作跨到复刻。
- 复刻页确实会保留同项目 `scriptEdited=true` 的工坊稿：`CreationPages.tsx:508–549`。不能误判为“跳复刻立即清空所有编辑”。
- 但复刻“送生成”真正取稿在 `CreationPages.tsx:849–858`：按 `originalScript`、`shots[].spoken_text`、纯画面默认文案的顺序取值，完全未读 `state.draft.script.text`。
- `live.ts:1121–1129` 又据此创建新的项目 script version，再编译提示词。结果是工坊终稿虽在草稿里，当前这条复刻提交仍根据原片稿创建版本并进入生成链。
- 判定：同项目草稿保留已实现，终稿用于复刻未实现。若客户的意图是“复刻镜头结构 + 改写我的文案”，当前不满足。

### 3.3 工坊“按 IP 二创” → 旧分析工作区 → 返回工坊

- 按钮 `CreationPages.tsx:305–306` 仅调用 `openLive("analysis")`。
- `StudioWorkspace.tsx:642–646` 仅按当前 `draft.projectId` 查项目；没有项目时显示项目列表。
- `LiveWorkspacePanel.tsx:102–110` 给 `AnalysisWorkspace` 只传项目、用户、只读及回调，不传当前工坊正文和 IP。
- `AnalysisWorkspace.tsx:514–525` 调用 `useGenerationDrafts` 没有提供 `identityId`。后者 `useGenerationDrafts.ts:423–425` 根据该参数决定调用两参通用改写还是三参 IP 改写。因此从这里进入的“按 IP 二创”未将已选 IP 送入改写调用。
- 旧分析面板内部的 `scriptText` 是自己的 state（`useGenerationDrafts.ts:89`），并非工坊的当前正文。
- 关闭面板 `StudioWorkspace.tsx:876–884` 仅关闭+刷新 `StudioData`；`loadStudioData` 只读项目/人物/任务/统计等（`live.ts:763–786`），不导入该项目最新文案。因此在旧分析页完成/保存改写后，不能靠“返回新工作台”自动回填工坊。
- 再次在旧项目列表选项目才会调用 `importProject`；但该旁路又有覆盖工坊本地稿的问题，见下文 F06。

## 4. 按客户优先级排序的确定问题

优先级解释：P1 = 核心创作结果错误、上下游主链失效或工作丢失；P2 = 明显体验/恢复/追溯不足。以下优先级基于客户影响，不表示已观察到生产事故。

### F01 — P1：爆款“提取文案”没有提取，而且可能给旧终稿贴上新来源

定位：`/Users/honor.pei/Documents/订单项目/乡墅爆款短视频复刻/client/src/studio/ContentPages.tsx:644`、`:699`、`:821`；`client/src/studio/state.ts:130–165`。

`prepare` 返回只消费 `kind`，后续回调没有使用该参数；URL 未成为资产/项目，也未启动 ASR。`patchDraft({sourceId})` 不触发项目切换清空，也不会撤销 confirmed。客户已有 A 项目终稿时点 B 爆款“提取文案”，会到一个显示 B 来源但正文仍是 A、项目和物理资产仍是 A 的页面。

最小修复方向：来源接入必须形成完整项目/资产/转写任务交接，再更新草稿；新来源选择时明确创建新稿或替换当前来源，并一致处理相关字段与确认状态。

### F02 — P1：工坊终稿没有进入复刻的生成请求

定位：`/Users/honor.pei/Documents/订单项目/乡墅爆款短视频复刻/client/src/studio/CreationPages.tsx:849`；`/Users/honor.pei/Documents/订单项目/乡墅爆款短视频复刻/client/src/studio/live.ts:1121`。

客户修改原文并确认，再进入同项目复刻，生成脚本仍取拆解原文/分镜台词。不能因为恢复时保留了 draft.script 就认为使用了稿件。应让客户明确选择“使用确认终稿”或“保留原片文案”，以实际选择创建/绑定项目脚本版本，必要时重新编译受影响的 Prompt。

### F03 — P1：“按 IP 二创”未交接 IP 和当前稿，返回也未取回结果

定位：`/Users/honor.pei/Documents/订单项目/乡墅爆款短视频复刻/client/src/studio/CreationPages.tsx:305`；`/Users/honor.pei/Documents/订单项目/乡墅爆款短视频复刻/client/src/AnalysisWorkspace.tsx:514`；`/Users/honor.pei/Documents/订单项目/乡墅爆款短视频复刻/client/src/studio/StudioWorkspace.tsx:876`。

“按 IP 二创”当前实质是打开旧分析工作区。独立输入的原创文案、已选择的人物定位都未进入那次改写操作；旧面板的生成结果也没有工坊回写协议。建议在当前稿上直接调用现有改写任务能力，提供提交中、恢复、失败和应用候选结果的状态，使用明确的人物 ID / 定位版本。

### F04 — P1：ASR 结果可跨页面、跨项目污染当前草稿

定位：`/Users/honor.pei/Documents/订单项目/乡墅爆款短视频复刻/client/src/studio/StudioWorkspace.tsx:797`、`:821`、`:1087`。

`extractingRef` 只防重复点击，并没有设置 `busyRef`、取消操作或记录草稿操作版本。用户可在 A 项目提取期间切页、上传 B 项目或点击从空白开始；A 的回调仍读取 `latestDraftRef.current.script`，用 A 的原文覆盖当前 original，混入旧 sourceId/sourceAssetId，再强制跳工坊。它不改 projectId，所以可能出现项目 B + 资产 A + B 正文/A 原文组合。

建议异步结果绑定 `draftId + projectId + sourceAssetId + taskId`。目标已变化时将结果归档到原任务、发完成通知，不改变新创作的当前稿和页面。

### F05 — P1：真实历史任务“调整脚本”不能返回工坊

定位：`/Users/honor.pei/Documents/订单项目/乡墅爆款短视频复刻/client/src/studio/MainPages.tsx:853`；`/Users/honor.pei/Documents/订单项目/乡墅爆款短视频复刻/client/src/studio/live.ts:638`。

真实 task 映射未提供 `draftSnapshot`；只有 review fixtures 设置它（`fixtures.ts:458`）。`recreate` 因此把实际客户请求统一转去旧任务记录，并提示从原任务重新生成。口播 API 类型原本有 `script_text`（`api.ts:1140`），但 `oralTask()` 映射时丢弃。可从服务端任务读出正文与资产形成新的创作快照，显式撤销确认并允许修改；不应让按钮名承诺“调整脚本”而落到无法续作的面板。

### F06 — P1：重新选同项目可能覆盖工坊未保存的文案

定位：`/Users/honor.pei/Documents/订单项目/乡墅爆款短视频复刻/client/src/studio/state.ts:80`；`/Users/honor.pei/Documents/订单项目/乡墅爆款短视频复刻/client/src/studio/StudioWorkspace.tsx:648`。

`withImportedProject` 判断 imported 的脚本 ID 不同或版本更大就覆盖 current，不看 `scriptEdited`。这条规则与复刻页正确保留本地稿的 `restoreSavedProject` 不一致。工坊未存稿是 `script-*`，服务端存稿是另一个 ID；再次选同项目足以覆盖当前文本，即使服务端版本号并不更大。

建议复用当前已有的编辑保护语义；让“读取服务端版本”成为明确的替换操作，保留本地编辑或形成冲突提示。

### F07 — P2：ASR 成功后来源条丢失

定位：`/Users/honor.pei/Documents/订单项目/乡墅爆款短视频复刻/client/src/studio/StudioWorkspace.tsx:826`；`/Users/honor.pei/Documents/订单项目/乡墅爆款短视频复刻/client/src/studio/CreationPages.tsx:87`、`:157`。

工作台上传时 sourceId 是 assetId，转写成功却写成 projectId。工坊 `findSource` 只查 `data.assets` 和 `data.videos`，不会按项目查找；因此正常上传成功并提取后可出现“尚未选择来源视频”。应统一 sourceId 的语义，或拆出显式的 source kind/reference 字段。

### F08 — P2：返回路径只有一个共享槽，且工坊没有消费它

定位：`/Users/honor.pei/Documents/订单项目/乡墅爆款短视频复刻/client/src/studio/StudioWorkspace.tsx:551`、`:571`；`/Users/honor.pei/Documents/订单项目/乡墅爆款短视频复刻/client/src/studio/CreationPages.tsx:145`、`:327`、`:2249`、`:2322`；`client/src/studio/ContentPages.tsx:719`。

- 爆款/任务/IP/口播进入工坊后，没有“返回来源”按钮。
- 口播 → 工坊写 returnTo=oral；工坊 → 口播又写 returnTo=copy。进入下游后原任务或来源路径丢失。
- `navigate` 默认不清空 returnTo。浏览器后退只修改 page，不恢复之前的 returnTo 或选中项；从爆款详情进工坊后再浏览器后退，详情的“返回列表”可能读到 returnTo=viral-detail，变成返回自身。
- 人物页部分路径会清空 returnTo，但口播/工坊/爆款没有统一消费规则。

建议用页面级返回上下文或导航历史条目携带上一页/选中 ID；每次消费要恢复上一层，避免一个全局字段承担全部路径。

### F09 — P2：ASR 超时提示的恢复目的地不存在

定位：`/Users/honor.pei/Documents/订单项目/乡墅爆款短视频复刻/client/src/studio/live.ts:1085`、`:1104`、`:796`。

ASR 的前端 helper 提交后最多轮询五分钟；超时提示“请稍后在任务中心重试”。而 Studio 任务列表仅组合生成批次和口播任务，没有 ASR 类型。关闭页面后也没有按当前草稿恢复这个轮询的入口。后端任务仍可能完成，这不是等价于结果已经能从任务中心找回。

建议将文案提取/改写任务纳入任务中心，或在工坊的来源区保存任务引用并实现恢复/重试/应用结果；错误提示指向真实可用页面。

### F10 — P2：人物定位未保存也可直接跳工坊

定位：`/Users/honor.pei/Documents/订单项目/乡墅爆款短视频复刻/client/src/studio/PeoplePages.tsx:313`、`:322`、`:405`。

IP 表单使用局部 draft，去工坊只传 person.id，不等待 save，也不提示存在未保存编辑。客户刚改完定位点“去文案工坊创作”，看到的仍是已保存数据中的旧定位。应在存在修改时提供“保存并创作”，保持用户刚输入的定位和下一步实际引用一致。

### F11 — P2：同一 IP 的定位内容变化未撤销终稿确认

定位：`/Users/honor.pei/Documents/订单项目/乡墅爆款短视频复刻/client/src/studio/PeoplePages.tsx:343`；`/Users/honor.pei/Documents/订单项目/乡墅爆款短视频复刻/client/src/studio/state.ts:153`。

保存定位仅更新 `data.people`。`patchStudioDraft` 只在 ipId 变化时清确认；同一人物姓名/身份/表达方式被改后，已有确认稿仍保留 confirmed。若终稿包含自我介绍，客户可能使用与最新定位不符的正文。建议确认稿记录其 IP 定位版本；相关定位变化后提示核对，不必让无关照片变化使正文失效。

### F12 — P2：新建/任务回填/导入不是一致的保存与恢复事件

定位：`/Users/honor.pei/Documents/订单项目/乡墅爆款短视频复刻/client/src/studio/StudioWorkspace.tsx:231`、`:252`、`:276`、`:648`、`:853`、`:1087`。

`draftTouchedRef` 和自动保存调度仅在 patchDraft 中设置。`importProject`、任务 `patchState({draft})`、“从空白创作开始”的 setState 不使用 patchDraft；同步最新 ref 的 effect 只是赋值，没有调保存。若没有后续编辑，旧云草稿仍可在下次启动恢复；挂载的云恢复响应比项目导入更晚时，导入也没有标 touched 来阻止覆盖。此项是源代码确定的保护缺口，实际触发依赖响应时序，尚未做浏览器故障注入。

## 5. 直接复现证据

不新增/改写测试文件，仅将当前 `state.ts` 用现有 TypeScript 依赖在内存中转译，并调用实际导出函数。

输入 A 草稿含 `project-A`、`asset-A`、已确认 A 文案，再调用 `patchStudioDraft(draft,{sourceId:"viral-B"})`，输出：

```json
{"case":"viral source switch","projectId":"project-A","sourceId":"viral-B","sourceAssetId":"asset-A","text":"A final","confirmed":true}
```

输入 A 草稿设 `scriptEdited=true`，导入相同项目、不同脚本 ID、同为初始版本的新 draft，输出：

```json
{"case":"same project import over local edit","scriptEdited":true,"text":"Saved server text"}
```

这两项证实 F01 的跨来源残留和 F06 的未保存稿覆盖。首次尝试误按 `client/node_modules/typescript` 读取失败，随后使用仓库已安装的 `typescript` 成功；没有安装依赖。

完整复现命令（在仓库根目录执行，不写文件、不启动网络；实际执行为等价的 `node -e` 形式，退出码 0）：

```bash
node <<'NODE'
const fs = require("fs");
const ts = require("typescript");
const vm = require("vm");
const compiled = ts.transpileModule(
  fs.readFileSync("client/src/studio/state.ts", "utf8"),
  {
    compilerOptions: {
      module: ts.ModuleKind.CommonJS,
      target: ts.ScriptTarget.ES2022,
    },
  },
).outputText;
const mod = { exports: {} };
vm.runInNewContext(compiled, {
  exports: mod.exports,
  module: mod,
  crypto: require("crypto").webcrypto,
});
const { createDraft, patchStudioDraft, withImportedProject } = mod.exports;
const draft = createDraft();
draft.projectId = "project-A";
draft.sourceId = "asset-A";
draft.sourceAssetId = "asset-A";
draft.ipId = "person-A";
draft.script = {
  ...draft.script,
  text: "A final",
  original: "A original",
  confirmed: true,
};
const switched = patchStudioDraft(draft, { sourceId: "viral-B" });
console.log(JSON.stringify({
  case: "viral source switch",
  projectId: switched.projectId,
  sourceId: switched.sourceId,
  sourceAssetId: switched.sourceAssetId,
  text: switched.script.text,
  confirmed: switched.script.confirmed,
}));
draft.scriptEdited = true;
const imported = createDraft();
imported.projectId = "project-A";
imported.sourceId = "asset-A";
imported.script.text = "Saved server text";
const result = withImportedProject(
  { page: "copy", draft, savedScripts: [], favorites: [] },
  imported,
);
console.log(JSON.stringify({
  case: "same project import over local edit",
  scriptEdited: result.draft.scriptEdited,
  text: result.draft.script.text,
}));
NODE
```

命令输出为本节上方两行 JSON；Node.js 版本为 v24.14.1。

已有测试的边界也值得记录：

- `ContentPages.test.tsx:228–264` 的爆款文案用例只断言“获取媒体 + patch sourceId + navigate”，没有断言 ASR 提交或 original/text 回填，所以它通过不证明提取闭环。
- `StudioWorkspace.test.tsx:537–563` 的上传提取用例模拟立即成功，没有在等待阶段切换项目/新建，也没有断言来源条的资产匹配。
- `CreationPages.test.tsx:1378` 等有同项目本地正文保护测试，但未因此保证 sendGeneration 的取稿来源正确。

## 6. 建议的客户最佳步骤

不要要求所有客户先完整拆解视频。应按客户已有原料走最短路径，共享同一工坊确认和版本协议。

### 路径 A：有参考视频，目标是数字人口播

1. 选一个爆款或上传视频，看到可核对的来源与预计处理步骤。
2. 点击提取；创建可恢复的转写任务，保留原文与来源，不自动把标题当完整原文。
3. 核对 ASR 原文；选择人物 IP 和内容目的，按需要调整目标人群/时长。
4. 按该人物定位二创；显示本次任务与结果，用户可应用结果、保留原稿或继续编辑。
5. 检查姓名身份、核心事实、开头/结尾和时长；确认一个可追溯的正文版本。
6. 转口播；自动带入终稿和 IP，补齐可用分身/声音，必要时试听。
7. 核对价格和参数后生成；从任务详情一键“基于此版本修改”，形成新稿而不覆盖历史。

### 路径 B：已有自己的文案或只有选题

1. 在工坊明确选择“粘贴文案 / 从选题写稿 / 打开已保存稿”。
2. 粘贴正文可以直接编辑，视频来源是可选，不应强制先新建空视频项目才能进行二创。
3. 选题入口收集主题、目标人群、内容目的、人物和期望时长；这属于新增产品建议，并非当前冻结需求一定要求。
4. 与路径 A 共用二创、核对、确认和下游分发。

### 路径 C：参考视频复刻，希望修改文案

1. 先导入参考视频并拆解分镜，保留原片稿与镜头结构。
2. 从复刻页明确进入“修改本项目文案”；工坊完成后“返回复刻并应用终稿”。
3. 明确选择“原片稿”还是“已确认的第 N 版文案”；脚本变化后重编与其相关的 Prompt，重新校验时长。
4. 准备原画面、人物替换或首帧，并确认所选版本。
5. 生成确认页同时展示将使用的稿件版本、首帧、Prompt 与参数，确保后台请求匹配这些内容。

### 路径 D：已有完整音频

1. 目标仅为驱动口播时，可从素材库直接选音频 → 人物/分身 → 生成，无需人为强加文案确认和选声音。
2. 目标是修改音频内容时，提供“转文字并进入工坊”，然后回到文案驱动路径。当前该跨模块入口尚未实现。

## 7. 建议的最小交接协议与验收

优先复用当前字段和接口，不新造不必要的工作流框架。必须统一的语义包括：

- 创作身份：draftId、projectId；独立原创允许无视频项目。
- 来源：source kind、可解析的物理资产/爆款引用、原文快照和提取任务 ID。
- 文稿：scriptId、内容版本、正文、确认状态；确认应绑定正文及选中 IP 定位版本。
- 导航：来处、下一步目标、原选中条目，不以一个永久残留的 returnTo 代表所有层级。
- 异步回填：发起时的草稿/来源/任务签名与完成时核对；不把旧结果写进新稿。
- 下游快照：任务保存真正提交的正文和关联版本；再创作读取该快照而不是当前可变草稿。

建议最先补以下跨页验收：

| 验收场景 | 必须证明的结果 |
|---|---|
| A 终稿已确认 → 选择 B 爆款提文案 | 来源、项目、物理资产与文本一致；不能保留 A 的 confirmed |
| 上传 A 开始 ASR → 改选 B → A 完成 | B 草稿和当前页面不受污染；A 结果可在原任务找回 |
| 上传后 ASR 成功 | 来源条显示上传的真实视频；不显示未选择 |
| 工坊选 IP、编辑正文 → 按 IP 二创 | 请求含正确 IP 与当前正文；结果能回当前稿并保留失败恢复 |
| 同项目本地编辑 → 打开/关闭旧分析页/重新选项目 | 不静默覆盖本地稿；保存的返回结果有明确应用动作 |
| 工坊确认不同于原片的终稿 → 复刻生成 | 实际 createScriptVersion / compile 请求引用终稿，不是拆解原文 |
| 口播任务完成 → 调整脚本 | 打开该任务提交时正文，新 draftId，confirmed=false |
| 爆款详情 → 工坊 → 返回来源 → 返回列表 | 两次返回都到正确页；浏览器后退和页面返回一致 |
| IP 定位有未保存编辑 → 去工坊 | 明确保存或保留用户输入，不默用旧定位 |
| 新建空白但不编辑 → 关闭再开 | 恢复行为符合用户新建意图，不无提示地出现旧稿 |

## 8. 边界与未执行项

没有运行全量 pytest、付费 Provider 或生产调用；没有对上述 ASR 时序问题做浏览器故障注入，也没有宣称真实平台生成成功。两项 state 纯函数复现有当前执行结果，其余核心断点来自逐调用链源码核对。代码中的 `LibraryPages.tsx` 当前不存在，素材库实际位于 `ContentPages.tsx`；没有漏掉另一个独立素材库页面文件。

本次仅写入本备忘。历史记忆只用于定位“独立 studio_drafts 与 sourceAssetId 接入”的背景，所有完成度判断均以本轮当前源码为依据。

## 9. 动态工作树复核（2026-09-07，报告生成期间第二次快照）

主审发现共享工作树外部变更后，按要求只重新核对 `CreationPages.tsx`、`StudioWorkspace.tsx`、`types.ts`。本次读取长度分别为 2523、1535、272 行。没有运行测试或修改业务代码。其他源码沿用主审确认未变的指纹。

最终再以主审于 **13:06:14** 冻结的 `source-snapshot/client/src/studio/` 三个副本复核；字节比较确认均与本节复核时的工作树一致，并重读副本关键区间。本节下列全部行号适用于该冻结副本，主报告应链接副本以避免后续漂移。副本 SHA-256：

| 文件 | SHA-256 |
|---|---|
| `CreationPages.tsx` | `79f85880633233185b4b995c2aa237831ed8bef050250093912a09ee0b859fe6` |
| `StudioWorkspace.tsx` | `66d8bfb8657c2eae2d7c9e89072f93bce36b4fc5fd9de4e3293710b7b03e4340` |
| `types.ts` | `1447039ac121b71ebd374f99e5511d7c40d441fcc6ec277b725cce7ff521140e` |

结论：本文的核心 copy/IP/ASR/复刻取稿/导航与任务交接发现仍成立。更新行号如下，本节定位优先于前文旧行号：

| 核查事项 | 当前准确位置 | 当前判断 |
|---|---|---|
| 工坊“按 IP 二创” | `CreationPages.tsx:305–306` | 仍只有 `openLive("analysis")` |
| 工坊仅有口播主出口 | `CreationPages.tsx:320–330` | `327` 写 `returnTo=copy`；无复刻出口、无返回来源按钮 |
| 复刻生成实际取原稿 | `CreationPages.tsx:849–858` | 完全未取 `state.draft.script.text`，F02 仍成立 |
| 打开旧分析只定位项目 | `StudioWorkspace.tsx:709–713` | 没有传工坊正文或 IP |
| 旧面板关闭只刷新数据 | `StudioWorkspace.tsx:939–947` | 无 `loadProjectDraft` 或工坊结果回写 |
| 同项目导入 | `StudioWorkspace.tsx:715–727` | `721` 仍直接用 `withImportedProject`；该 helper 指纹未变，F06 仍成立 |
| ASR 发起与回写 | `StudioWorkspace.tsx:855–901` | 仍仅 `extractingRef` 防重，没有草稿/来源/任务匹配校验 |
| ASR 来源 ID 误写 | `StudioWorkspace.tsx:882–893` | `884` 仍写 `sourceId: projectId`；`889` 仍混用当前正文；`893` 仍强制跳工坊 |
| 空白新建 | `StudioWorkspace.tsx:1147–1155` | `1150` 直接替换草稿，未取消旧 ASR，未置 touched 或调保存 |
| hash/popstate 返回 | `StudioWorkspace.tsx:618–636` | 仍只恢复 page，未恢复返回上下文 |
| 通用导航 | `StudioWorkspace.tsx:638–649` | 仍合并 patch 并保留其他 returnTo |
| 口播返回 | `CreationPages.tsx:2308–2313` | `2311` 仍直接使用全局 returnTo，未消费/恢复上层 |
| 口播去工坊修改 | `CreationPages.tsx:2383–2387` | `2384` 仍写 `returnTo=oral` |
| 任务回填通道 | `StudioWorkspace.tsx:916` | `patchState` 仍直接 setState；`MainPages.tsx:853–864` 与真实 task 映射未变 |
| 云草稿恢复与 touched | `StudioWorkspace.tsx:239,292–315,651–656` | 只有 `patchDraft` 写 touched，项目导入/空白新建仍未写 |
| 草稿变化 effect | `StudioWorkspace.tsx:318–327` | 仍无自动保存调度；新增的内容只处理素材恢复状态 |
| 工坊稿/来源/任务类型契约 | `types.ts:172–206,247–272` | 未增加文稿快照/ASR任务/来源类型交接契约；当前新增的状态属于视频能力和参考素材重试 |

需要保留的正面更新：`StudioWorkspace.tsx:251–284` 已为素材元数据恢复增加请求序号和 draft ID 校验、失败状态；`286–290` 有素材恢复重试；`318–325` 在草稿 ID 变更后失效旧素材请求。这是素材恢复链路的改进，不能继续笼统描述该链路完全缺少错误/归属保护；它并未被 `extractScriptFromUpload` 使用，因此不修复 F04 的 ASR 回填问题。
