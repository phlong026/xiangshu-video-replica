import type { ReactNode } from "react";

/**
 * 口径说明折叠块（整改 T25）。
 *
 * 管理端不少页面顶部堆了 3～5 段口径说明（`admin-hint` 全站 140 余处），真正的
 * 数据被挤到首屏以下。口径必须保留（它是「数字怎么算」的唯一出处），但不该和
 * 结论抢首屏：默认折叠成一行「口径说明」，需要时点开。
 */
export function RuleNote({
  children,
  summary = "口径说明",
}: {
  children: ReactNode;
  summary?: string;
}) {
  return (
    <details className="admin-rule-note">
      <summary>{summary}</summary>
      <div className="admin-rule-note__body">{children}</div>
    </details>
  );
}
