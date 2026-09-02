// 统一分页条：offset 驱动，文案固定为"第 X / Y 页（共 N 条）"。
// 两种模式：
// - 已知 total（多数列表）：total 不足以翻页时整条不渲染；
// - 未知 total（服务端暂未返回 total 的端点，如 /devices）：传 hasMore，
//   只显示"第 X 页"，"下一页"按 hasMore 启用——不伪造总数。
export function Pagination({
  offset,
  limit,
  total,
  hasMore,
  disabled = false,
  noun = "条",
  onPageChange,
}: {
  offset: number;
  limit: number;
  total?: number;
  /** total 未知模式的续页信号：本次取满一页即视为可能还有下一页。 */
  hasMore?: boolean;
  disabled?: boolean;
  /** 计数名词：条数用"条"，客户用"位"。 */
  noun?: string;
  onPageChange: (nextOffset: number) => void;
}) {
  const knownTotal = typeof total === "number";
  const totalPages = knownTotal ? Math.ceil(total / limit) : null;
  if (knownTotal && !(totalPages !== null && totalPages > 1)) {
    return null;
  }
  const currentPage = Math.floor(offset / limit) + 1;
  const hasNext = knownTotal
    ? offset + limit < (total as number)
    : Boolean(hasMore);
  return (
    <nav aria-label="分页" className="pagination">
      <button
        disabled={disabled || offset <= 0}
        type="button"
        onClick={() => onPageChange(Math.max(0, offset - limit))}
      >
        上一页
      </button>
      <span>
        {totalPages !== null
          ? `第 ${currentPage} / ${totalPages} 页（共 ${total} ${noun}）`
          : `第 ${currentPage} 页`}
      </span>
      <button
        disabled={disabled || !hasNext}
        type="button"
        onClick={() => onPageChange(offset + limit)}
      >
        下一页
      </button>
    </nav>
  );
}
