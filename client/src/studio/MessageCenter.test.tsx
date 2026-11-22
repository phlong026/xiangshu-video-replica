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

it.each([
  [true, false],
  [true, true],
  [false, false],
])(
  "标记消息后分页不漏项（仅未读=%s，回执失败=%s）",
  async (unreadOnly, readFails) => {
    let messages = Array.from({ length: 25 }, (_, index) => ({
      ...announcementItem,
      id: `announcement:page-${index + 1}`,
      title: `分页公告${String(index + 1).padStart(2, "0")}`,
      detail: `第 ${index + 1} 条公告全文。`,
    }));
    const unreadCount = () => messages.filter((item) => item.unread).length;
    mocks.getStudioMessagesSummary.mockImplementation(async () => ({
      ...summary(),
      total_unread: unreadCount(),
      by_category: {
        task: 0,
        billing: 0,
        announcement: unreadCount(),
        security: 0,
      },
    }));
    mocks.getStudioMessages.mockImplementation(
      async (request: {
        unreadOnly: boolean;
        offset: number;
        limit: number;
      }) => {
        const filtered = request.unreadOnly
          ? messages.filter((item) => item.unread)
          : messages;
        return {
          ...page(
            filtered.slice(request.offset, request.offset + request.limit),
          ),
          total: filtered.length,
          unread_count: unreadCount(),
          has_more: request.offset + request.limit < filtered.length,
        };
      },
    );
    mocks.markStudioMessagesRead.mockImplementation(
      async ({ keys }: { keys: string[] }) => {
        if (readFails) throw new Error("回执暂不可用");
        messages = messages.map((item) =>
          keys.includes(item.id) ? { ...item, unread: false } : item,
        );
        return { marked: keys.length, read_before: {} };
      },
    );
    render(<MessageCenter />);
    fireEvent.click(screen.getByRole("button", { name: "打开消息中心" }));
    if (unreadOnly) fireEvent.click(screen.getByRole("checkbox"));
    await screen.findByText("分页公告20");

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /分页公告01/ }));
    });
    fireEvent.click(await screen.findByRole("button", { name: "加载更多" }));
    await screen.findByText("分页公告25");

    const expected = unreadOnly
      ? messages.filter((item) => item.unread)
      : messages;
    for (const item of expected) {
      expect(screen.getAllByText(item.title)).toHaveLength(1);
    }
    const firstMessage = screen.queryByRole("button", {
      name: /分页公告01/,
    });
    // 公告可以暂留供用户阅读展开的正文，但成功回执后不能继续显示未读点。
    if (!readFails && firstMessage) {
      expect(firstMessage.querySelector(".mc-item__dot")).toBeNull();
    }
  },
);

it("翻页与已读回执在途时串行处理，后续未读消息不遗漏", async () => {
  let messages = Array.from({ length: 45 }, (_, index) => ({
    ...announcementItem,
    id: `announcement:serial-${index + 1}`,
    title: `串行公告${String(index + 1).padStart(2, "0")}`,
    detail: `第 ${index + 1} 条串行公告全文。`,
  }));
  let resolveNextPage!: (value: StudioMessagesPage) => void;
  const nextPage = new Promise<StudioMessagesPage>((resolve) => {
    resolveNextPage = resolve;
  });
  let resolveRead!: () => void;
  const readReceipt = new Promise<void>((resolve) => {
    resolveRead = resolve;
  });
  mocks.getStudioMessages.mockImplementation(
    async (request: { unreadOnly: boolean; offset: number; limit: number }) => {
      if (request.offset === 20) return nextPage;
      const filtered = request.unreadOnly
        ? messages.filter((item) => item.unread)
        : messages;
      return {
        ...page(filtered.slice(request.offset, request.offset + request.limit)),
        total: filtered.length,
        has_more: request.offset + request.limit < filtered.length,
      };
    },
  );
  mocks.markStudioMessagesRead.mockImplementation(
    async ({ keys }: { keys: string[] }) => {
      await readReceipt;
      messages = messages.map((item) =>
        keys.includes(item.id) ? { ...item, unread: false } : item,
      );
      return { marked: keys.length, read_before: {} };
    },
  );
  render(<MessageCenter />);
  fireEvent.click(screen.getByRole("button", { name: "打开消息中心" }));
  fireEvent.click(screen.getByRole("checkbox"));
  await screen.findByText("串行公告20");

  fireEvent.click(screen.getByRole("button", { name: "加载更多" }));
  const firstMessage = screen.getByRole("button", { name: /串行公告01/ });
  expect(firstMessage).toBeDisabled();
  fireEvent.click(firstMessage);
  expect(mocks.markStudioMessagesRead).not.toHaveBeenCalled();
  await act(async () => {
    resolveNextPage({
      ...page(messages.slice(20, 40)),
      total: 45,
      has_more: true,
    });
  });
  await screen.findByText("串行公告40");

  fireEvent.click(firstMessage);
  await screen.findByText("第 1 条串行公告全文。");
  const callsBeforeReceipt = mocks.getStudioMessages.mock.calls.length;
  const loadMore = screen.getByRole("button", { name: "加载更多" });
  expect(loadMore).toBeDisabled();
  fireEvent.click(loadMore);
  expect(mocks.getStudioMessages).toHaveBeenCalledTimes(callsBeforeReceipt);
  await act(async () => resolveRead());
  await waitFor(() => expect(loadMore).toBeEnabled());
  expect(firstMessage.querySelector(".mc-item__dot")).toBeNull();
  expect(screen.getByText("第 1 条串行公告全文。")).toBeInTheDocument();

  fireEvent.click(loadMore);
  await screen.findByText("串行公告45");
  for (const item of messages.filter((item) => item.unread)) {
    expect(screen.getAllByText(item.title)).toHaveLength(1);
  }
});

it("偏好保存触发的迟到列表刷新不回滚已读状态或漏掉未读消息", async () => {
  let messages = Array.from({ length: 25 }, (_, index) => ({
    ...announcementItem,
    id: `announcement:refresh-${index + 1}`,
    title: `刷新公告${String(index + 1).padStart(2, "0")}`,
  }));
  let resolveSave!: () => void;
  const saving = new Promise<void>((resolve) => {
    resolveSave = resolve;
  });
  let resolveRead!: () => void;
  const receipt = new Promise<void>((resolve) => {
    resolveRead = resolve;
  });
  let deferRefresh = false;
  let resolveRefresh!: () => void;
  mocks.getStudioMessages.mockImplementation(
    (request: { unreadOnly: boolean; offset: number; limit: number }) => {
      const filtered = request.unreadOnly
        ? messages.filter((item) => item.unread)
        : messages;
      const result = {
        ...page(filtered.slice(request.offset, request.offset + request.limit)),
        total: filtered.length,
        has_more: request.offset + request.limit < filtered.length,
      };
      if (deferRefresh && request.offset === 0) {
        deferRefresh = false;
        return new Promise<StudioMessagesPage>((resolve) => {
          // 保留请求时的快照，让刷新晚于已读回执返回。
          resolveRefresh = () => resolve(result);
        });
      }
      return Promise.resolve(result);
    },
  );
  mocks.updateStudioMessagePreferences.mockImplementation(
    async (value: unknown) => {
      await saving;
      return value;
    },
  );
  mocks.markStudioMessagesRead.mockImplementation(
    async ({ keys }: { keys: string[] }) => {
      await receipt;
      messages = messages.map((item) =>
        keys.includes(item.id) ? { ...item, unread: false } : item,
      );
      return { marked: keys.length, read_before: {} };
    },
  );
  render(<MessageCenter />);
  fireEvent.click(screen.getByRole("button", { name: "打开消息中心" }));
  fireEvent.click(screen.getByRole("checkbox"));
  await screen.findByText("刷新公告20");
  fireEvent.click(screen.getByRole("button", { name: "消息设置" }));
  fireEvent.click(await screen.findByRole("button", { name: "每日汇总" }));
  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: "返回" }));
  });

  fireEvent.click(screen.getByRole("button", { name: /刷新公告01/ }));
  deferRefresh = true;
  await act(async () => resolveSave());
  await act(async () => resolveRead());
  await act(async () => resolveRefresh());
  fireEvent.click(screen.getByRole("button", { name: "加载更多" }));
  await screen.findByText("刷新公告25");
  for (const item of messages.filter((item) => item.unread)) {
    expect(screen.getAllByText(item.title)).toHaveLength(1);
  }

  // 保存与其刷新均完成后，公告仍可正常展开并提交一次已读回执。
  const firstMessage = screen.getByRole("button", { name: /刷新公告01/ });
  fireEvent.click(firstMessage);
  await waitFor(() =>
    expect(firstMessage.querySelector(".mc-item__dot")).toBeNull(),
  );
  expect(mocks.markStudioMessagesRead).toHaveBeenCalledTimes(1);
  expect(
    screen.getByText(announcementItem.detail as string),
  ).toBeInTheDocument();
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
