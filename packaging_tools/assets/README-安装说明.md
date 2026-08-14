# 短视频复刻 Skill V0.6.4 一键安装包

## 一键安装

- macOS：双击 `安装-macOS.command`
- Windows：双击 `安装-Windows.bat`，缺少 Python/FFmpeg 时自动下载安装
- Linux：运行 `sh 安装-Linux.sh`

安装器会先核对 ZIP 的 SHA-256，运行内置回归测试，再原子替换 Skill。若电脑上已有同名 Skill，会自动保留带时间戳的备份，不会直接删除旧版。

内层 Skill 包为 `video-reverse-prompt-script-firstframe-v0.6.4.zip`；Skill 本体遵循 Codex 标准目录命名，不携带安装说明、更新记录或历史测试报告。

默认安装到：

- macOS / Linux：`~/.codex/skills/video-reverse-prompt-script-firstframe`
- Windows：`%USERPROFILE%\.codex\skills\video-reverse-prompt-script-firstframe`

如设置了 `CODEX_HOME`，则安装到该目录下的 `skills/`。

需要回退时，先退出 Codex，将当前同名 Skill 目录改名，再把安装结果中显示的 `backup` 目录改回 `video-reverse-prompt-script-firstframe`。

## 运行依赖

- Python 3.9 或更高版本
- FFmpeg（同时提供 `ffmpeg` 与 `ffprobe`）
- 支持宿主内置 ImageGen 与多模态看图的 Codex 环境

Windows 安装器会通过系统 `winget` 自动安装 `Python.Python.3.11` 和 `Gyan.FFmpeg`，使用当前用户范围，并自动刷新 PATH。该步骤需要网络，且会自动接受 WinGet 源与软件包协议。如系统没有 `winget`，需先从 Microsoft Store 安装 **Microsoft App Installer**。

Windows 下载、安装和 Skill 自测日志保存在解压目录的 `install-logs/`。macOS/Linux 仍只检测环境，不自动联网安装依赖。

安装后重启 Codex。实际运行结果的 `outputs/` 仍严格只有：

1. `first_frame.png`
2. `image_to_video_prompt.md`

如需手动复核内置测试，进入已安装的 `video-reverse-prompt-script-firstframe` 目录后运行：

```bash
python3 -m unittest discover -s tests -q
```
