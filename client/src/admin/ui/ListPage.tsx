import type { ReactNode } from "react";
import { PageBanner } from "./PageBanner";
import { SkeletonTable } from "./SkeletonTable";

/**
 * 列表页骨架（整改 T21）：把各页重复搭建的「提示条 → 筛选 → 加载/空/表格 →
 * 分页」收成一处，加载、错误、空态、正常四种状态的呈现全站一致。
 *
 * 状态优先级（互斥）：
 * 1. 首次加载（loading 且还没有数据）→ 骨架；
 * 2. 有数据 → 渲染 children（刷新中沿用旧数据，不闪成骨架）；
 * 3. 没有数据且没有错误 → 空态提示；
 * 4. 没有数据且有错误 → 只显示错误（带「重试」），不再叠一条「暂无数据」。
 */
export function ListPage({
  ariaLabel,
  className = "",
  actions,
  filters,
  error,
  notice,
  onRetry,
  loading,
  hasItems,
  loadingText = "加载中…",
  emptyText = "暂无数据。",
  skeletonColumns,
  pagination,
  children,
}: {
  ariaLabel: string;
  className?: string;
  /** 顶部动作区（自动刷新、导出等）；放在提示条之前。 */
  actions?: ReactNode;
  /** 筛选栏（通常是 FilterBar）。 */
  filters?: ReactNode;
  error?: string;
  notice?: string;
  /** 传入后错误提示带「重试」按钮。 */
  onRetry?: () => void;
  loading: boolean;
  hasItems: boolean;
  loadingText?: string;
  emptyText?: string;
  skeletonColumns?: number;
  pagination?: ReactNode;
  children: ReactNode;
}) {
  return (
    <section
      aria-label={ariaLabel}
      className={`admin-panel ${className}`.trim()}
    >
      {actions}
      {error ? (
        <PageBanner tone="error">
          {error}
          {onRetry ? (
            <>
              {" "}
              <button disabled={loading} type="button" onClick={onRetry}>
                重试
              </button>
            </>
          ) : null}
        </PageBanner>
      ) : null}
      {notice ? <PageBanner tone="notice">{notice}</PageBanner> : null}
      {filters}
      {loading && !hasItems ? (
        <SkeletonTable columns={skeletonColumns} label={loadingText} />
      ) : hasItems ? (
        children
      ) : error ? null : (
        <PageBanner tone="notice">{emptyText}</PageBanner>
      )}
      {pagination}
    </section>
  );
}
