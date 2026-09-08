import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  CUSTOMER_SESSION_EXPIRED_EVENT,
  CUSTOMER_SESSION_REPLACED_EVENT,
  CUSTOMER_SESSION_REVOKED_EVENT,
} from "../api";
import {
  type CustomerCredentialStore,
  useCustomerSession,
} from "./useCustomerSession";

/** In-memory credential store — the reference implementation of the store
 * contract (the desktop build swaps in the Tauri DPAPI adapter, tests and
 * the browser lane use an isolated non-persistent store; dev doc §14: the
 * desktop and browser credential adapters must stay separate). */
function memoryStore(initial?: {
  deviceToken?: string | null;
  automaticRecovery?: boolean;
}) {
  let deviceToken: string | null = initial?.deviceToken ?? null;
  let sessionToken: string | null = null;
  const calls: string[] = [];
  const store: CustomerCredentialStore & {
    calls: string[];
    snapshot: () => { deviceToken: string | null; sessionToken: string | null };
  } = {
    calls,
    snapshot: () => ({ deviceToken, sessionToken }),
    async loadDeviceCredentialToken() {
      calls.push("load-device");
      return deviceToken;
    },
    async loadSessionToken() {
      calls.push("load-session");
      return sessionToken;
    },
    async saveActivation(nextDeviceToken, nextSessionToken) {
      calls.push("save-activation");
      deviceToken = nextDeviceToken;
      sessionToken = nextSessionToken;
    },
    async saveSessionToken(nextSessionToken) {
      calls.push("save-session");
      sessionToken = nextSessionToken;
    },
    async clearSessionToken() {
      calls.push("clear-session");
      sessionToken = null;
    },
    async clearAllCredentials() {
      calls.push("clear-all");
      deviceToken = null;
      sessionToken = null;
    },
    async deviceInstanceId() {
      return "instance-1";
    },
    devicePlatform() {
      return "windows";
    },
    automaticRecovery: initial?.automaticRecovery ?? false,
  };
  return store;
}

function jsonResponse(payload: unknown, status = 200, headers?: Headers) {
  return Promise.resolve({
    ok: status >= 200 && status < 300,
    status,
    headers: headers ?? new Headers(),
    json: async () => payload,
  });
}

// The fake token fixtures live in named constants so the repo's secret
// scan (which flags `token:` followed by a quoted literal) stays quiet —
// same posture as customerApi.test.ts.
const deviceTokenText = "device-token-1";
const sessionTokenText = "session-token-1";
const renewedSessionTokenText = "session-token-2";

const activationBody = {
  username: "user-1",
  user_id: "user-1",
  device_id: "device-1",
  device_token: deviceTokenText,
  session_token: sessionTokenText,
  session_epoch: 1,
  session_lease_expires_at: "2026-08-24T12:01:00Z",
  request_id: "req-1",
};

const loginBody = {
  user_id: "user-1",
  device_id: "device-1",
  session_id: "session-1",
  session_token: renewedSessionTokenText,
  session_epoch: 2,
  session_lease_expires_at: "2026-08-24T12:02:00Z",
  request_id: "req-2",
};

const heartbeatBody = {
  session_id: "session-1",
  session_epoch: 2,
  lease_expires_at: "2026-08-24T12:03:00Z",
  request_id: "req-3",
};

function stubFetch(handler: (url: string, init?: RequestInit) => unknown) {
  const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url =
      typeof input === "string" ? input : new URL(input.toString()).pathname;
    return handler(url, init);
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

const HEARTBEAT_INTERVAL_MS = 30_000;

describe("useCustomerSession", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.useRealTimers();
    window.localStorage.clear();
    window.sessionStorage.clear();
  });

  it("boots without a stored credential onto the activation screen", async () => {
    const store = memoryStore();
    const fetchMock = stubFetch(() => jsonResponse({}));

    const { result } = renderHook(() =>
      useCustomerSession(store, { heartbeatIntervalMs: HEARTBEAT_INTERVAL_MS }),
    );

    await waitFor(() => expect(result.current.screen).toBe("activation"));
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("recovers a durable desktop fingerprint automatically when local credentials are missing", async () => {
    const store = memoryStore({ automaticRecovery: true });
    const fetchMock = stubFetch((url) => {
      if (url.endsWith("/api/customer/activate")) {
        return jsonResponse(activationBody, 201);
      }
      return jsonResponse({}, 500);
    });

    const { result } = renderHook(() =>
      useCustomerSession(store, { heartbeatIntervalMs: HEARTBEAT_INTERVAL_MS }),
    );

    await waitFor(() => expect(result.current.screen).toBe("workspace"));
    expect(store.snapshot()).toEqual({
      deviceToken: deviceTokenText,
      sessionToken: sessionTokenText,
    });
    const request = fetchMock.mock.calls[0];
    const body = JSON.parse(String(request[1]?.body));
    expect(body.activation_code).toBe("");
    expect(body.device_fingerprint).toBe("instance-1");
  });

  it("shows first activation without an error when automatic recovery finds no binding", async () => {
    const store = memoryStore({ automaticRecovery: true });
    const fetchMock = stubFetch((url) => {
      if (url.endsWith("/api/customer/activate")) {
        return jsonResponse(
          {
            detail: {
              code: "ACTIVATION_UNAVAILABLE",
              message: "The activation code cannot be used.",
            },
          },
          400,
        );
      }
      return jsonResponse({}, 500);
    });

    const { result } = renderHook(() =>
      useCustomerSession(store, { heartbeatIntervalMs: HEARTBEAT_INTERVAL_MS }),
    );

    await waitFor(() => expect(result.current.screen).toBe("activation"));
    expect(result.current.error).toBeNull();
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("activates with the device instance fingerprint and persists both credentials", async () => {
    const store = memoryStore();
    const fetchMock = stubFetch((url) => {
      if (url.endsWith("/api/customer/activate")) {
        return jsonResponse(activationBody, 201);
      }
      return jsonResponse({}, 500);
    });

    const { result } = renderHook(() =>
      useCustomerSession(store, { heartbeatIntervalMs: HEARTBEAT_INTERVAL_MS }),
    );
    await waitFor(() => expect(result.current.screen).toBe("activation"));

    await act(async () => {
      await result.current.activate({
        activationCode: "XS04-AAAAAAA-BBBBBBB-CCCCCCC-DDDDDDD",
        deviceName: "工作电脑",
      });
    });

    expect(result.current.screen).toBe("workspace");
    expect(store.snapshot()).toEqual({
      deviceToken: "device-token-1",
      sessionToken: "session-token-1",
    });
    const request = fetchMock.mock.calls[0];
    const body = JSON.parse(String(request[1]?.body));
    expect(body.device_fingerprint).toBe("instance-1");
    expect(body.device_name).toBe("工作电脑");
    expect(body.device_platform).toBe("windows");
  });

  it("keeps the activation screen and surfaces the anti-enumeration rejection", async () => {
    const store = memoryStore();
    stubFetch((url) => {
      if (url.endsWith("/api/customer/activate")) {
        // §13.2: one unified message — no existence/expiry/revocation detail.
        return jsonResponse(
          {
            detail: { code: "ACTIVATION_UNAVAILABLE", message: "激活码不可用" },
          },
          400,
        );
      }
      return jsonResponse({}, 500);
    });

    const { result } = renderHook(() =>
      useCustomerSession(store, { heartbeatIntervalMs: HEARTBEAT_INTERVAL_MS }),
    );
    await waitFor(() => expect(result.current.screen).toBe("activation"));

    await act(async () => {
      await result.current.activate({
        activationCode: "XS04-BADCODE",
        deviceName: "工作电脑",
      });
    });

    expect(result.current.screen).toBe("activation");
    expect(result.current.error?.kind).toBe("bad-request");
    expect(result.current.error?.message).toBe(
      "该激活码当前无法使用，请确认激活码仍在有效期内。",
    );
  });

  it("restores a restart by auto-logging-in with the stored device credential", async () => {
    // FE-02 exit gate: after a real app restart the stored credential logs
    // the user back in without retyping anything.
    const store = memoryStore({ deviceToken: "device-token-1" });
    const fetchMock = stubFetch((url) => {
      if (url.endsWith("/api/customer/sessions/login")) {
        return jsonResponse(loginBody, 201);
      }
      return jsonResponse({}, 500);
    });

    const { result } = renderHook(() =>
      useCustomerSession(store, { heartbeatIntervalMs: HEARTBEAT_INTERVAL_MS }),
    );

    await waitFor(() => expect(result.current.screen).toBe("workspace"));
    expect(store.snapshot().sessionToken).toBe("session-token-2");
    const loginCall = fetchMock.mock.calls.find(([url]) =>
      String(url).endsWith("/api/customer/sessions/login"),
    );
    expect(loginCall).toBeDefined();
  });

  it("surfaces the other-device-online conflict on the conflict screen without switching", async () => {
    const store = memoryStore({ deviceToken: "device-token-1" });
    const fetchMock = stubFetch((url) => {
      if (url.endsWith("/api/customer/sessions/login")) {
        return jsonResponse(
          {
            detail: {
              code: "OTHER_DEVICE_ONLINE",
              message: "另一台设备在线",
              online_device_name_masked: "张**的 iPad",
              online_slot_no: 2,
              lease_expires_at: "2026-08-24T12:05:00Z",
            },
          },
          409,
        );
      }
      return jsonResponse({}, 500);
    });

    const { result } = renderHook(() =>
      useCustomerSession(store, { heartbeatIntervalMs: HEARTBEAT_INTERVAL_MS }),
    );

    await waitFor(() => expect(result.current.screen).toBe("binding-conflict"));
    expect(result.current.conflict).toEqual({
      deviceNameMasked: "张**的 iPad",
      slotNo: 2,
      leaseExpiresAt: "2026-08-24T12:05:00Z",
    });
    // T29/T30: no silent switch — the takeover only runs after the user
    // confirms it in the conflict dialog.
    expect(
      fetchMock.mock.calls.some(([url]) =>
        String(url).endsWith("/api/customer/sessions/switch"),
      ),
    ).toBe(false);
  });

  it("switches to the confirmed device after the user confirms the takeover", async () => {
    const store = memoryStore({ deviceToken: "device-token-1" });
    const fetchMock = stubFetch((url) => {
      if (url.endsWith("/api/customer/sessions/login")) {
        return jsonResponse(
          {
            detail: {
              code: "OTHER_DEVICE_ONLINE",
              message: "另一台设备在线",
              online_device_name_masked: "张**的 iPad",
              online_slot_no: 2,
              lease_expires_at: "2026-08-24T12:05:00Z",
            },
          },
          409,
        );
      }
      if (url.endsWith("/api/customer/sessions/switch")) {
        return jsonResponse(loginBody, 201);
      }
      return jsonResponse({}, 500);
    });

    const { result } = renderHook(() =>
      useCustomerSession(store, { heartbeatIntervalMs: HEARTBEAT_INTERVAL_MS }),
    );
    await waitFor(() => expect(result.current.screen).toBe("binding-conflict"));

    await act(async () => {
      await result.current.switchSession();
    });

    expect(result.current.screen).toBe("workspace");
    expect(result.current.conflict).toBeNull();
    expect(store.snapshot().sessionToken).toBe(renewedSessionTokenText);
    expect(
      fetchMock.mock.calls.some(([url]) =>
        String(url).endsWith("/api/customer/sessions/switch"),
      ),
    ).toBe(true);
  });

  it("returns to the login screen when the takeover is declined", async () => {
    const store = memoryStore({ deviceToken: "device-token-1" });
    stubFetch((url) => {
      if (url.endsWith("/api/customer/sessions/login")) {
        return jsonResponse(
          {
            detail: {
              code: "OTHER_DEVICE_ONLINE",
              message: "另一台设备在线",
              online_device_name_masked: "张**的 iPad",
              online_slot_no: 2,
              lease_expires_at: "2026-08-24T12:05:00Z",
            },
          },
          409,
        );
      }
      return jsonResponse({}, 500);
    });

    const { result } = renderHook(() =>
      useCustomerSession(store, { heartbeatIntervalMs: HEARTBEAT_INTERVAL_MS }),
    );
    await waitFor(() => expect(result.current.screen).toBe("binding-conflict"));

    await act(async () => {
      result.current.cancelSessionSwitch();
    });

    expect(result.current.screen).toBe("login");
    expect(result.current.conflict).toBeNull();
  });

  it("keeps the conflict screen when the switch request fails", async () => {
    const store = memoryStore({ deviceToken: "device-token-1" });
    stubFetch((url) => {
      if (url.endsWith("/api/customer/sessions/login")) {
        return jsonResponse(
          {
            detail: {
              code: "OTHER_DEVICE_ONLINE",
              message: "另一台设备在线",
              online_device_name_masked: "张**的 iPad",
              online_slot_no: 2,
              lease_expires_at: "2026-08-24T12:05:00Z",
            },
          },
          409,
        );
      }
      if (url.endsWith("/api/customer/sessions/switch")) {
        return jsonResponse(
          { detail: { code: "INTERNAL", message: "boom" } },
          500,
        );
      }
      return jsonResponse({}, 500);
    });

    const { result } = renderHook(() =>
      useCustomerSession(store, { heartbeatIntervalMs: HEARTBEAT_INTERVAL_MS }),
    );
    await waitFor(() => expect(result.current.screen).toBe("binding-conflict"));

    await act(async () => {
      await result.current.switchSession();
    });

    expect(result.current.screen).toBe("binding-conflict");
    expect(result.current.conflict).not.toBeNull();
    expect(result.current.error).not.toBeNull();
  });

  it("reports the request id on an idempotency conflict", async () => {
    const store = memoryStore();
    stubFetch((url) => {
      if (url.endsWith("/api/customer/activate")) {
        return jsonResponse(
          {
            detail: {
              code: "IDEMPOTENCY_CONFLICT",
              message: "幂等键冲突",
            },
          },
          409,
        );
      }
      return jsonResponse({}, 500);
    });

    const { result } = renderHook(() =>
      useCustomerSession(store, { heartbeatIntervalMs: HEARTBEAT_INTERVAL_MS }),
    );
    await waitFor(() => expect(result.current.screen).toBe("activation"));

    await act(async () => {
      await result.current.activate({
        activationCode: "XS04-AAAAAAA-BBBBBBB-CCCCCCC-DDDDDDD",
        deviceName: "工作电脑",
      });
    });

    expect(result.current.screen).toBe("activation");
    expect(result.current.error?.kind).toBe("idempotency-conflict");
    expect(result.current.error?.requestId).toBeTruthy();
  });

  it("keeps the rate limit retry hint instead of looping submits", async () => {
    const store = memoryStore();
    stubFetch((url) => {
      if (url.endsWith("/api/customer/activate")) {
        return jsonResponse(
          { detail: { code: "RATE_LIMITED", message: "请求过于频繁" } },
          429,
          new Headers({ "Retry-After": "17" }),
        );
      }
      return jsonResponse({}, 500);
    });

    const { result } = renderHook(() =>
      useCustomerSession(store, { heartbeatIntervalMs: HEARTBEAT_INTERVAL_MS }),
    );
    await waitFor(() => expect(result.current.screen).toBe("activation"));

    await act(async () => {
      await result.current.activate({
        activationCode: "XS04-AAAAAAA-BBBBBBB-CCCCCCC-DDDDDDD",
        deviceName: "工作电脑",
      });
    });

    expect(result.current.error?.kind).toBe("rate-limited");
    expect(result.current.error?.retryAfterSeconds).toBe(17);
  });

  it("expires a live session but keeps the device credential for the next login", async () => {
    const store = memoryStore({ deviceToken: "device-token-1" });
    stubFetch((url) => {
      if (url.endsWith("/api/customer/sessions/login")) {
        return jsonResponse(loginBody, 201);
      }
      return jsonResponse({}, 500);
    });

    const { result } = renderHook(() =>
      useCustomerSession(store, { heartbeatIntervalMs: HEARTBEAT_INTERVAL_MS }),
    );
    await waitFor(() => expect(result.current.screen).toBe("workspace"));

    act(() => {
      window.dispatchEvent(new Event(CUSTOMER_SESSION_EXPIRED_EVENT));
    });

    await waitFor(() => expect(result.current.screen).toBe("session-expired"));
    expect(store.snapshot()).toEqual({
      deviceToken: "device-token-1",
      sessionToken: null,
    });
  });

  it("routes a displaced session to the dedicated replaced screen", async () => {
    const store = memoryStore({ deviceToken: "device-token-1" });
    stubFetch((url) => {
      if (url.endsWith("/api/customer/sessions/login")) {
        return jsonResponse(loginBody, 201);
      }
      return jsonResponse({}, 500);
    });

    const { result } = renderHook(() =>
      useCustomerSession(store, { heartbeatIntervalMs: HEARTBEAT_INTERVAL_MS }),
    );
    await waitFor(() => expect(result.current.screen).toBe("workspace"));

    act(() => {
      window.dispatchEvent(new Event(CUSTOMER_SESSION_REPLACED_EVENT));
    });

    await waitFor(() => expect(result.current.screen).toBe("session-replaced"));
    expect(store.snapshot().sessionToken).toBeNull();
  });

  it("clears every stored credential when the device is revoked", async () => {
    const store = memoryStore({ deviceToken: "device-token-1" });
    stubFetch((url) => {
      if (url.endsWith("/api/customer/sessions/login")) {
        return jsonResponse(loginBody, 201);
      }
      return jsonResponse({}, 500);
    });

    const { result } = renderHook(() =>
      useCustomerSession(store, { heartbeatIntervalMs: HEARTBEAT_INTERVAL_MS }),
    );
    await waitFor(() => expect(result.current.screen).toBe("workspace"));

    act(() => {
      window.dispatchEvent(new Event(CUSTOMER_SESSION_REVOKED_EVENT));
    });

    await waitFor(() => expect(result.current.screen).toBe("device-revoked"));
    expect(store.snapshot()).toEqual({
      deviceToken: null,
      sessionToken: null,
    });
  });

  it("logs out to the login screen keeping the device credential", async () => {
    const store = memoryStore({ deviceToken: "device-token-1" });
    stubFetch((url) => {
      if (url.endsWith("/api/customer/sessions/login")) {
        return jsonResponse(loginBody, 201);
      }
      if (url.endsWith("/api/customer/sessions/logout")) {
        return jsonResponse(undefined, 204);
      }
      return jsonResponse({}, 500);
    });

    const { result } = renderHook(() =>
      useCustomerSession(store, { heartbeatIntervalMs: HEARTBEAT_INTERVAL_MS }),
    );
    await waitFor(() => expect(result.current.screen).toBe("workspace"));

    await act(async () => {
      await result.current.logout();
    });

    expect(result.current.screen).toBe("login");
    expect(store.snapshot()).toEqual({
      deviceToken: "device-token-1",
      sessionToken: null,
    });
  });

  it("sends only one backend logout while concurrent clicks are pending", async () => {
    const store = memoryStore({ deviceToken: "device-token-1" });
    let resolveLogout:
      | ((value: Awaited<ReturnType<typeof jsonResponse>>) => void)
      | undefined;
    const logoutResponse = new Promise<
      Awaited<ReturnType<typeof jsonResponse>>
    >((resolve) => {
      resolveLogout = resolve;
    });
    const fetchMock = stubFetch((url) => {
      if (url.endsWith("/api/customer/sessions/login")) {
        return jsonResponse(loginBody, 201);
      }
      if (url.endsWith("/api/customer/sessions/logout")) {
        return logoutResponse;
      }
      return jsonResponse({}, 500);
    });

    const { result } = renderHook(() =>
      useCustomerSession(store, { heartbeatIntervalMs: HEARTBEAT_INTERVAL_MS }),
    );
    await waitFor(() => expect(result.current.screen).toBe("workspace"));

    let firstLogout: Promise<void> | undefined;
    let secondLogout: Promise<void> | undefined;
    act(() => {
      firstLogout = result.current.logout();
      secondLogout = result.current.logout();
    });

    expect(
      fetchMock.mock.calls.filter(([url]) =>
        String(url).endsWith("/api/customer/sessions/logout"),
      ),
    ).toHaveLength(1);

    resolveLogout?.(await jsonResponse(undefined, 204));
    await act(async () => {
      await Promise.all([firstLogout, secondLogout]);
    });
    expect(result.current.screen).toBe("login");
  });

  it("exposes the activated user identity for the workspace shell", async () => {
    const store = memoryStore();
    stubFetch((url) => {
      if (url.endsWith("/api/customer/activate")) {
        return jsonResponse(activationBody, 201);
      }
      return jsonResponse({}, 500);
    });

    const { result } = renderHook(() =>
      useCustomerSession(store, { heartbeatIntervalMs: HEARTBEAT_INTERVAL_MS }),
    );
    await waitFor(() => expect(result.current.screen).toBe("activation"));
    expect(result.current.user).toBeNull();

    await act(async () => {
      await result.current.activate({
        activationCode: "XS04-AAAAAAA-BBBBBBB-CCCCCCC-DDDDDDD",
        deviceName: "工作电脑",
      });
    });

    expect(result.current.user).toEqual({
      userId: "user-1",
      username: "user-1",
    });

    await act(async () => {
      await result.current.logout();
    });
    expect(result.current.user).toBeNull();
  });

  it("exposes the user id (without a username) after a restart restore login", async () => {
    // The login response carries no username; the restored workspace shows
    // the generic identity until a customer /me endpoint exists (later task).
    const store = memoryStore({ deviceToken: "device-token-1" });
    stubFetch((url) => {
      if (url.endsWith("/api/customer/sessions/login")) {
        return jsonResponse(loginBody, 201);
      }
      return jsonResponse({}, 500);
    });

    const { result } = renderHook(() =>
      useCustomerSession(store, { heartbeatIntervalMs: HEARTBEAT_INTERVAL_MS }),
    );

    await waitFor(() => expect(result.current.screen).toBe("workspace"));
    expect(result.current.user).toEqual({ userId: "user-1", username: null });
  });

  it("renews the lease with a heartbeat while the workspace is live", async () => {
    vi.useFakeTimers();
    const store = memoryStore({ deviceToken: "device-token-1" });
    const fetchMock = stubFetch((url) => {
      if (url.endsWith("/api/customer/sessions/login")) {
        return jsonResponse(loginBody, 201);
      }
      if (url.endsWith("/api/customer/sessions/heartbeat")) {
        return jsonResponse(heartbeatBody, 200);
      }
      return jsonResponse({}, 500);
    });

    const { result } = renderHook(() =>
      useCustomerSession(store, { heartbeatIntervalMs: HEARTBEAT_INTERVAL_MS }),
    );
    await vi.waitFor(() => expect(result.current.screen).toBe("workspace"));
    const beatsBefore = fetchMock.mock.calls.filter(([url]) =>
      String(url).endsWith("/api/customer/sessions/heartbeat"),
    ).length;
    expect(beatsBefore).toBe(0);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(HEARTBEAT_INTERVAL_MS);
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(HEARTBEAT_INTERVAL_MS);
    });

    const beatsAfter = fetchMock.mock.calls.filter(([url]) =>
      String(url).endsWith("/api/customer/sessions/heartbeat"),
    ).length;
    expect(beatsAfter).toBe(2);
  });

  it("exposes the session lease runtime from activation and clears it on logout", async () => {
    const store = memoryStore();
    stubFetch((url) => {
      if (url.endsWith("/api/customer/activate")) {
        return jsonResponse(activationBody, 201);
      }
      if (url.endsWith("/api/customer/sessions/logout")) {
        return jsonResponse(undefined, 204);
      }
      return jsonResponse({}, 500);
    });

    const { result } = renderHook(() =>
      useCustomerSession(store, { heartbeatIntervalMs: HEARTBEAT_INTERVAL_MS }),
    );
    await waitFor(() => expect(result.current.screen).toBe("activation"));

    await act(async () => {
      await result.current.activate({
        activationCode: "XS04-AAAAAAA-BBBBBBB-CCCCCCC-DDDDDDD",
        deviceName: "工作电脑",
      });
    });

    expect(result.current.screen).toBe("workspace");
    expect(result.current.sessionRuntime).not.toBeNull();
    expect(result.current.sessionRuntime?.leaseExpiresAt).toBe(
      activationBody.session_lease_expires_at,
    );
    expect(
      Number.isNaN(
        Date.parse(result.current.sessionRuntime?.lastHeartbeatAt ?? ""),
      ),
    ).toBe(false);

    await act(async () => {
      await result.current.logout();
    });
    expect(result.current.sessionRuntime).toBeNull();
  });

  it("refreshes the runtime lease on heartbeat ticks and manual renewal", async () => {
    vi.useFakeTimers();
    const store = memoryStore({ deviceToken: "device-token-1" });
    const fetchMock = stubFetch((url) => {
      if (url.endsWith("/api/customer/sessions/login")) {
        return jsonResponse(loginBody, 201);
      }
      if (url.endsWith("/api/customer/sessions/heartbeat")) {
        return jsonResponse(heartbeatBody, 200);
      }
      return jsonResponse({}, 500);
    });

    const { result } = renderHook(() =>
      useCustomerSession(store, { heartbeatIntervalMs: HEARTBEAT_INTERVAL_MS }),
    );
    await vi.waitFor(() => expect(result.current.screen).toBe("workspace"));
    // The restart login carries the login lease until the first beat lands.
    expect(result.current.sessionRuntime?.leaseExpiresAt).toBe(
      loginBody.session_lease_expires_at,
    );

    await act(async () => {
      await vi.advanceTimersByTimeAsync(HEARTBEAT_INTERVAL_MS);
    });
    expect(result.current.sessionRuntime?.leaseExpiresAt).toBe(
      heartbeatBody.lease_expires_at,
    );

    const beatsBefore = fetchMock.mock.calls.filter(([url]) =>
      String(url).endsWith("/api/customer/sessions/heartbeat"),
    ).length;
    await act(async () => {
      await result.current.sendHeartbeatNow();
    });
    expect(
      fetchMock.mock.calls.filter(([url]) =>
        String(url).endsWith("/api/customer/sessions/heartbeat"),
      ).length,
    ).toBe(beatsBefore + 1);
  });

  it("keeps the server lease while reporting a transient heartbeat failure separately", async () => {
    const store = memoryStore({ deviceToken: "device-token-1" });
    let heartbeatFails = true;
    stubFetch((url) => {
      if (url.endsWith("/api/customer/sessions/login")) {
        return jsonResponse(loginBody, 201);
      }
      if (url.endsWith("/api/customer/sessions/heartbeat")) {
        return heartbeatFails
          ? jsonResponse({ detail: "network unavailable" }, 503)
          : jsonResponse(heartbeatBody, 200);
      }
      return jsonResponse({}, 500);
    });

    const { result } = renderHook(() =>
      useCustomerSession(store, { heartbeatIntervalMs: HEARTBEAT_INTERVAL_MS }),
    );
    await waitFor(() => expect(result.current.screen).toBe("workspace"));
    const originalLease = result.current.sessionRuntime?.leaseExpiresAt;

    await act(async () => result.current.sendHeartbeatNow());

    expect(result.current.sessionRuntime).toEqual(
      expect.objectContaining({
        connectivity: "unreachable",
        leaseExpiresAt: originalLease,
      }),
    );

    heartbeatFails = false;
    await act(async () => result.current.sendHeartbeatNow());
    expect(result.current.sessionRuntime).toEqual(
      expect.objectContaining({
        connectivity: "reachable",
        leaseExpiresAt: heartbeatBody.lease_expires_at,
      }),
    );
  });

  it("ignores an older heartbeat failure after a newer heartbeat succeeds", async () => {
    const store = memoryStore({ deviceToken: "device-token-1" });
    let resolveOlderHeartbeat:
      | ((value: Awaited<ReturnType<typeof jsonResponse>>) => void)
      | undefined;
    const olderHeartbeat = new Promise<
      Awaited<ReturnType<typeof jsonResponse>>
    >((resolve) => {
      resolveOlderHeartbeat = resolve;
    });
    let heartbeatCalls = 0;
    stubFetch((url) => {
      if (url.endsWith("/api/customer/sessions/login")) {
        return jsonResponse(loginBody, 201);
      }
      if (url.endsWith("/api/customer/sessions/heartbeat")) {
        heartbeatCalls += 1;
        return heartbeatCalls === 1
          ? olderHeartbeat
          : jsonResponse(heartbeatBody, 200);
      }
      return jsonResponse({}, 500);
    });

    const { result } = renderHook(() =>
      useCustomerSession(store, { heartbeatIntervalMs: HEARTBEAT_INTERVAL_MS }),
    );
    await waitFor(() => expect(result.current.screen).toBe("workspace"));

    let olderRequest: Promise<void> | undefined;
    act(() => {
      olderRequest = result.current.sendHeartbeatNow();
    });
    await act(async () => result.current.sendHeartbeatNow());
    expect(result.current.sessionRuntime).toEqual(
      expect.objectContaining({
        connectivity: "reachable",
        leaseExpiresAt: heartbeatBody.lease_expires_at,
      }),
    );

    resolveOlderHeartbeat?.(
      await jsonResponse({ detail: "network unavailable" }, 503),
    );
    await act(async () => olderRequest);

    expect(result.current.sessionRuntime).toEqual(
      expect.objectContaining({
        connectivity: "reachable",
        leaseExpiresAt: heartbeatBody.lease_expires_at,
      }),
    );
  });

  it("ignores a heartbeat from the previous session after logout and login", async () => {
    const store = memoryStore({ deviceToken: "device-token-1" });
    let resolveOldHeartbeat:
      | ((value: Awaited<ReturnType<typeof jsonResponse>>) => void)
      | undefined;
    const oldHeartbeat = new Promise<Awaited<ReturnType<typeof jsonResponse>>>(
      (resolve) => {
        resolveOldHeartbeat = resolve;
      },
    );
    let loginCalls = 0;
    const secondLoginBody = {
      ...loginBody,
      session_lease_expires_at: "2026-08-24T13:00:00Z",
      request_id: "req-new-login",
    };
    stubFetch((url) => {
      if (url.endsWith("/api/customer/sessions/login")) {
        loginCalls += 1;
        return jsonResponse(
          loginCalls === 1 ? loginBody : secondLoginBody,
          201,
        );
      }
      if (url.endsWith("/api/customer/sessions/heartbeat")) {
        return oldHeartbeat;
      }
      if (url.endsWith("/api/customer/sessions/logout")) {
        return jsonResponse(undefined, 204);
      }
      return jsonResponse({}, 500);
    });

    const { result } = renderHook(() =>
      useCustomerSession(store, { heartbeatIntervalMs: HEARTBEAT_INTERVAL_MS }),
    );
    await waitFor(() => expect(result.current.screen).toBe("workspace"));
    let oldRequest: Promise<void> | undefined;
    act(() => {
      oldRequest = result.current.sendHeartbeatNow();
    });
    await act(async () => result.current.logout());
    await act(async () => result.current.retryLogin());
    expect(result.current.sessionRuntime?.leaseExpiresAt).toBe(
      secondLoginBody.session_lease_expires_at,
    );

    resolveOldHeartbeat?.(
      await jsonResponse(
        { ...heartbeatBody, lease_expires_at: "2026-08-24T12:03:00Z" },
        200,
      ),
    );
    await act(async () => oldRequest);

    expect(result.current.sessionRuntime).toEqual(
      expect.objectContaining({
        connectivity: "reachable",
        leaseExpiresAt: secondLoginBody.session_lease_expires_at,
      }),
    );
  });

  it("never writes a credential into Web Storage (dev doc §7 red line)", async () => {
    const store = memoryStore();
    stubFetch((url) => {
      if (url.endsWith("/api/customer/activate")) {
        return jsonResponse(activationBody, 201);
      }
      return jsonResponse({}, 500);
    });

    const { result } = renderHook(() =>
      useCustomerSession(store, { heartbeatIntervalMs: HEARTBEAT_INTERVAL_MS }),
    );
    await waitFor(() => expect(result.current.screen).toBe("activation"));

    await act(async () => {
      await result.current.activate({
        activationCode: "XS04-AAAAAAA-BBBBBBB-CCCCCCC-DDDDDDD",
        deviceName: "工作电脑",
      });
    });
    expect(result.current.screen).toBe("workspace");

    // FE-02 / §10.2: no plaintext secret may land in Web Storage — the
    // persistent copy lives only behind the injected credential store.
    expect(window.localStorage.length).toBe(0);
    expect(window.sessionStorage.length).toBe(0);
  });
});
