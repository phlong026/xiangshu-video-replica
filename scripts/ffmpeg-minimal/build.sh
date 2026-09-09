#!/usr/bin/env bash
# 精简 LGPL 构建ffmpeg/ffprobe（决策 #5，C7/ASR 文案链路）。
# 产出静态二进制，覆盖图片解码校验、抽音轨和探测时长，不含任何 GPL
# 组件（x264/x265 等）。产物复制到 client/src-tauri/resources/ffmpeg/。
#
# 用法：scripts/ffmpeg-minimal/build.sh [输出目录]
# 依赖：Docker（musl 交叉编译镜像 ffmpeg-builds 的最小等价物）。
set -euo pipefail

OUT_DIR="${1:-$(dirname "$0")/../../client/src-tauri/resources/ffmpeg}"
IMAGE_NAME="ffmpeg-minimal-builder"

# BuildKit 导出：把 export 阶段的静态 exe 直接落到输出目录。
DOCKER_BUILDKIT=1 docker build --output "type=local,dest=$OUT_DIR" "$(dirname "$0")"

echo "── 自检（应为 LGPL 构建，无 --enable-gpl）──"
if [ -x "$OUT_DIR/ffmpeg" ]; then
  "$OUT_DIR/ffmpeg" -version 2>&1 | head -2 || true
  "$(dirname "$0")/smoke.sh" "$OUT_DIR/ffmpeg"
fi
ls -la "$OUT_DIR"
