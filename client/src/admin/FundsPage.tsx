import { useState } from "react";
import type { ReconciliationAnomaly } from "../api";

import { AccountsPage } from "./AccountsPage";
import { AdjustmentsPage } from "./AdjustmentsPage";
import { FundsOverview } from "./FundsOverview";
import { OrdersPage } from "./OrdersPage";
import { ReconciliationPage } from "./ReconciliationPage";
import { RefundsPage } from "./RefundsPage";
import { PageBanner } from "./ui/PageBanner";
import { TabBar } from "./ui/TabBar";
import { useHashParam } from "./ui/useHashParam";

const tabs = [
  { id: "overview", label: "资金概览" },
  { id: "orders", label: "收款订单" },
  { id: "transactions", label: "积分流水" },
  { id: "adjustments", label: "人工调整" },
  { id: "refunds", label: "退款管理" },
  { id: "reconciliation", label: "对账异常" },
];

const FUNDS_TABS = [
  "overview",
  "orders",
  "transactions",
  "adjustments",
  "refunds",
  "reconciliation",
] as const;

export type FundsTab =
  | "overview"
  | "orders"
  | "transactions"
  | "adjustments"
  | "refunds"
  | "reconciliation";

/**
 * 资金中心（方案 P1）：从「资金流水」两页签扩为五个，财务月度对账不离开
 * 本页。调账记录从审计中心迁入（一件事只有一个入口），对账异常清单让总览
 * 待办的数字「点进去条数一致」。
 */
export function FundsPage({
  readOnly = false,
  initialTab,
  userId = "",
  orderNo = "",
  initialMonth,
  onMonthChange,
  initialAnomaly = "paid_without_charge",
  initialOrderStatus = "",
  onCustomer,
}: {
  initialOrderStatus?: string;
  readOnly?: boolean;
  initialTab?: FundsTab;
  userId?: string;
  orderNo?: string;
  initialMonth?: string;
  onMonthChange?: (month: string) => void;
  initialAnomaly?: ReconciliationAnomaly;
  onCustomer?: (id: string) => void;
}) {
  const [month, setMonth] = useState(
    () =>
      initialMonth ??
      new Intl.DateTimeFormat("en-CA", {
        timeZone: "Asia/Shanghai",
        year: "numeric",
        month: "2-digit",
      })
        .format(new Date())
        .slice(0, 7),
  );
  const [anomaly, setAnomaly] = useState<ReconciliationAnomaly>(initialAnomaly);
  // 页签写入 URL（?sub=）：刷新与分享链接都停在当前页签；跳转意图优先。
  const [tab, setTab] = useHashParam<FundsTab>(
    "sub",
    "overview",
    FUNDS_TABS,
    initialTab,
  );
  return (
    <div>
      {userId && ["orders", "transactions", "adjustments"].includes(tab) ? (
        <PageBanner tone="notice">
          当前只看一位客户的记录（编号 {userId}）。
          <a href={`#admin/funds?sub=${tab}`}>查看全部客户</a>
        </PageBanner>
      ) : null}
      <TabBar
        active={tab}
        ariaLabel="资金中心页签"
        items={tabs}
        onChange={(next) => setTab(next as FundsTab)}
      />
      {tab === "overview" && (
        <FundsOverview
          readOnly={readOnly}
          selectedMonth={month}
          onMonthChange={(next) => {
            setMonth(next);
            onMonthChange?.(next);
          }}
          onOpenReconciliation={() => setTab("reconciliation")}
        />
      )}
      {tab === "orders" && (
        <OrdersPage
          onCustomer={onCustomer}
          readOnly={readOnly}
          userId={userId}
          orderNo={orderNo}
          initialStatus={initialOrderStatus}
          onOpenReconciliation={(next) => {
            setAnomaly(next);
            setTab("reconciliation");
          }}
        />
      )}
      {tab === "transactions" && (
        <AccountsPage readOnly={readOnly} userId={userId} />
      )}
      {tab === "adjustments" && (
        <AdjustmentsPage
          readOnly={readOnly}
          onCustomer={onCustomer}
          userId={userId || undefined}
        />
      )}
      {tab === "refunds" && (
        <RefundsPage readOnly={readOnly} onCustomer={onCustomer} />
      )}
      {tab === "reconciliation" && (
        <ReconciliationPage
          key={anomaly}
          readOnly={readOnly}
          initialAnomaly={anomaly}
        />
      )}
    </div>
  );
}
