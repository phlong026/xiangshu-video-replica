import { useId, useRef, useState } from "react";
import { type GlobalSearchItem, globalSearch } from "../api.admin";

const TYPE_LABELS: Record<GlobalSearchItem["type"], string> = {
  customer: "客户",
  order: "订单",
  task: "任务",
};

/**
 * 顶栏全局搜索（整改 T39）：粘贴客户名 / 订单号 / 任务号，回车。
 * 只命中一项时直接跳转；命中多项时列出让人选；没有命中给出明确提示而不是静默。
 */
export function GlobalSearch({
  onPick,
}: {
  onPick: (item: GlobalSearchItem) => void;
}) {
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<GlobalSearchItem[] | null>(null);
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const listId = useId();
  // 过期响应丢弃：连续回车时只认最后一次。
  const sequence = useRef(0);

  function pick(item: GlobalSearchItem) {
    setResults(null);
    setMessage("");
    setQuery("");
    onPick(item);
  }

  async function run() {
    const term = query.trim();
    if (term.length < 2) {
      setResults(null);
      setMessage("请至少输入 2 个字符。");
      return;
    }
    const current = ++sequence.current;
    setBusy(true);
    setMessage("");
    try {
      const { items } = await globalSearch(term);
      if (current !== sequence.current) return;
      if (items.length === 1) {
        pick(items[0]);
      } else if (items.length === 0) {
        setResults(null);
        setMessage("没有找到匹配的客户、订单或任务。");
      } else {
        setResults(items);
      }
    } catch (cause) {
      if (current !== sequence.current) return;
      setResults(null);
      setMessage(cause instanceof Error ? cause.message : "搜索失败");
    } finally {
      if (current === sequence.current) setBusy(false);
    }
  }

  return (
    <search className="admin-global-search">
      <form
        className="admin-global-search__form"
        onSubmit={(event) => {
          event.preventDefault();
          void run();
        }}
      >
        <input
          aria-controls={results ? listId : undefined}
          aria-label="全局搜索"
          disabled={busy}
          placeholder="搜客户名 / 订单号 / 任务号"
          value={query}
          onChange={(event) => {
            setQuery(event.target.value);
            setMessage("");
          }}
        />
        <button disabled={busy} type="submit">
          搜索
        </button>
        {message ? (
          <span className="admin-global-search__message" role="status">
            {message}
          </span>
        ) : null}
        {results ? (
          <ul className="admin-global-search__results" id={listId}>
            {results.map((item) => (
              <li key={`${item.type}:${item.id}`}>
                <button type="button" onClick={() => pick(item)}>
                  <strong>
                    [{TYPE_LABELS[item.type]}] {item.label}
                  </strong>
                  <small>{item.detail}</small>
                </button>
              </li>
            ))}
          </ul>
        ) : null}
      </form>
    </search>
  );
}
