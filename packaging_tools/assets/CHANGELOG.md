# Changelog

## V0.6.4

Windows 环境自动引导，不改变 V0.6 的视频分析和双文件交付契约。

- Windows 入口改由 PowerShell 5.1 引导，不再要求电脑预先具备 Python。
- 缺少 Python 3.9+ 时，使用 WinGet 安静安装当前用户级 `Python.Python.3.11`。
- 缺少 `ffmpeg`/`ffprobe` 时，使用 WinGet 安装 `Gyan.FFmpeg`，并补齐当前进程与用户 PATH。
- WinGet 下载和 Skill 安装都有超时、退出码检查与 `install-logs/` 日志；任一环节失败则不声称安装成功。
- 系统缺少 WinGet 时 fail-closed，明确引导安装微软 Microsoft App Installer，不使用未校验的第三方直链。
- 外层安装/打包回归测试增加至 12 项；可安装 Skill 的 86 项测试保持全绿。

## V0.6.3

迁移与维护收口，不改变用户只输入一个视频、最终只交付两个文件的产品契约。

- 恢复并整理 V0.6.2 的场景感知抽帧、双解码通道和重复帧去除实现。
- 统一方向判定、Schema 版本与 Release Gate 的共享常量，补齐横屏、方形和宿主额外 QA 失败项验证。
- 可安装 Skill 保持 23 个文件，不携带历史运行、Golden Sample、评审报告或缓存。
- 新增跨平台一键安装包：安装前校验 SHA-256 和 Skill 自测，已有安装自动原子备份，失败时恢复旧版。
- 一键安装支持 macOS、Linux 与 Windows；缺少 FFmpeg 时明确提示，不伪装成可运行状态。
- 当前可安装 Skill 通过 86 项回归测试，外层安装/打包工具另通过 10 项测试。

## V0.6.2

生成提效。交付契约与 Prompt 输出逐字不变，改动全部集中在证据准备阶段。

以真实 15.082 秒样本（720×1280，8 个镜头）实测：

| 指标 | 前 | 后 |
|---|---|---|
| 抽帧 | 40 张均匀、源分辨率 | 24 张按镜头分组、长边 640 |
| 估算视觉 token | 47,840 | 7,176（−85.0%） |
| 抽帧总字节 | 1.59 MB | 0.35 MB |
| 预处理耗时 | 1.01 秒 | 0.80 秒（−21%） |

- 抽帧改为切点驱动：先检测镜头切点，再按镜头抽取首/中/尾帧，直接对应契约要求的 `evidence_timestamps`，宿主不必再从均匀帧里猜镜头归属。
- 长镜头按时长追加采样，镜头过多时自动降级，总量受 `--max-frames` 约束。
- 抽帧分辨率长边限制为 640，判断走动/转身/左右手足够，视觉成本降至约 1/4。
- 切点检测与音轨提取合并为一次解码；全部抽帧合并为另一次解码，总解码次数由 3 次降为 2 次。
- 帧与镜头的映射改用 `showinfo` 回读的真实时间戳，选择容差再宽也不会错位；并对每个计划时间点只保留最接近的一帧，去掉相邻重复帧（实测 47 → 24 帧）。
- 新增 `internal/debug/shot_segments.json`：镜头分段、每帧真实时间戳与 `head`/`mid`/`tail` 角色。
- 新增 `internal/debug/contact_sheet.jpg` 抽帧总览图；生成失败不阻断主流程，且不做正方形填充以免浪费约 44% 像素在黑边上。
- `preprocess_video.py` 参数由 `--sample-count` 改为 `--max-frames`（默认 60）与 `--long-edge`（默认 640）。
- `detect_scenes.py` 保留为独立工具，SKILL.md 第 1 步不再需要单独调用它。
- SKILL.md 第 2 步改为分层查看：先读 `shot_segments.json` 建立结构，再看总览图，最后只对人物出镜镜头细看单帧。
- Golden Sample 的抽帧证据按新策略重建；分析、QA、渲染来源与两个交付物保持不变。
- 当时开发归档的测试从 95 项增加到 113 项，覆盖抽帧规划、时间戳映射、重复帧去除与总览图降级；V0.6.3 便携包重新整理为独立、可随包运行的当前测试集。

## V0.6.1

缺陷修复与精简，交付契约与 Prompt 输出逐字不变。

- 修复 `verify_package.py` 传相对路径（含 README 文档写法 `.`）时前缀为空、全部文件误报缺失的缺陷。
- Release Gate 的首帧方向改为与源视频比对，不再硬编码竖屏；横屏与方形参考视频不再必然卡死在 Gate。
- Release Gate 新增 `video_metadata.json` 为必需证据，并在报告中输出 `orientation`。
- ImageGen 请求同步写入画幅约束：`build_imagegen_request.py` 新增必需参数 `--metadata`，按源视频判定 PORTRAIT / LANDSCAPE / SQUARE 并生成对应构图指令，横屏源显式禁止"按短视频惯例出竖图"。
- 方向分类器 `orientation_of()` 收敛到 `contracts.py`，生成端与验收端共用同一个判定，杜绝两端打架。
- 视觉 QA 中所有被记录的检查项都必须 PASS，宿主额外上报的 FAIL 项不再被忽略。
- `preprocess_video.py` 拒绝覆盖已分析的 `run_manifest.json`，并在重跑前清空 `sample_frames/`，避免上一轮残留帧混入动作证据。
- `publish_outputs.py` 把两次文件复制纳入回滚范围，复制中断不再留下半发布状态阻塞后续尝试。
- `verification/` 与 `docs/` 移出发布 ZIP：安装包从 8.36 MB 降到 46 KB（82 → 25 个文件）。
- `SKILL.md` 去除与 `references/` 重复的字段罗列和 Gate 清单，217 → 148 行，改为按需指向参考文件。
- Schema 版本号收敛为 `contracts.SCHEMA_VERSION` 单一常量；移除 `compose_prompt.py` 中被 `validate_analysis` 完全覆盖的重复校验。
- 测试从 70 项增加到 89 项，补齐 `publish_outputs.py`、`detect_scenes.py` 与打包路径的回归覆盖。

## V0.6

- 增加参考视频连续动作事实契约，人物动作不再使用固定走路、固定手势或默认静止模板。
- 人物镜头强制记录动作状态、连续帧时间点、动作起始/结束、移动路径、身体力学、左右手和分阶段动作。
- 人物镜头的 Prompt 动作由结构化 `motion_observation` 生成，自由文本不能覆盖参考视频事实。
- 首帧增加 `motion_ready_pose_match` 硬门，避免把迈步或转身准备姿态生成为对称静止站姿。
- Release Gate 要求 Prompt 明确声明动作来自参考视频连续帧。
- 首帧请求、Prompt 与 Release Gate 统一使用同一份严格视频分析校验，动作阶段必须完整覆盖镜头。
- Release Gate 重新生成并逐字比对 Prompt，同时用 SHA-256 绑定 QA、渲染来源与最终首帧。
- 增加 ZIP 逐文件一致性校验，禁止历史运行、缓存或陈旧文件进入发布包。
- 正式 Skill 根目录禁止保留 `video_reverse_runs/`，历史运行记录移出安装目录。
- 用 Codex/宿主内置 ImageGen 取代独立 Image Edit API。
- 用 Codex/宿主多模态视觉 QA 取代外部 Vision API。
- 增加最多 3 次的自动重生闭环。
- 增加三层乡村别墅“轻高端改善型乡墅”硬质量下限。
- 把字幕、水印、招牌、春联、灯笼文字等全部设为 QA 硬失败。
- 保持 V0.5 的视频时序、人物/摄影机/相对运动、口播策略、口型与画外音逻辑。
- 无人物参考图也可完整运行。
- Release Gate 强制 `outputs/` 只含两个交付物。
