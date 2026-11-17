// 与服务端媒体门禁对应；源片范围与单镜头生成能力分别校验。
export const SOURCE_DURATION_TOLERANCE_SECONDS = 0.1;
export const MIN_SOURCE_SECONDS = 4;
export const MAX_REPLICA_SOURCE_SECONDS = 30;
export const MAX_LONG_SOURCE_SECONDS = 180;
export const MAX_REPLICA_SOURCE_BYTES = 200 * 1024 * 1024;
export const MAX_LONG_SOURCE_BYTES = 600 * 1024 * 1024;

export function sourceAnalysisPurpose(
  duration: number,
): "replica" | "replica_prompt" {
  if (
    !Number.isFinite(duration) ||
    duration < MIN_SOURCE_SECONDS - SOURCE_DURATION_TOLERANCE_SECONDS
  ) {
    throw new Error("请选择不少于 4 秒的有效视频。");
  }
  if (duration > MAX_LONG_SOURCE_SECONDS + SOURCE_DURATION_TOLERANCE_SECONDS) {
    throw new Error("长视频拆解仅支持 4–180 秒的视频，请裁剪后再试。");
  }
  return duration <=
    MAX_REPLICA_SOURCE_SECONDS + SOURCE_DURATION_TOLERANCE_SECONDS
    ? "replica"
    : "replica_prompt";
}

export function validateSourceAnalysis(duration: number, size: number) {
  const purpose = sourceAnalysisPurpose(duration);
  const limit =
    purpose === "replica" ? MAX_REPLICA_SOURCE_BYTES : MAX_LONG_SOURCE_BYTES;
  if (!Number.isSafeInteger(size) || size <= 0)
    throw new Error("视频内容为空或大小无效。");
  if (size > limit) {
    throw new Error(
      purpose === "replica"
        ? "参考视频不能超过 200 MB，请压缩后再上传。"
        : "长视频不能超过 600 MB，请压缩后再上传。",
    );
  }
  return purpose;
}
