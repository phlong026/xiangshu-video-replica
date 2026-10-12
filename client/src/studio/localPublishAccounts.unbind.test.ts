import { invoke, isTauri } from "@tauri-apps/api/core";
import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  captureWorkspaceCredentialContext,
  publishBrowserRequest,
} from "../api";
import {
  disconnectPublishAccount,
  type LocalPublishAccount,
} from "./localPublishAccounts";

vi.mock("../api", () => ({
  publishBrowserRequest: vi.fn(),
  captureWorkspaceCredentialContext: vi.fn(),
}));
vi.mock("@tauri-apps/api/core", () => ({ invoke: vi.fn(), isTauri: vi.fn() }));
const account: LocalPublishAccount = {
  id: "local-1",
  platform: "douyin",
  platform_user_id: "uid",
  username: "name",
  verified_at: 0,
};
const cloud = {
  ...account,
  id: "cloud-1",
  status: "connected",
  source: "desktop",
};
beforeEach(() => {
  vi.resetAllMocks();
  vi.mocked(captureWorkspaceCredentialContext).mockReturnValue(() => {});
  vi.mocked(isTauri).mockReturnValue(true);
  vi.spyOn(navigator, "userAgent", "get").mockReturnValue("Windows");
});
describe("云端优先解绑", () => {
  it("云列表失败时不删除本机，保留目标可重试", async () => {
    vi.mocked(publishBrowserRequest).mockRejectedValue(new Error("列表 503"));
    await expect(disconnectPublishAccount("owner", account)).rejects.toThrow(
      "云端",
    );
    expect(invoke).not.toHaveBeenCalled();
  });
  it("云删除失败且仍存在时，不删除本机", async () => {
    vi.mocked(publishBrowserRequest).mockImplementation(async (_path, init) => {
      if (init?.method === "DELETE") throw new Error("删除 503");
      return Response.json([cloud]);
    });
    await expect(disconnectPublishAccount("owner", account)).rejects.toThrow(
      "云端",
    );
    expect(invoke).not.toHaveBeenCalled();
  });
  it("删除响应丢失但重读确认云端已无时才删除本机", async () => {
    const calls: string[] = [];
    let present = true;
    vi.mocked(publishBrowserRequest).mockImplementation(async (_path, init) => {
      if (init?.method === "DELETE") {
        calls.push("delete-cloud");
        present = false;
        throw new Error("响应丢失");
      }
      calls.push(present ? "list-present" : "list-absent");
      return Response.json(present ? [cloud] : []);
    });
    vi.mocked(invoke).mockImplementation(async () => {
      calls.push("delete-local");
    });
    await disconnectPublishAccount("owner", account);
    expect(calls).toEqual([
      "list-present",
      "delete-cloud",
      "list-absent",
      "list-absent",
      "delete-local",
    ]);
  });
  it("云端已无时仍可清理本机", async () => {
    vi.mocked(publishBrowserRequest).mockResolvedValue(Response.json([]));
    await disconnectPublishAccount("owner", account);
    expect(invoke).toHaveBeenCalledWith("remove_local_publish_account", {
      owner: "owner",
      accountId: "local-1",
    });
  });
  it("本机失败准确报告分项结果，并可重试", async () => {
    vi.mocked(publishBrowserRequest).mockImplementation(async () =>
      Response.json([]),
    );
    vi.mocked(invoke).mockRejectedValueOnce(new Error("本机文件占用"));
    await expect(disconnectPublishAccount("owner", account)).rejects.toThrow(
      "云端已解绑，本机清理未完成",
    );
    vi.mocked(invoke).mockResolvedValue(undefined);
    await disconnectPublishAccount("owner", account);
  });
  it("云列表等待中切换用户时，不删除新用户云账号或旧用户本机账号", async () => {
    let sameSession = true;
    vi.mocked(captureWorkspaceCredentialContext).mockReturnValue(() => {
      if (!sameSession) throw new Error("登录状态已变化");
    });
    vi.mocked(publishBrowserRequest).mockImplementation(async () => {
      sameSession = false;
      return Response.json([cloud]);
    });
    await expect(disconnectPublishAccount("owner", account)).rejects.toThrow(
      "登录状态已变化",
    );
    expect(publishBrowserRequest).toHaveBeenCalledTimes(1);
    expect(invoke).not.toHaveBeenCalled();
  });

  it("两个窗口重复解绑以两端已无收口", async () => {
    let cloudPresent = true;
    let localPresent = true;
    vi.mocked(publishBrowserRequest).mockImplementation(async (_path, init) => {
      if (init?.method === "DELETE") cloudPresent = false;
      return Response.json(cloudPresent ? [cloud] : []);
    });
    vi.mocked(invoke).mockImplementation(async (command) => {
      if (command === "list_local_publish_accounts")
        return localPresent ? [account] : [];
      if (!localPresent) throw new Error("账号已无");
      localPresent = false;
    });
    await Promise.all([
      disconnectPublishAccount("owner", account),
      disconnectPublishAccount("owner", account),
    ]);
    expect(cloudPresent).toBe(false);
    expect(localPresent).toBe(false);
  });
});
