import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { beforeAll, beforeEach, expect, it, vi } from "vitest";
import type { StudioMessage, StudioMessagesPage } from "../api";
import { MessageCenter } from "./MessageCenter";

const mocks = vi.hoisted(() => ({
  useStudio: vi.fn(),
  getStudioMessages: vi.fn(),
  getStudioMessagesSummary: vi.fn(),
  markStudioMessagesRead: vi.fn(),
  getStudioMessagePreferences: vi.fn(),
  updateStudioMessagePreferences: vi.fn(),
}));

vi.mock("./context", () => ({ useStudio: mocks.useStudio }));
vi.mock("../api", async (original) => ({
  ...(await original<object>()),
  ...mocks,
}));

const studio = {
  user: {
    id: "u1",
    username: "u1",
    display_name: "客户一",
    role: "customer",
  },
  review: false,
  state: {},
  data: {},
  navigate: vi.fn(),
  openLive: vi.fn(),
  patchState: vi.fn(),
  updateData: vi.fn(),
};

const taskItem: StudioMessage = {
  id: "generation:b1",
  category: "task",
  kind: "video",
  title: "视频生成完成",
  summary: "示范批次 · 4 条视频 · 消耗 68 积分",
  tone: "success",
  status_label: "已完成",
  occurred_at: new Date().toISOString(),
  unread: true,
  amount_credits: 68,
  detail: null,
  action: {
    kind: "task",
    label: "查看任务",
    task_kind: "generation_batch",
    task_id: "b1",
  },
};

const failedItem: StudioMessage = {
  ...taskItem,
  id: "generation:b2",
  title: "视频生成失败",
  tone: "danger",
  status_label: "失败",
  action: {
    kind: "task",
    label: "去处理",
    task_kind: "generation_batch",
    task_id: "b2",
  },
};

const billingItem: StudioMessage = {
  id: "billing:op-1",
  category: "billing",
  kind: "settle",
  title: "扣费结算",
  summary: "实扣 18 积分 · 预扣 20，退回 2 · 视频生成 · 768P",
  tone: "info",
  status_label: "已结算",
  occurred_at: new Date().toISOString(),
  unread: true,
  amount_credits: -18,
  detail: null,
  action: { kind: "wallet", label: "查看账单" },
};

const announcementItem: StudioMessage = {
  id: "announcement:a1",
  category: "announcement",
  kind: "announcement",
  title: "系统维护通知",
  summary: "10 月 20 日 02:00-04:00 系统维护，请提前保存。",
  tone: "warning",
  status_label: "置顶",
  occurred_at: new Date().toISOString(),
  unread: true,
  amount_credits: null,
  detail:
    "10 月 20 日 02:00-04:00 系统维护，请提前保存；期间生成任务可能延迟。",
  action: null,
};

function page(items: StudioMessage[]): StudioMessagesPage {
  return {
    items,
    total: items.length,
    unread_count: items.length,
    by_category: { task: 2, billing: 1, announcement: 1, security: 0 },
    has_more: false,
    enabled: true,
    muted: [],
    billing_style: "per_item",
  };
}

function summary() {
  return {
    total_unread: 4,
    by_category: { task: 2, billing: 1, announcement: 1, security: 0 },
    enabled: true,
    muted: [] as string[],
    billing_style: "per_item" as const,
  };
}

function preferences() {
  return {
    enabled: true,
    muted: [] as Array<"task" | "billing" | "announcement" | "security">,
    billing_style: "per_item" as const,
    balance_threshold: 50,
  };
}

beforeAll(() => {
  // jsdom 对 <dialog> 的实现因版本而异；测试只关心内容，统一用 open 属性。
  HTMLDialogElement.prototype.showModal = function showModal(
    this: HTMLDialogElement,
  ) {
    this.setAttribute("open", "");
  };
  HTMLDialogElement.prototype.close = function close(this: HTMLDialogElement) {
    this.removeAttribute("open");
  };
});

beforeEach(() => {
  vi.clearAllMocks();
  mocks.useStudio.mockReturnValue(studio);
  mocks.getStudioMessagesSummary.mockResolvedValue(summary());
  mocks.getStudioMessages.mockResolvedValue(page([taskItem, billingItem]));
  mocks.markStudioMessagesRead.mockResolvedValue({
    marked: 1,
    read_before: {},
  });
  mocks.getStudioMessagePreferences.mockResolvedValue(preferences());
  mocks.updateStudioMessagePreferences.mockImplementation(
    async (value: unknown) => value,
  );
});

it("铃铛显示总未读，打开后按分类取消息并时间分组", async () => {
  render(<MessageCenter />);
  await waitFor(() => expect(screen.getByText("4")).toBeInTheDocument());

  fireEvent.click(screen.getByRole("button", { name: "打开消息中心" }));
  await screen.findByText("视频生成完成");
  expect(screen.getByText("今天")).toBeInTheDocument();
  expect(mocks.getStudioMessages).toHaveBeenLastCalledWith({
    category: "all",
    unreadOnly: false,
    limit: 20,
    offset: 0,
  });

  fireEvent.click(screen.getByRole("button", { name: /账单通知/ }));
  await waitFor(() =>
    expect(mocks.getStudioMessages).toHaveBeenLastCalledWith({
      category: "billing",
      unreadOnly: false,
      limit: 20,
      offset: 0,
    }),
  );
});

it("点击消息写单条已读回执，并按任务身份跳转详情", async () => {
  mocks.getStudioMessages.mockResolvedValue(page([taskItem, failedItem]));
  render(<MessageCenter />);
  fireEvent.click(screen.getByRole("button", { name: "打开消息中心" }));
  fireEvent.click(await screen.findByRole("button", { name: /视频生成失败/ }));

  await waitFor(() =>
    expect(mocks.markStudioMessagesRead).toHaveBeenCalledWith({
      keys: ["generation:b2"],
    }),
  );
  expect(studio.navigate).toHaveBeenCalledWith("task-detail", {
    selectedTaskId: "b2",
    selectedTaskKind: "generation_batch",
    selectedTaskBackendId: "b2",
    returnTo: "tasks",
  });
});

it("全部已读按当前分类下发，公告点击后在原位展开全文", async () => {
  mocks.getStudioMessages.mockResolvedValue(page([announcementItem]));
  render(<MessageCenter />);
  fireEvent.click(screen.getByRole("button", { name: "打开消息中心" }));

  fireEvent.click(await screen.findByRole("button", { name: /系统维护通知/ }));
  await screen.findByText(/期间生成任务可能延迟/);
  await waitFor(() =>
    expect(mocks.markStudioMessagesRead).toHaveBeenCalledWith({
      keys: ["announcement:a1"],
    }),
  );

  mocks.getStudioMessages.mockResolvedValue(page([announcementItem]));
  fireEvent.click(screen.getByRole("button", { name: "全部已读" }));
  await waitFor(() =>
    expect(mocks.markStudioMessagesRead).toHaveBeenCalledWith({
      category: "all",
    }),
  );
});

it("仅看未读为空时给出空态，可一键回到全部", async () => {
  mocks.getStudioMessages.mockResolvedValue(page([]));
  render(<MessageCenter />);
  fireEvent.click(screen.getByRole("button", { name: "打开消息中心" }));
  fireEvent.click(screen.getByRole("checkbox"));
  await screen.findByText("暂无未读消息");
  fireEvent.click(screen.getByRole("button", { name: "查看全部消息" }));
  await waitFor(() =>
    expect(mocks.getStudioMessages).toHaveBeenLastCalledWith({
      category: "all",
      unreadOnly: false,
      limit: 20,
      offset: 0,
    }),
  );
});

it("审核员不能标记已读；设置页静音类别会提交偏好", async () => {
  mocks.useStudio.mockReturnValue({
    ...studio,
    user: { ...studio.user, role: "auditor" },
  });
  render(<MessageCenter />);
  fireEvent.click(screen.getByRole("button", { name: "打开消息中心" }));
  await screen.findByText("视频生成完成");
  expect(screen.getByRole("button", { name: "全部已读" })).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: /视频生成完成/ }));
  expect(mocks.markStudioMessagesRead).not.toHaveBeenCalled();
});

it("设置页读取并保存消息偏好（含静音与扣费粒度）", async () => {
  render(<MessageCenter />);
  fireEvent.click(screen.getByRole("button", { name: "打开消息中心" }));
  await screen.findByText("视频生成完成");
  fireEvent.click(screen.getByRole("button", { name: "消息设置" }));

  await screen.findByText("扣费消息粒度");
  fireEvent.click(screen.getByRole("button", { name: "每日汇总" }));
  await waitFor(() =>
    expect(mocks.updateStudioMessagePreferences).toHaveBeenCalledWith(
      expect.objectContaining({ billing_style: "daily" }),
    ),
  );

  await act(async () => {
    fireEvent.click(screen.getByRole("checkbox", { name: "接收账单通知" }));
  });
  await waitFor(() =>
    expect(mocks.updateStudioMessagePreferences).toHaveBeenLastCalledWith(
      expect.objectContaining({ muted: ["billing"] }),
    ),
  );
});
