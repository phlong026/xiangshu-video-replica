import type { ReactNode } from "react";

/**
 * 统一的筛选栏（整改 T20）。
 *
 * 此前各页自己写筛选表单：提交按钮叫「查询 / 筛选 / 搜索 / 查询诊断」，只有
 * 审计页有「重置」，导出与自动刷新的位置和顺序也各不相同。这里固定为：
 *
 *     [筛选项…]  查询 | 重置 | 其它动作（导出、自动刷新…）
 *
 * - 「查询」是唯一的提交按钮；
 * - 「重置」只在传入 onReset 时出现，并且由调用方决定「重置」的含义（清空
 *   草稿并重新查询），本组件不替它猜；
 * - 其它动作经 actions 传入，排在重置之后。
 */
export function FilterBar({
  ariaLabel,
  children,
  onSubmit,
  onReset,
  busy = false,
  submitLabel = "查询",
  actions,
  className = "",
}: {
  ariaLabel?: string;
  /** 筛选项（通常是若干 label + input/select）。 */
  children: ReactNode;
  onSubmit: () => void;
  onReset?: () => void;
  busy?: boolean;
  submitLabel?: string;
  actions?: ReactNode;
  className?: string;
}) {
  return (
    <form
      aria-label={ariaLabel}
      className={`admin-toolbar admin-filter-bar ${className}`.trim()}
      onSubmit={(event) => {
        event.preventDefault();
        onSubmit();
      }}
    >
      {children}
      <div className="admin-filter-bar__actions">
        <button disabled={busy} type="submit">
          {submitLabel}
        </button>
        {onReset ? (
          <button disabled={busy} type="button" onClick={onReset}>
            重置
          </button>
        ) : null}
        {actions}
      </div>
    </form>
  );
}
