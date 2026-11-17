import {
  chooseNativeSource,
  isNativeSource,
  type NativeSourceFile,
  restoreNativeSource,
  type SourceFile,
} from "../nativeSource";

type LocalSource = { file: SourceFile; duration: number };

// 页面切换不等于换源；只缓存原始 File 引用，不复制媒体字节或写入云端草稿。
const sources = new Map<string, Map<string, LocalSource>>();

export function rememberLocalSource(
  accountId: string,
  assetId: string,
  source: LocalSource,
) {
  let account = sources.get(accountId);
  if (!account) {
    account = new Map();
    sources.set(accountId, account);
  }
  account.delete(assetId);
  account.set(assetId, source);
  if (isNativeSource(source.file)) {
    try {
      const key = `native-source:${accountId}:${assetId}`;
      localStorage.setItem(
        key,
        JSON.stringify({ native_source_id: source.file.native_source_id }),
      );
    } catch {
      /* 持久化失败不影响本次已选择的来源。 */
    }
  }
  while (account.size > 3) {
    const oldest = account.keys().next().value;
    if (oldest === undefined) break;
    account.delete(oldest);
  }
}

export function localSourceForAsset(
  accountId: string,
  assetId: string | undefined,
) {
  return assetId ? sources.get(accountId)?.get(assetId) : undefined;
}

export function clearLocalSources(accountId: string) {
  sources.delete(accountId);
}

export async function restoreLocalSource(
  accountId: string,
  assetId: string | undefined,
  allowRelocate = false,
) {
  const cached = localSourceForAsset(accountId, assetId);
  if (cached || !assetId) return cached;
  const key = `native-source:${accountId}:${assetId}`;
  let id: unknown;
  try {
    const raw = localStorage.getItem(key);
    if (!raw) return undefined;
    id = (JSON.parse(raw) as Pick<NativeSourceFile, "native_source_id">)
      .native_source_id;
  } catch {
    return undefined;
  }
  if (typeof id !== "string") return undefined;
  let file: NativeSourceFile;
  try {
    file = await restoreNativeSource(accountId, id);
  } catch (cause) {
    if (!allowRelocate) throw cause;
    const selected = await chooseNativeSource(accountId, id);
    if (!selected) throw new Error("原视频未重新定位，稍后可继续。");
    file = selected;
  }
  const source = { file, duration: file.duration_seconds };
  rememberLocalSource(accountId, assetId, source);
  return source;
}
