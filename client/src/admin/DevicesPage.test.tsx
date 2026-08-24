import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import * as adminApi from "../api.admin";
import { DevicesPage } from "./DevicesPage";

// Mock the admin API module
vi.mock("../api.admin", () => ({
  listDevices: vi.fn(),
  AdminDeviceError: class extends Error {
    constructor(message: string) {
      super(message);
      this.name = "AdminDeviceError";
    }
  },
}));

describe("DevicesPage (ADM-02 / T33)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("renders device list with pagination", async () => {
    const mockDevices = [
      {
        device_id: "device-1",
        activation_code_id: "code-1",
        user_id: "user-1",
        slot_no: 1,
        display_name: "iPhone 15 Pro",
        platform: "ios",
        status: "active",
        bound_at: "2026-08-24T10:00:00Z",
        unbound_at: null,
        revoked_at: null,
      },
      {
        device_id: "device-2",
        activation_code_id: "code-2",
        user_id: "user-2",
        slot_no: 1,
        display_name: "MacBook Pro",
        platform: "macos",
        status: "active",
        bound_at: "2026-08-24T11:00:00Z",
        unbound_at: null,
        revoked_at: null,
      },
    ];

    vi.mocked(adminApi.listDevices).mockResolvedValue({
      items: mockDevices,
      limit: 20,
      offset: 0,
    });

    render(<DevicesPage />);

    await waitFor(() => {
      expect(screen.getByText("iPhone 15 Pro")).toBeInTheDocument();
      expect(screen.getByText("MacBook Pro")).toBeInTheDocument();
    });
  });

  it("shows loading state while fetching devices", () => {
    vi.mocked(adminApi.listDevices).mockImplementation(
      () => new Promise(() => {}), // Never resolves
    );

    render(<DevicesPage />);

    expect(screen.getByText("加载中...")).toBeInTheDocument();
  });

  it("handles API error gracefully", async () => {
    vi.mocked(adminApi.listDevices).mockRejectedValue(
      new adminApi.AdminDeviceError("网络错误"),
    );

    render(<DevicesPage />);

    await waitFor(() => {
      expect(screen.getByText("加载失败：网络错误")).toBeInTheDocument();
    });
  });

  it("supports pagination navigation", async () => {
    const mockDevices = Array.from({ length: 20 }, (_, i) => ({
      device_id: `device-${i}`,
      activation_code_id: `code-${i}`,
      user_id: `user-${i}`,
      slot_no: 1,
      display_name: `Device ${i}`,
      platform: "ios",
      status: "active",
      bound_at: "2026-08-24T10:00:00Z",
      unbound_at: null,
      revoked_at: null,
    }));

    vi.mocked(adminApi.listDevices).mockResolvedValue({
      items: mockDevices,
      limit: 20,
      offset: 0,
    });

    render(<DevicesPage />);

    await waitFor(() => {
      expect(screen.getByText(/偏移 0 起/)).toBeInTheDocument();
    });

    const nextPageButton = screen.getByRole("button", { name: "下一页" });
    fireEvent.click(nextPageButton);

    expect(adminApi.listDevices).toHaveBeenCalledWith({
      limit: 20,
      offset: 20,
    });
  });

  it("displays device status badges correctly", async () => {
    const mockDevices = [
      {
        device_id: "device-1",
        activation_code_id: "code-1",
        user_id: "user-1",
        slot_no: 1,
        display_name: "Active Device",
        platform: "ios",
        status: "active",
        bound_at: "2026-08-24T10:00:00Z",
        unbound_at: null,
        revoked_at: null,
      },
      {
        device_id: "device-2",
        activation_code_id: "code-1",
        user_id: "user-1",
        slot_no: 2,
        display_name: "Revoked Device",
        platform: "ios",
        status: "revoked",
        bound_at: "2026-08-24T09:00:00Z",
        unbound_at: null,
        revoked_at: "2026-08-24T10:00:00Z",
      },
    ];

    vi.mocked(adminApi.listDevices).mockResolvedValue({
      items: mockDevices,
      limit: 20,
      offset: 0,
    });

    render(<DevicesPage />);

    await waitFor(() => {
      expect(screen.getByText("活跃")).toBeInTheDocument();
      expect(screen.getByText("已作废")).toBeInTheDocument();
    });
  });

  it("shows empty state when no devices exist", async () => {
    vi.mocked(adminApi.listDevices).mockResolvedValue({
      items: [],
      limit: 20,
      offset: 0,
    });

    render(<DevicesPage />);

    await waitFor(() => {
      expect(screen.getByText("暂无设备数据")).toBeInTheDocument();
    });
  });

  it("displays platform icons correctly", async () => {
    const mockDevices = [
      {
        device_id: "device-1",
        activation_code_id: "code-1",
        user_id: "user-1",
        slot_no: 1,
        display_name: "iOS Device",
        platform: "ios",
        status: "active",
        bound_at: "2026-08-24T10:00:00Z",
        unbound_at: null,
        revoked_at: null,
      },
      {
        device_id: "device-2",
        activation_code_id: "code-2",
        user_id: "user-2",
        slot_no: 1,
        display_name: "Android Device",
        platform: "android",
        status: "active",
        bound_at: "2026-08-24T11:00:00Z",
        unbound_at: null,
        revoked_at: null,
      },
    ];

    vi.mocked(adminApi.listDevices).mockResolvedValue({
      items: mockDevices,
      limit: 20,
      offset: 0,
    });

    render(<DevicesPage />);

    await waitFor(() => {
      expect(screen.getByText("iOS")).toBeInTheDocument();
      expect(screen.getByText("Android")).toBeInTheDocument();
    });
  });
});
