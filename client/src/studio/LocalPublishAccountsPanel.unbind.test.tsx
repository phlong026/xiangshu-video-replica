import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { LocalPublishAccountsPanel } from "./LocalPublishAccountsPanel";
import * as accountsApi from "./localPublishAccounts";

vi.mock("./context", () => ({
  useStudio: () => ({ user: { id: "owner", role: "employee" }, review: false }),
}));
vi.mock("./localPublishAccounts", async (original) => ({
  ...(await original<typeof import("./localPublishAccounts")>()),
  canUseLocalPublishAccounts: () => true,
  listLocalPublishAccounts: vi.fn(),
  listCloudPublishAccounts: vi.fn(),
  disconnectPublishAccount: vi.fn(),
  exportLocalPublishAccountState: vi.fn(),
  importCloudPublishAccount: vi.fn(),
}));
const account = {
  id: "local",
  platform: "douyin" as const,
  platform_user_id: "uid",
  username: "发布账号",
  verified_at: 0,
};
beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(accountsApi.listLocalPublishAccounts).mockResolvedValue([account]);
  vi.mocked(accountsApi.listCloudPublishAccounts).mockResolvedValue([]);
});
describe("解绑分项失败界面", () => {
  it("云端失败保留确认目标及重试入口，不通知两端成功", async () => {
    const notify = vi.fn();
    vi.mocked(accountsApi.disconnectPublishAccount).mockRejectedValueOnce(
      new Error("云端解绑尚未确认，本机登录状态已保留。"),
    );
    render(<LocalPublishAccountsPanel notify={notify} />);
    fireEvent.click(await screen.findByRole("button", { name: "解绑" }));
    fireEvent.click(screen.getByRole("button", { name: "确认解绑" }));
    await screen.findByText("云端解绑尚未确认，本机登录状态已保留。");
    expect(notify).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "确认解绑" })).toBeEnabled();
    vi.mocked(accountsApi.disconnectPublishAccount).mockResolvedValueOnce(
      undefined,
    );
    fireEvent.click(screen.getByRole("button", { name: "确认解绑" }));
    await waitFor(() =>
      expect(notify).toHaveBeenCalledWith(
        expect.stringContaining("在途发布仍需核对"),
      ),
    );
    expect(vi.mocked(accountsApi.disconnectPublishAccount).mock.calls).toEqual([
      ["owner", account],
      ["owner", account],
    ]);
  });
  it("云端同步尚未完成时阻止同时解绑", async () => {
    let complete!: (account: accountsApi.CloudPublishAccount) => void;
    vi.mocked(accountsApi.exportLocalPublishAccountState).mockResolvedValue({
      identity: account,
      storage_state: { cookies: [{}], origins: [] },
    });
    vi.mocked(accountsApi.importCloudPublishAccount).mockReturnValue(
      new Promise((resolve) => {
        complete = resolve;
      }),
    );
    render(<LocalPublishAccountsPanel notify={vi.fn()} />);
    fireEvent.click(
      await screen.findByRole("button", { name: "同步到服务端" }),
    );
    await waitFor(() =>
      expect(accountsApi.importCloudPublishAccount).toHaveBeenCalledOnce(),
    );
    expect(screen.getByRole("button", { name: "解绑" })).toBeDisabled();
    complete({
      ...account,
      id: "cloud",
      status: "connected",
      source: "desktop",
      error_message: null,
    });
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "解绑" })).toBeEnabled(),
    );
  });
});
