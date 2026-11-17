import { invoke, isTauri } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";

export type NativeSourceFile = {
  native_source_id: string;
  account_id: string;
  name: string;
  size: number;
  type: string;
  duration_seconds: number;
  sha256: string;
};
export type SourceFile = File | NativeSourceFile;
export function isNativeSource(file: SourceFile): file is NativeSourceFile {
  return "native_source_id" in file;
}
export function nativeSourceAvailable() {
  return isTauri();
}
export function chooseNativeSource(accountId: string, replaceId?: string) {
  return invoke<NativeSourceFile | null>("choose_local_source", {
    accountId,
    replaceId: replaceId ?? null,
  });
}
export function restoreNativeSource(accountId: string, sourceId: string) {
  return invoke<NativeSourceFile>("get_local_source", { accountId, sourceId });
}
export function releaseNativeAudio(file: SourceFile) {
  if (!isNativeSource(file))
    return Promise.resolve();
  return invoke<void>("release_source_audio", {
    accountId: file.account_id,
    sourceId: file.native_source_id,
  });
}
async function nativeOperation<T>(
  file: NativeSourceFile,
  command: string,
  parameters: Record<string, unknown>,
  signal?: AbortSignal,
  onProgress?: (percent: number) => void,
): Promise<T> {
  signal?.throwIfAborted();
  const operationId = crypto.randomUUID();
  const cancel = () => {
    void invoke("cancel_source_operation", {
      accountId: file.account_id,
      operationId,
    }).catch(() => {});
  };
  const stop = onProgress
    ? await listen<{ operation_id: string; percent: number }>(
        "source-upload-progress",
        ({ payload }) => {
          if (payload.operation_id === operationId) onProgress(payload.percent);
        },
      )
    : undefined;
  try {
    signal?.throwIfAborted();
    signal?.addEventListener("abort", cancel, { once: true });
    const result = await invoke<T>(command, {
      accountId: file.account_id,
      sourceId: file.native_source_id,
      operationId,
      ...parameters,
    });
    signal?.throwIfAborted();
    return result;
  } finally {
    signal?.removeEventListener("abort", cancel);
    stop?.();
  }
}
export async function extractNativeAudio(
  file: NativeSourceFile,
  signal?: AbortSignal,
) {
  const audio = await nativeOperation<NativeSourceFile>(
    file,
    "extract_source_audio",
    {},
    signal,
  );
  return { ...audio, name: `${file.name.replace(/\.[^.]+$/, "")}.m4a` };
}

export async function trimNativeSource(file: NativeSourceFile, startSeconds: number, endSeconds: number, signal?: AbortSignal) {
  const clip = await nativeOperation<NativeSourceFile>(file, "trim_source_video", { startSeconds, endSeconds }, signal);
  return { ...clip, name: `${file.name.replace(/\.[^.]+$/, "").slice(0, 200)}-片段-${startSeconds}-${endSeconds}.mp4` };
}
export function uploadNativeSource(
  file: NativeSourceFile,
  url: string,
  headers: Record<string, string>,
  onProgress: (percent: number) => void,
  signal?: AbortSignal,
) {
  return nativeOperation<void>(
    file,
    "upload_local_source",
    { url, headers },
    signal,
    onProgress,
  );
}
