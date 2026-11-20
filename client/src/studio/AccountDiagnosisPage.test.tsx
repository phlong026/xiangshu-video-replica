import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  notify: vi.fn(),
  navigate: vi.fn(),
  getAvailability: vi.fn(),
  listHistory: vi.fn(),
  createDiagnosis: vi.fn(),
  getReport: vi.fn(),
}));

vi.mock("./context", () => ({
  useStudio: () => ({
    requireLogin: undefined,
    review: false,
    notify: mocks.notify,
    navigate: mocks.navigate,
    customerAccount: {
      store: {},
      onSessionExpired: vi.fn(),
    },
    state: { accountDiagnosisInput: "" },
  }),
}));

vi.mock("../api", () => ({
  getAccountDiagnosisAvailability: mocks.getAvailability,
  listAccountDiagnoses: mocks.listHistory,
  createAccountDiagnosis: mocks.createDiagnosis,
  getAccountDiagnosis: mocks.getReport,
}));

vi.mock("../customer/CustomerRechargeDialog", () => ({
  CustomerRechargeDialog: ({ isOpen }: { isOpen: boolean }) =>
    isOpen ? <div>充值对话框</div> : null,
}));

import { AccountDiagnosisPage } from "./AccountDiagnosisPage";

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
  vi.clearAllMocks();
  mocks.getAvailability.mockResolvedValue({
    enabled: true,
    freeQuota: true,
    member: false,
    priceCredits: 8,
  });
  mocks.listHistory.mockResolvedValue({ items: [] });
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
    expect(screen.getByText("互动健康度")).toBeInTheDocument();
    expect(screen.getByText(/衰退 · 中危/)).toBeInTheDocument();
    expect(screen.getByText("建议复刻")).toBeInTheDocument();
    expect(mocks.createDiagnosis).toHaveBeenCalledWith(
      "https://www.douyin.com/user/sec-1",
    );
  });

  it("第二次使用被会员门禁拦截时直接弹出充值对话框", async () => {
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

    expect(await screen.findByText("充值对话框")).toBeInTheDocument();
    await waitFor(() =>
      expect(screen.getByRole("alert")).toHaveTextContent(
        "账号诊断需要先购买充值套餐。",
      ),
    );
  });
});
