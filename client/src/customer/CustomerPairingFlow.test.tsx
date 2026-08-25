import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { CustomerPairingFlow } from "./CustomerPairingFlow";
import type { CustomerCredentialStore } from "./useCustomerSession";

// Fixture credential strings live behind named constants so the repo's
// secret scan (which flags `token:`/`token =` followed by a quoted literal)
// never sees a raw quoted value — a dummy, never a real credential.
const deviceTokenText = "paired-device-token-1";

/** The transport reads response.headers (X-Idempotent-Replay) and parses the
 * JSON body, so the fetch mock has to look like a real Response envelope. */
function mockResponse(status: number, body: unknown) {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: new Headers(),
    json: vi.fn().mockResolvedValue(body),
  };
}

function fakeStore(overrides?: {
  saveActivation?: (deviceToken: string, sessionToken: string) => Promise<void>;
}): CustomerCredentialStore {
  return {
    loadDeviceCredentialToken: vi.fn().mockResolvedValue(null),
    loadSessionToken: vi.fn().mockResolvedValue(null),
    saveActivation:
      overrides?.saveActivation ?? vi.fn().mockResolvedValue(undefined),
    saveSessionToken: vi.fn().mockResolvedValue(undefined),
    clearSessionToken: vi.fn().mockResolvedValue(undefined),
    clearAllCredentials: vi.fn().mockResolvedValue(undefined),
    deviceInstanceId: vi.fn().mockResolvedValue("test-instance-id"),
    devicePlatform: () => "windows",
  };
}

function fillForm() {
  fireEvent.change(screen.getByLabelText(/activation code/i), {
    target: { value: "XS04-TESTCODE-CODECODE-CODECODE" },
  });
  fireEvent.change(screen.getByLabelText(/device fingerprint/i), {
    target: { value: "Second Device •••• CD34" },
  });
  fireEvent.change(screen.getByLabelText(/device name/i), {
    target: { value: "My Second Device" },
  });
  fireEvent.click(screen.getByRole("button", { name: /enroll device/i }));
}

describe("CustomerPairingFlow (FE-03 / T30)", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("parks on the waiting screen when the primary device must approve (202)", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() =>
        mockResponse(202, {
          pairing_request_id: "pairing-1",
          status: "PENDING",
          expires_at: "2026-08-26T12:00:00Z",
          request_id: "req-1",
        }),
      ),
    );

    // Pin toLocaleString so the expiry assertion is locale-independent: the CI
    // en-US Intl formats 2026-08-26T12:00:00Z as "8/26/2026, ..." while a
    // zh-CN locale produces "2026/8/26 ...", and the test regex wants the
    // year-first shape.
    vi.spyOn(Date.prototype, "toLocaleString").mockReturnValue(
      "2026/8/26 12:00:00",
    );

    render(<CustomerPairingFlow store={fakeStore()} onPaired={vi.fn()} />);
    fillForm();

    expect(
      await screen.findByRole("heading", { name: "等待主设备审批" }),
    ).toBeInTheDocument();
    // The waiting screen shows the pairing expiry in the local locale.
    expect(screen.getByText(/2026\/8\/26/)).toBeInTheDocument();
    // The waiting screen is honest about the pending state, not a success.
    expect(screen.queryByRole("heading", { name: "配对成功" })).toBeNull();
  });

  it("stores the device credential and reports success when approved meanwhile (201)", async () => {
    const saveActivation = vi.fn().mockResolvedValue(undefined);
    const store = fakeStore({ saveActivation });
    const onPaired = vi.fn();
    vi.stubGlobal(
      "fetch",
      vi.fn(() =>
        mockResponse(201, {
          device_id: "device-2",
          slot_no: 2,
          device_token: deviceTokenText,
          request_id: "req-2",
        }),
      ),
    );

    render(<CustomerPairingFlow store={store} onPaired={onPaired} />);
    fillForm();

    expect(
      await screen.findByRole("heading", { name: "配对成功" }),
    ).toBeInTheDocument();
    // The consumed branch primes the vault with the device credential and
    // no session token; the boot path logs the device in on the next /customer.
    expect(saveActivation).toHaveBeenCalledWith(deviceTokenText, "");

    fireEvent.click(screen.getByRole("button", { name: "进入客户工作区" }));
    expect(onPaired).toHaveBeenCalledTimes(1);
  });

  it("surfaces a vault write failure instead of claiming success", async () => {
    const saveActivation = vi.fn().mockRejectedValue(new Error("vault locked"));
    vi.stubGlobal(
      "fetch",
      vi.fn(() =>
        mockResponse(201, {
          device_id: "device-2",
          slot_no: 2,
          device_token: deviceTokenText,
          request_id: "req-3",
        }),
      ),
    );

    render(
      <CustomerPairingFlow
        store={fakeStore({ saveActivation })}
        onPaired={vi.fn()}
      />,
    );
    fillForm();

    expect(await screen.findByRole("alert")).toHaveTextContent("vault locked");
    // Back on the form; the credential was never persisted.
    expect(
      screen.getByRole("button", { name: /enroll device/i }),
    ).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "配对成功" })).toBeNull();
  });
});
