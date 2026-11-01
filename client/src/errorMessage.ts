/** 错误 → 用户可见文案的唯一实现。
 *
 * 2026-10-31 前六个组件各持一份 errorMessage（两种判断口径 + 一份冗余的
 * CustomerApiError 分支——它是 Error 子类，instanceof Error 已覆盖）。
 * 统一为 trim 口径：空白-only 的 error.message 视为无文案，回退 fallback
 * （多数副本本就如此；仅两处从「照显空白」变为回退，属修正）。
 */
export function errorMessage(error: unknown, fallback: string): string {
  return error instanceof Error && error.message.trim()
    ? error.message
    : fallback;
}
