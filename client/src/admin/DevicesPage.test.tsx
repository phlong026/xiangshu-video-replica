import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import * as adminApi from "../api.admin";
import { DevicesPage } from "./DevicesPage";

// Mock the admin API module
vi.mock("../api.admin", () => ({
  listDevices: vi.fn(),
  unbindDevice: vi.fn(),
  revokeDeviceCredential: vi.fn(),
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
      total: 20,
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

  it("hides device mutations for auditors", async () => {
    vi.mocked(adminApi.listDevices).mockResolvedValue({
      items: [
        {
          device_id: "device-1",
          user_id: "user-1",
          activation_code_id: "code-1",
          slot_no: 1,
          display_name: "审计设备",
          platform: "windows",
          status: "BOUND",
          bound_at: "2026-08-24T10:00:00Z",
          unbound_at: null,
          revoked_at: null,
        },
      ],
      total: 2,
      limit: 20,
      offset: 0,
    });

    render(<DevicesPage readOnly />);

    expect(await screen.findByText("仅查看")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /下线设备/ })).toBeNull();
    expect(screen.queryByRole("button", { name: /强制退出/ })).toBeNull();
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
      total: 41,
      limit: 20,
      offset: 0,
    });

    render(<DevicesPage />);

    // A5：服务端返回 total，分页条显示真实页数。
    await waitFor(() => {
      expect(screen.getByText("第 1 / 3 页（共 41 条）")).toBeInTheDocument();
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
        status: "BOUND",
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
        status: "REVOKED",
        bound_at: "2026-08-24T09:00:00Z",
        unbound_at: null,
        revoked_at: "2026-08-24T10:00:00Z",
      },
    ];

    vi.mocked(adminApi.listDevices).mockResolvedValue({
      items: mockDevices,
      total: 20,
      limit: 20,
      offset: 0,
    });

    render(<DevicesPage />);

    // 设备 REVOKED 统一译作"已强制退出"（与操作动词一致），不再出现"已退出"。
    await waitFor(() => {
      expect(screen.getByText("已绑定")).toBeInTheDocument();
      // 表格徽章与概览卡片各出现一次。
      expect(screen.getAllByText("已强制退出").length).toBeGreaterThan(0);
    });
  });

  it("shows empty state when no devices exist", async () => {
    vi.mocked(adminApi.listDevices).mockResolvedValue({
      items: [],
      total: 0,
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
      total: 20,
      limit: 20,
      offset: 0,
    });

    render(<DevicesPage />);

    await waitFor(() => {
      expect(screen.getByText("iOS")).toBeInTheDocument();
      expect(screen.getByText("Android")).toBeInTheDocument();
    });
  });

  it("requires a reason and confirms device offline operations", async () => {
    vi.mocked(adminApi.listDevices).mockResolvedValue({
      items: [
        {
          device_id: "device-1",
          activation_code_id: "code-1",
          user_id: "user-1",
          slot_no: 1,
          display_name: "测试电脑",
          platform: "windows",
          status: "BOUND",
          bound_at: "2026-08-24T10:00:00Z",
          unbound_at: null,
          revoked_at: null,
        },
      ],
      total: 2,
      limit: 20,
      offset: 0,
    });
    vi.mocked(adminApi.unbindDevice).mockResolvedValue({
      device_id: "device-1",
      status: "UNBOUND",
      outcome: "UNBOUND",
      request_id: "request-1",
    });

    render(<DevicesPage />);
    fireEvent.click(
      await screen.findByRole("button", { name: "下线设备：测试电脑" }),
    );
    fireEvent.click(screen.getByRole("button", { name: "确认下线" }));
    expect(screen.getByRole("alert")).toHaveTextContent("请填写操作原因");
    fireEvent.change(screen.getByLabelText("操作原因"), {
      target: { value: "客户要求更换电脑" },
    });
    fireEvent.click(screen.getByRole("button", { name: "确认下线" }));

    await waitFor(() =>
      expect(adminApi.unbindDevice).toHaveBeenCalledWith(
        "device-1",
        "客户要求更换电脑",
      ),
    );
    expect(await screen.findByRole("status")).toHaveTextContent("设备已下线");
  });
});
