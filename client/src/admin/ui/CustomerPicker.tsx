import { useEffect, useId, useRef, useState } from "react";
import { listCustomers } from "../../api.admin";

export type PickedCustomer = {
  user_id: string;
  username: string;
  display_name: string;
};

// 客户选择器（整改 T13）：按用户名或公司名联想，回填 userId。
// 此前工单要粘贴用户编号、总览快捷操作跳到列表后还得再搜一次客户，
// 运营的识别路径是「公司名 → 账号」，这里把它做成一步。
//
// 搜索走客户列表同一接口（关键字同时匹配用户名与公司名），300ms 防抖，
// 只取前 8 条；过期响应按请求序号丢弃，不会把旧结果盖到新输入上。

const SEARCH_DELAY_MS = 300;
const MAX_OPTIONS = 8;

export function customerLabel(customer: PickedCustomer): string {
  return customer.display_name?.trim()
    ? `${customer.display_name}（${customer.username}）`
    : customer.username;
}

export function CustomerPicker({
  label = "客户",
  value,
  onChange,
  placeholder = "输入用户名或公司名搜索",
  autoFocus = false,
  disabled = false,
}: {
  label?: string;
  value: PickedCustomer | null;
  onChange: (customer: PickedCustomer | null) => void;
  placeholder?: string;
  autoFocus?: boolean;
  disabled?: boolean;
}) {
  const [query, setQuery] = useState("");
  const [options, setOptions] = useState<PickedCustomer[]>([]);
  const [status, setStatus] = useState("");
  const sequence = useRef(0);
  const listId = useId();
  const inputRef = useRef<HTMLInputElement | null>(null);
  // 只在挂载时聚焦一次；不能每次渲染都抢焦点。
  // biome-ignore lint/correctness/useExhaustiveDependencies: 刻意只在挂载时执行。
  useEffect(() => {
    if (autoFocus) inputRef.current?.focus();
  }, []);

  useEffect(() => {
    const keyword = query.trim();
    const current = ++sequence.current;
    if (!keyword) {
      setOptions([]);
      setStatus("");
      return;
    }
    setStatus("正在搜索…");
    const timer = window.setTimeout(() => {
      void listCustomers({ username_filter: keyword, limit: MAX_OPTIONS })
        .then((response) => {
          if (current !== sequence.current) return;
          const found = response.items.map((item) => ({
            user_id: item.user_id,
            username: item.username,
            display_name: item.display_name ?? "",
          }));
          setOptions(found);
          setStatus(found.length ? "" : "没有匹配的客户。");
        })
        .catch((cause: unknown) => {
          if (current !== sequence.current) return;
          setOptions([]);
          setStatus(cause instanceof Error ? cause.message : "搜索客户失败");
        });
    }, SEARCH_DELAY_MS);
    return () => window.clearTimeout(timer);
  }, [query]);

  if (value) {
    return (
      <div className="admin-customer-picker admin-customer-picker--chosen">
        <span>{label}</span>
        <strong>{customerLabel(value)}</strong>
        {disabled ? null : (
          <button type="button" onClick={() => onChange(null)}>
            更换客户
          </button>
        )}
      </div>
    );
  }

  return (
    <div className="admin-customer-picker">
      <label>
        {label}
        <input
          ref={inputRef}
          aria-controls={listId}
          disabled={disabled}
          placeholder={placeholder}
          type="search"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
        />
      </label>
      {status ? (
        <small className="admin-hint" role="status">
          {status}
        </small>
      ) : null}
      {options.length ? (
        <ul className="admin-customer-picker__options" id={listId}>
          {options.map((option) => (
            <li key={option.user_id}>
              <button
                type="button"
                onClick={() => {
                  onChange(option);
                  setQuery("");
                  setOptions([]);
                }}
              >
                {customerLabel(option)}
              </button>
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}
