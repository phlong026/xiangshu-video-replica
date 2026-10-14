import { fireEvent, render, screen, within } from "@testing-library/react";
import type { ReactNode } from "react";
import { expect, test, vi } from "vitest";

import { SystemSettingsPage } from "./SystemSettingsPage";

// 系统设置是「v4 导航合并」后的容器页：四个页签（价格与套餐、收款设置、功能开关、技术配置）、服务配置下还套一层子页签，
// 并把只读态与**控制面设置后端**注入给共用组件 SettingsPanel（CW-019：客户
// 构建制品的依赖图不可达，控制面 API 只出现在管理制品里）。
// 各区块各自已有独立测试，这里只钉这层接线 —— 它此前完全无覆盖。
vi.mock("./CustomerPricingManager", () => ({
  CustomerPricingManager: ({ readOnly }: { readOnly?: boolean }) => (
    <div data-testid="customer-pricing">{`ro=${String(readOnly)}`}</div>
  ),
}));
vi.mock("./RechargePackageManager", () => ({
  RechargePackageManager: ({ readOnly }: { readOnly?: boolean }) => (
    <div data-testid="recharge-packages">{`ro=${String(readOnly)}`}</div>
  ),
}));
vi.mock("./RegistrationBonusSection", () => ({
  RegistrationBonusSection: ({ readOnly }: { readOnly?: boolean }) => (
    <div data-testid="registration-bonus">{`ro=${String(readOnly)}`}</div>
  ),
}));
vi.mock("./PaymentSettingsSection", () => ({
  PaymentSettingsSection: ({ readOnly }: { readOnly?: boolean }) => (
    <div data-testid="payment-settings">{`ro=${String(readOnly)}`}</div>
  ),
}));
vi.mock("./BillingRatesManager", () => ({
  BillingRatesManager: ({ readOnly }: { readOnly?: boolean }) => (
    <div data-testid="billing-rates">{`ro=${String(readOnly)}`}</div>
  ),
}));
vi.mock("./AdminEnvironmentSwitch", () => ({
  AdminEnvironmentSwitch: ({ readOnly }: { readOnly?: boolean }) => (
    <div data-testid="environment-switch">{`ro=${String(readOnly)}`}</div>
  ),
}));
vi.mock("./H3AccountsManager", () => ({
  H3AccountsManager: () => <div data-testid="h3-accounts">视频账号</div>,
}));
vi.mock("./QueueModeSection", () => ({
  QueueModeSection: () => <div data-testid="queue-mode">排队模式</div>,
}));
vi.mock("./ViralRuntimeSection", () => ({
  ViralRuntimeSection: () => <div data-testid="viral-runtime">爆款运行时</div>,
}));
vi.mock("./FeatureSwitchesSection", () => ({
  FeatureSwitchesSection: ({ readOnly }: { readOnly?: boolean }) => (
    <div data-testid="feature-switches">{`功能开关 ro=${String(readOnly)}`}</div>
  ),
}));
vi.mock("../SettingsPanel", () => ({
  SettingsPanel: ({
    source,
    section,
    videoAccounts,
  }: {
    source?: string;
    section?: string;
    videoAccounts?: ReactNode;
  }) => (
    <div data-testid="settings-panel">
      {`source=${String(source)} section=${String(section)}`}
      {videoAccounts}
    </div>
  ),
}));

test("defaults to the pricing tab with every block writable", () => {
  render(<SystemSettingsPage />);

  expect(
    screen.getByRole("tablist", { name: "系统设置页签" }),
  ).toBeInTheDocument();
  // 价格与套餐一页化（方案 P1）：定价、套餐、费率、试算器同页挂载。
  expect(screen.getByTestId("customer-pricing")).toHaveTextContent("ro=false");
  expect(screen.getByTestId("recharge-packages")).toHaveTextContent("ro=false");
  expect(screen.getByTestId("billing-rates")).toHaveTextContent("ro=false");
  // 注册赠送积分与充值套餐同类（给客户多少积分的策略），也挂在本页签。
  expect(screen.getByTestId("registration-bonus")).toHaveTextContent(
    "ro=false",
  );
  // 其它页签的内容不该提前挂载（各区块都会各自发请求）。
  expect(screen.queryByTestId("payment-settings")).toBeNull();
  expect(screen.queryByTestId("settings-panel")).toBeNull();
});

test("honours the initialTab prop", () => {
  // 旧 rates 意图归并到「价格与套餐」：定价区块随之挂载。
  render(<SystemSettingsPage initialTab="rates" />);

  expect(screen.getByTestId("billing-rates")).toBeInTheDocument();
  expect(screen.getByTestId("customer-pricing")).toBeInTheDocument();
});

test("mounts the feature switches tab only when selected", () => {
  render(<SystemSettingsPage initialTab="features" />);

  expect(screen.getByTestId("feature-switches")).toBeInTheDocument();
  expect(screen.queryByTestId("customer-pricing")).toBeNull();
  expect(screen.queryByTestId("settings-panel")).toBeNull();
});

test("forwards readOnly into the pricing blocks", () => {
  render(<SystemSettingsPage readOnly />);

  expect(screen.getByTestId("customer-pricing")).toHaveTextContent("ro=true");
  expect(screen.getByTestId("recharge-packages")).toHaveTextContent("ro=true");
  expect(screen.getByTestId("billing-rates")).toHaveTextContent("ro=true");
  expect(screen.getByTestId("registration-bonus")).toHaveTextContent("ro=true");
  // 收款设置在独立页签：切过去后同样收到只读态。
  fireEvent.click(screen.getByRole("tab", { name: "收款设置" }));
  expect(screen.getByTestId("payment-settings")).toHaveTextContent("ro=true");
});

test("injects the control-plane backend into the shared settings panel", () => {
  render(<SystemSettingsPage initialTab="services" isSuperAdmin />);

  const panel = screen.getByTestId("settings-panel");
  // CW-019：管理端必须走 source="control"（SettingsPanel 据此拒绝 reveal 已保存
  // 密钥等仅内部车道可用的动作），且后端由本页注入而非 SettingsPanel 静态引用。
  expect(panel).toHaveTextContent("source=control section=providers");
  // 视频账号管理作为插槽传进同一面板。
  expect(within(panel).getByTestId("h3-accounts")).toBeInTheDocument();
  expect(screen.getByTestId("environment-switch")).toHaveTextContent(
    "ro=false",
  );
});

test("switches the service sub-tab to the runtime controls", () => {
  render(<SystemSettingsPage initialTab="services" isSuperAdmin />);

  fireEvent.click(screen.getByRole("tab", { name: "运行控制" }));

  expect(screen.getByTestId("settings-panel")).toHaveTextContent(
    "section=runtime",
  );
  expect(screen.getByTestId("queue-mode")).toBeInTheDocument();
  expect(screen.queryByTestId("h3-accounts")).toBeNull();
  // 采集设置已迁到内容组（方案 P1 内容模块），技术配置里不再出现。
  expect(screen.queryByTestId("viral-runtime")).toBeNull();
});

test("keeps the service sub-tab selection inside the services tab", () => {
  render(<SystemSettingsPage initialTab="services" isSuperAdmin />);

  fireEvent.click(screen.getByRole("tab", { name: "运行控制" }));
  fireEvent.click(screen.getByRole("tab", { name: "价格与套餐" }));
  // 离开服务配置再回来，子页签保持上一次的选择（状态由本页持有）。
  fireEvent.click(screen.getByRole("tab", { name: "技术配置" }));

  expect(screen.getByTestId("settings-panel")).toHaveTextContent(
    "section=runtime",
  );
});

test("forwards readOnly into the feature switches tab", () => {
  render(<SystemSettingsPage initialTab="features" readOnly />);

  expect(screen.getByTestId("feature-switches")).toHaveTextContent("ro=true");
});

test("no longer hosts alerts or team: they are standalone menu entries", () => {
  render(<SystemSettingsPage isSuperAdmin />);

  expect(screen.queryByRole("tab", { name: "通知与告警" })).toBeNull();
  expect(screen.queryByRole("tab", { name: "团队与权限" })).toBeNull();
});

test("价格页配置在前、试算器后置", () => {
  render(<SystemSettingsPage />);

  const pricing = screen.getByTestId("customer-pricing");
  const rates = screen.getByTestId("billing-rates");
  const calculator = screen.getByRole("region", { name: /试算/ });
  expect(
    pricing.compareDocumentPosition(calculator) &
      Node.DOCUMENT_POSITION_FOLLOWING,
  ).toBeTruthy();
  expect(
    rates.compareDocumentPosition(calculator) &
      Node.DOCUMENT_POSITION_FOLLOWING,
  ).toBeTruthy();
});

test("带费率意图进入：滚动到费率区块并高亮", () => {
  const scrollIntoView = vi.fn();
  Element.prototype.scrollIntoView = scrollIntoView;
  render(<SystemSettingsPage initialTab="rates" />);

  const section = document.getElementById("billing-rates-section");
  expect(section).toHaveClass("admin-settings-highlight");
  expect(scrollIntoView).toHaveBeenCalled();
  expect(section).toContainElement(screen.getByTestId("billing-rates"));
});

test("收款设置页签 id 为 payment，旧 id collection（意图与地址）仍兼容", () => {
  const { unmount } = render(<SystemSettingsPage initialTab="collection" />);
  expect(screen.getByTestId("payment-settings")).toBeInTheDocument();
  unmount();

  window.location.hash = "#admin/systemSettings?sub=collection";
  render(<SystemSettingsPage />);
  expect(screen.getByTestId("payment-settings")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("tab", { name: "价格与套餐" }));
  fireEvent.click(screen.getByRole("tab", { name: "收款设置" }));
  expect(window.location.hash).toContain("sub=payment");
});
