import { useCallback, useEffect, useRef, useState } from "react";

import {
  type ControlAccount,
  type ControlWalletTransaction,
  getControlAccounts,
  getControlWalletTransactions,
} from "../api";
import { DataTable } from "./ui/DataTable";
import { PageBanner } from "./ui/PageBanner";
import { Pagination } from "./ui/Pagination";
import { useAutoRefresh } from "./ui/useAutoRefresh";
import {
  formatCredits,
  roleLabel,
  transactionTypeLabel,
} from "./ui/vocabulary";

const PAGE_SIZE = 20;

/**
 * 账号与钱包页（从 AdminApp 内联表格抽出，2026-09-02 评估 §0.5）：
 * 补上此前被忽略的分页——服务端一直返回 total 并支持 limit/offset。
 */
export function AccountsPage() {
  const [accounts, setAccounts] = useState<ControlAccount[]>([]);
  const [accountTotal, setAccountTotal] = useState(0);
  const [accountOffset, setAccountOffset] = useState(0);
  const [transactions, setTransactions] = useState<ControlWalletTransaction[]>(
    [],
  );
  const [transactionTotal, setTransactionTotal] = useState(0);
  const [transactionOffset, setTransactionOffset] = useState(0);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const { autoRefresh, toggleAutoRefresh } = useAutoRefresh(() => {
    void loadRef.current?.();
  });

  const loadAccounts = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const [accountPage, transactionPage] = await Promise.all([
        getControlAccounts({ limit: PAGE_SIZE, offset: accountOffset }),
        getControlWalletTransactions({
          limit: PAGE_SIZE,
          offset: transactionOffset,
        }),
      ]);
      setAccounts(accountPage.items);
      setAccountTotal(accountPage.total);
      setTransactions(transactionPage.items);
      setTransactionTotal(transactionPage.total);
    } catch (cause) {
      setError(
        cause instanceof Error && cause.message
          ? `加载失败：${cause.message}`
          : "加载失败：读取账号与钱包失败。",
      );
    } finally {
      setLoading(false);
    }
  }, [accountOffset, transactionOffset]);

  const loadRef = useRef<(() => void) | null>(null);
  loadRef.current = () => void loadAccounts();

  useEffect(() => {
    void loadAccounts();
  }, [loadAccounts]);

  return (
    <section aria-label="账号与钱包" className="admin-panel">
      <div className="admin-actions">
        <button
          aria-pressed={autoRefresh}
          type="button"
          onClick={toggleAutoRefresh}
        >
          {autoRefresh ? "自动刷新：开（30 秒）" : "自动刷新：关"}
        </button>
      </div>
      <h2>账号钱包</h2>
      {error ? <PageBanner tone="error">{error}</PageBanner> : null}
      {loading ? <div className="loading">加载中...</div> : null}
      {!loading && accounts.length === 0 && !error ? (
        <PageBanner tone="notice">暂无内部账号。</PageBanner>
      ) : null}

      {!loading && accounts.length > 0 ? (
        <DataTable
          ariaLabel="账号钱包列表"
          headers={
            <>
              <th>账号</th>
              <th>姓名</th>
              <th>角色</th>
              <th>可用（条）</th>
              <th>冻结（条）</th>
              <th>令牌</th>
            </>
          }
        >
          {accounts.map((account) => (
            <tr key={account.id}>
              <td>{account.username}</td>
              <td>{account.display_name}</td>
              <td>{roleLabel(account.role)}</td>
              <td>{account.available_credits}</td>
              <td>{account.reserved_credits}</td>
              <td>{account.active_token_count}</td>
            </tr>
          ))}
        </DataTable>
      ) : null}
      <Pagination
        disabled={loading}
        limit={PAGE_SIZE}
        offset={accountOffset}
        total={accountTotal}
        onPageChange={setAccountOffset}
      />

      <h2>账务流水</h2>
      {!loading && transactions.length > 0 ? (
        <DataTable
          ariaLabel="账务流水列表"
          headers={
            <>
              <th>ID</th>
              <th>账号</th>
              <th>类型</th>
              <th>可用变动</th>
              <th>冻结变动</th>
            </>
          }
        >
          {transactions.map((tx) => (
            <tr key={tx.id}>
              <td>{tx.id}</td>
              <td>{tx.username}</td>
              <td>{transactionTypeLabel(tx.type)}</td>
              <td>{tx.available_delta}</td>
              <td>{tx.reserved_delta}</td>
            </tr>
          ))}
        </DataTable>
      ) : null}
      {!loading && transactions.length === 0 && !error ? (
        <PageBanner tone="notice">暂无账务流水。</PageBanner>
      ) : null}
      <Pagination
        disabled={loading}
        limit={PAGE_SIZE}
        offset={transactionOffset}
        total={transactionTotal}
        onPageChange={setTransactionOffset}
      />
      <p className="admin-hint">
        余额单位为条：{formatCredits(1)}视频 = 1 条，充值与调账都按条入账。
      </p>
    </section>
  );
}
