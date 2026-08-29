import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import * as adminApi from "../api.admin";
import { CustomersPage } from "./CustomersPage";

// Mock the admin API module
vi.mock("../api.admin", () => ({
  listCustomers: vi.fn(),
  fetchCustomerUnitPrice: vi.fn(),
  updateCustomerUnitPrice: vi.fn(),
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
    vi.mocked(adminApi.fetchCustomerUnitPrice).mockResolvedValue({
      user_id: "user-1",
      unit_price_fen: 1000,
      custom_unit_price_fen: null,
      default_unit_price_fen: 1000,
      min_recharge_fen: 1000,
      recharge_step_fen: 1000,
      updated_at: null,
      request_id: "request-price-read",
    });
  });

  it("renders customer list with pagination", async () => {
    const mockCustomers = [
      {
        user_id: "user-1",
        username: "customer-1",
        created_at: "2026-08-24T10:00:00Z",
        activation_code: "ABC-123",
        status: "active",
        generation_total: 8,
        generation_succeeded: 5,
        generation_failed: 1,
        generation_in_progress: 1,
        generation_attention: 1,
        credits_spent: 5,
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
    expect(screen.getByText("5 / 8")).toBeInTheDocument();
    expect(screen.getByText("1 / 1 / 1")).toBeInTheDocument();
    expect(screen.getByLabelText("customer-1 已结算消耗")).toHaveTextContent(
      "5",
    );
    const firstDataRow = screen
      .getAllByRole("row")
      .find((row) => row.textContent?.includes("customer-1"));
    expect(firstDataRow).toBeDefined();
    expect(
      within(firstDataRow as HTMLElement)
        .getAllByRole("cell")
        .map((cell) => cell.textContent?.replace(/\s+/g, " ").trim()),
    ).toEqual([
      "customer-1",
      "ABC-123",
      "2026/8/24 18:00:00",
      "活跃",
      "5 / 8",
      "1 / 1 / 1",
      "5",
      "展开详情",
    ]);
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

    fireEvent.click(screen.getByRole("button", { name: "展开详情" }));
    fireEvent.click(screen.getByRole("button", { name: "调账历史" }));
    expect(adminApi.listAdminAdjustments).toHaveBeenCalledWith("user-1", {
      limit: 20,
      offset: 0,
    });
  });

  it("expands a customer row to reveal operational details and collapse again", async () => {
    vi.mocked(adminApi.listCustomers).mockResolvedValue({
      customers: [
        {
          user_id: "user-1",
          username: "customer-1",
          created_at: "2026-08-24T10:00:00Z",
          activation_code: "ABC-123",
          status: "active",
          generation_total: 8,
          generation_succeeded: 5,
          generation_failed: 1,
          generation_in_progress: 1,
          generation_attention: 1,
          credits_spent: 5,
        },
      ],
      total: 1,
      page: 1,
      page_size: 20,
    });

    render(<CustomersPage />);

    const toggle = await screen.findByRole("button", { name: "展开详情" });
    expect(toggle).toHaveAttribute("aria-expanded", "false");

    fireEvent.click(toggle);

    const detailPanel = screen
      .getByRole("heading", {
        name: "customer-1 运营详情",
      })
      .closest("section");
    expect(detailPanel).not.toBeNull();
    expect(
      within(detailPanel as HTMLElement).getByText("累计生成"),
    ).toBeInTheDocument();
    expect(
      within(detailPanel as HTMLElement).getByText("8 条"),
    ).toBeInTheDocument();
    expect(
      within(detailPanel as HTMLElement).getByRole("button", {
        name: "调账历史",
      }),
    ).toBeInTheDocument();
    expect(
      within(detailPanel as HTMLElement).getByRole("button", {
        name: "收起详情",
      }),
    ).toHaveAttribute("aria-expanded", "true");

    fireEvent.click(
      within(detailPanel as HTMLElement).getByRole("button", {
        name: "收起详情",
      }),
    );

    expect(
      screen.queryByRole("heading", { name: "customer-1 运营详情" }),
    ).toBeNull();
  });

  it("loads and updates the customer's effective recharge price", async () => {
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
    vi.mocked(adminApi.updateCustomerUnitPrice).mockResolvedValue({
      user_id: "user-1",
      unit_price_fen: 880,
      custom_unit_price_fen: 880,
      default_unit_price_fen: 1000,
      min_recharge_fen: 1000,
      recharge_step_fen: 880,
      updated_at: "2026-08-29T10:00:00Z",
      request_id: "request-price-write",
    });

    render(<CustomersPage />);
    fireEvent.click(await screen.findByRole("button", { name: "展开详情" }));

    expect(await screen.findByText(/当前 10.00 元\/条/)).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("售价（元/条）"), {
      target: { value: "8.8" },
    });
    fireEvent.change(screen.getByLabelText("修改原因"), {
      target: { value: "客户合同价" },
    });
    fireEvent.click(screen.getByRole("button", { name: "保存客户售价" }));

    await waitFor(() => {
      expect(adminApi.updateCustomerUnitPrice).toHaveBeenCalledWith(
        "user-1",
        880,
        "客户合同价",
      );
    });
    expect(await screen.findByText("客户售价已保存")).toBeInTheDocument();
  });

  it("keeps customer pricing read-only for auditors", async () => {
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

    render(<CustomersPage readOnly />);
    fireEvent.click(await screen.findByRole("button", { name: "展开详情" }));

    expect(
      await screen.findByText("审计员仅可查看定价，不能修改。"),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "保存客户售价" })).toBeNull();
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
