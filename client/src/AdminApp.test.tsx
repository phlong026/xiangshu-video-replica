import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AdminApp, adminRouteFromHash } from "./AdminApp";
import { getAdminCsrfToken, SESSION_EXPIRED_EVENT } from "./api";
import { getCustomerPricing } from "./api.admin";

const SERVICE_KEY_TEXT = ["service", "key"].join("-");
const MASKED_SERVICE_KEY = ["********", "cret"].join("");
const MASKED_STORAGE_SECRET = ["********", "5678"].join("");
const CSRF_TOKEN_TEXT = ["csrf", "token", "admin"].join("-");

const adminActor = {
  user_id: "admin-1",
  username: "admin",
  display_name: "管理员一号",
  role: "admin",
  // 技术配置 / 团队与权限页签仅超管可见（方案 P1 设置收拢），测试会话
  // 直接给超管标记，普通管理员路径由 SystemSettingsPage 测试单独覆盖。
  is_super_admin: true,
};

// 五个角色模板的权限点（整改 T41）。与迁移 20261014T1200 预置的模板逐字一致——
// 服务端测试钉住同一份，这里钉住「同样的权限点在界面上长什么样」。
const READ_POINTS = [
  "business.read",
  "funds.read",
  "customer.read",
  "generation.read",
  "content.read",
  "settings.read",
  "audit.read",
];
const WRITE_POINTS = [
  "funds.write",
  "settings.write",
  "customer.write",
  "content.write",
  "export",
];
const TEMPLATE_PERMISSIONS = {
  super_admin: [...READ_POINTS, ...WRITE_POINTS],
  operations: [...READ_POINTS, ...WRITE_POINTS],
  finance: [
    "business.read",
    "funds.read",
    "customer.read",
    "settings.read",
    "funds.write",
    "settings.write",
    "export",
  ],
  content: [
    "generation.read",
    "content.read",
    "customer.read",
    "content.write",
  ],
  auditor: [...READ_POINTS, "export"],
} as const;

const adminSession = {
  session_id: "session-1",
  expires_at: "2026-08-28T00:00:00+00:00",
  last_activity_at: "2026-08-27T16:00:00+00:00",
  csrf_token: CSRF_TOKEN_TEXT,
  actor: adminActor,
  auth_method: "password",
};

function jsonResponse(payload: unknown, status = 200) {
  return Promise.resolve({
    ok: status >= 200 && status < 300,
    status,
    json: async () => payload,
  });
}

function blobResponse() {
  return Promise.resolve({
    ok: true,
    status: 200,
    headers: new Headers({
      "X-Export-Total": "1",
      "X-Export-Returned": "1",
      "X-Export-Truncated": "false",
    }),
    blob: async () => new Blob(["id\n1"], { type: "text/csv" }),
  });
}

const accountsPage = {
  items: [
    {
      id: "user-1",
      username: "operator-1",
      display_name: "运营一号",
      role: "employee",
      is_active: true,
      available_credits: 18,
      reserved_credits: 2,
      active_token_count: 1,
    },
  ],
  total: 1,
  limit: 50,
  offset: 0,
};

const ordersPage = {
  items: [
    {
      id: "order-1",
      user_id: "user-1",
      username: "operator-1",
      display_name: "运营一号",
      order_no: "202608190001",
      status: "PENDING",
      amount_fen: 10000,
      credits: 10,
      channel: "alipay",
      provider_trade_no: null,
      created_at: "2026-08-19 10:00:00",
      paid_at: null,
    },
  ],
  total: 1,
  limit: 50,
  offset: 0,
};

const transactionsPage = {
  items: [
    {
      id: "tx-1",
      user_id: "user-1",
      username: "operator-1",
      type: "CHARGE",
      available_delta: 10,
      reserved_delta: 0,
      recharge_order_id: "order-1",
      task_id: null,
      billing_round: null,
      created_at: "2026-08-19 10:01:00",
    },
  ],
  total: 1,
  limit: 50,
  offset: 0,
};

const reconciliation = {
  wallet_count: 1,
  wallet_mismatch_count: 0,
  paid_order_without_charge_count: 2,
  charge_without_paid_order_count: 1,
  pending_order_count: 1,
};

const settings = {
  providers: {
    metaso: { provider: "metaso", configured: false, config: {} },
    apilio: { provider: "apilio", configured: false, config: {} },
    cos: {
      provider: "cos",
      configured: true,
      config: {
        access_key_id: "********1234",
        secret_access_key: MASKED_STORAGE_SECRET,
        bucket: "private-materials",
        region: "ap-shanghai",
      },
    },
    deepseek: { provider: "deepseek", configured: false, config: {} },
  },
  runtime: {
    max_generation_count_per_batch: 4,
    max_concurrent_h3_tasks: 2,
    active_storage_provider: "cos",
  },
  billing: {
    internal_base_unit_price_fen: 1000,
    charged_unit_price_fen: 1000,
    oral_unit_price_fen: 1000,
    min_recharge_fen: 10000,
    recharge_step_fen: 1000,
  },
  zpay: {
    provider: "zpay",
    configured: true,
    config: {
      pid: "merchant-1",
      key: "********cret",
      enabled_channels: "alipay,wxpay",
    },
  },
  deployment: {
    gateway_url: "https://zpayz.cn/submit.php",
    notify_url: "https://internal.example/api/payments/zpay/notify",
    return_url: "https://internal.example/api/payments/zpay/return",
  },
};

function installFetch(options?: {
  session?: "valid" | "missing";
  failedTasks?: number;
  reconciliationProblems?: number;
  pendingOrders?: number;
  openTickets?: number;
  generationTotal?: number;
  /** 以只读的 auditor 角色登录；默认是超级管理员。 */
  role?: "admin" | "auditor";
  isSuperAdmin?: boolean;
  /** 发布成效开关；默认关闭，菜单不出现该入口。 */
  publishStatsEnabled?: boolean;
  /** 以某个角色模板登录（带该模板的权限点）；不传则沿用旧夹具（无 permissions）。 */
  template?: keyof typeof TEMPLATE_PERMISSIONS;
}) {
  const sessionState = options?.session ?? "missing";
  const templateActor = options?.template
    ? {
        ...adminActor,
        role: options.template === "auditor" ? "auditor" : "admin",
        is_super_admin: options.template === "super_admin",
        template: options.template,
        permissions: [...TEMPLATE_PERMISSIONS[options.template]],
      }
    : null;
  const activeSession = templateActor
    ? { ...adminSession, actor: templateActor }
    : options?.role === "auditor"
      ? {
          ...adminSession,
          actor: { ...adminActor, role: "auditor", is_super_admin: false },
        }
      : {
          ...adminSession,
          actor: {
            ...adminActor,
            is_super_admin: options?.isSuperAdmin ?? true,
          },
        };
  const fetchMock = vi.fn((url: string, requestInit?: RequestInit) => {
    if (
      url.endsWith("/api/control/admin/session") &&
      (!requestInit?.method || requestInit.method === "GET")
    ) {
      if (sessionState === "valid") {
        return jsonResponse(activeSession);
      }
      return jsonResponse(
        {
          detail: {
            code: "ADMIN_SESSION_INVALID",
            message: "Admin session is missing, revoked or invalid.",
          },
        },
        401,
      );
    }
    if (url.endsWith("/api/control/admin/session/password")) {
      return jsonResponse(activeSession, 201);
    }
    if (url.endsWith("/api/control/admin/session/exchange")) {
      return jsonResponse(activeSession, 201);
    }
    if (
      url.endsWith("/api/control/admin/password") &&
      requestInit?.method === "PUT"
    ) {
      return jsonResponse(undefined, 204);
    }
    if (
      url.endsWith("/api/control/admin/session") &&
      requestInit?.method === "DELETE"
    ) {
      return jsonResponse(undefined, 204);
    }
    if (url.includes("/api/control/search?")) {
      const q = new URL(url).searchParams.get("q");
      return jsonResponse({
        items:
          q === "TASK-9"
            ? [
                {
                  type: "task",
                  id: "TASK-9",
                  label: "视频任务 TASK-9",
                  detail: "生成记录",
                },
              ]
            : [],
      });
    }
    if (url.endsWith("/api/control/publish-stats/enabled")) {
      return jsonResponse({ enabled: options?.publishStatsEnabled ?? false });
    }
    if (url.includes("/api/control/accounts?")) {
      return jsonResponse(accountsPage);
    }
    if (url.endsWith("/api/control/dashboard/summary")) {
      return jsonResponse({
        today: {
          generated: 0,
          succeeded: 0,
          failed: 0,
          online_devices: 0,
          active_customers: 0,
          recharge_fen: 0,
        },
        trend: [],
        todos: {
          pending_pairings: 0,
          failed_tasks_7d: options?.failedTasks ?? 0,
          reconciliation_problems: options?.reconciliationProblems ?? 0,
          pending_orders: options?.pendingOrders ?? 0,
          open_tickets: options?.openTickets ?? 0,
          expiring_codes_7d: 0,
        },
        device_slots: { bound: 0, total: 0 },
      });
    }
    if (url.includes("/api/control/recharge-orders?")) {
      return jsonResponse(ordersPage);
    }
    if (url.includes("/api/control/wallet-transactions?")) {
      return jsonResponse(transactionsPage);
    }
    if (url.includes("/api/control/customers?")) {
      return jsonResponse({
        items: [],
        total: 0,
        limit: 20,
        offset: 0,
      });
    }
    if (url.includes("/api/control/generation-records/summary")) {
      return jsonResponse({ total: 1, counts: [], failure_reasons: [] });
    }
    if (url.includes("/api/control/generation-records?")) {
      return jsonResponse({
        items: [
          {
            record_id: "first-frame-1",
            record_type: "FIRST_FRAME_IMAGE",
            operation: "GENERATE",
            user_id: "user-1",
            username: "customer-1",
            display_name: "客户一",
            project_id: "project-1",
            project_name: "演示项目",
            status: "SUCCEEDED",
            provider: "apilio",
            model: "gpt-image-2",
            provider_cost: null,
            provider_cost_status: "UNAVAILABLE",
            record_data_status: "VALID",
            charged_credits: 0,
            result_reference: "version-1",
            error_code: null,
            created_at: "2026-09-02T10:00:00Z",
            completed_at: "2026-09-02T10:01:00Z",
          },
        ],
        total: options?.generationTotal ?? 1,
        limit: 50,
        offset: 0,
      });
    }
    if (url.includes("/api/control/billing-reconciliation/items?")) {
      return jsonResponse({ items: [], total: 0, limit: 20, offset: 0 });
    }
    if (url.endsWith("/api/control/billing-reconciliation")) {
      return jsonResponse(reconciliation);
    }
    if (
      (url.endsWith("/api/control/settings") ||
        url.endsWith("/api/control/settings/customer-payments")) &&
      !requestInit?.method
    ) {
      return jsonResponse(settings);
    }
    if (url.endsWith("/api/control/settings/customer-payments/zpay")) {
      return jsonResponse(settings.zpay);
    }
    if (
      url.endsWith("/api/control/settings/billing") ||
      url.endsWith("/api/control/settings/customer-payments/billing")
    ) {
      return jsonResponse(settings.billing);
    }
    if (url.endsWith("/api/control/settings/h3-accounts")) {
      return jsonResponse({
        accounts: [],
        total_concurrency: 0,
        managed: false,
      });
    }
    if (url.endsWith("/api/control/settings/providers/apilio")) {
      return jsonResponse({
        provider: "apilio",
        configured: true,
        config: { api_key: MASKED_SERVICE_KEY },
      });
    }
    if (
      url.endsWith("/api/control/settings/providers/metaso/connection-test")
    ) {
      return jsonResponse({
        status: "configured_only",
        provider: "metaso",
        test_kind: "connection",
      });
    }
    if (url.endsWith("/api/control/settings/runtime")) {
      return jsonResponse(settings.runtime);
    }
    if (url.endsWith("/api/control/settings/queue-mode")) {
      if (requestInit?.method === "PATCH") {
        return jsonResponse({ fair_queue_enabled: true });
      }
      return jsonResponse({ fair_queue_enabled: false });
    }
    if (url.endsWith("/api/control/settings/recharge-packages")) {
      // 充值套餐管理：默认无档位（用例只验证页面可渲染）。
      return jsonResponse({ items: [] });
    }
    if (url.endsWith("/api/control/recharge-orders/202608190001/sync")) {
      return jsonResponse({ ...ordersPage.items[0], status: "PAID" });
    }
    if (
      new URL(url).pathname.endsWith("/api/control/recharge-orders.csv") ||
      new URL(url).pathname.endsWith("/api/control/wallet-transactions.csv")
    ) {
      return blobResponse();
    }
    if (url.includes("/api/control/devices?")) {
      return jsonResponse({ items: [], total: 0, limit: 100, offset: 0 });
    }
    if (url.includes("/api/control/customer-sessions/live")) {
      return jsonResponse({ items: [], total: 0, limit: 20, offset: 0 });
    }
    throw new Error(`unexpected request: ${url}`);
  });
  vi.stubGlobal("fetch", fetchMock);
  vi.spyOn(URL, "createObjectURL").mockReturnValue("blob:test");
  vi.spyOn(URL, "revokeObjectURL").mockImplementation(() => undefined);
  vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(
    () => undefined,
  );
  return fetchMock;
}

async function signInWithPassword() {
  fireEvent.change(await screen.findByLabelText("管理员账号"), {
    target: { value: "admin" },
  });
  fireEvent.change(screen.getByLabelText("管理员密码"), {
    target: { value: "Admin Login Passphrase 2026!" },
  });
  fireEvent.click(screen.getByRole("button", { name: "登录后台" }));
  await waitFor(() => {
    expect(
      screen.queryByRole("navigation", { name: "管理端导航" }) ??
        screen.queryByRole("button", { name: "展开导航" }),
    ).toBeTruthy();
  });
}

describe("AdminApp", () => {
  it("restores a recovery session to password setup without loading business pages", async () => {
    const fetchMock = vi.fn(() =>
      jsonResponse({ ...adminSession, auth_method: "exchange" }),
    );
    vi.stubGlobal("fetch", fetchMock);
    render(<AdminApp />);
    expect(await screen.findByLabelText("新管理员密码")).toBeVisible();
    expect(
      screen.queryByRole("heading", { name: "总览" }),
    ).not.toBeInTheDocument();
    expect(fetchMock.mock.calls).toHaveLength(1);
  });
  beforeEach(() => {
    window.location.hash = "";
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
    window.location.hash = "";
  });

  it("七个管理模块地址均可恢复且未知地址回总览", () => {
    for (const tab of [
      "overview",
      "analytics",
      "funds",
      "customersMgmt",
      "generationRecords",
      "auditCenter",
      "systemSettings",
    ] as const) {
      expect(adminRouteFromHash(`#admin/${tab}`).tab).toBe(tab);
    }
    expect(adminRouteFromHash("#admin/unknown").tab).toBe("overview");
  });

  it.each(["admin", "auditor"] as const)(
    "%s 从总览对账待办进入异常页",
    async (role) => {
      installFetch({ session: "valid", role, reconciliationProblems: 3 });
      render(<AdminApp />);
      const todos = await screen.findByRole("region", { name: "待办事项" });
      fireEvent.click(
        within(todos).getByRole("button", {
          name: role === "auditor" ? "去查看" : "去处理",
        }),
      );
      expect(
        await screen.findByRole("tablist", { name: "对账异常类型" }),
      ).toBeInTheDocument();
      expect(window.location.hash).toBe("#admin/funds?intent=recon");
    },
  );

  it("资金中心子页签写入地址，刷新后仍停在该页签", async () => {
    window.history.replaceState(null, "", "/admin#admin/funds?sub=refunds");
    installFetch({ session: "valid" });
    const { unmount } = render(<AdminApp />);
    expect(
      await screen.findByRole("tab", { name: "退款管理", selected: true }),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole("tab", { name: "人工调整" }));
    expect(window.location.hash).toBe("#admin/funds?sub=adjustments");
    unmount();

    // 模拟刷新：同一地址重新挂载。
    render(<AdminApp />);
    expect(
      await screen.findByRole("tab", { name: "人工调整", selected: true }),
    ).toBeInTheDocument();
  });

  it("全局待办落到带筛选的清单：待支付订单、未结案工单", async () => {
    installFetch({ session: "valid", pendingOrders: 2, openTickets: 3 });
    render(<AdminApp />);
    const todos = await screen.findByRole("region", { name: "待办事项" });
    const orders = within(todos).getByText("待支付充值订单").closest("li");
    fireEvent.click(
      within(orders as HTMLElement).getByRole("button", { name: "去处理" }),
    );
    expect(window.location.hash).toBe("#admin/funds?sub=orders&status=PENDING");
    expect(
      await screen.findByRole("tab", { name: "收款订单", selected: true }),
    ).toBeInTheDocument();
    await waitFor(() =>
      expect(
        vi.mocked(fetch).mock.calls.some(([url]) => {
          const parsed = new URL(String(url));
          return (
            parsed.pathname.endsWith("/recharge-orders") &&
            parsed.searchParams.get("status") === "PENDING"
          );
        }),
      ).toBe(true),
    );

    fireEvent.click(screen.getByRole("link", { name: "总览" }));
    const again = await screen.findByRole("region", { name: "待办事项" });
    const tickets = within(again).getByText("工单未结案").closest("li");
    fireEvent.click(
      within(tickets as HTMLElement).getByRole("button", { name: "去处理" }),
    );
    expect(window.location.hash).toBe(
      "#admin/supportTickets?status=unresolved",
    );
    await waitFor(() =>
      expect(
        vi.mocked(fetch).mock.calls.some(([url]) => {
          const parsed = new URL(String(url));
          return (
            parsed.pathname.endsWith("/support-tickets") &&
            parsed.searchParams.get("status") === "unresolved"
          );
        }),
      ).toBe(true),
    );
  });

  it("刷新资金对账地址保留异常清单意图", () => {
    expect(adminRouteFromHash("#admin/funds?intent=recon")).toEqual({
      tab: "funds",
      intent: "recon",
    });
  });

  it("恢复拆解失败意图时同时带上类型与状态筛选", async () => {
    window.history.replaceState(
      null,
      "",
      "/admin#admin/generationRecords?intent=analysisFailures",
    );
    const fetchMock = installFetch({ session: "valid" });
    render(<AdminApp />);

    expect(await screen.findByLabelText("生成类型")).toHaveValue("ANALYSIS");
    // 5 组口径：FAILED 意图归一到「失败」组。
    expect(screen.getByLabelText("生成状态")).toHaveValue(
      "FAILED,CANCELED,CANCELLED,ARCHIVE_FAILED",
    );
    expect(screen.getByLabelText("失败阶段")).toHaveValue("");
    expect(
      fetchMock.mock.calls.some(([url]) =>
        String(url).includes("record_type=ANALYSIS"),
      ),
    ).toBe(true);
  });

  it("恢复总览加款意图时带到客户管理页的加款引导", async () => {
    window.history.replaceState(
      null,
      "",
      "/admin#admin/customersMgmt?intent=customerAdjustments",
    );
    const fetchMock = installFetch({ session: "valid" });
    render(<AdminApp />);

    expect(
      await screen.findByRole("heading", { level: 1, name: "客户列表" }),
    ).toBeInTheDocument();
    expect(
      await screen.findByText(/赠送积分在客户详情内完成/),
    ).toBeInTheDocument();
    expect(
      fetchMock.mock.calls.some(([url]) =>
        String(url).includes("/api/control/customers?"),
      ),
    ).toBe(true);
  });

  it("同一客户管理模块切换意图时同步收起加款引导", async () => {
    window.history.replaceState(
      null,
      "",
      "/admin#admin/customersMgmt?intent=customerAdjustments",
    );
    installFetch({ session: "valid" });
    render(<AdminApp />);

    expect(
      await screen.findByText(/赠送积分在客户详情内完成/),
    ).toBeInTheDocument();

    // 从侧栏点回「客户管理」= 不带 intent，引导必须跟着 intent 一起消失。
    fireEvent.click(screen.getByRole("link", { name: "客户列表" }));
    expect(window.location.hash).toMatch(/^#admin\/customersMgmt(?:\?|$)/);
    expect(window.location.hash).not.toContain("intent=");
    await waitFor(() =>
      expect(screen.queryByText(/赠送积分在客户详情内完成/)).toBeNull(),
    );

    await act(async () => window.history.back());
    await waitFor(() => expect(window.location.hash).toContain("intent="));
    expect(
      await screen.findByText(/赠送积分在客户详情内完成/),
    ).toBeInTheDocument();
  });

  it("刷新和前进后退恢复模块及失败记录筛选意图", async () => {
    window.history.replaceState(
      null,
      "",
      "/admin#admin/generationRecords?intent=failedGenerationRecords",
    );
    const fetchMock = installFetch({ session: "valid" });
    render(<AdminApp />);

    expect(
      await screen.findByRole("heading", { level: 1, name: "生成记录" }),
    ).toBeInTheDocument();
    // 5 组口径：FAILED 意图归一到「失败」组。
    expect(screen.getByLabelText("生成状态")).toHaveValue(
      "FAILED,CANCELED,CANCELLED,ARCHIVE_FAILED",
    );
    expect(
      fetchMock.mock.calls.some(([url]) =>
        String(url).includes("status=FAILED"),
      ),
    ).toBe(true);

    window.history.pushState(null, "", "/admin#admin/funds");
    window.dispatchEvent(new PopStateEvent("popstate"));
    expect(
      await screen.findByRole("heading", { level: 1, name: "资金中心" }),
    ).toBeInTheDocument();
  });

  it("同一生成记录模块切换意图时同步清空筛选和分页", async () => {
    window.history.replaceState(
      null,
      "",
      "/admin#admin/generationRecords?intent=failedGenerationRecords",
    );
    const fetchMock = installFetch({
      session: "valid",
      generationTotal: 101,
    });
    render(<AdminApp />);

    expect(await screen.findByLabelText("生成状态")).toHaveValue(
      "FAILED,CANCELED,CANCELLED,ARCHIVE_FAILED",
    );
    fireEvent.change(screen.getByLabelText("生成账号"), {
      target: { value: "customer-1" },
    });
    fireEvent.click(screen.getByRole("button", { name: "查询" }));
    fireEvent.click(await screen.findByRole("button", { name: "下一页" }));
    await waitFor(() =>
      expect(
        fetchMock.mock.calls.some(([url]) =>
          String(url).includes("offset=50&username=customer-1&status=FAILED"),
        ),
      ).toBe(true),
    );

    fireEvent.click(screen.getByRole("link", { name: "生成记录" }));
    expect(window.location.hash).toBe("#admin/generationRecords");
    expect(screen.getByLabelText("生成账号")).toHaveValue("");
    expect(screen.getByLabelText("生成状态")).toHaveValue("");
    await waitFor(() =>
      expect(
        fetchMock.mock.calls.some(([url]) =>
          String(url).endsWith(
            "/api/control/generation-records?limit=50&offset=0",
          ),
        ),
      ).toBe(true),
    );

    await act(async () => window.history.back());
    await waitFor(() => expect(window.location.hash).toContain("intent="));
    // 5 组口径：FAILED 意图归一到「失败」组。
    expect(screen.getByLabelText("生成状态")).toHaveValue(
      "FAILED,CANCELED,CANCELLED,ARCHIVE_FAILED",
    );
    await act(async () => window.history.forward());
    await waitFor(() =>
      expect(window.location.hash).toBe("#admin/generationRecords"),
    );
    expect(screen.getByLabelText("生成状态")).toHaveValue("");
  });

  it("starts at the account-password gate before exposing control navigation", async () => {
    const fetchMock = installFetch();

    render(<AdminApp />);

    expect(await screen.findByLabelText("管理员账号")).toBeInTheDocument();
    expect(screen.getByLabelText("管理员密码")).toBeInTheDocument();
    expect(screen.queryByRole("navigation", { name: "管理端导航" })).toBeNull();
    expect(
      fetchMock.mock.calls.some(([url]) =>
        String(url).includes("/api/control/accounts?"),
      ),
    ).toBe(false);
  });

  it("keeps order operations to sync and CSV export", async () => {
    const fetchMock = installFetch();
    render(<AdminApp />);
    await signInWithPassword();

    fireEvent.click(screen.getByRole("link", { name: "资金中心" }));
    fireEvent.click(screen.getByRole("tab", { name: "收款订单" }));

    expect(
      await screen.findByText(
        (_, element) => element?.textContent === "待支付订单 1",
      ),
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        (_, element) => element?.textContent === "已支付未入账 2",
      ),
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        (_, element) => element?.textContent === "入账但订单未支付 1",
      ),
    ).toBeInTheDocument();

    // 查单同步先经"原因必填"确认（A4 写契约），再发请求。
    fireEvent.click(await screen.findByRole("button", { name: "查单补单" }));
    await screen.findByRole("dialog");
    fireEvent.change(screen.getByLabelText("操作原因"), {
      target: { value: "客服反馈未到账" },
    });
    fireEvent.click(screen.getByRole("button", { name: "确认查单" }));
    await screen.findByText("订单 202608190001 状态已同步。");

    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "导出充值订单 CSV" }),
      ).toBeEnabled(),
    );
    fireEvent.click(screen.getByRole("button", { name: "导出充值订单 CSV" }));
    expect(
      screen.queryByRole("button", { name: "导出账务流水 CSV" }),
    ).toBeNull();

    await waitFor(() =>
      expect(
        fetchMock.mock.calls.some(([url]) =>
          String(url).endsWith(
            "/api/control/recharge-orders/202608190001/sync",
          ),
        ),
      ).toBe(true),
    );
    // 查单补单现在统一覆盖微信订单（后端按渠道分派网关），这里只确认
    // 没有出现「改余额」这类直接改账的入口。
    expect(screen.queryByRole("button", { name: /改余额/ })).toBeNull();
    fireEvent.click(screen.getByRole("tab", { name: "积分流水" }));
    const walletExport = await screen.findByRole("button", {
      name: "导出账务流水 CSV",
    });
    await waitFor(() => expect(walletExport).toBeEnabled());
    fireEvent.click(walletExport);
    await waitFor(() => {
      for (const pathname of [
        "/api/control/recharge-orders.csv",
        "/api/control/wallet-transactions.csv",
      ]) {
        expect(
          fetchMock.mock.calls.some(
            ([url]) => new URL(String(url)).pathname === pathname,
          ),
        ).toBe(true);
      }
    });
  });

  it("审计员看不到真正的资金写操作，但可以导出（T19）", async () => {
    installFetch({ role: "auditor" });
    render(<AdminApp />);
    await signInWithPassword();

    fireEvent.click(screen.getByRole("link", { name: "资金中心" }));
    fireEvent.click(screen.getByRole("tab", { name: "收款订单" }));
    expect(
      await screen.findByText(
        (_, element) => element?.textContent === "待支付订单 1",
      ),
    ).toBeInTheDocument();

    // 查单补单是写级动作：只读角色看不到入口，而不是点了才 403。
    expect(screen.queryByRole("button", { name: "查单补单" })).toBeNull();
    // 整表导出对审计员开放（决策 D2）：服务端每次导出留审计并限流。
    expect(
      screen.getByRole("button", { name: "导出充值订单 CSV" }),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole("tab", { name: "积分流水" }));
    await screen.findByRole("table", { name: "账务流水列表" });
    expect(
      screen.getByRole("button", { name: "导出账务流水 CSV" }),
    ).toBeInTheDocument();
  });

  it("saves ZPay and price settings while deployment URLs stay server-owned", async () => {
    const fetchMock = installFetch();
    render(<AdminApp />);
    await signInWithPassword();

    fireEvent.click(screen.getByRole("link", { name: "系统设置" }));
    // ZPay 商户配置在「收款设置」页签（方案 P1：价格与收款分开）。
    fireEvent.click(screen.getByRole("tab", { name: "收款设置" }));
    expect(await screen.findByDisplayValue("merchant-1")).toBeInTheDocument();
    expect(screen.getByText("********cret")).toBeInTheDocument();
    expect(screen.queryByLabelText("网关地址")).toBeNull();
    expect(screen.queryByLabelText("异步回调地址")).toBeNull();
    expect(screen.queryByLabelText("同步返回地址")).toBeNull();
    expect(
      screen.getByText("支付接口地址由系统自动配置，无需填写。"),
    ).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("ZPay 商户 PID"), {
      target: { value: "merchant-2" },
    });
    fireEvent.change(screen.getByLabelText("新商户密钥"), {
      target: { value: "" },
    });
    fireEvent.click(screen.getByRole("button", { name: "保存 ZPay 设置" }));

    await screen.findByRole("dialog", { name: "保存 ZPay 支付设置" });
    expect(screen.queryByLabelText("操作原因")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "确认保存 ZPay 设置" }));

    await waitFor(() => {
      expect(
        fetchMock.mock.calls.some(
          ([url, options]) =>
            String(url).endsWith(
              "/api/control/settings/customer-payments/zpay",
            ) && options?.method === "PATCH",
        ),
      ).toBe(true);
    });
    const zpayCall = fetchMock.mock.calls.find(
      ([url, options]) =>
        String(url).endsWith("/api/control/settings/customer-payments/zpay") &&
        options?.method === "PATCH",
    );
    const zpayRequest = zpayCall?.[1] as RequestInit | undefined;
    expect(zpayRequest).toBeTruthy();
    expect(zpayCall?.[1]?.body).toBe(
      JSON.stringify({
        pid: "merchant-2",
        key: "",
        enabled_channels: ["alipay", "wxpay"],
        confirm: true,
        reason: "保存 ZPay 设置",
      }),
    );
    expect(
      new Headers(zpayRequest?.headers).get("Idempotency-Key"),
    ).toBeTruthy();
    expect(String(zpayCall?.[1]?.body)).not.toContain("gateway_url");
    expect(String(zpayCall?.[1]?.body)).not.toContain("notify_url");
    expect(String(zpayCall?.[1]?.body)).not.toContain("return_url");
  });

  it("configures generic services through the production control plane", async () => {
    const fetchMock = installFetch();
    render(<AdminApp />);
    await signInWithPassword();

    fireEvent.click(screen.getByRole("link", { name: "系统设置" }));
    fireEvent.click(screen.getByRole("tab", { name: "技术配置" }));
    expect(
      await screen.findByRole("heading", { name: "视频生成 · 多账号" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { name: "腾讯云存储" }),
    ).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("图像模型 API Key"), {
      target: { value: SERVICE_KEY_TEXT },
    });
    fireEvent.click(screen.getAllByRole("button", { name: "保存" })[0]);

    await waitFor(() =>
      expect(
        fetchMock.mock.calls.some(
          ([url, options]) =>
            String(url).endsWith("/api/control/settings/providers/apilio") &&
            options?.method === "PUT",
        ),
      ).toBe(true),
    );
    const saveCall = fetchMock.mock.calls.find(
      ([url, options]) =>
        String(url).endsWith("/api/control/settings/providers/apilio") &&
        options?.method === "PUT",
    );
    const saveRequest = saveCall?.[1] as RequestInit | undefined;
    expect(saveRequest).toBeTruthy();
    expect(saveCall?.[1]?.body).toBe(
      JSON.stringify({
        // analysis_model / analysis_model_fallbacks 是可选项，留空分别表示
        // 回落到服务端默认模型与内置替补名单；replica_prompt_model(_fallbacks)
        // 是长视频链路的同构可选键（方案 D9）。管理端与客户端共用
        // SettingsPanel 的表单定义，因此两处都会带上这些键。
        config: {
          api_key: SERVICE_KEY_TEXT,
          analysis_api_key: "",
          analysis_model: "",
          analysis_model_fallbacks: "",
          replica_prompt_model: "",
          replica_prompt_model_fallbacks: "",
        },
        confirm: true,
        reason: "更新 apilio 服务配置",
      }),
    );
    expect(
      new Headers(saveRequest?.headers).get("Idempotency-Key"),
    ).toBeTruthy();
    expect(screen.queryByText(/metaso|minimax|cos/i)).toBeNull();
  });

  it("opens customer management only after account-password login", async () => {
    installFetch();

    render(<AdminApp />);
    await signInWithPassword();
    fireEvent.click(screen.getByRole("link", { name: "客户列表" }));
    expect(
      screen.queryByRole("tab", { name: "激活码" }),
    ).not.toBeInTheDocument();
    expect(
      screen.getByRole("heading", { level: 1, name: "客户列表" }),
    ).toBeInTheDocument();
  });

  it("opens the unified image and video generation records", async () => {
    installFetch({ session: "valid" });

    render(<AdminApp />);
    fireEvent.click(await screen.findByRole("link", { name: "生成记录" }));

    expect(
      await screen.findByRole("heading", { level: 1, name: "生成记录" }),
    ).toBeInTheDocument();
    // P2-1：类型下拉从词典生成后，「人物置换首帧 / 成本待核对」同时出现
    // 在下拉选项与表格单元格，断言限定到表格内。
    const table = await screen.findByRole("table", {
      name: "用户生成记录列表",
    });
    expect(within(table).getByText("人物置换首帧")).toBeInTheDocument();
    expect(within(table).getByText("成本待核对")).toBeInTheDocument();
  });

  it("opens failed generation records from the overview todo", async () => {
    const fetchMock = installFetch({ session: "valid", failedTasks: 1 });

    render(<AdminApp />);
    const failedTasks = await screen.findByText("失败任务待处理");
    fireEvent.click(
      failedTasks.closest("li")?.querySelector("button") as HTMLButtonElement,
    );

    await waitFor(() => {
      expect(
        fetchMock.mock.calls.some(([url]) =>
          String(url).includes(
            "/api/control/generation-records?limit=50&offset=0&status=FAILED",
          ),
        ),
      ).toBe(true);
    });
    // 5 组口径：FAILED 意图归一到「失败」组。
    expect(screen.getByLabelText("生成状态")).toHaveValue(
      "FAILED,CANCELED,CANCELLED,ARCHIVE_FAILED",
    );
  });

  it("restores a writable session after refresh without another login", async () => {
    installFetch({ session: "valid" });

    render(<AdminApp />);

    expect(
      await screen.findByRole("navigation", { name: "管理端导航" }),
    ).toBeInTheDocument();
    expect(screen.queryByLabelText("管理员密码")).toBeNull();
    expect(screen.getAllByText("管理员一号").length).toBeGreaterThan(0);
  });

  it("登出请求失败时仍回登录门，但如实提示服务端未确认释放", async () => {
    const fetchMock = installFetch({ session: "valid" });
    const baseMock = fetchMock.getMockImplementation();
    // 保持非 async：mockImplementation 的返回类型是「各分支 Promise 的联合」，
    // async 包装会折叠成 Promise<联合> 而无法赋值。
    fetchMock.mockImplementation((url: string, requestInit?: RequestInit) => {
      if (
        url.endsWith("/api/control/admin/session") &&
        requestInit?.method === "DELETE"
      ) {
        // 5xx：HttpOnly 会话 cookie 在服务端仍然有效，登出必须如实反馈，
        // 不能让管理员误以为共享机器上的会话已经安全结束。
        return jsonResponse(
          { detail: { message: "control plane temporarily unavailable" } },
          503,
        );
      }
      // installFetch 必然安装了实现；兜底分支仅为类型收窄。
      return baseMock
        ? baseMock(url, requestInit)
        : jsonResponse({ detail: "unexpected request" }, 500);
    });

    render(<AdminApp />);
    await screen.findByRole("navigation", { name: "管理端导航" });
    fireEvent.click(screen.getByRole("button", { name: "退出登录" }));

    expect(await screen.findByLabelText("管理员账号")).toBeInTheDocument();
    expect(screen.getByText(/服务端未确认本次会话释放/)).toBeInTheDocument();
  });

  it("总览与经营分析不再渲染与左侧菜单重复的页内导航", async () => {
    installFetch({ session: "valid" });
    render(<AdminApp />);
    await screen.findByRole("navigation", { name: "管理端导航" });
    expect(
      screen.queryByRole("tablist", { name: "运营概览快捷导航" }),
    ).toBeNull();
    fireEvent.click(screen.getByRole("link", { name: "经营看板" }));
    expect(screen.getByRole("link", { name: "经营看板" })).toHaveAttribute(
      "aria-current",
      "page",
    );
    expect(screen.queryByRole("tablist", { name: "经营分析页签" })).toBeNull();
    fireEvent.click(screen.getByRole("link", { name: "生成记录" }));
    expect(screen.queryByRole("tablist", { name: "生成记录页签" })).toBeNull();
  });

  it("renders the five navigation groups with scoped pages", async () => {
    installFetch({ session: "valid" });

    render(<AdminApp />);
    await screen.findByRole("navigation", { name: "管理端导航" });

    expect(
      within(screen.getByRole("navigation", { name: "管理端导航" }))
        .getAllByRole("heading", { level: 3 })
        .map((heading) => heading.textContent),
    ).toEqual(["经营", "客户", "生成运维", "内容", "系统"]);

    for (const name of [
      "总览",
      "经营看板",
      "资金中心",
      "客户列表",
      "登录与设备",
      "生成记录",
      "管理操作日志",
      "系统设置",
    ]) {
      expect(screen.getByRole("link", { name })).toBeInTheDocument();
    }

    fireEvent.click(screen.getByRole("link", { name: "资金中心" }));
    expect(screen.getByRole("tab", { name: "收款订单" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "积分流水" })).toBeInTheDocument();

    fireEvent.click(screen.getByRole("link", { name: "经营看板" }));
    expect(screen.getByRole("link", { name: "成本核对" })).toBeInTheDocument();

    fireEvent.click(screen.getByRole("link", { name: "客户列表" }));
    expect(
      screen.queryByRole("tab", { name: "在线会话" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("tab", { name: "激活码" }),
    ).not.toBeInTheDocument();
    expect(screen.queryByText("当前模块")).not.toBeInTheDocument();
    expect(
      screen.queryByRole("tab", { name: "设备与会话" }),
    ).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("link", { name: "管理操作日志" }));
    // 调账记录已迁入资金中心·人工调整（方案 P1），审计中心只剩审计日志单页。
    expect(
      screen.getByRole("region", { name: "审计事件" }),
    ).toBeInTheDocument();
    expect(screen.queryByRole("tab", { name: "调账记录" })).toBeNull();

    fireEvent.click(screen.getByRole("link", { name: "资金中心" }));
    expect(screen.getByRole("tab", { name: "人工调整" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "对账异常" })).toBeInTheDocument();

    fireEvent.click(screen.getByRole("link", { name: "系统设置" }));
    expect(screen.getByRole("tab", { name: "价格与套餐" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "收款设置" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "技术配置" })).toBeInTheDocument();
  });

  it("mounts the restored sessions page from the customer-operations group", async () => {
    // 任务书 C：SessionsPage 此前是孤儿（只被自身测试引用），这里钉住恢复挂载。
    installFetch({ session: "valid" });

    render(<AdminApp />);
    await screen.findByRole("navigation", { name: "管理端导航" });

    fireEvent.click(screen.getByRole("link", { name: "登录与设备" }));

    expect(await screen.findByLabelText("公司名或用户名")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "查询" })).toBeInTheDocument();
  });

  it("returns to the login gate and clears admin session state when a control 401 emits the shared expiry event", async () => {
    installFetch({ session: "valid" });

    render(<AdminApp />);

    expect(
      await screen.findByRole("navigation", { name: "管理端导航" }),
    ).toBeInTheDocument();
    expect(getAdminCsrfToken()).toBe(CSRF_TOKEN_TEXT);

    vi.stubGlobal(
      "fetch",
      vi.fn(() => jsonResponse({ detail: "会话过期" }, 401)),
    );
    await act(async () => {
      await expect(getCustomerPricing()).rejects.toMatchObject({ status: 401 });
    });

    expect(await screen.findByLabelText("管理员账号")).toBeInTheDocument();
    expect(screen.getByLabelText("管理员密码")).toBeInTheDocument();
    expect(screen.queryByRole("navigation", { name: "管理端导航" })).toBeNull();
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "会话已失效，请重新登录。",
    );
    expect(getAdminCsrfToken()).toBeNull();
  });

  it("removes the shared expiry listener when the admin shell unmounts", async () => {
    installFetch({ session: "valid" });
    const addSpy = vi.spyOn(window, "addEventListener");
    const removeSpy = vi.spyOn(window, "removeEventListener");

    const { unmount } = render(<AdminApp />);

    await screen.findByRole("navigation", { name: "管理端导航" });
    const sessionListener = addSpy.mock.calls.find(
      ([name]) => name === SESSION_EXPIRED_EVENT,
    )?.[1];
    expect(sessionListener).toBeTypeOf("function");

    unmount();

    expect(removeSpy).toHaveBeenCalledWith(
      SESSION_EXPIRED_EVENT,
      sessionListener,
    );
  });

  it("uses a one-time credential only to set or recover the password", async () => {
    const fetchMock = installFetch();
    render(<AdminApp />);

    fireEvent.click(
      await screen.findByRole("button", { name: "首次设置或找回密码" }),
    );
    fireEvent.change(screen.getByLabelText("一次性恢复凭据"), {
      target: { value: "ASX1.body.signature" },
    });
    fireEvent.click(screen.getByRole("button", { name: "验证恢复凭据" }));
    expect(await screen.findByLabelText("新管理员密码")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("新管理员密码"), {
      target: { value: "Recovered Admin Passphrase 2026!" },
    });
    fireEvent.change(screen.getByLabelText("确认新管理员密码"), {
      target: { value: "Recovered Admin Passphrase 2026!" },
    });
    fireEvent.click(screen.getByRole("button", { name: "保存新密码" }));

    expect(await screen.findByLabelText("管理员账号")).toBeInTheDocument();
    expect(
      fetchMock.mock.calls.some(
        ([url, options]) =>
          String(url).endsWith("/api/control/admin/password") &&
          options?.method === "PUT",
      ),
    ).toBe(true);
  });

  it("登录门提供密码可见性切换并回写状态", async () => {
    installFetch();

    render(<AdminApp />);

    expect(await screen.findByLabelText("管理员密码")).toHaveAttribute(
      "type",
      "password",
    );

    fireEvent.click(screen.getByRole("button", { name: "显示密码" }));
    expect(screen.getByLabelText("管理员密码")).toHaveAttribute("type", "text");
    const hideToggle = screen.getByRole("button", { name: "隐藏密码" });
    expect(hideToggle).toHaveAttribute("aria-pressed", "true");

    fireEvent.click(hideToggle);
    expect(screen.getByLabelText("管理员密码")).toHaveAttribute(
      "type",
      "password",
    );
    expect(screen.getByRole("button", { name: "显示密码" })).toHaveAttribute(
      "aria-pressed",
      "false",
    );
  });

  it("登录提交期间禁用按钮并阻止重复提交以保护登录限流预算", async () => {
    // 后端按 IP+账号双维度对每次登录尝试计数（admin_auth_routes
    // _spend_admin_password_budget）：前端双击等于白烧两份预算，可能把管理员
    // 锁在门外。提交期间必须禁用按钮，直到当前请求落地。
    let resolveLogin:
      | ((response: {
          ok: boolean;
          status: number;
          json: () => Promise<unknown>;
        }) => void)
      | undefined;
    const fetchMock = vi.fn((url: string, requestInit?: RequestInit) => {
      if (
        url.endsWith("/api/control/admin/session") &&
        (!requestInit?.method || requestInit.method === "GET")
      ) {
        return jsonResponse(
          {
            detail: {
              code: "ADMIN_SESSION_INVALID",
              message: "Admin session is missing, revoked or invalid.",
            },
          },
          401,
        );
      }
      if (url.endsWith("/api/control/admin/session/password")) {
        return new Promise((resolve) => {
          resolveLogin = resolve;
        });
      }
      throw new Error(`unexpected request: ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<AdminApp />);

    fireEvent.change(await screen.findByLabelText("管理员账号"), {
      target: { value: "admin" },
    });
    fireEvent.change(screen.getByLabelText("管理员密码"), {
      target: { value: "Admin Login Passphrase 2026!" },
    });
    fireEvent.click(screen.getByRole("button", { name: "登录后台" }));

    const pendingButton = await screen.findByRole("button", {
      name: "正在登录…",
    });
    expect(pendingButton).toBeDisabled();
    fireEvent.click(pendingButton);
    expect(
      fetchMock.mock.calls.filter(([url]) =>
        String(url).endsWith("/api/control/admin/session/password"),
      ).length,
    ).toBe(1);

    resolveLogin?.({
      ok: true,
      status: 201,
      json: async () => adminSession,
    });
    expect(
      await screen.findByRole("navigation", { name: "管理端导航" }),
    ).toBeInTheDocument();
  });

  it("登录限流错误呈现中文提示而非服务端英文原文", async () => {
    const fetchMock = vi.fn((url: string, requestInit?: RequestInit) => {
      if (
        url.endsWith("/api/control/admin/session") &&
        (!requestInit?.method || requestInit.method === "GET")
      ) {
        return jsonResponse(
          {
            detail: {
              code: "ADMIN_SESSION_INVALID",
              message: "Admin session is missing, revoked or invalid.",
            },
          },
          401,
        );
      }
      if (url.endsWith("/api/control/admin/session/password")) {
        return jsonResponse(
          {
            detail: {
              code: "RATE_LIMITED",
              message:
                "Too many administrator sign-in attempts. Try again later.",
            },
          },
          429,
        );
      }
      throw new Error(`unexpected request: ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<AdminApp />);

    fireEvent.change(await screen.findByLabelText("管理员账号"), {
      target: { value: "admin" },
    });
    fireEvent.change(screen.getByLabelText("管理员密码"), {
      target: { value: "Admin Login Passphrase 2026!" },
    });
    fireEvent.click(screen.getByRole("button", { name: "登录后台" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("登录尝试过于频繁，请稍后再试。");
    expect(alert.textContent).not.toContain("Too many");
  });

  it("会话上下文变化错误呈现中文提示", async () => {
    const fetchMock = vi.fn((url: string, requestInit?: RequestInit) => {
      if (
        url.endsWith("/api/control/admin/session") &&
        (!requestInit?.method || requestInit.method === "GET")
      ) {
        return jsonResponse(
          {
            detail: {
              code: "ADMIN_SESSION_INVALID",
              message: "Admin session is missing, revoked or invalid.",
            },
          },
          401,
        );
      }
      if (url.endsWith("/api/control/admin/session/password")) {
        return jsonResponse(
          {
            detail: {
              code: "ADMIN_SESSION_CONTEXT_CHANGED",
              message:
                "Admin session network or browser context changed; sign in again.",
            },
          },
          401,
        );
      }
      throw new Error(`unexpected request: ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<AdminApp />);

    fireEvent.change(await screen.findByLabelText("管理员账号"), {
      target: { value: "admin" },
    });
    fireEvent.change(screen.getByLabelText("管理员密码"), {
      target: { value: "Admin Login Passphrase 2026!" },
    });
    fireEvent.click(screen.getByRole("button", { name: "登录后台" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("检测到浏览器环境变化，请重新登录。");
    expect(alert.textContent).not.toContain("context changed");
  });

  it("恢复凭据表单提供格式提示并对粘贴空白给出忽略反馈", async () => {
    installFetch();

    render(<AdminApp />);

    fireEvent.click(
      await screen.findByRole("button", { name: "首次设置或找回密码" }),
    );

    expect(screen.getByText("凭据以 ASX1. 开头。")).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("一次性恢复凭据"), {
      target: { value: "  ASX1.body.signature  " },
    });

    expect(screen.getByText("已自动忽略首尾空白。")).toBeInTheDocument();
  });

  it("keeps key order actions available on a narrow viewport", async () => {
    Object.defineProperty(window, "innerWidth", {
      configurable: true,
      value: 375,
    });
    installFetch();

    render(<AdminApp />);
    await signInWithPassword();
    fireEvent.click(screen.getByRole("button", { name: "展开导航" }));
    fireEvent.click(screen.getByRole("link", { name: "资金中心" }));
    fireEvent.click(screen.getByRole("tab", { name: "收款订单" }));

    expect(
      await screen.findByRole("button", { name: "查单补单" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "导出充值订单 CSV" }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "导出账务流水 CSV" }),
    ).toBeNull();
    fireEvent.click(screen.getByRole("tab", { name: "积分流水" }));
    expect(
      await screen.findByRole("button", { name: "导出账务流水 CSV" }),
    ).toBeInTheDocument();
  });

  it.each(["admin", "auditor"] as const)(
    "%s 无全局日志导航，直接地址也不发敏感读取",
    async (role) => {
      window.history.replaceState(null, "", "#admin/externalCalls");
      installFetch({ session: "valid", role, isSuperAdmin: false });
      render(<AdminApp />);
      const banner = await screen.findByText("此功能仅超级管理员可用。");
      // 页面内容（含无权提示）必须排在页面标题之后，不能把标题顶到下方。
      const heading = screen.getByRole("heading", {
        level: 1,
        name: "接口调用日志",
      });
      expect(
        heading.compareDocumentPosition(banner) &
          Node.DOCUMENT_POSITION_FOLLOWING,
      ).toBeTruthy();
      expect(screen.queryByRole("link", { name: "接口调用日志" })).toBeNull();
      expect(
        vi
          .mocked(fetch)
          .mock.calls.some(([url]) => String(url).includes("/external-calls")),
      ).toBe(false);
    },
  );

  it("每个导航入口的图标互不重复", async () => {
    Object.defineProperty(window, "innerWidth", {
      configurable: true,
      value: 1280,
    });
    installFetch({ session: "valid" });
    render(<AdminApp />);
    const nav = await screen.findByRole("navigation", { name: "管理端导航" });
    const icons = [
      ...nav.querySelectorAll<HTMLImageElement>(".admin-navigation-icon"),
    ].map((icon) => icon.getAttribute("src"));
    expect(icons.length).toBeGreaterThan(20);
    expect(new Set(icons).size).toBe(icons.length);
  });

  it.each([
    ["announcements", "公告与消息"],
    ["supportTickets", "工单反馈"],
    ["reports", "报表中心"],
    ["workerObservations", "队列观测"],
    ["publishStats", "发布成效"],
    ["customerLifecycle", "客户运营"],
    ["viralOverview", "内容概览"],
    ["viralHomepage", "首页编排"],
    ["sessions", "登录与设备"],
  ])(
    "%s 页面只有一个页面级标题（外壳的 h1，页内不再重复）",
    async (tab, title) => {
      window.history.replaceState(null, "", `#admin/${tab}`);
      installFetch({ session: "valid" });
      render(<AdminApp />);
      await screen.findByRole("heading", { level: 1, name: title });
      expect(screen.getAllByRole("heading", { name: title })).toHaveLength(1);
    },
  );

  it("发布成效默认不进菜单，开关打开后才出现", async () => {
    installFetch({ session: "valid" });
    const { unmount } = render(<AdminApp />);
    await screen.findByRole("link", { name: "客户列表" });
    expect(screen.queryByRole("link", { name: "发布成效" })).toBeNull();
    unmount();

    installFetch({ session: "valid", publishStatsEnabled: true });
    render(<AdminApp />);
    expect(
      await screen.findByRole("link", { name: "发布成效" }),
    ).toBeInTheDocument();
  });

  it("告警总览在生成运维组，团队与权限仅超管可见", async () => {
    installFetch({ session: "valid" });
    const { unmount } = render(<AdminApp />);
    const nav = await screen.findByRole("navigation", { name: "管理端导航" });
    expect(within(nav).getByRole("link", { name: "告警总览" })).toHaveAttribute(
      "href",
      "#admin/alerts",
    );
    expect(
      within(nav).getByRole("link", { name: "团队与权限" }),
    ).toBeInTheDocument();
    unmount();

    installFetch({ session: "valid", isSuperAdmin: false });
    render(<AdminApp />);
    const plain = await screen.findByRole("navigation", {
      name: "管理端导航",
    });
    expect(
      within(plain).queryByRole("link", { name: "团队与权限" }),
    ).toBeNull();
  });

  it("队列观测「去处理」的地址把类型与状态带进生成记录筛选", async () => {
    window.history.replaceState(
      null,
      "",
      "#admin/generationRecords?recordType=ORAL_VIDEO&status=SUBMISSION_UNCERTAIN",
    );
    const fetchMock = installFetch({ session: "valid" });
    render(<AdminApp />);
    await screen.findByRole("heading", { level: 1, name: "生成记录" });
    await waitFor(() => {
      const listCall = fetchMock.mock.calls
        .map(([url]) => new URL(String(url)))
        .find((url) =>
          url.pathname.endsWith("/api/control/generation-records"),
        );
      expect(listCall?.searchParams.get("record_type")).toBe("ORAL_VIDEO");
      expect(listCall?.searchParams.get("status")).toBe(
        "UNKNOWN,SUBMISSION_UNCERTAIN",
      );
    });
  });

  it("顶栏全局搜索：粘贴任务号一步到对应生成记录", async () => {
    const fetchMock = installFetch({ session: "valid" });
    render(<AdminApp />);
    fireEvent.change(await screen.findByLabelText("全局搜索"), {
      target: { value: "TASK-9" },
    });
    fireEvent.click(screen.getByRole("button", { name: "搜索" }));
    await waitFor(() =>
      expect(window.location.hash).toContain("#admin/generationRecords"),
    );
    expect(window.location.hash).toContain("taskId=TASK-9");
    await waitFor(() => {
      const listCall = fetchMock.mock.calls
        .map(([url]) => new URL(String(url)))
        .find((url) =>
          url.pathname.endsWith("/api/control/generation-records"),
        );
      expect(listCall?.searchParams.get("task_ref")).toBe("TASK-9");
    });
  });

  // 每个模板可见的菜单（总览人人有；发布成效需开关、接口调用日志与团队仅超管，另测）。
  const MENU_BY_TEMPLATE: Record<keyof typeof TEMPLATE_PERMISSIONS, string[]> =
    {
      super_admin: [
        "总览",
        "经营看板",
        "成本核对",
        "资金中心",
        "报表中心",
        "客户列表",
        "客户运营",
        "公告与消息",
        "工单反馈",
        "登录与设备",
        "生成记录",
        "失败诊断",
        "队列观测",
        "告警总览",
        "接口调用日志",
        "内容概览",
        "视频库",
        "首页编排",
        "客户需求洞察",
        "采集设置",
        "管理操作日志",
        "团队与权限",
        "系统设置",
      ],
      operations: [
        "总览",
        "经营看板",
        "成本核对",
        "资金中心",
        "报表中心",
        "客户列表",
        "客户运营",
        "公告与消息",
        "工单反馈",
        "登录与设备",
        "生成记录",
        "失败诊断",
        "队列观测",
        "告警总览",
        "内容概览",
        "视频库",
        "首页编排",
        "客户需求洞察",
        "采集设置",
        "管理操作日志",
        "系统设置",
      ],
      finance: [
        "总览",
        "经营看板",
        "成本核对",
        "资金中心",
        "报表中心",
        "客户列表",
        "客户运营",
        "公告与消息",
        "工单反馈",
        "登录与设备",
        "系统设置",
      ],
      content: [
        "总览",
        "客户列表",
        "客户运营",
        "公告与消息",
        "工单反馈",
        "登录与设备",
        "生成记录",
        "失败诊断",
        "队列观测",
        "告警总览",
        "内容概览",
        "视频库",
        "首页编排",
        "客户需求洞察",
        "采集设置",
      ],
      auditor: [
        "总览",
        "经营看板",
        "成本核对",
        "资金中心",
        "报表中心",
        "客户列表",
        "客户运营",
        "公告与消息",
        "工单反馈",
        "登录与设备",
        "生成记录",
        "失败诊断",
        "队列观测",
        "告警总览",
        "内容概览",
        "视频库",
        "首页编排",
        "客户需求洞察",
        "采集设置",
        "管理操作日志",
        "系统设置",
      ],
    };

  it.each(
    Object.keys(MENU_BY_TEMPLATE) as Array<keyof typeof MENU_BY_TEMPLATE>,
  )("%s 模板：左侧菜单只出现该模板可读的入口", async (template) => {
    installFetch({ session: "valid", template });
    render(<AdminApp />);
    const nav = await screen.findByRole("navigation", { name: "管理端导航" });
    const links = within(nav)
      .getAllByRole("link")
      .map((link) => link.getAttribute("aria-label"));
    expect(links).toEqual(MENU_BY_TEMPLATE[template]);
  });

  it("没有读权限的页面即使手输地址也不渲染，给出说明", async () => {
    window.history.replaceState(null, "", "#admin/funds");
    installFetch({ session: "valid", template: "content" });
    render(<AdminApp />);
    expect(
      await screen.findByText(/当前角色没有查看此页面的权限/),
    ).toBeInTheDocument();
    expect(screen.queryByRole("tab", { name: "收款订单" })).toBeNull();
  });

  // 按钮级：同一个页面，不同模板看到的操作不同。
  it.each([
    ["finance", true],
    ["operations", true],
    ["auditor", true],
    ["content", false],
  ] as const)("%s 模板：客户列表导出按钮 %s", async (template, visible) => {
    window.history.replaceState(null, "", "#admin/customersMgmt");
    installFetch({ session: "valid", template });
    render(<AdminApp />);
    await screen.findByRole("heading", { level: 1, name: "客户列表" });
    await screen.findByRole("form", { name: "客户筛选" });
    expect(
      screen.queryByRole("button", { name: "导出列表 CSV" }) !== null,
    ).toBe(visible);
  });

  it.each([
    ["finance", true],
    ["operations", true],
    ["auditor", false],
  ] as const)(
    "%s 模板：资金中心订单页——查单 / 补单类写操作 %s",
    async (template, writable) => {
      window.history.replaceState(null, "", "#admin/funds?sub=orders");
      installFetch({ session: "valid", template });
      render(<AdminApp />);
      await screen.findByRole("tab", { name: "收款订单" });
      // 订单页的导出按钮三者都有（审计员有 export 点）；写类操作只有 funds.write 才有。
      expect(
        await screen.findByRole("button", { name: "导出充值订单 CSV" }),
      ).toBeInTheDocument();
      const manual = screen.queryAllByRole("button", { name: /手动查单|查单/ });
      expect(manual.length > 0).toBe(writable);
    },
  );

  it.each([
    ["operations", true],
    ["finance", false],
    ["content", false],
  ] as const)(
    "%s 模板：公告页「发布公告」按钮 %s（customer.write）",
    async (template, visible) => {
      window.history.replaceState(null, "", "#admin/announcements");
      installFetch({ session: "valid", template });
      render(<AdminApp />);
      await screen.findByRole("heading", { level: 1, name: "公告与消息" });
      await waitFor(() =>
        expect(
          screen.queryByRole("button", { name: "发布公告" }) !== null,
        ).toBe(visible),
      );
    },
  );

  it("财务模板能改系统设置；内容模板看不到系统设置入口", async () => {
    window.history.replaceState(null, "", "#admin/systemSettings");
    installFetch({ session: "valid", template: "finance" });
    const { unmount } = render(<AdminApp />);
    await screen.findByRole("heading", { level: 1, name: "系统设置" });
    expect(screen.getByRole("tab", { name: "运营参数" })).toBeInTheDocument();
    unmount();

    installFetch({ session: "valid", template: "content" });
    render(<AdminApp />);
    expect(
      await screen.findByText(/当前角色没有查看此页面的权限/),
    ).toBeInTheDocument();
  });

  it("每个导航分组至少两项", async () => {
    installFetch({ session: "valid", isSuperAdmin: false });
    render(<AdminApp />);
    const nav = await screen.findByRole("navigation", { name: "管理端导航" });
    for (const group of nav.querySelectorAll(".admin-nav-group")) {
      expect(
        group.querySelectorAll("a.admin-tab").length,
        group.querySelector("h3")?.textContent ?? "",
      ).toBeGreaterThanOrEqual(2);
    }
  });

  it("旧的系统设置告警 / 团队地址重定向到独立入口", () => {
    expect(adminRouteFromHash("#admin/systemSettings?sub=alerts").tab).toBe(
      "alerts",
    );
    expect(adminRouteFromHash("#admin/systemSettings?sub=team").tab).toBe(
      "team",
    );
    expect(adminRouteFromHash("#admin/systemSettings?sub=pricing").tab).toBe(
      "systemSettings",
    );
  });

  it("顶栏提供修改密码入口，打开后是改密对话框", async () => {
    installFetch({ session: "valid" });
    render(<AdminApp />);
    fireEvent.click(await screen.findByRole("button", { name: "修改密码" }));
    expect(
      await screen.findByRole("dialog", { name: /修改我的密码/ }),
    ).toBeInTheDocument();
  });

  it("菜单项是真链接：带地址，修饰键点击交给浏览器新开标签页", async () => {
    Object.defineProperty(window, "innerWidth", {
      configurable: true,
      value: 1280,
    });
    installFetch({ session: "valid" });
    render(<AdminApp />);
    const funds = await screen.findByRole("link", { name: "资金中心" });
    expect(funds).toHaveAttribute("href", "#admin/funds");
    fireEvent.click(funds, { ctrlKey: true });
    expect(funds).not.toHaveAttribute("aria-current");
    fireEvent.click(funds);
    expect(funds).toHaveAttribute("aria-current", "page");
    expect(window.location.hash).toBe("#admin/funds");
  });

  it("groups admin navigation and lets compact layouts collapse and reopen it", async () => {
    Object.defineProperty(window, "innerWidth", {
      configurable: true,
      value: 390,
    });
    installFetch({ session: "valid" });

    render(<AdminApp />);

    expect(
      await screen.findByRole("button", { name: "展开导航" }),
    ).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByRole("link", { name: "客户列表" })).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "展开导航" }));

    expect(screen.getAllByText("经营").length).toBeGreaterThan(0);
    expect(screen.getAllByText("客户").length).toBeGreaterThan(0);
    expect(screen.getAllByText("系统").length).toBeGreaterThan(0);
    expect(screen.getByRole("link", { name: "客户列表" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "关闭导航" })).toHaveAttribute(
      "aria-expanded",
      "true",
    );

    fireEvent.click(screen.getByRole("link", { name: "客户列表" }));

    expect(
      await screen.findByRole("heading", { name: "客户列表" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "展开导航" })).toHaveAttribute(
      "aria-expanded",
      "false",
    );
  });
  it("switches independent cost and failure navigation to their real filtered pages", async () => {
    Object.defineProperty(window, "innerWidth", {
      configurable: true,
      value: 1440,
    });
    installFetch({ session: "valid" });
    render(<AdminApp />);
    await screen.findByRole("navigation", { name: "管理端导航" });
    fireEvent.click(screen.getByRole("link", { name: "经营看板" }));
    fireEvent.click(screen.getByRole("link", { name: "成本核对" }));
    expect(
      await screen.findByRole("region", { name: "成本明细" }),
    ).toBeInTheDocument();
    expect(window.location.hash).toContain("#admin/costReview");
    fireEvent.click(screen.getByRole("link", { name: "生成记录" }));
    await waitFor(() =>
      expect(
        vi
          .mocked(fetch)
          .mock.calls.some(([url]) =>
            String(url).includes("/generation-records?"),
          ),
      ).toBe(true),
    );
    vi.mocked(fetch).mockClear();
    fireEvent.click(screen.getByRole("link", { name: "失败诊断" }));
    // 菜单「失败诊断」必须落在诊断视图（诊断队列读取），而不是失败状态的记录列表。
    await waitFor(() =>
      expect(
        vi.mocked(fetch).mock.calls.some(([url]) => {
          const parsed = new URL(String(url));
          return (
            parsed.pathname.endsWith("/generation-records") &&
            parsed.searchParams.get("diagnostics") === "true"
          );
        }),
      ).toBe(true),
    );
    expect(
      await screen.findByRole("region", { name: "全类型失败诊断" }),
    ).toBeInTheDocument();
    expect(window.location.hash).toContain("#admin/generationFailures");
  });

  it("restores the old cost address without losing its filters", async () => {
    Object.defineProperty(window, "innerWidth", {
      configurable: true,
      value: 1440,
    });
    window.history.replaceState(
      null,
      "",
      "#admin/analytics?analyticsTab=cost&start=2026-09-01&end=2026-09-02&billingQuery=user_id%3Dsame-a%26attention%3Dpending",
    );
    installFetch({ session: "valid" });
    render(<AdminApp />);
    await screen.findByRole("navigation", { name: "管理端导航" });
    expect(screen.getByRole("link", { name: "成本核对" })).toHaveAttribute(
      "aria-current",
      "page",
    );
    expect(
      await screen.findByRole("region", { name: "成本明细" }),
    ).toBeInTheDocument();
    expect(window.location.hash).toContain("#admin/costReview");
    const params = new URLSearchParams(window.location.hash.split("?")[1]);
    expect(params.get("billingQuery")).toBe("user_id=same-a&attention=pending");
    expect(params.get("start")).toBe("2026-09-01");
  });
});
