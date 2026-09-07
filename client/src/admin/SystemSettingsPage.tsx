import { useState } from "react";
import { SettingsPanel } from "../SettingsPanel";
import { PaymentSettingsSection } from "./PaymentSettingsSection";
import { QueueModeSection } from "./QueueModeSection";
import { RatesManager } from "./RatesManager";
import { TabBar } from "./ui/TabBar";
import { ViralRuntimeSection } from "./ViralRuntimeSection";

const tabs = [
  { id: "payment", label: "支付与价格" },
  { id: "rates", label: "费率管理" },
  { id: "services", label: "服务配置" },
];

/**
 * v4 导航合并 — 系统设置：支付与价格、费率管理、服务配置合并为一个菜单项。
 * 费率管理承载上游成本费率（按科目/分辨率）与对外售价（按秒）配置。
 */
export function SystemSettingsPage({
  readOnly = false,
  initialTab = "payment",
}: {
  readOnly?: boolean;
  initialTab?: "payment" | "rates" | "services";
}) {
  const [tab, setTab] = useState<string>(initialTab);
  return (
    <div>
      <TabBar
        active={tab}
        ariaLabel="系统设置页签"
        items={tabs}
        onChange={setTab}
      />
      {tab === "payment" ? (
        <PaymentSettingsSection readOnly={readOnly} />
      ) : null}
      {tab === "rates" ? <RatesManager readOnly={readOnly} /> : null}
      {tab === "services" ? (
        <>
          <QueueModeSection readOnly={readOnly} />
          <ViralRuntimeSection readOnly={readOnly} />
          <section className="admin-panel" aria-label="服务配置">
            <SettingsPanel readOnly={readOnly} source="control" />
          </section>
        </>
      ) : null}
    </div>
  );
}
