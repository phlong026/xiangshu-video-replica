/**
 * 骨架加载：占位行 + 可读的加载文案（整改 T21）。
 *
 * 此前这个组件写死了「ID / 显示名称 / 用户名…」六列且全站零引用，列表页
 * 加载中各写各的（"加载中..." / "正在读取…" / 什么都不显示）。改成与列数
 * 无关的通用占位，由 ListPage 统一使用；文案可由调用方传入以保持原有提示。
 */
export function SkeletonTable({
  numRows = 5,
  columns = 6,
  label = "加载中…",
}: {
  numRows?: number;
  columns?: number;
  label?: string;
}) {
  return (
    <div aria-busy="true" className="skeleton-table" role="status">
      <p className="admin-hint">{label}</p>
      <table aria-hidden="true" className="data-table">
        <tbody>
          {Array.from({ length: numRows }).map((_, row) => (
            // biome-ignore lint/suspicious/noArrayIndexKey: 骨架行是固定数量的纯占位展示，无重排/增删，顺序即内容。
            <tr key={row}>
              {Array.from({ length: columns }).map((_, cell) => (
                // biome-ignore lint/suspicious/noArrayIndexKey: 同上，纯占位单元格。
                <td className="skeleton-cell" key={cell} />
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
