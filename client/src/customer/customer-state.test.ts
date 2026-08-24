import { describe, expect, it } from "vitest";

import {
  type CustomerScreenEvent,
  customerScreenReducer,
  initialCustomerScreen,
} from "./customer-state";

describe("customerScreenReducer", () => {
  it("starts in the checking screen", () => {
    expect(initialCustomerScreen).toBe("checking");
  });

  it("boots without a device credential into the activation screen", () => {
    expect(
      customerScreenReducer("checking", {
        type: "boot-check-completed",
        hasDeviceCredential: false,
      }),
    ).toBe("activation");
  });

  it("boots with a stored device credential into the login screen", () => {
    expect(
      customerScreenReducer("checking", {
        type: "boot-check-completed",
        hasDeviceCredential: true,
      }),
    ).toBe("login");
  });

  it("moves activation success straight into the workspace", () => {
    expect(
      customerScreenReducer("activation", { type: "activation-succeeded" }),
    ).toBe("workspace");
  });

  it("moves login success into the workspace", () => {
    expect(customerScreenReducer("login", { type: "login-succeeded" })).toBe(
      "workspace",
    );
  });

  it("routes an other-device-online login to the conflict screen", () => {
    expect(customerScreenReducer("login", { type: "conflict-detected" })).toBe(
      "binding-conflict",
    );
    // Guarded: a conflict can only be detected while logging in.
    expect(
      customerScreenReducer("workspace", { type: "conflict-detected" }),
    ).toBe("workspace");
  });

  it("returns to the login screen when the takeover is declined", () => {
    expect(
      customerScreenReducer("binding-conflict", { type: "conflict-cancelled" }),
    ).toBe("login");
    expect(customerScreenReducer("login", { type: "conflict-cancelled" })).toBe(
      "login",
    );
  });

  it("ends a confirmed takeover on the workspace", () => {
    // The server confirmed the switch and minted a fresh session token.
    expect(
      customerScreenReducer("binding-conflict", { type: "login-succeeded" }),
    ).toBe("workspace");
  });

  it("sends a credential-missing switch straight to activation for recovery", () => {
    expect(
      customerScreenReducer("binding-conflict", { type: "credential-missing" }),
    ).toBe("activation");
  });

  it("keeps a logout on the login screen (the device credential survives)", () => {
    expect(customerScreenReducer("workspace", { type: "logout" })).toBe(
      "login",
    );
  });

  it("routes an expired session to the dedicated expired screen", () => {
    expect(
      customerScreenReducer("workspace", { type: "session-expired" }),
    ).toBe("session-expired");
  });

  it("routes a replaced session to the dedicated replaced screen", () => {
    // §4.2: being displaced must surface as its own session outcome — never
    // misreported as a balance/network/service failure.
    expect(
      customerScreenReducer("workspace", { type: "session-replaced" }),
    ).toBe("session-replaced");
  });

  it("routes a revoked device to the dedicated revoked screen from anywhere", () => {
    for (const from of [
      "checking",
      "activation",
      "login",
      "workspace",
      "session-expired",
      "session-replaced",
    ] as const) {
      expect(customerScreenReducer(from, { type: "device-revoked" })).toBe(
        "device-revoked",
      );
    }
  });

  it("lets an expired or replaced session fall back to the login screen", () => {
    expect(
      customerScreenReducer("session-expired", { type: "restart-login" }),
    ).toBe("login");
    expect(
      customerScreenReducer("session-replaced", { type: "restart-login" }),
    ).toBe("login");
  });

  it("sends a revoked device through the recovery flow back to activation", () => {
    expect(
      customerScreenReducer("device-revoked", { type: "restart-activation" }),
    ).toBe("activation");
  });

  it("ignores transient events that do not apply to the current screen", () => {
    // A heartbeat answer or an unrelated dispatch must never kick the UI out
    // of its current screen.
    const event: CustomerScreenEvent = { type: "login-succeeded" };
    expect(customerScreenReducer("workspace", event)).toBe("workspace");
    expect(customerScreenReducer("activation", { type: "logout" })).toBe(
      "activation",
    );
  });

  it("returns to the activation screen when a credential-missing event fires from the login screen", () => {
    expect(customerScreenReducer("login", { type: "credential-missing" })).toBe(
      "activation",
    );
    // Guarded: only from login — no-op from other screens.
    expect(
      customerScreenReducer("checking", { type: "credential-missing" }),
    ).toBe("checking");
    expect(
      customerScreenReducer("workspace", { type: "credential-missing" }),
    ).toBe("workspace");
  });
});
