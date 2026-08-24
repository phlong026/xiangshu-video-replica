import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import * as adminApi from "../api.admin";
import { CustomersPage } from "./CustomersPage";

// Mock the admin API module
vi.mock("../api.admin", () => ({
  listCustomers: vi.fn(),
  listAdminAdjustments: vi.fn().mockResolvedValue({
    items: [],
    total: 0,
    limit: 20,
    offset: 0,
  }),
  AdminCustomerError: class extends Error {
    constructor(message: string) {
      super(message);
      this.name = "AdminCustomerError";
    }
  },
  AdminAdjustmentError: class extends Error {
    constructor(message: string) {
      super(message);
      this.name = "AdminAdjustmentError";
    }
  },
}));

describe("CustomersPage (ADM-02 / T33)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("renders customer list with pagination", async () => {
    const mockCustomers = [
      {
        user_id: "user-1",
        username: "customer-1",
        created_at: "2026-08-24T10:00:00Z",
        activation_code: "ABC-123",
        status: "active",
      },
      {
        user_id: "user-2",
        username: "customer-2",
        created_at: "2026-08-24T11:00:00Z",
        activation_code: "DEF-456",
        status: "active",
      },
    ];

    vi.mocked(adminApi.listCustomers).mockResolvedValue({
      customers: mockCustomers,
      total: 2,
      page: 1,
      page_size: 20,
    });

    render(<CustomersPage />);

    await waitFor(() => {
      expect(screen.getByText("customer-1")).toBeInTheDocument();
      expect(screen.getByText("customer-2")).toBeInTheDocument();
    });

    expect(screen.getByText(/共 2 位客户/)).toBeInTheDocument();
  });

  it("shows loading state while fetching customers", () => {
    vi.mocked(adminApi.listCustomers).mockImplementation(
      () => new Promise(() => {}), // Never resolves
    );

    render(<CustomersPage />);

    expect(screen.getByText("加载中...")).toBeInTheDocument();
  });

  it("handles API error gracefully", async () => {
    vi.mocked(adminApi.listCustomers).mockRejectedValue(
      new adminApi.AdminCustomerError("网络错误"),
    );

    render(<CustomersPage />);

    await waitFor(() => {
      expect(screen.getByText("加载失败：网络错误")).toBeInTheDocument();
    });
  });

  it("supports pagination navigation", async () => {
    const mockCustomers = Array.from({ length: 20 }, (_, i) => ({
      user_id: `user-${i}`,
      username: `user-${i}`,
      created_at: "2026-08-24T10:00:00Z",
      activation_code: `CODE-${i}`,
      status: "active",
    }));

    vi.mocked(adminApi.listCustomers).mockResolvedValue({
      customers: mockCustomers,
      total: 50,
      page: 1,
      page_size: 20,
    });

    render(<CustomersPage />);

    await waitFor(() => {
      expect(screen.getByText(/第 1 页/)).toBeInTheDocument();
    });

    const nextPageButton = screen.getByRole("button", { name: "下一页" });
    fireEvent.click(nextPageButton);

    expect(adminApi.listCustomers).toHaveBeenCalledWith({
      page: 2,
      page_size: 20,
    });
  });

  it("supports filtering by username", async () => {
    vi.mocked(adminApi.listCustomers).mockResolvedValue({
      customers: [],
      total: 0,
      page: 1,
      page_size: 20,
    });

    render(<CustomersPage />);

    const filterInput = screen.getByPlaceholderText("按用户名筛选");
    fireEvent.change(filterInput, { target: { value: "customer-1" } });
    fireEvent.click(screen.getByRole("button", { name: "筛选" }));

    expect(adminApi.listCustomers).toHaveBeenCalledWith({
      page: 1,
      page_size: 20,
      username_filter: "customer-1",
    });
  });

  it("opens the adjustment history for the selected customer", async () => {
    vi.mocked(adminApi.listCustomers).mockResolvedValue({
      customers: [
        {
          user_id: "user-1",
          username: "customer-1",
          created_at: "2026-08-24T10:00:00Z",
          activation_code: "ABC-123",
          status: "active",
        },
      ],
      total: 1,
      page: 1,
      page_size: 20,
    });

    render(<CustomersPage />);

    await waitFor(() => {
      expect(screen.getByText("customer-1")).toBeInTheDocument();
    });

    fireEvent.click(screen.getByRole("button", { name: "调账历史" }));
    expect(adminApi.listAdminAdjustments).toHaveBeenCalledWith("user-1", {
      limit: 20,
      offset: 0,
    });
  });

  it("shows empty state when no customers exist", async () => {
    vi.mocked(adminApi.listCustomers).mockResolvedValue({
      customers: [],
      total: 0,
      page: 1,
      page_size: 20,
    });

    render(<CustomersPage />);

    await waitFor(() => {
      expect(screen.getByText("暂无客户数据")).toBeInTheDocument();
    });
  });

  it("displays customer status badges correctly", async () => {
    const mockCustomers = [
      {
        user_id: "user-1",
        username: "active-user",
        created_at: "2026-08-24T10:00:00Z",
        activation_code: "ABC-123",
        status: "active",
      },
      {
        user_id: "user-2",
        username: "suspended-user",
        created_at: "2026-08-24T11:00:00Z",
        activation_code: "DEF-456",
        status: "suspended",
      },
    ];

    vi.mocked(adminApi.listCustomers).mockResolvedValue({
      customers: mockCustomers,
      total: 2,
      page: 1,
      page_size: 20,
    });

    render(<CustomersPage />);

    await waitFor(() => {
      expect(screen.getByText("活跃")).toBeInTheDocument();
      expect(screen.getByText("已暂停")).toBeInTheDocument();
    });
  });
});
