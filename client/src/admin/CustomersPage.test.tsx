import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import * as api from "../api";
import * as adminApi from "../api.admin";
import { CustomersPage } from "./CustomersPage";

// Mock the admin API module
vi.mock("../api.admin", () => ({
  listCustomers: vi.fn(),
  fetchCustomerUnitPrice: vi.fn(),
  updateCustomerUnitPrice: vi.fn(),
  createCustomerAdjustment: vi.fn(),
  listAdminRechargeOrders: vi.fn(),
  listAdminWalletTransactions: vi.fn(),
  listDevices: vi.fn(),
  listCustomerSessions: vi.fn(),
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
  AdminActivationError: class extends Error {
    readonly status: number | undefined;
    constructor(message: string, status?: number) {
      super(message);
      this.name = "AdminActivationError";
      this.status = status;
    }
  },
}));

vi.mock("../api", () => ({
  downloadCustomersCsv: vi.fn(),
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
    vi.mocked(adminApi.listAdminRechargeOrders).mockResolvedValue({
      items: [],
      total: 0,
      limit: 3,
      offset: 0,
    });
    vi.mocked(adminApi.listAdminWalletTransactions).mockResolvedValue({
      items: [],
      total: 0,
      limit: 3,
      offset: 0,
    });
    vi.mocked(adminApi.listDevices).mockResolvedValue({
      items: [],
      total: 0,
      limit: 3,
      offset: 0,
    });
    vi.mocked(adminApi.listCustomerSessions).mockResolvedValue({
      items: [],
      total: 0,
      limit: 3,
      offset: 0,
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
      items: mockCustomers,
      total: 2,
      limit: 20,
      offset: 0,
    });

    render(<CustomersPage />);

    await waitFor(() => {
      expect(screen.getByText("customer-1")).toBeInTheDocument();
      expect(screen.getByText("customer-2")).toBeInTheDocument();
    });

    expect(screen.getByText(/共 2 位客户/)).toBeInTheDocument();
    expect(screen.getByText("5 / 8")).toBeInTheDocument();
    expect(
      screen.getByText("1", { selector: "td[data-label='待关注']" }),
    ).toBeInTheDocument();
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
      "—",
      "user-1",
      "ABC-123",
      // 与 formatDateTime 的展示契约一致：固定 Asia/Shanghai，
      // 否则期望值随 runner 时区漂移（CI 为 UTC，本地为 +8）。
      new Date(mockCustomers[0].created_at).toLocaleString("zh-CN", {
        hour12: false,
        timeZone: "Asia/Shanghai",
      }),
      "活跃",
      "0/2",
      "0 秒",
      "0 秒",
      "5 秒",
      "5 / 8 · 1 失败 · 1 进行中",
      "1",
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
      items: mockCustomers,
      total: 50,
      limit: 20,
      offset: 0,
    });

    render(<CustomersPage />);

    await waitFor(() => {
      expect(screen.getByText("第 1 / 3 页（共 50 位）")).toBeInTheDocument();
    });

    const nextPageButton = screen.getByRole("button", { name: "下一页" });
    fireEvent.click(nextPageButton);

    expect(adminApi.listCustomers).toHaveBeenCalledWith({
      limit: 20,
      offset: 20,
    });
  });

  it("supports filtering by username", async () => {
    vi.mocked(adminApi.listCustomers).mockResolvedValue({
      items: [],
      total: 0,
      limit: 20,
      offset: 0,
    });

    render(<CustomersPage />);

    const filterInput = screen.getByPlaceholderText("按用户名筛选");
    fireEvent.change(filterInput, { target: { value: "customer-1" } });
    fireEvent.click(screen.getByRole("button", { name: "筛选" }));

    expect(adminApi.listCustomers).toHaveBeenCalledWith({
      limit: 20,
      offset: 0,
      username_filter: "customer-1",
    });
  });

  it("exports the same date, status and balance filters as the list", async () => {
    vi.mocked(adminApi.listCustomers).mockResolvedValue({
      items: [],
      total: 0,
      limit: 20,
      offset: 0,
    });
    render(<CustomersPage />);

    fireEvent.change(screen.getByLabelText("注册起始"), {
      target: { value: "2026-09-01" },
    });
    fireEvent.change(screen.getByLabelText("注册截止"), {
      target: { value: "2026-09-05" },
    });
    fireEvent.change(screen.getByLabelText("最低余额"), {
      target: { value: "10" },
    });
    fireEvent.change(screen.getByLabelText("最高余额"), {
      target: { value: "500" },
    });
    fireEvent.change(screen.getByLabelText("客户状态"), {
      target: { value: "active" },
    });
    fireEvent.change(screen.getByPlaceholderText("按用户名筛选"), {
      target: { value: "customer-1" },
    });
    fireEvent.click(screen.getByRole("button", { name: "筛选" }));
    fireEvent.click(screen.getByRole("button", { name: "导出列表 CSV" }));

    expect(api.downloadCustomersCsv).toHaveBeenCalledWith({
      status: "active",
      username: "customer-1",
      createdFrom: "2026-09-01",
      createdTo: "2026-09-05",
      balanceMin: 10,
      balanceMax: 500,
    });
  });

  it("opens the adjustment history for the selected customer", async () => {
    vi.mocked(adminApi.listCustomers).mockResolvedValue({
      items: [
        {
          user_id: "user-1",
          username: "customer-1",
          created_at: "2026-08-24T10:00:00Z",
          activation_code: "ABC-123",
          status: "active",
        },
      ],
      total: 1,
      limit: 20,
      offset: 0,
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

  it("opens a focused customer detail and returns to the filtered list", async () => {
    const onOpenDevices = vi.fn();
    vi.mocked(adminApi.listCustomers).mockResolvedValue({
      items: [
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
      limit: 20,
      offset: 0,
    });

    render(<CustomersPage onOpenDevices={onOpenDevices} />);

    fireEvent.change(await screen.findByPlaceholderText("按用户名筛选"), {
      target: { value: "customer-1" },
    });
    fireEvent.click(screen.getByRole("button", { name: "筛选" }));
    await waitFor(() => {
      expect(adminApi.listCustomers).toHaveBeenLastCalledWith(
        expect.objectContaining({ username_filter: "customer-1" }),
      );
    });
    const toggle = await screen.findByRole("button", { name: "展开详情" });
    expect(toggle).toHaveAttribute("aria-expanded", "false");

    fireEvent.click(toggle);

    expect(screen.queryByRole("table", { name: "客户列表" })).toBeNull();
    const detailPanel = screen
      .getByRole("heading", { name: "customer-1" })
      .closest("div");
    expect(detailPanel).not.toBeNull();
    expect(
      screen.getByRole("region", { name: "客户核心指标" }),
    ).toBeInTheDocument();
    expect(screen.getByText("累计消耗")).toBeInTheDocument();
    expect(
      screen.getByRole("button", {
        name: "调账历史",
      }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "← 返回客户列表" }),
    ).toBeInTheDocument();
    await waitFor(() => {
      expect(adminApi.listAdminRechargeOrders).toHaveBeenCalledWith({
        userId: "user-1",
        limit: 3,
        offset: 0,
      });
      expect(adminApi.listAdminWalletTransactions).toHaveBeenCalledWith({
        userId: "user-1",
        limit: 3,
        offset: 0,
      });
      expect(adminApi.listDevices).toHaveBeenCalledWith({
        userId: "user-1",
        limit: 3,
        offset: 0,
      });
      expect(adminApi.listCustomerSessions).toHaveBeenCalledWith("user-1", {
        limit: 3,
        offset: 0,
      });
      expect(adminApi.listAdminAdjustments).toHaveBeenCalledWith("user-1", {
        limit: 3,
        offset: 0,
        sort: "desc",
      });
    });
    fireEvent.click(screen.getByRole("button", { name: "查看设备" }));
    expect(onOpenDevices).toHaveBeenCalledWith("user-1");

    fireEvent.click(screen.getByRole("button", { name: "← 返回客户列表" }));

    expect(
      await screen.findByRole("table", { name: "客户列表" }),
    ).toBeInTheDocument();
    expect(screen.getByPlaceholderText("按用户名筛选")).toHaveValue(
      "customer-1",
    );
  });

  it("loads and updates the customer's effective recharge price", async () => {
    vi.mocked(adminApi.listCustomers).mockResolvedValue({
      items: [
        {
          user_id: "user-1",
          username: "customer-1",
          created_at: "2026-08-24T10:00:00Z",
          activation_code: "ABC-123",
          status: "active",
        },
      ],
      total: 1,
      limit: 20,
      offset: 0,
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

    expect(await screen.findByText(/当前 ¥10.00 \/ 秒/)).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("售价（元/秒）"), {
      target: { value: "8.8" },
    });
    fireEvent.click(screen.getByRole("button", { name: "保存客户售价" }));

    // 改价确认对话框：原因必填，随写请求一起提交。
    await screen.findByRole("dialog", { name: "修改客户售价" });
    fireEvent.change(screen.getByLabelText("操作原因"), {
      target: { value: "客户合同价" },
    });
    fireEvent.click(screen.getByRole("button", { name: "确认保存" }));

    await waitFor(() => {
      expect(adminApi.updateCustomerUnitPrice).toHaveBeenCalledWith(
        "user-1",
        880,
        "客户合同价",
      );
    });
    expect(await screen.findByText("客户售价已保存")).toBeInTheDocument();
  });

  it("grants free credits through the audited FREE_GRANT adjustment", async () => {
    // 免费秒数发放（FREE_GRANT / 054）：来源单号必填，高危对话框确认，
    // 服务端调用走 T23 调账闭环且 source_document_type 固定为 FREE_GRANT。
    vi.mocked(adminApi.listCustomers).mockResolvedValue({
      items: [
        {
          user_id: "user-1",
          username: "customer-1",
          created_at: "2026-08-24T10:00:00Z",
          activation_code: "ABC-123",
          status: "active",
        },
      ],
      total: 1,
      limit: 20,
      offset: 0,
    });
    vi.mocked(adminApi.createCustomerAdjustment).mockResolvedValue({
      adjustment_id: "adj-free-1",
      order_id: "order-free-1",
      credits: "10",
      amount_fen: "0",
      pricing_scope: "CUSTOMER_STANDARD",
      wallet_balance_after: 60,
      source_document_type: "FREE_GRANT",
      source_document_ref: "PROMO-2026-09-001",
      request_id: "request-free-grant",
    });

    render(<CustomersPage />);
    fireEvent.click(await screen.findByRole("button", { name: "展开详情" }));

    fireEvent.change(await screen.findByLabelText("发放秒数"), {
      target: { value: "10" },
    });
    fireEvent.change(screen.getByLabelText("来源单号"), {
      target: { value: "PROMO-2026-09-001" },
    });
    fireEvent.click(screen.getByRole("button", { name: "发放免费秒数" }));

    // 高危对话框：原因必填 + 我已知晓勾选。
    await screen.findByRole("dialog", { name: "发放免费秒数" });
    fireEvent.click(screen.getByRole("button", { name: "确认发放" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "请填写操作原因",
    );
    expect(adminApi.createCustomerAdjustment).not.toHaveBeenCalled();

    fireEvent.change(screen.getByLabelText("操作原因"), {
      target: { value: "新客活动发放" },
    });
    fireEvent.click(screen.getByLabelText("我已知晓该操作的影响"));
    fireEvent.click(screen.getByRole("button", { name: "确认发放" }));

    await waitFor(() => {
      expect(adminApi.createCustomerAdjustment).toHaveBeenCalledWith(
        "user-1",
        {
          sourceDocumentType: "FREE_GRANT",
          sourceDocumentRef: "PROMO-2026-09-001",
          credits: 10,
        },
        "新客活动发放",
        expect.any(String),
      );
    });
    expect(await screen.findByText(/已发放 10 秒免费时长/)).toBeInTheDocument();
  });

  it("keeps customer pricing read-only for auditors", async () => {
    vi.mocked(adminApi.listCustomers).mockResolvedValue({
      items: [
        {
          user_id: "user-1",
          username: "customer-1",
          created_at: "2026-08-24T10:00:00Z",
          activation_code: "ABC-123",
          status: "active",
        },
      ],
      total: 1,
      limit: 20,
      offset: 0,
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
      items: [],
      total: 0,
      limit: 20,
      offset: 0,
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
      items: mockCustomers,
      total: 2,
      limit: 20,
      offset: 0,
    });

    render(<CustomersPage />);

    await waitFor(() => {
      expect(screen.getByText("活跃")).toBeInTheDocument();
      expect(screen.getByText("已暂停")).toBeInTheDocument();
    });
  });
});
