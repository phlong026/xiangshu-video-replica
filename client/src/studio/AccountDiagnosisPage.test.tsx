import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  notify: vi.fn(),
  navigate: vi.fn(),
  getAvailability: vi.fn(),
  listHistory: vi.fn(),
  createDiagnosis: vi.fn(),
  getReport: vi.fn(),
  getWallet: vi.fn(),
  listPackages: vi.fn(),
  createRechargeOrder: vi.fn(),
  createPaymentCode: vi.fn(),
  getRechargeOrder: vi.fn(),
  store: { loadSessionToken: vi.fn() },
  onSessionExpired: vi.fn(),
}));

vi.mock("./context", () => ({
  useStudio: () => ({
    requireLogin: undefined,
    review: false,
    notify: mocks.notify,
    navigate: mocks.navigate,
    customerAccount: {
      store: mocks.store,
      onSessionExpired: mocks.onSessionExpired,
    },
    state: { accountDiagnosisInput: "" },
  }),
}));

vi.mock("../api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api")>()),
  getAccountDiagnosisAvailability: mocks.getAvailability,
  listAccountDiagnoses: mocks.listHistory,
  createAccountDiagnosis: mocks.createDiagnosis,
  getAccountDiagnosis: mocks.getReport,
  customerGetWallet: mocks.getWallet,
  customerListRechargePackages: mocks.listPackages,
  customerCreateRechargeOrder: mocks.createRechargeOrder,
  customerCreateRechargePaymentCode: mocks.createPaymentCode,
  customerGetRechargeOrder: mocks.getRechargeOrder,
}));

import { AccountDiagnosisPage } from "./AccountDiagnosisPage";

const rechargeOrder = {
  order_no: "diagnosis-recharge-1",
  status: "PENDING",
  amount_fen: 10000,
  credits: 10,
};
const paymentCode = {
  order_no: rechargeOrder.order_no,
  amount_fen: rechargeOrder.amount_fen,
  credits: rechargeOrder.credits,
  qr_image_url: "https://payment.example/diagnosis-qr.png",
  payment_url: "https://payment.example/diagnosis-pay",
};

async function openRechargeDialog() {
  mocks.createDiagnosis.mockRejectedValue(
    Object.assign(new Error("账号诊断需要先购买充值套餐。"), {
      code: "ACCOUNT_DIAGNOSIS_MEMBER_REQUIRED",
    }),
  );
  render(<AccountDiagnosisPage />);
  await screen.findByText("新用户首单免费");
  fireEvent.change(screen.getByLabelText("抖音账号链接或抖音号"), {
    target: { value: "zsj001" },
  });
  fireEvent.click(screen.getByRole("button", { name: "开始诊断" }));
  return screen.findByRole("dialog", { name: "扫码充值" });
}

const reportFixture = {
  id: "report-1",
  status: "SUCCEEDED",
  platform: "douyin",
  isFree: true,
  cached: false,
  dataLevel: "L1" as const,
  createdAt: "2026-11-20T10:00:00+00:00",
  completedAt: "2026-11-20T10:00:05+00:00",
  account: {
    secUserId: "sec-1",
    uniqueId: "zsj001",
    nickname: "众墅之家",
    avatarUrl: null,
    followerCount: 50_000,
    awemeCount: 120,
  },
  narrative: {
    summary: "综合 72 分（正常，腰部量级）。",
    strengths: [],
    weaknesses: [],
    actions: [
      {
        dimension: "互动健康度",
        title: "粉丝互动率",
        evidence: "2.10%（腰部基准 6.0/4.0/2.0%）",
        suggestion: "结尾抛一个具体问题，把看完转成说两句。",
      },
    ],
    generatedBy: "rules",
  },
  error: null,
  report: {
    score: 72,
    tier: "B",
    tier_label: "腰部",
    rating: "正常" as const,
    sample_size: 8,
    low_confidence: false,
    data_level: "L1" as const,
    metrics: {},
    risks: [
      {
        kind: "decline",
        label: "衰退",
        level: "medium" as const,
        evidence: "近期作品均互动较早期 -62.0%",
      },
    ],
    dimensions: [
      {
        key: "engagement",
        label: "互动健康度",
        weight: 0.3,
        score: 18,
        effective_full: 30,
        full: 30,
        comment: "粉丝互动率",
        sub_scores: [
          {
            key: "fan_engagement",
            label: "粉丝互动率",
            score: 4,
            full: 10,
            counted: true,
            detail: "2.10%（腰部基准 6.0/4.0/2.0%）",
          },
        ],
      },
    ],
    platform: "douyin",
    account: {
      secUserId: "sec-1",
      uniqueId: "zsj001",
      nickname: "众墅之家",
      avatarUrl: null,
      followerCount: 50_000,
      awemeCount: 120,
    },
    collectedAt: "2026-11-20T10:00:00+00:00",
    l2: null,
    actionCards: [
      {
        key: "interact-hook",
        priority: "P0" as const,
        dimension: "互动健康度",
        title: "互动钩子改造",
        problem: "粉丝互动率 2.1%，低于本量级良好线 4.0%",
        evidence: ["评论占赞比 1.8%（健康线 ≥4%）"],
        selfChecks: ["自查：结尾 3 秒是否有互动提问？"],
        steps: ["结尾 3 秒改为开放式提问", "发布后 1 小时内回复前 10 条评论"],
        beforeAfter: [
          {
            before: "感谢观看，关注我看更多干货。",
            after: "你家厨房做 U 型还是 L 型？评论区告诉我。",
          },
        ],
        target: "粉丝互动率 ≥ 4.0%",
        metric: "engagement_rate",
        targetValue: 4,
        dueWeek: 1,
        generatedBy: "rules" as const,
      },
    ],
    referenceWorks: [
      {
        videoId: "ref-1",
        title: "45㎡老破小改造，拆完业主哭了",
        author: "同赛道账号",
        likes: 124_000,
        score: 8.9,
        url: "https://www.douyin.com/video/ref-1",
      },
    ],
    priorReview: [
      { title: "头像更换", status: "fixed" as const, note: "当前已达目标" },
    ],
    calendar: [
      {
        label: "第 1 周",
        days: [
          {
            date: "2026-11-23",
            weekday: 1,
            task: "回复评论区 / 备素材",
            rest: true,
          },
        ],
      },
    ],
    works: [
      {
        awemeId: "aweme-1",
        title: "三层小别墅开工",
        createdAt: "2026-11-18T10:00:00+00:00",
        digg: 3200,
        comment: 120,
        share: 90,
        collect: 60,
        interaction: 3470,
        playCount: null,
        recommended: true,
      },
    ],
  },
};

beforeEach(() => {
  vi.resetAllMocks();
  mocks.getAvailability.mockResolvedValue({
    enabled: true,
    freeQuota: true,
    member: false,
    priceCredits: 8,
  });
  mocks.listHistory.mockResolvedValue({ items: [] });
  mocks.store.loadSessionToken.mockResolvedValue("diagnosis-test-session");
  mocks.getWallet.mockResolvedValue({
    available_credits: 0,
    reserved_credits: 0,
    internal_unit_price_fen: 1000,
    min_recharge_fen: 10000,
    recharge_step_fen: 1000,
  });
  mocks.listPackages.mockResolvedValue([
    {
      id: "diagnosis-package",
      name: "标准档",
      amount_fen: 10000,
      credits: 10,
      discount_rate: null,
      discount_interfaces: [],
    },
  ]);
  mocks.createRechargeOrder.mockResolvedValue(rechargeOrder);
  mocks.createPaymentCode.mockResolvedValue(paymentCode);
  mocks.getRechargeOrder.mockResolvedValue(rechargeOrder);
});

describe("账号诊断页", () => {
  it("展示首单免费状态并渲染六维报告与风险", async () => {
    mocks.createDiagnosis.mockResolvedValue(reportFixture);
    render(<AccountDiagnosisPage />);

    expect(await screen.findByText("新用户首单免费")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("抖音账号链接或抖音号"), {
      target: { value: "https://www.douyin.com/user/sec-1" },
    });
    fireEvent.click(screen.getByRole("button", { name: "开始诊断" }));

    expect(await screen.findByText("72")).toBeInTheDocument();
    expect(screen.getAllByText("互动健康度").length).toBeGreaterThan(0);
    expect(screen.getByText(/衰退 · 中危/)).toBeInTheDocument();
    expect(screen.getByText("建议复刻")).toBeInTheDocument();
    expect(mocks.createDiagnosis).toHaveBeenCalledWith(
      "https://www.douyin.com/user/sec-1",
    );
  });

  it("第二次使用被会员门禁拦截时直接弹出充值对话框", async () => {
    expect(await openRechargeDialog()).toBeInTheDocument();
    await waitFor(() =>
      expect(screen.getByRole("alert")).toHaveTextContent(
        "账号诊断需要先购买充值套餐。",
      ),
    );
  });

  it("建单后保留真实支付窗口，等待付款确认后才关闭", async () => {
    await openRechargeDialog();
    fireEvent.click(await screen.findByRole("button", { name: /标准档/ }));

    expect(
      await screen.findByRole("img", { name: "充值支付二维码" }),
    ).toHaveAttribute("src", paymentCode.qr_image_url);
    await waitFor(() => expect(mocks.getRechargeOrder).toHaveBeenCalled());
    expect(screen.getByText("正在等待支付结果")).toBeInTheDocument();
    expect(
      screen.getByRole("dialog", { name: "扫码充值" }),
    ).toBeInTheDocument();
    expect(mocks.getAvailability).toHaveBeenCalledTimes(2);
    expect(mocks.notify).not.toHaveBeenCalledWith("充值到账，可以继续诊断了。");

    mocks.getRechargeOrder.mockResolvedValue({
      ...rechargeOrder,
      status: "PAID",
    });
    await waitFor(
      () =>
        expect(screen.queryByRole("dialog", { name: "扫码充值" })).toBeNull(),
      { timeout: 3500 },
    );
    expect(mocks.notify).toHaveBeenCalledWith("充值到账，可以继续诊断了。");
    expect(mocks.createRechargeOrder).toHaveBeenCalledOnce();
    expect(mocks.getAvailability).toHaveBeenCalledTimes(3);
  });

  it("取码失败后窗口仍在，重试复用同一个充值订单", async () => {
    mocks.createPaymentCode.mockRejectedValueOnce(new Error("二维码暂不可用"));
    await openRechargeDialog();
    fireEvent.click(await screen.findByRole("button", { name: /标准档/ }));

    expect(await screen.findByText("二维码暂不可用")).toBeInTheDocument();
    expect(
      screen.getByRole("dialog", { name: "扫码充值" }),
    ).toBeInTheDocument();
    fireEvent.click(
      screen.getByRole("button", { name: "重新获取上一订单二维码" }),
    );

    expect(
      await screen.findByRole("img", { name: "充值支付二维码" }),
    ).toHaveAttribute("src", paymentCode.qr_image_url);
    expect(mocks.createRechargeOrder).toHaveBeenCalledOnce();
    expect(mocks.createPaymentCode).toHaveBeenCalledTimes(2);
    for (const call of mocks.createPaymentCode.mock.calls) {
      expect(call).toEqual([
        { kind: "session", token: "diagnosis-test-session" },
        rechargeOrder.order_no,
      ]);
    }
  });

  it("等待付款时仍允许用户主动关闭充值窗口", async () => {
    await openRechargeDialog();
    fireEvent.click(await screen.findByRole("button", { name: /标准档/ }));
    await screen.findByRole("img", { name: "充值支付二维码" });
    fireEvent.click(screen.getByRole("button", { name: "关闭充值窗口" }));

    expect(screen.queryByRole("dialog", { name: "扫码充值" })).toBeNull();
    expect(mocks.notify).not.toHaveBeenCalledWith("充值到账，可以继续诊断了。");
  });

  it("建议复刻的作品可一键带链接进工作台发起拆解", async () => {
    mocks.createDiagnosis.mockResolvedValue(reportFixture);
    render(<AccountDiagnosisPage />);
    await screen.findByText("新用户首单免费");

    fireEvent.change(screen.getByLabelText("抖音账号链接或抖音号"), {
      target: { value: "https://www.douyin.com/user/sec-1" },
    });
    fireEvent.click(screen.getByRole("button", { name: "开始诊断" }));
    await screen.findByText("三层小别墅开工");

    fireEvent.click(screen.getAllByRole("button", { name: "一键拆解" })[0]);
    expect(mocks.navigate).toHaveBeenCalledWith("workbench", {
      replicateIntentLink: "https://www.douyin.com/video/aweme-1",
    });
  });

  it("导出按钮把报告下载为 Markdown 文件", async () => {
    mocks.createDiagnosis.mockResolvedValue(reportFixture);
    render(<AccountDiagnosisPage />);
    await screen.findByText("新用户首单免费");

    fireEvent.change(screen.getByLabelText("抖音账号链接或抖音号"), {
      target: { value: "https://www.douyin.com/user/sec-1" },
    });
    fireEvent.click(screen.getByRole("button", { name: "开始诊断" }));
    await screen.findByText("三层小别墅开工");

    const click = vi.fn();
    const createObjectURL = vi.fn(() => "blob:mock");
    const revokeObjectURL = vi.fn();
    Object.defineProperty(URL, "createObjectURL", {
      value: createObjectURL,
      configurable: true,
    });
    Object.defineProperty(URL, "revokeObjectURL", {
      value: revokeObjectURL,
      configurable: true,
    });
    const originalCreate = document.createElement.bind(document);
    vi.spyOn(document, "createElement").mockImplementation(((tag: string) => {
      if (tag === "a") {
        return {
          set href(_: string) {},
          set download(_: string) {},
          click,
        };
      }
      return originalCreate(tag);
    }) as unknown as typeof document.createElement);

    fireEvent.click(screen.getByRole("button", { name: "导出报告" }));
    expect(click).toHaveBeenCalled();
    expect(createObjectURL).toHaveBeenCalled();
    vi.restoreAllMocks();
  });
});
