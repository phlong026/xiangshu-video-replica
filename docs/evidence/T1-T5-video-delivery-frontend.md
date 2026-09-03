# 视频播放与交付 T1–T5 验收记录

日期：2026-09-03。分支：`fix/video-playback-delivery-ui`。

## 范围与结果

本次 T1–T5 指用户本轮确认的视频播放与交付整改，不是早期冻结任务清单的同名编号。
最初 T1–T5 修改共享前端与回归测试；0.1.15 发布评审另补充下文非生产 Fake Provider 兼容修复。不改数据库、计费或 AI 检查算法，不处理 T6 历史数据恢复。

| 编号 | 完成内容 | 主要文件 |
| --- | --- | --- |
| T1 | 将中央按钮居中定位与按下动画分离，避免鼠标松开时移出按钮；播放结束后可重播 | `client/src/styles.css`、`client/src/VideoResultStage.tsx` |
| T2 | 主视图和诊断视图使用同一检查状态解释；画面问题不再误报为音频问题；未知检查码保留原文 | `client/src/VideoResultStage.tsx`、`client/src/TaskRecordsPanel.tsx` |
| T3 | 直链任务先经现有权限接口获取 URL，再下载 MP4；下载不携带供应商不需要的会话凭据；保留旧归档资产下载 | `client/src/api.ts`、`client/src/TaskRecordsPanel.tsx` |
| T4 | 同步左侧列表和详情；轮询不显示反复顶开布局的加载提示；刷新当前批次不卸载播放器、不清空播放 URL | `client/src/TaskRecordsPanel.tsx` |
| T5 | 显示任务结束数、成功数和失败数；没有直链且归档失败的资产占位记录不再冒充可播放成片 | `client/src/VideoResultStage.tsx`、`client/src/TaskRecordsPanel.tsx` |

## 实现上的简化

- 复用现有预览权限接口及 Blob 下载工具，没有新增依赖、后端端点、媒体代理或上传流程。
- 统一结果来源与质检标签判断，删除重复/错误的音频判断和多余的下载分支。
- 保留现有分页、两秒轮询和请求失效保护，未增加第二套后台任务机制。

## 测试与评审

先新增失败测试覆盖直链下载、播放器交互、状态同步与失败资产，再修正实现。
独立只读评审发现的两项问题（终态后手动刷新失效、未知质检码丢失）均补充失败回归后修复；复审通过，无剩余阻断项。

| 检查 | 结果 |
| --- | --- |
| `npm.cmd run check --workspace client` | Biome、TypeScript 通过；59 个测试文件、689 项测试通过 |
| 三个专项套件：API、TaskRecordsPanel、VideoResultStage | 125 项通过，包含 403/404/409、超时、失败后重试不重新生成及 auditor 限制 |
| `npm.cmd run check:e2e` | 15 个文件静态检查通过 |
| `e2e/gate1/player-delivery.spec.mjs` | 主代理和独立评审分别运行，各 1 项真实 Chrome 测试通过 |
| `npm.cmd run build --workspace client` | Windows 浏览器目标构建通过；仍有大于 500 kB 的已有分包警告 |
| `scripts/verify_no_secrets.sh`、`git diff --check` | 通过 |

真实浏览器测试独立启动本地 Vite，挂载真实 TaskRecordsPanel/VideoResultStage，浏览器现场录制短 MP4 作为供应商返回数据，接口全部拦截为测试数据。它验证：

1. 按住中央按钮 250 毫秒后放开仍开始播放，播放结束后可重播，底部暂停可用。
2. 画面问题不会显示“音频质检未通过”，并正确显示成功数。
3. “下载 MP4”产生文件，文件名正确、文件字节与测试成片完全一致。
4. 刷新后左右状态更新，而播放器 DOM、暂停位置及已有播放 URL 保留；不重复申请播放地址。
5. 未访问付费生成接口，未向供应商携带 Authorization。

本地浏览器证据（运行产物不入库）：

- 主验证：`.tmp-tools/player-delivery-e2e/logs/playwright-report.json`
- 独立复核：`output/review-player-delivery-20260903/logs/playwright-report.json`

重跑浏览器验证（PowerShell，需本机 Chrome）：

```powershell
$env:GATE1_RUN_DIR = Join-Path (Get-Location) 'output/player-delivery'
npm.cmd exec -- playwright test --config e2e/gate1/playwright.config.mjs player-delivery.spec.mjs
```

## 0.1.15 发布评审补充

- PR #90 指出 `fake://` 不能被浏览器下载：现有预览接口在项目授权后，将非生产 `fake_h3` 测试文件转换为 MP4 data URL。生产真实供应商仍返回原链接；保留生产禁用 Fake Provider 的门禁，没有新增端点、依赖或云上传。
- 补充测试验证文件字节、越权前置拦截、文件缺失/为空、生产禁用，以及真实供应商路径不加载测试 Provider。
- 前端不再仅因检查服务不可用而额外标记“需要处理”；真实检查失败仍明确提示。后端批次状态保持原样，本次不修改服务端质检或状态算法。补充音频和画面不可用两种回归，保持预览、下载可用且不触发付费重生成。

## 发布边界与剩余风险

- 最初 T1–T5 本地验收未推送、部署或打包；后续 0.1.15 发布状态以 PR #90 和发布回执为准，不能将本文测试通过视为已经上线。
- 桌面端内嵌共享前端，必须重新打包安装才能得到上述修改；只部署服务器不会更新已安装的桌面页面。
- 本次 Chrome 验证不替代打包后 Windows WebView2 的真实供应商播放与下载验收；未运行无关的后端全量测试。
- 直链播放和下载仍依赖供应商 URL 有效期、网络、跨域策略和编码兼容性。前端修复不能恢复已过期/丢失的历史成片。
- 下载复用 Blob 方案，会在浏览器内存中接收完整文件；本次未新增超大视频的流式下载机制。
- AI 检查只改了前端标签和提示，不代表取消后端检查或修复其潜在误判。

回滚：本次不含数据迁移，可使用 `git revert` 撤销对应前端提交；不得覆盖用户另行维护的 `docs/design/`。
