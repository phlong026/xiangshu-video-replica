import {
  getControlSettings,
  testControlProviderConnection,
  updateControlBillingSettings,
  updateControlProviderSettings,
  updateControlRuntimeSettings,
} from "../api";
import { paidTestControlProvider } from "../api.admin";
import { type SettingsBackend, SettingsPanel } from "../SettingsPanel";
import { AdminEnvironmentSwitch } from "./AdminEnvironmentSwitch";
import { BillingQuoteCalculator } from "./BillingQuoteCalculator";
import { BillingRatesManager } from "./BillingRatesManager";
import { CustomerPricingManager } from "./CustomerPricingManager";
import { FeatureSwitchesSection } from "./FeatureSwitchesSection";
import { H3AccountsManager } from "./H3AccountsManager";
import { PaymentSettingsSection } from "./PaymentSettingsSection";
import { QueueModeSection } from "./QueueModeSection";
import { RechargePackageManager } from "./RechargePackageManager";
import { RegistrationBonusSection } from "./RegistrationBonusSection";
import { TabBar } from "./ui/TabBar";
import { useHashParam } from "./ui/useHashParam";

// 方案 P1「价格与套餐一页化」：兑换比例 / 业务单价 / 充值套餐 / 试算器收拢
// 到一个页签，讲清任一客户的扣费规则；收款设置单独一页（商户配置是另一类
// 受众）；技术配置（API 服务、账号并发、运行参数）仅超级管理员可见——
// 页签不出现，而不是点进去看 403。「告警总览」与「团队与权限」已提升为侧边栏
// 独立入口（T27），旧的 ?sub=alerts / ?sub=team 由外壳重定向。
const baseTabs = [
  { id: "pricing", label: "价格与套餐" },
  { id: "collection", label: "收款设置" },
  { id: "features", label: "功能开关" },
];
const technicalTab = { id: "services", label: "技术配置" };

/**
 * 控制面设置后端：走 `/api/control/settings`（内部通道，由反代注入
 * `X-Control-Proxy-Token`）。只由本管理端页面注入给 SettingsPanel，
 * 因此这些控制面 API 仅出现在管理构建制品（`client/dist-admin`），
 * 客户构建制品的依赖图不可达（CW-019）。
 */
const controlBackend: SettingsBackend = {
  load: getControlSettings,
  saveProvider: updateControlProviderSettings,
  saveRuntime: updateControlRuntimeSettings,
  saveBilling: updateControlBillingSettings,
  testProvider: testControlProviderConnection,
  // 付费探针：与免费连接测试并列挂在每个服务卡上。它可能真生成扣费费，服务端按
  // 「敏感写」受理，所以走 `api.admin` 的 adminWrite（confirm + reason + 幂等键
  // + 审计），而不是 api.ts 那条不带写契约的封装。
  testPaidProvider: paidTestControlProvider,
};

/**
 * v4 导航合并 — 系统设置：支付与价格、费率管理、服务配置合并为一个菜单项。
 * 费率管理承载成本费率（按科目/分辨率）与对外售价（按秒）配置。
 */
export function SystemSettingsPage({
  readOnly = false,
  initialTab,
  isSuperAdmin = false,
  onPublishStatsChanged,
}: {
  readOnly?: boolean;
  /** 旧页签 id（payment/rates）在挂载时归并到 pricing，外部 intent 不用改。 */
  initialTab?:
    | "pricing"
    | "collection"
    | "services"
    | "features"
    | "payment"
    | "rates";
  isSuperAdmin?: boolean;
  /** 功能开关切换后通知外壳刷新菜单显隐。 */
  onPublishStatsChanged?: (enabled: boolean) => void;
}) {
  // 收拢归并：payment / rates 都落「价格与套餐」；非超管看不到技术配置页签，
  // 带着该意图进来也落回价格页而不是看一页 404。
  const normalizeTab = (value: string) => {
    if (value === "payment" || value === "rates") return "pricing";
    if (value === "services" && !isSuperAdmin) return "pricing";
    return value;
  };
  const visibleTabs = isSuperAdmin ? [...baseTabs, technicalTab] : baseTabs;
  // 设置页签与技术配置分组都写入 URL（?sub= / ?section=），刷新不丢。
  const [tab, setTab] = useHashParam<string>(
    "sub",
    "pricing",
    visibleTabs.map(({ id }) => id),
    initialTab ? normalizeTab(initialTab) : undefined,
  );
  const [serviceTab, setServiceTab] = useHashParam<string>(
    "section",
    "providers",
    ["providers", "runtime"],
  );
  return (
    <div>
      <TabBar
        active={tab}
        ariaLabel="系统设置页签"
        items={visibleTabs}
        onChange={setTab}
      />
      {/* T37 分层说明：普通管理员的业务设置入口与超管技术配置的边界在此讲清，
          不靠「看不到页签」隐式传达。 */}
      {!isSuperAdmin ? (
        <p className="admin-hint">
          这里是业务设置：价格与套餐、收款、告警均可由管理员直接配置；
          接口服务连接与运行参数等技术配置由超级管理员在「技术配置」页维护，
          配置校验结果见各服务卡的「连接测试」。
        </p>
      ) : null}
      {tab === "pricing" ? (
        <>
          <BillingQuoteCalculator readOnly={readOnly} />
          <CustomerPricingManager readOnly={readOnly} />
          <RechargePackageManager readOnly={readOnly} />
          <BillingRatesManager readOnly={readOnly} />
          <RegistrationBonusSection readOnly={readOnly} />
        </>
      ) : null}
      {tab === "collection" ? (
        <PaymentSettingsSection readOnly={readOnly} />
      ) : null}
      {tab === "services" ? (
        <div className="admin-services">
          <AdminEnvironmentSwitch readOnly={readOnly} />
          <header className="admin-services__header">
            <TabBar
              active={serviceTab}
              ariaLabel="服务配置分组"
              items={[
                { id: "providers", label: "API 服务" },
                { id: "runtime", label: "运行控制" },
              ]}
              onChange={setServiceTab}
            />
          </header>
          {serviceTab === "providers" ? (
            <SettingsPanel
              controlBackend={controlBackend}
              readOnly={readOnly}
              source="control"
              section="providers"
              videoAccounts={<H3AccountsManager readOnly={readOnly} />}
            />
          ) : (
            <div className="admin-services__runtime">
              <div className="admin-services__switches">
                <QueueModeSection readOnly={readOnly} />
              </div>
              <SettingsPanel
                controlBackend={controlBackend}
                readOnly={readOnly}
                source="control"
                section="runtime"
              />
            </div>
          )}
        </div>
      ) : null}
      {tab === "features" ? (
        <FeatureSwitchesSection
          readOnly={readOnly}
          onChanged={onPublishStatsChanged}
        />
      ) : null}
    </div>
  );
}
