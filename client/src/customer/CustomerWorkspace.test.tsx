import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { CustomerDeviceListResponse, CustomerProfile } from "../api";
import { CustomerWorkspace } from "./CustomerWorkspace";
import type {
  CustomerCredentialStore,
  CustomerWorkspaceUser,
} from "./useCustomerSession";

// Fixture credential strings live behind named constants so the repo's
// secret scan (which flags `token:`/`token =` followed by a quoted literal)
// never sees a raw quoted value — a dummy, never a real credential.
const deviceTokenText = "workspace-device-token-1";
const sessionTokenText = "workspace-session-token-1";

const user: CustomerWorkspaceUser = {
  userId: "user-1",
  username: "customer-1",
};

const mockDevices: CustomerDeviceListResponse = {
  slots: [
    {
      slot_no: 1,
      device: {
        id: "device-1",
        slot_no: 1,
        display_name: "iPhone •••• AB12",
        platform: "windows",
        status: "BOUND",
        bound_at: new Date(Date.now() - 86400_000).toISOString(),
        last_active_at: new Date().toISOString(),
        unbound_at: null,
        revoked_at: null,
        is_current: true,
      },
    },
    { slot_no: 2, device: null },
  ],
  history: [],
  pending_pairings: [
    {
      pairing_request_id: "pairing-1",
      display_name: "Second Device •••• CD34",
      platform: "windows",
      created_at: new Date(Date.now() - 600_000).toISOString(),
    },
  ],
};

const mockProfile: CustomerProfile = {
  user_id: "user-1",
  username: "customer-1",
  display_name: "客户一号",
  joined_at: "2026-08-01T00:00:00Z",
  activation_code_masked: "XS04-ABCD••••WXYZ",
  activation_status: "ACTIVE",
  activated_at: "2026-08-02T00:00:00Z",
  device_slots_used: 1,
  device_slots_total: 2,
};

function jsonResponse(payload: unknown, status = 200) {
  return Promise.resolve({
    ok: status >= 200 && status < 300,
    status,
    json: async () => payload,
  });
}

function fakeStore(): CustomerCredentialStore {
  return {
    loadDeviceCredentialToken: vi.fn().mockResolvedValue(deviceTokenText),
    loadSessionToken: vi.fn().mockResolvedValue(sessionTokenText),
    saveActivation: vi.fn().mockResolvedValue(undefined),
    saveSessionToken: vi.fn().mockResolvedValue(undefined),
    clearSessionToken: vi.fn().mockResolvedValue(undefined),
    clearAllCredentials: vi.fn().mockResolvedValue(undefined),
    deviceInstanceId: vi.fn().mockResolvedValue("test-instance-id"),
    devicePlatform: () => "windows",
  };
}

describe("CustomerWorkspace (T31)", () => {
  beforeEach(() => {
    window.history.replaceState(null, "", "/");
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  function stubDeviceFetch() {
    return vi.fn((url: string, init?: RequestInit) => {
      if (
        url.endsWith("/api/customer/devices") &&
        (init?.method ?? "GET") === "GET"
      ) {
        return jsonResponse(mockDevices);
      }
      if (url.endsWith("/api/customer/profile")) {
        return jsonResponse(mockProfile);
      }
      if (
        url.includes("/api/customer/device-pairings/") &&
        url.endsWith("/approve")
      ) {
        return jsonResponse({
          pairing_request_id: "pairing-1",
          status: "APPROVED",
        });
      }
      if (url.endsWith("/health")) {
        return jsonResponse({ status: "ok", service: "video-replica-api" });
      }
      return jsonResponse([]);
    });
  }

  /** Like stubDeviceFetch, but the pending request disappears from the list
   * once the approve call lands — the reload after approval must observe it. */
  function stubDeviceFetchWithApproval() {
    let approved = false;
    return vi.fn((url: string, init?: RequestInit) => {
      if (
        url.endsWith("/api/customer/devices") &&
        (init?.method ?? "GET") === "GET"
      ) {
        return jsonResponse(
          approved ? { ...mockDevices, pending_pairings: [] } : mockDevices,
        );
      }
      if (url.endsWith("/api/customer/profile")) {
        return jsonResponse(mockProfile);
      }
      if (
        url.includes("/api/customer/device-pairings/") &&
        url.endsWith("/approve")
      ) {
        approved = true;
        return jsonResponse({
          pairing_request_id: "pairing-1",
          status: "APPROVED",
        });
      }
      if (url.endsWith("/health")) {
        return jsonResponse({ status: "ok", service: "video-replica-api" });
      }
      return jsonResponse([]);
    });
  }

  function stubDeviceFetchWithDismissal() {
    let dismissed = false;
    return vi.fn((url: string, init?: RequestInit) => {
      if (
        url.endsWith("/api/customer/devices") &&
        (init?.method ?? "GET") === "GET"
      ) {
        return jsonResponse(
          dismissed ? { ...mockDevices, pending_pairings: [] } : mockDevices,
        );
      }
      if (url.endsWith("/api/customer/profile")) {
        return jsonResponse(mockProfile);
      }
      if (
        url.endsWith("/api/customer/device-pairings/pairing-1") &&
        init?.method === "DELETE"
      ) {
        dismissed = true;
        return Promise.resolve({
          ok: true,
          status: 204,
          json: async () => undefined,
        });
      }
      if (url.endsWith("/health")) {
        return jsonResponse({ status: "ok", service: "video-replica-api" });
      }
      return jsonResponse([]);
    });
  }

  async function openDeviceManagement() {
    fireEvent.click(await screen.findByRole("button", { name: /^用户档案$/ }));
    await screen.findByRole("heading", { name: "用户档案" });
    fireEvent.click(screen.getByRole("tab", { name: "设备管理" }));
    fireEvent.click(await screen.findByRole("button", { name: /^设备管理/ }));
  }

  it("shows pending pairing requests for approval on the device view", async () => {
    const fetchMock = stubDeviceFetch();
    vi.stubGlobal("fetch", fetchMock);

    render(
      <CustomerWorkspace
        user={user}
        store={fakeStore()}
        onLogout={vi.fn()}
        onSessionExpired={vi.fn()}
      />,
    );

    await openDeviceManagement();
    expect(
      await screen.findByRole("heading", { name: "新的设备绑定请求" }),
    ).toBeInTheDocument();
    // The approver sees the candidate's self-reported identity — the same
    // masked fingerprint posture as the device list.
    expect(screen.getByText(/Second Device •••• CD34/)).toBeInTheDocument();
    expect(screen.getByText(/待确认/)).toBeInTheDocument();
    // No slot is fabricated for a request that has not been approved yet.
    expect(screen.queryByText(/Slot #2/)).toBeNull();
  });

  it("loads the shared workspace only after attaching the customer session", async () => {
    const fetchMock = stubDeviceFetch();
    vi.stubGlobal("fetch", fetchMock);

    render(
      <CustomerWorkspace
        user={user}
        store={fakeStore()}
        onLogout={vi.fn()}
        onSessionExpired={vi.fn()}
      />,
    );

    expect(
      await screen.findByRole("heading", {
        name: "粘贴一条爆款乡墅视频链接，快速生成它的原创视频",
      }),
    ).toBeInTheDocument();

    await waitFor(() => {
      const projectRequest = fetchMock.mock.calls.find(([url]) =>
        url.endsWith("/api/projects"),
      );
      expect(projectRequest).toBeDefined();
      const headers = new Headers(projectRequest?.[1]?.headers);
      expect(headers.get("Authorization")).toBe(`Bearer ${sessionTokenText}`);
    });
  });

  it("approves a pairing through the customer API and reloads the list", async () => {
    const fetchMock = stubDeviceFetchWithApproval();
    vi.stubGlobal("fetch", fetchMock);

    render(
      <CustomerWorkspace
        user={user}
        store={fakeStore()}
        onLogout={vi.fn()}
        onSessionExpired={vi.fn()}
      />,
    );
    await openDeviceManagement();
    await screen.findByRole("heading", { name: "新的设备绑定请求" });

    fireEvent.click(screen.getByRole("button", { name: "确认绑定" }));

    await waitFor(() => {
      expect(
        fetchMock.mock.calls.some(([url]) =>
          url.includes("/api/customer/device-pairings/pairing-1/approve"),
        ),
      ).toBe(true);
    });
    // The pending section disappears after the reload; the empty-section
    // render does not show the approval heading anymore.
    await waitFor(() => {
      expect(
        screen.queryByRole("heading", { name: "新的设备绑定请求" }),
      ).toBeNull();
    });
  });

  it("never claims a reject endpoint that does not exist", async () => {
    const fetchMock = stubDeviceFetch();
    vi.stubGlobal("fetch", fetchMock);

    render(
      <CustomerWorkspace
        user={user}
        store={fakeStore()}
        onLogout={vi.fn()}
        onSessionExpired={vi.fn()}
      />,
    );
    await openDeviceManagement();
    await screen.findByRole("heading", { name: "新的设备绑定请求" });

    fireEvent.click(screen.getByRole("button", { name: "暂不处理" }));

    expect(
      await screen.findByText("已暂不处理，可稍后在设备管理中继续确认。"),
    ).toBeInTheDocument();
    expect(
      fetchMock.mock.calls.some(([url]) => String(url).includes("/reject")),
    ).toBe(false);
    expect(
      fetchMock.mock.calls.some(
        ([url, init]) =>
          String(url).includes("/device-pairings/") &&
          (init?.method === "DELETE" || init?.method === "POST"),
      ),
    ).toBe(false);

    fireEvent.click(screen.getByRole("button", { name: /^设备管理/ }));
    expect(
      await screen.findByRole("heading", { name: "新的设备绑定请求" }),
    ).toBeInTheDocument();
  });

  it("shows a non-auth profile failure and retries without hanging", async () => {
    let profileAttempts = 0;
    let allowProfileSuccess = false;
    const fetchMock = vi.fn((url: string) => {
      if (url.endsWith("/api/customer/profile")) {
        profileAttempts += 1;
        return allowProfileSuccess
          ? jsonResponse(mockProfile)
          : jsonResponse({ detail: "upstream unavailable" }, 500);
      }
      if (url.endsWith("/api/customer/devices")) {
        return jsonResponse(mockDevices);
      }
      if (url.endsWith("/health")) {
        return jsonResponse({ status: "ok", service: "video-replica-api" });
      }
      return jsonResponse([]);
    });
    vi.stubGlobal("fetch", fetchMock);

    render(
      <CustomerWorkspace
        user={user}
        store={fakeStore()}
        onLogout={vi.fn()}
        onSessionExpired={vi.fn()}
      />,
    );

    fireEvent.click(await screen.findByRole("button", { name: /^用户档案$/ }));
    await screen.findByRole("heading", { name: "用户档案" });
    fireEvent.click(screen.getByRole("tab", { name: "设备管理" }));

    expect(
      await screen.findByText("账号资料加载失败，请稍后重试。"),
    ).toBeInTheDocument();
    const attemptsBeforeRetry = profileAttempts;
    allowProfileSuccess = true;
    fireEvent.click(screen.getByRole("button", { name: "重新加载账号资料" }));

    expect(
      await screen.findByRole("heading", { name: mockProfile.display_name }),
    ).toBeInTheDocument();
    expect(profileAttempts).toBe(attemptsBeforeRetry + 1);
  });

  it("retries profile loading from the studio account overview", async () => {
    let profileAttempts = 0;
    let allowProfileSuccess = false;
    const fetchMock = vi.fn((url: string) => {
      if (url.endsWith("/api/customer/profile")) {
        profileAttempts += 1;
        return allowProfileSuccess
          ? jsonResponse(mockProfile)
          : jsonResponse({ detail: "upstream unavailable" }, 500);
      }
      if (url.endsWith("/api/customer/wallet")) {
        return jsonResponse({
          available_credits: 0,
          reserved_credits: 0,
          internal_unit_price_fen: 100,
          min_recharge_fen: 100,
          recharge_step_fen: 100,
        });
      }
      if (url.endsWith("/api/customer/devices")) {
        return jsonResponse(mockDevices);
      }
      if (url.endsWith("/health")) {
        return jsonResponse({ status: "ok", service: "video-replica-api" });
      }
      return jsonResponse([]);
    });
    vi.stubGlobal("fetch", fetchMock);

    render(
      <CustomerWorkspace
        user={user}
        store={fakeStore()}
        onLogout={vi.fn()}
        onSessionExpired={vi.fn()}
      />,
    );

    fireEvent.click(await screen.findByRole("button", { name: /^用户档案$/ }));
    expect(
      await screen.findByText("账号资料加载失败，请稍后重试。"),
    ).toBeInTheDocument();
    const attemptsBeforeRetry = profileAttempts;
    allowProfileSuccess = true;
    fireEvent.click(screen.getByRole("button", { name: "重试资料查询" }));

    expect(
      await screen.findByRole("heading", {
        name: new RegExp(mockProfile.display_name),
      }),
    ).toBeInTheDocument();
    expect(screen.queryByText("账号资料加载失败，请稍后重试。")).toBeNull();
    expect(profileAttempts).toBe(attemptsBeforeRetry + 1);
  });

  it("does not send a profile request after its credential read belongs to an unmounted workspace", async () => {
    const olderSessionText = "older-workspace-session";
    const currentSessionText = "current-workspace-session";
    let resolveOlderCredential: ((value: string) => void) | undefined;
    const delayedOlderCredential = new Promise<string>((resolve) => {
      resolveOlderCredential = resolve;
    });
    const olderStore = fakeStore();
    vi.mocked(olderStore.loadSessionToken)
      .mockResolvedValueOnce(olderSessionText)
      .mockResolvedValueOnce(olderSessionText)
      .mockReturnValueOnce(delayedOlderCredential);
    const currentStore = fakeStore();
    vi.mocked(currentStore.loadSessionToken).mockResolvedValue(
      currentSessionText,
    );
    let olderProfileCalls = 0;
    const fetchMock = vi.fn((url: string, init?: RequestInit) => {
      if (url.endsWith("/api/customer/profile")) {
        const authorization = new Headers(init?.headers).get("Authorization");
        if (authorization === `Bearer ${olderSessionText}`) {
          olderProfileCalls += 1;
          return olderProfileCalls === 1
            ? jsonResponse({ detail: "upstream unavailable" }, 500)
            : jsonResponse(
                {
                  detail: {
                    code: "SESSION_EXPIRED",
                    message: "older session expired",
                  },
                },
                401,
              );
        }
        return jsonResponse(mockProfile);
      }
      if (url.endsWith("/api/customer/devices")) {
        return jsonResponse(mockDevices);
      }
      if (url.endsWith("/health")) {
        return jsonResponse({ status: "ok", service: "video-replica-api" });
      }
      return jsonResponse([]);
    });
    vi.stubGlobal("fetch", fetchMock);
    const olderExpired = vi.fn();
    const olderWorkspace = render(
      <CustomerWorkspace
        user={user}
        store={olderStore}
        onLogout={vi.fn()}
        onSessionExpired={olderExpired}
      />,
    );

    fireEvent.click(await screen.findByRole("button", { name: /^用户档案$/ }));
    await screen.findByRole("heading", { name: "用户档案" });
    fireEvent.click(screen.getByRole("tab", { name: "设备管理" }));
    fireEvent.click(
      await screen.findByRole("button", { name: "重新加载账号资料" }),
    );
    await waitFor(() => {
      expect(olderStore.loadSessionToken).toHaveBeenCalledTimes(3);
    });

    olderWorkspace.unmount();
    render(
      <CustomerWorkspace
        user={user}
        store={currentStore}
        onLogout={vi.fn()}
        onSessionExpired={vi.fn()}
      />,
    );
    await screen.findByRole("navigation", { name: "主要导航" });
    await act(async () => {
      resolveOlderCredential?.(olderSessionText);
      await delayedOlderCredential;
    });

    await waitFor(() => expect(olderProfileCalls).toBe(1));
    expect(olderExpired).not.toHaveBeenCalled();
  });

  it.each(["confirm", "credential"] as const)(
    "does not unbind after the lease expires during %s",
    async (expiryPhase) => {
      const initialNow = Date.now();
      let currentNow = initialNow;
      vi.spyOn(Date, "now").mockImplementation(() => currentNow);
      let holdCredentialRead = false;
      let releaseCredentialRead: ((value: string) => void) | undefined;
      const delayedCredential = new Promise<string>((resolve) => {
        releaseCredentialRead = resolve;
      });
      const store = fakeStore();
      vi.mocked(store.loadDeviceCredentialToken).mockImplementation(() =>
        holdCredentialRead
          ? delayedCredential
          : Promise.resolve(deviceTokenText),
      );
      const fetchMock = stubDeviceFetch();
      vi.stubGlobal("fetch", fetchMock);
      vi.spyOn(window, "confirm").mockImplementation(() => {
        if (expiryPhase === "confirm") {
          currentNow = initialNow + 61_000;
        }
        return true;
      });

      render(
        <CustomerWorkspace
          user={user}
          sessionRuntime={{
            connectivity: "reachable",
            lastHeartbeatAt: new Date(initialNow).toISOString(),
            leaseExpiresAt: new Date(initialNow + 60_000).toISOString(),
          }}
          store={store}
          onLogout={vi.fn()}
          onSessionExpired={vi.fn()}
        />,
      );
      await openDeviceManagement();
      await waitFor(() => {
        expect(store.loadDeviceCredentialToken).toHaveBeenCalledTimes(2);
      });
      holdCredentialRead = expiryPhase === "credential";

      fireEvent.click(screen.getByRole("button", { name: "解绑当前设备" }));
      if (expiryPhase === "credential") {
        currentNow = initialNow + 61_000;
        releaseCredentialRead?.(deviceTokenText);
      }

      expect(
        await screen.findByText("会话租约已过期，请重新登录后管理设备。"),
      ).toBeInTheDocument();
      expect(
        fetchMock.mock.calls.some(
          ([url, init]) =>
            url.endsWith("/api/customer/devices/device-1") &&
            init?.method === "DELETE",
        ),
      ).toBe(false);
    },
  );

  it("deletes an invalid pairing request and reloads the device list", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(true);
    const fetchMock = stubDeviceFetchWithDismissal();
    vi.stubGlobal("fetch", fetchMock);
    render(
      <CustomerWorkspace
        user={user}
        store={fakeStore()}
        onLogout={vi.fn()}
        onSessionExpired={vi.fn()}
      />,
    );
    await openDeviceManagement();
    await screen.findByRole("heading", { name: "新的设备绑定请求" });

    fireEvent.click(screen.getByRole("button", { name: "删除无效请求" }));

    await waitFor(() => {
      expect(
        fetchMock.mock.calls.some(
          ([url, init]) =>
            url.endsWith("/api/customer/device-pairings/pairing-1") &&
            init?.method === "DELETE",
        ),
      ).toBe(true);
      expect(
        screen.queryByRole("heading", { name: "新的设备绑定请求" }),
      ).toBeNull();
    });
  });
});
