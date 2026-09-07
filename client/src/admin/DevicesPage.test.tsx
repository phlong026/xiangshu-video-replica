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

  it("keeps customer context when opened from customer details", async () => {
    vi.mocked(adminApi.listDevices).mockResolvedValue({
      items: [],
      total: 0,
      limit: 20,
      offset: 0,
    });

    render(<DevicesPage userId="user-1" />);

    await waitFor(() => {
      expect(adminApi.listDevices).toHaveBeenCalledWith({
        limit: 20,
        offset: 0,
        platform: undefined,
        status: undefined,
        userId: "user-1",
      });
    });
    expect(screen.getByText("当前客户：user-1")).toBeInTheDocument();
    expect(screen.queryByLabelText("设备概览")).toBeNull();
  });

  it("ignores a stale customer response after switching context", async () => {
    let resolveCustomerA:
      | ((value: Awaited<ReturnType<typeof adminApi.listDevices>>) => void)
      | undefined;
    vi.mocked(adminApi.listDevices)
      .mockReturnValueOnce(
        new Promise((resolve) => {
          resolveCustomerA = resolve;
        }),
      )
      .mockResolvedValueOnce({
        items: [
          {
            device_id: "device-b",
            activation_code_id: "code-b",
            user_id: "customer-b",
            slot_no: 1,
            display_name: "B 的电脑",
            platform: "windows",
            status: "BOUND",
            bound_at: "2026-08-24T10:00:00Z",
            unbound_at: null,
            revoked_at: null,
          },
        ],
        total: 1,
        limit: 20,
        offset: 0,
      });

    const { rerender } = render(<DevicesPage userId="customer-a" />);
    rerender(<DevicesPage userId="customer-b" />);
    expect(await screen.findByText("B 的电脑")).toBeInTheDocument();

    resolveCustomerA?.({
      items: [
        {
          device_id: "device-a",
          activation_code_id: "code-a",
          user_id: "customer-a",
          slot_no: 1,
          display_name: "A 的旧电脑",
          platform: "windows",
          status: "BOUND",
          bound_at: "2026-08-24T09:00:00Z",
          unbound_at: null,
          revoked_at: null,
        },
      ],
      total: 1,
      limit: 20,
      offset: 0,
    });

    await waitFor(() => expect(screen.queryByText("A 的旧电脑")).toBeNull());
    expect(screen.getByText("当前客户：customer-b")).toBeInTheDocument();
  });

  it("does not apply an old operation after leaving and returning to a customer", async () => {
    let resolveUnbind:
      | ((value: Awaited<ReturnType<typeof adminApi.unbindDevice>>) => void)
      | undefined;
    vi.mocked(adminApi.listDevices).mockImplementation(async (options) => {
      const userId = options?.userId;
      return {
        items: [
          {
            device_id: `device-${userId}`,
            activation_code_id: `code-${userId}`,
            user_id: userId ?? "all",
            slot_no: 1,
            display_name: `${userId} 的电脑`,
            platform: "windows",
            status: "BOUND",
            bound_at: "2026-08-24T10:00:00Z",
            unbound_at: null,
            revoked_at: null,
          },
        ],
        total: 1,
        limit: 20,
        offset: 0,
      };
    });
    vi.mocked(adminApi.unbindDevice).mockReturnValue(
      new Promise((resolve) => {
        resolveUnbind = resolve;
      }),
    );

    const { rerender } = render(<DevicesPage userId="customer-a" />);
    fireEvent.click(
      await screen.findByRole("button", {
        name: "下线设备：customer-a 的电脑",
      }),
    );
    fireEvent.change(screen.getByLabelText("操作原因"), {
      target: { value: "客户申请更换设备" },
    });
    fireEvent.click(screen.getByRole("button", { name: "确认下线" }));

    rerender(<DevicesPage userId="customer-b" />);
    await screen.findByText("customer-b 的电脑");
    rerender(<DevicesPage userId="customer-a" />);
    await screen.findByText("customer-a 的电脑");
    const listCallCount = vi.mocked(adminApi.listDevices).mock.calls.length;

    resolveUnbind?.({
      device_id: "device-customer-a",
      status: "UNBOUND",
      outcome: "UNBOUND",
      request_id: "old-request",
    });

    await waitFor(() => {
      expect(screen.queryByRole("status")).toBeNull();
      expect(adminApi.listDevices).toHaveBeenCalledTimes(listCallCount);
    });
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
