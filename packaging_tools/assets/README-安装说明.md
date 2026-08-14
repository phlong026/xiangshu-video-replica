# 短视频复刻 Skill V0.6.3 一键安装包

## 一键安装

- macOS：双击 `安装-macOS.command`
- Windows：双击 `安装-Windows.bat`
- Linux：运行 `sh 安装-Linux.sh`

安装器会先核对 ZIP 的 SHA-256，运行内置回归测试，再原子替换 Skill。若电脑上已有同名 Skill，会自动保留带时间戳的备份，不会直接删除旧版。

内层 Skill 包为 `video-reverse-prompt-script-firstframe-v0.6.3.zip`；Skill 本体遵循 Codex 标准目录命名，不携带安装说明、更新记录或历史测试报告。

默认安装到：

- macOS / Linux：`~/.codex/skills/video-reverse-prompt-script-firstframe`
- Windows：`%USERPROFILE%\.codex\skills\video-reverse-prompt-script-firstframe`

如设置了 `CODEX_HOME`，则安装到该目录下的 `skills/`。

需要回退时，先退出 Codex，将当前同名 Skill 目录改名，再把安装结果中显示的 `backup` 目录改回 `video-reverse-prompt-script-firstframe`。

## 运行依赖

- Python 3.9 或更高版本
- FFmpeg（同时提供 `ffmpeg` 与 `ffprobe`）
- 支持宿主内置 ImageGen 与多模态看图的 Codex 环境

安装器发现缺少 FFmpeg 时仍会完成 Skill 安装，但视频拆解无法运行，直到补齐 FFmpeg。

安装后重启 Codex。实际运行结果的 `outputs/` 仍严格只有：

1. `first_frame.png`
2. `image_to_video_prompt.md`

如需手动复核内置测试，进入已安装的 `video-reverse-prompt-script-firstframe` 目录后运行：

```bash
python3 -m unittest discover -s tests -q
```
