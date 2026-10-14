import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { setAdminCsrfToken } from "../api";
import { AnnouncementsPage } from "./AnnouncementsPage";
import { CustomerLifecyclePage } from "./CustomerLifecyclePage";

afterEach(() => vi.unstubAllGlobals());
beforeEach(() => setAdminCsrfToken("csrf-token-1"));

it("公告页：清单渲染已读数，发布走确认弹窗并回调创建", async () => {
  const fetchMock = vi.fn(async (url: string, init?: RequestInit) => ({
    ok: true,
    status: 200,
    json: async () => {
      if (String(url).includes("/api/control/announcements")) {
        if ((init?.method ?? "GET") === "GET") {
          return {
            items: [
              {
                id: "a-1",
                title: "今晚维护",
                body: "23:00-24:00 例行维护",
                tone: "maintenance",
                tone_label: "维护",
                starts_at: "",
                ends_at: "",
                pinned: true,
                created_by: "honor",
                created_at: "2026-10-08T07:00:00Z",
                read_count: 12,
                live: true,
                status: "live",
              },
            ],
            total: 1,
            audience_total: 48,
          };
        }
        return { id: "a-2" };
      }
      return {};
    },
  }));
  vi.stubGlobal("fetch", fetchMock);
  render(<AnnouncementsPage />);
  expect(await screen.findByText("今晚维护")).toBeInTheDocument();
  expect(screen.getByText("12 人")).toBeInTheDocument();
  expect(screen.getByText(/共 48 名客户（25.0%）/)).toBeInTheDocument();
  expect(screen.getByText("生效中")).toBeInTheDocument();

  fireEvent.click(screen.getByRole("button", { name: "发布公告" }));
  fireEvent.change(screen.getByLabelText(/标题/), {
    target: { value: "充值满赠活动" },
  });
  fireEvent.change(screen.getByLabelText(/正文/), {
    target: { value: "本月充值 100 送 20" },
  });
  fireEvent.change(screen.getByLabelText("操作原因"), {
    target: { value: "发布活动公告" },
  });
  fireEvent.click(screen.getByRole("button", { name: "确认发布" }));
  await waitFor(() =>
    expect(screen.getByText(/公告已发布/)).toBeInTheDocument(),
  );
  const create = fetchMock.mock.calls.find(
    ([url, init]) =>
      String(url).endsWith("/api/control/announcements") &&
      (init?.method ?? "POST") === "POST",
  );
  const body = JSON.parse(String(create?.[1]?.body));
  expect(body).toMatchObject({
    title: "充值满赠活动",
    tone: "info",
    pinned: false,
    confirm: true,
  });
});

it("客户运营页：分布卡+分层清单+勾选批量赠送逐户调账", async () => {
  const fetchMock = vi.fn(async (url: string) => {
    if (String(url).includes("/customers/lifecycle")) {
      return {
        ok: true,
        status: 200,
        json: async () => ({
          items: [
            {
              user_id: "u1",
              username: "jxhjmz",
              display_name: "江西店",
              created_at: "2026-05-01T00:00:00Z",
              last_active_at: "2026-10-01T00:00:00Z",
              available_credits: 20,
              recharge_total_fen: 100000,
              succeeded_30d: 0,
              failed_30d: 2,
              stage: "dormant",
              health_score: 20,
            },
            {
              user_id: "u2",
              username: "hongya",
              display_name: "洪雅店",
              created_at: "2026-10-01T00:00:00Z",
              last_active_at: "2026-10-07T00:00:00Z",
              available_credits: 800,
              recharge_total_fen: 50000,
              succeeded_30d: 5,
              failed_30d: 0,
              stage: "new",
              health_score: 90,
            },
          ],
          distribution: [
            { stage: "new", count: 1, min_score: 90, max_score: 90 },
            { stage: "dormant", count: 1, min_score: 20, max_score: 20 },
          ],
          total: 2,
          lifecycleRule: "分层口径",
          healthRule: "健康分口径",
          limit: 20,
          offset: 0,
        }),
      };
    }
    return {
      ok: true,
      status: 200,
      json: async () => ({}),
    };
  }) as unknown as typeof fetch;
  vi.stubGlobal("fetch", fetchMock);
  render(<CustomerLifecyclePage />);
  expect(await screen.findByText("洪雅店")).toBeInTheDocument();
  // 分层徽标与筛选下拉同名：只断言清单行存在。
  expect(screen.getAllByText("沉睡").length).toBeGreaterThan(0);
  expect(screen.getAllByText("新客").length).toBeGreaterThan(0);
  expect(screen.getAllByText(/人/).length).toBeGreaterThan(0);

  // 勾选沉睡客户 → 批量赠送 → 两户逐户 POST 调账。
  fireEvent.click(screen.getByRole("checkbox", { name: /选择 江西店/ }));
  fireEvent.click(screen.getByRole("checkbox", { name: /选择 洪雅店/ }));
  fireEvent.click(screen.getByRole("button", { name: /批量赠送/ }));
  fireEvent.change(screen.getByLabelText(/每户赠送积分/), {
    target: { value: "50" },
  });
  fireEvent.change(screen.getByLabelText("操作原因"), {
    target: { value: "十月激活活动" },
  });
  fireEvent.click(screen.getByRole("button", { name: "确认发放" }));
  // 本页沿用主线既有的内联赠送流程（含批次恢复），完成文案与之一致。
  await waitFor(() =>
    expect(screen.getByText(/已向 2 名客户各赠送 50 积分/)).toBeInTheDocument(),
  );
  const grants = (
    fetchMock as unknown as ReturnType<typeof vi.fn>
  ).mock.calls.filter(([url]) => String(url).includes("/adjustments"));
  expect(grants).toHaveLength(2);
  const first = JSON.parse(String(grants[0][1]?.body));
  expect(first).toMatchObject({
    source_document_type: "FREE_GRANT",
    credits: 50,
    confirm: true,
  });
});

it("客户运营页：readOnly 不显示批量赠送入口", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => ({
      ok: true,
      status: 200,
      json: async () => ({
        items: [],
        total: 0,
        distribution: [],
        lifecycleRule: "",
        healthRule: "",
        limit: 20,
        offset: 0,
      }),
    })) as unknown as typeof fetch,
  );
  render(<CustomerLifecyclePage readOnly />);
  await screen.findByText("该分层暂无客户。");
  expect(screen.queryByRole("button", { name: /批量赠送/ })).toBeNull();
});

function lifecyclePage(offset: number, stage: string) {
  const rows = Array.from({ length: offset < 40 ? 20 : 5 }, (_, i) => ({
    user_id: `u${offset + i}`,
    username: `user${offset + i}`,
    display_name: `门店${offset + i}`,
    created_at: "2026-05-01T00:00:00Z",
    last_active_at: "2026-10-01T00:00:00Z",
    available_credits: 0,
    recharge_total_fen: 0,
    succeeded_30d: 0,
    failed_30d: 0,
    stage: stage || "dormant",
    health_score: 30,
  }));
  return {
    items: rows,
    // 分布始终是全量统计：选中某层后其余分层卡片仍在。
    total: stage === "new" ? 3 : 45,
    distribution: [
      { stage: "new", count: 3, min_score: 0, max_score: 90 },
      { stage: "dormant", count: 42, min_score: 0, max_score: 40 },
    ],
    lifecycleRule: "",
    healthRule: "",
    limit: 20,
    offset,
  };
}

it("客户运营页：按服务端总数翻页，选中分层后其余卡片仍可点", async () => {
  const fetchMock = vi.fn(async (url: string) => {
    const params = new URL(String(url), "http://x").searchParams;
    return {
      ok: true,
      status: 200,
      json: async () =>
        String(url).includes("/customers/lifecycle")
          ? lifecyclePage(
              Number(params.get("offset") ?? 0),
              params.get("stage") ?? "",
            )
          : {},
    };
  }) as unknown as typeof fetch;
  vi.stubGlobal("fetch", fetchMock);
  render(<CustomerLifecyclePage />);
  expect(await screen.findByText("门店0")).toBeInTheDocument();
  expect(screen.getByText("第 1 / 3 页（共 45 条）")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "下一页" }));
  expect(await screen.findByText("门店20")).toBeInTheDocument();

  fireEvent.click(screen.getByRole("button", { name: /新客\s*3 人/ }));
  await waitFor(() =>
    expect(screen.getByRole("button", { name: /新客\s*3 人/ })).toHaveAttribute(
      "aria-pressed",
      "true",
    ),
  );
  expect(screen.getByRole("button", { name: /沉睡\s*42 人/ })).toHaveAttribute(
    "aria-pressed",
    "false",
  );
  expect(
    screen.getByRole("button", { name: /全部分层\s*45 人/ }),
  ).toBeVisible();
});

it("客户运营页：全选本页；只读角色没有勾选列", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => ({
      ok: true,
      status: 200,
      json: async () => lifecyclePage(0, ""),
    })) as unknown as typeof fetch,
  );
  const { unmount } = render(<CustomerLifecyclePage />);
  fireEvent.click(
    await screen.findByRole("checkbox", { name: "全选本页客户" }),
  );
  expect(
    screen.getByRole("button", { name: "批量赠送（已选 20）" }),
  ).toBeInTheDocument();
  unmount();

  render(<CustomerLifecyclePage readOnly />);
  expect(await screen.findByText("门店0")).toBeInTheDocument();
  expect(screen.queryAllByRole("checkbox")).toHaveLength(0);
});

it("公告页：编辑预填原值并按北京时间提交区间，可把失效时间清回不限", async () => {
  const fetchMock = vi.fn(async (url: string, init?: RequestInit) => ({
    ok: true,
    status: 200,
    json: async () =>
      (init?.method ?? "GET") === "GET"
        ? {
            items: [
              {
                id: "a-9",
                title: "国庆活动",
                body: "充值满赠",
                tone: "promo",
                tone_label: "活动",
                starts_at: "2026-10-20T02:00:00+00:00",
                ends_at: "2026-10-31T16:00:00+00:00",
                pinned: false,
                created_by: "honor",
                created_at: "2026-10-08T07:00:00Z",
                read_count: 0,
                live: false,
                status: "scheduled",
              },
            ],
            total: 1,
            audience_total: 10,
          }
        : { id: "a-9" },
  }));
  vi.stubGlobal("fetch", fetchMock);
  render(<AnnouncementsPage />);
  expect(await screen.findByText("待生效")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "编辑" }));
  // 2026-10-20T02:00Z = 北京时间 10:00。
  expect(screen.getByLabelText(/生效时间/)).toHaveValue("2026-10-20T10:00");
  expect(screen.getByLabelText(/标题/)).toHaveValue("国庆活动");
  expect(screen.getByRole("region", { name: "客户端预览" })).toHaveTextContent(
    "国庆活动",
  );
  fireEvent.change(screen.getByLabelText(/失效时间/), {
    target: { value: "" },
  });
  fireEvent.change(screen.getByLabelText("操作原因"), {
    target: { value: "延长活动" },
  });
  fireEvent.click(screen.getByRole("button", { name: "保存修改" }));
  await waitFor(() =>
    expect(screen.getByText(/公告已更新/)).toBeInTheDocument(),
  );
  const patch = fetchMock.mock.calls.find(
    ([, init]) => (init as RequestInit | undefined)?.method === "PATCH",
  );
  const body = JSON.parse(
    String((patch?.[1] as RequestInit | undefined)?.body),
  );
  expect(body).toMatchObject({
    starts_at: "2026-10-20T10:00:00+08:00",
    ends_at: "",
    clear_ends_at: true,
    title: "国庆活动",
  });
});
