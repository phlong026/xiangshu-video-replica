# 精简 LGPL 构建 ffmpeg/ffprobe（静态 exe，musl 交叉编译）。
# 只保留图片解码校验和"上传视频 → 抽音轨（AAC/m4a）→ ffprobe 探测时长"所需能力；
# 不含 x264/x265 等 GPL 库，产物按 LGPL v3 分发（见
# client/src-tauri/resources/ffmpeg/README.md）。
FROM debian:bookworm-slim AS build

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential pkg-config curl ca-certificates xz-utils make \
        nasm yasm && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /src

COPY smoke.sh /usr/local/bin/ffmpeg-image-smoke
RUN chmod +x /usr/local/bin/ffmpeg-image-smoke

# musl 交叉工具链
RUN curl -fsSL https://musl.cc/x86_64-linux-musl-cross.tgz \
      | tar -xz -C /opt
ENV PATH="/opt/x86_64-linux-musl-cross/bin:${PATH}"
ENV CC=x86_64-linux-musl-gcc CXX=x86_64-linux-musl-g++ \
    AR=x86_64-linux-musl-ar RANLIB=x86_64-linux-musl-ranlib

ARG ZLIB_VERSION=1.3.1
WORKDIR /tmp/zlib
RUN curl -fsSL "https://zlib.net/fossils/zlib-${ZLIB_VERSION}.tar.gz" \
      | tar -xz --strip-components=1 && \
    ./configure --static --prefix=/opt/zlib && \
    make -j"$(nproc)" && make install

ARG FFMPEG_VERSION=7.1.1
WORKDIR /src
RUN curl -fsSL "https://ffmpeg.org/releases/ffmpeg-${FFMPEG_VERSION}.tar.xz" \
      | tar -xJ --strip-components=1

# 精简配置：图片校验、音视频容器/解码、AAC 原生编码、无 GPL 组件。
RUN ./configure \
        --target-os=linux --arch=x86_64 \
        --cross-prefix=x86_64-linux-musl- \
        --pkg-config-flags=--static \
        --extra-cflags="-I/opt/zlib/include -Os -ffunction-sections -fdata-sections" \
        --extra-ldflags="-L/opt/zlib/lib -static -Wl,--gc-sections" \
        --disable-everything --disable-autodetect --disable-debug \
        --disable-doc --disable-programs --disable-network --disable-iconv \
        --disable-bzlib --disable-lzma --disable-sdl2 --disable-xlib \
        --disable-libxcb --disable-vaapi --disable-vdpau --disable-d3d11va \
        --enable-small \
        --enable-zlib \
        --enable-ffmpeg --enable-ffprobe \
        --enable-protocol=file,pipe \
        --enable-demuxer=mov,mp4,m4a,matroska,webm,avi,flv,ogg,wav,mp3,aac,image_jpeg_pipe,image_png_pipe,image_webp_pipe \
        --enable-parser=aac,ac3,dca,flac,h264,hevc,mjpeg,mpegaudio,opus,png,vorbis,vp8,vp9,webp \
        --enable-decoder=aac,mp3,mp2,flac,opus,vorbis,pcm_s16le,pcm_u8,ac3,eac3,mjpeg,png,webp \
        --enable-encoder=aac,flac,pcm_s16le,wrapped_avframe \
        --enable-muxer=mp4,ipod,adts,flac,wav,null \
        --enable-bsf=aac_adtstoasc \
        --enable-filter=aresample,aformat,anull,volume \
        && make -j"$(nproc)"

RUN test -x ffmpeg && test -x ffprobe && \
    ./ffmpeg -version | head -3 && \
    /usr/local/bin/ffmpeg-image-smoke ./ffmpeg

FROM scratch AS export
COPY --from=build /src/ffmpeg /out/ffmpeg
COPY --from=build /src/ffprobe /out/ffprobe
