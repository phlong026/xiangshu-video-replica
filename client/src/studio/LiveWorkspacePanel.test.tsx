import { fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import type { GenerationBatch } from "../api";
import * as api from "../api";
import type { CustomerCredentialStore } from "../customer/useCustomerSession";
import { reviewUser } from "./fixtures";

vi.mock("../TaskRecordsPanel", () => ({
  TaskRecordsPanel: ({
    onHandoffConsumed,
  }: {
    onHandoffConsumed: () => void;
  }) => (
    <button type="button" onClick={onHandoffConsumed}>
      消费交接
    </button>
  ),
}));

vi.mock("../CharacterLibrary", () => ({
  CharacterLibrary: ({
    initialIdentityId,
    initialTab,
  }: {
    initialIdentityId?: string;
    initialTab?: string;
  }) => <p>{`${initialIdentityId ?? "none"}:${initialTab ?? "base"}`}</p>,
}));

import { LiveWorkspacePanel } from "./LiveWorkspacePanel";

afterEach(() => vi.restoreAllMocks());

it("任务面板消费交接后回传给工作区控制器清除暂存批次", () => {
  const onHandoffConsumed = vi.fn();
  render(
    <LiveWorkspacePanel
      currentUser={reviewUser}
      handoffBatch={{ id: "batch-1" } as GenerationBatch}
      onBatchCreated={vi.fn()}
      onBusyChange={vi.fn()}
      onClose={vi.fn()}
      onHandoffConsumed={onHandoffConsumed}
      onProjectSelected={vi.fn()}
      onRefresh={vi.fn()}
      panel="tasks"
    />,
  );

  fireEvent.click(screen.getByRole("button", { name: "消费交接" }));
  expect(onHandoffConsumed).toHaveBeenCalledTimes(1);
});

<<<<<<< HEAD
it("人物面板会带入 Studio 已选人物和场景造型页签", () => {
  render(
    <LiveWorkspacePanel
      characterIdentityId="identity-1"
      characterInitialTab="scenes"
      currentUser={reviewUser}
=======
it("客户账号进入使用记录时读取客户钱包并能打开预设充值", async () => {
  const sessionTokenText = "workspace-wallet-fixture-session";
  const store: CustomerCredentialStore = {
    loadDeviceCredentialToken: vi.fn().mockResolvedValue(null),
    loadSessionToken: vi.fn().mockResolvedValue(sessionTokenText),
    saveActivation: vi.fn(),
    saveSessionToken: vi.fn(),
    clearSessionToken: vi.fn(),
    clearAllCredentials: vi.fn(),
    deviceInstanceId: vi.fn().mockResolvedValue("workspace-wallet-fixture"),
    devicePlatform: () => "windows",
  };
  const customerWallet = vi.spyOn(api, "customerGetWallet").mockResolvedValue({
    available_credits: 12,
    reserved_credits: 2,
    internal_unit_price_fen: 1000,
    min_recharge_fen: 10000,
    recharge_step_fen: 1000,
  });
  const internalWallet = vi
    .spyOn(api, "getWallet")
    .mockRejectedValue(new Error("客户不应请求内部钱包"));
  const emptyPage = { items: [], total: 0, limit: 20, offset: 0 };
  vi.spyOn(api, "customerListWalletTransactions").mockResolvedValue(emptyPage);
  vi.spyOn(api, "customerListRechargeOrders").mockResolvedValue(emptyPage);
  render(
    <LiveWorkspacePanel
      currentUser={reviewUser}
      customerAccount={{
        devices: null,
        deviceError: "",
        profile: null,
        store,
        onApprovePairing: vi.fn(),
        onDismissPairing: vi.fn(),
        onProfileUpdated: vi.fn(),
        onRefreshDevices: vi.fn(),
        onResetActivationCode: vi.fn(),
        onSessionExpired: vi.fn(),
        onUnbind: vi.fn(),
        onUpdateProfile: vi.fn(),
      }}
>>>>>>> origin/codex/frontend-v1.4
      onBatchCreated={vi.fn()}
      onBusyChange={vi.fn()}
      onClose={vi.fn()}
      onHandoffConsumed={vi.fn()}
      onProjectSelected={vi.fn()}
      onRefresh={vi.fn()}
<<<<<<< HEAD
      panel="characters"
    />,
  );

  expect(screen.getByText("identity-1:scenes")).toBeInTheDocument();
=======
      panel="wallet"
    />,
  );

  expect(await screen.findByText("10元 / 条")).toBeInTheDocument();
  expect(screen.getByText("冻结 2 条")).toBeInTheDocument();
  expect(customerWallet).toHaveBeenCalledWith({
    kind: "session",
    token: sessionTokenText,
  });
  expect(internalWallet).not.toHaveBeenCalled();

  fireEvent.click(screen.getByRole("button", { name: "充值100元" }));
  const dialog = await screen.findByRole("dialog");
  expect(within(dialog).getByRole("spinbutton")).toHaveValue(100);
>>>>>>> origin/codex/frontend-v1.4
});
