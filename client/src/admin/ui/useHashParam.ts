import { useCallback, useRef, useState } from "react";

// 管理端可见状态进 URL（整改 T07）：页内子页签、客户详情页签等此前只存在
// 组件 state 里，刷新即回到默认页签，链接也无法分享给同事。
//
// 约定：
// - 只用 replaceState 改写当前 hash 的 query，不新增历史条目、不触发
//   hashchange，外层 AdminApp 不会因此重挂载页面；
// - 取值必须落在 allowed 内，手改出来的脏值按 fallback 处理；
// - 值等于 fallback 时从 URL 删除该键，默认页签不留冗余参数；
// - 用户主动切换时一并去掉 intent：跳转意图只对「刚跳进来那一刻」有效，
//   留着它会让刷新后的页面又被意图拉回入口页签。
//
// 页签改名时用 aliases 把旧值映射到新值，保证旧链接不失效。
//
// 允许的键需登记在 AdminApp 的 hash 参数白名单里，否则会在加载时被清掉。

function currentParams(): URLSearchParams {
  if (typeof window === "undefined") return new URLSearchParams();
  return new URLSearchParams(window.location.hash.split("?")[1] ?? "");
}

export function readHashParam(key: string): string {
  return currentParams().get(key) ?? "";
}

export function writeHashParams(
  updates: Record<string, string | null>,
  { dropIntent = true }: { dropIntent?: boolean } = {},
): void {
  if (typeof window === "undefined") return;
  const [path] = window.location.hash.split("?", 1);
  const params = currentParams();
  for (const [key, value] of Object.entries(updates)) {
    if (value) params.set(key, value);
    else params.delete(key);
  }
  if (dropIntent) params.delete("intent");
  const query = params.toString();
  const next = `${path || "#"}${query ? `?${query}` : ""}`;
  if (next !== window.location.hash) {
    window.history.replaceState(null, "", next);
  }
}

/**
 * 与 useState 同形的 URL 参数状态。`preferred` 是外部意图指定的初始值
 * （优先于 URL），未指定时取 URL 里的值，再退回 `fallback`。
 */
export function useHashParam<T extends string>(
  key: string,
  fallback: T,
  allowed: readonly T[],
  preferred?: T,
  /** 旧取值 → 新取值：改过名的页签 id 让旧书签 / 转发链接继续有效。 */
  aliases: Readonly<Record<string, T>> = {},
): [T, (next: T) => void] {
  const [value, setValue] = useState<T>(() => {
    if (preferred && allowed.includes(preferred)) return preferred;
    const raw = readHashParam(key);
    const fromUrl = (aliases[raw] ?? raw) as T;
    return allowed.includes(fromUrl) ? fromUrl : fallback;
  });
  const current = useRef(value);
  current.current = value;
  const update = useCallback(
    (next: T) => {
      // 值未变（如意图落地时的同值回写）不改 URL，免得无谓地吞掉意图参数。
      if (next === current.current) return;
      current.current = next;
      setValue(next);
      writeHashParams({ [key]: next === fallback ? null : next });
    },
    [key, fallback],
  );
  return [value, update];
}
