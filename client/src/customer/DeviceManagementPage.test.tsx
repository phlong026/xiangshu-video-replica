import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { DeviceManagementPage } from "./DeviceManagementPage";

describe("DeviceManagementPage (FE-04 / T31)", () => {
  const mockDevices = {
    devices: [
      {
        id: "device-1",
        device_name: "iPhone •••• AB12",
        slot_no: 1,
        is_online: true,
        lease_expires_at: new Date(Date.now() + 3600_000).toISOString(), // 1 小时后过期
      },
    ],
    next_free_slot: 2,
  };

  const mockOnUnbind = vi.fn();
  const mockOnError = vi.fn();
  const mockOnRecharge = vi.fn();

  function renderWithProps(props?: {
    onUnbind?: () => void;
    onError?: () => void;
    onRecharge?: () => void;
  }) {
    render(
      <DeviceManagementPage
        devices={mockDevices}
        isOnline={true}
        onUnbind={props?.onUnbind ?? mockOnUnbind}
        onError={props?.onError ?? mockOnError}
        onRecharge={props?.onRecharge ?? mockOnRecharge}
      />,
    );
  }

  it("displays current online status at the top of the page", () => {
    renderWithProps();
    expect(screen.getByText(/status:/i)).toBeInTheDocument();
  });

  it("shows two-slot status with current slot #", () => {
    renderWithProps();
    expect(screen.getByText(/Slot #1/i)).toBeInTheDocument();
  });

  it("displays masked device name without exposing full fingerprint", () => {
    renderWithProps();
    expect(
      screen.getByText(/iPhone \u2022\u2022\u2022\u2022 AB12/i),
    ).toBeInTheDocument();
  });

  it("has unbind button for each device slot that calls onUnbind callback", async () => {
    renderWithProps();
    fireEvent.click(
      screen.getByRole("button", { name: /unbind this device/i }),
    );

    // Should prompt user to confirm action first
    await waitFor(() => {
      expect(mockOnUnbind).toHaveBeenCalled();
    });
  });

  it("shows lease expiry countdown for active session", () => {
    renderWithProps();
    expect(screen.getByText(/session expires at/i)).toBeInTheDocument();
  });

  it("provides recharge button that calls onRecharge callback", () => {
    renderWithProps();
    fireEvent.click(screen.getByRole("button", { name: /recharge/i }));
    expect(mockOnRecharge).toHaveBeenCalledTimes(1);
  });

  it("does not provide second master activation code entry point (wallet panel only)", () => {
    renderWithProps();
    // No input field for entering a second master activation code
    expect(
      screen.queryByLabelText(/second-master-code/i),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByPlaceholderText(/master-code/i),
    ).not.toBeInTheDocument();
  });

  it("shows appropriate messaging when no second device is available yet", () => {
    renderWithProps();
    expect(screen.getByText(/next free slot: #2/i)).toBeInTheDocument();
  });
});
