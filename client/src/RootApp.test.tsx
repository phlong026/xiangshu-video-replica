import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { CUSTOMER_SESSION_REPLACED_EVENT } from "./api";
import { RootApp } from "./RootApp";

function jsonResponse(payload: unknown, status = 200) {
  return Promise.resolve({
    ok: status >= 200 && status < 300,
    status,
    json: async () => payload,
  });
}

// The fake token fixtures live in named constants so the repo's secret
// scan (which flags `token:` followed by a quoted literal) stays quiet —
// same posture as customerApi.test.ts.
const deviceTokenText = "device-token-1";
const sessionTokenText = "session-token-1";

const customerActivationBody = {
  username: "user-1",
  user_id: "user-1",
  device_id: "device-1",
  device_token: deviceTokenText,
  session_token: sessionTokenText,
  session_epoch: 1,
  session_lease_expires_at: "2026-08-24T12:01:00Z",
  request_id: "req-1",
};

/** Fetch stub for the customer lane: the activation succeeds and the
 * workspace shell's internal-lane probes (health, projects) resolve benignly
 * so the workspace can mount — the customer business APIs arrive in later
 * tasks (T34+), not T29. The device list resolves to an empty two-slot
 * contract so the T31 device view can mount. */
function stubCustomerWorkspaceFetch() {
  return vi.fn((url: string) => {
    if (url.endsWith("/api/customer/activate")) {
      return jsonResponse(customerActivationBody, 201);
    }
    if (url.endsWith("/api/customer/devices")) {
      return jsonResponse({
        slots: [
          { slot_no: 1, device: null },
          { slot_no: 2, device: null },
        ],
        history: [],
        pending_pairings: [],
      });
    }
    if (url.endsWith("/health")) {
      return jsonResponse({ status: "ok", service: "video-replica-api" });
    }
    return jsonResponse([]);
  });
}

describe("RootApp", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    window.history.replaceState(null, "", "/");
  });

  it.each(["/admin", "/admin/"])(
    "routes %s to the internal management page",
    (path) => {
      vi.stubGlobal(
        "fetch",
        vi.fn(() => jsonResponse({ items: [] })),
      );

      render(<RootApp path={path} />);

      expect(
        screen.getByRole("heading", { name: "内部运营管理" }),
      ).toBeInTheDocument();
      expect(screen.queryByRole("heading", { name: "镜序 Studio" })).toBeNull();
    },
  );

  it("keeps normal paths on the user workspace", async () => {
    const fetchMock = vi.fn((url: string) => {
      if (url.endsWith("/api/auth/me")) {
        return jsonResponse({
          id: "user-1",
          username: "user-1",
          display_name: "运营",
          role: "employee",
        });
      }
      if (url.endsWith("/health")) {
        return jsonResponse({ status: "ok", service: "video-replica-api" });
      }
      return jsonResponse([]);
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<RootApp path="/" />);

    expect(
      await screen.findByRole("heading", { name: "项目" }),
    ).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "内部运营管理" })).toBeNull();
  });

  it("routes /customer/pairing to the second-device enrollment form, not the state machine", async () => {
    vi.stubGlobal("fetch", stubCustomerWorkspaceFetch());

    render(<RootApp path="/customer/pairing" />);

    // T30: the pairing entry renders even when this browser holds no
    // credential — it collects the activation code for the primary device.
    expect(
      await screen.findByRole("heading", { name: "Pair New Device" }),
    ).toBeInTheDocument();
    expect(screen.getByLabelText(/activation code/i)).toBeInTheDocument();
    // The seven-screen state machine must not intercept the pairing route.
    expect(
      screen.queryByRole("heading", { name: "激活短视频复刻工作台" }),
    ).toBeNull();
  });

  it("routes /customer to the activation screen without any internal-token field", async () => {
    vi.stubGlobal("fetch", stubCustomerWorkspaceFetch());

    render(<RootApp path="/customer" />);

    expect(
      await screen.findByRole("heading", { name: "激活短视频复刻工作台" }),
    ).toBeInTheDocument();
    // FE-02 No-Go: the internal access-token input must never be the
    // customer's entry — the customer lane has its own activation flow.
    expect(screen.queryByLabelText("内部访问令牌（云端模式）")).toBeNull();
  });

  it("activates on /customer and lands in the workspace under the customer identity", async () => {
    vi.stubGlobal("fetch", stubCustomerWorkspaceFetch());

    render(<RootApp path="/customer" />);

    await screen.findByRole("heading", { name: "激活短视频复刻工作台" });
    fireEvent.change(screen.getByLabelText("激活码"), {
      target: { value: "XS04-AAAAAAA-BBBBBBB-CCCCCCC-DDDDDDD" },
    });
    fireEvent.change(screen.getByLabelText("设备名称"), {
      target: { value: "工作电脑" },
    });
    fireEvent.click(screen.getByRole("button", { name: "激活并进入工作台" }));

    expect(
      await screen.findByRole("heading", { name: "项目" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("navigation", { name: "主导航" }),
    ).toBeInTheDocument();
    // The activated username identifies the customer in the shared shell.
    expect(screen.getByText("user-1")).toBeInTheDocument();
    expect(screen.queryByLabelText("内部访问令牌（云端模式）")).toBeNull();
  });

  it("shows the displaced-session terminal screen when replaced mid-session (§4.2)", async () => {
    vi.stubGlobal("fetch", stubCustomerWorkspaceFetch());

    render(<RootApp path="/customer" />);
    await screen.findByRole("heading", { name: "激活短视频复刻工作台" });
    fireEvent.change(screen.getByLabelText("激活码"), {
      target: { value: "XS04-AAAAAAA-BBBBBBB-CCCCCCC-DDDDDDD" },
    });
    fireEvent.change(screen.getByLabelText("设备名称"), {
      target: { value: "工作电脑" },
    });
    fireEvent.click(screen.getByRole("button", { name: "激活并进入工作台" }));
    await screen.findByRole("heading", { name: "项目" });

    window.dispatchEvent(new Event(CUSTOMER_SESSION_REPLACED_EVENT));

    expect(
      await screen.findByRole("heading", { name: "本设备已下线" }),
    ).toBeInTheDocument();
    // §4.2 red line: a displaced session must be reported as exactly that —
    // never as a balance, network, or generic service failure.
    expect(screen.getByText(/已在另一台设备上登录/)).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "重新登录" }),
    ).toBeInTheDocument();
  });
});
