import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { setAdminCsrfToken } from "../api";
import { ViralAcquirePage } from "./ViralAcquirePage";

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

function respond(body: unknown, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  };
}

function row(overrides: Record<string, unknown> = {}) {
  return {
    platform: "douyin",
    video_id: "acquire-1",
    category: "",
    title: "秋天第一杯奶茶 · 探店 vlog",
    author: "奶茶日记",
    author_avatar: null,
    verified: true,
    tags: ["探店", "奶茶"],
    duration_ms: 34_000,
    likes: 231_000,
    comments: 1852,
    shares: 4371,
    collects: 9620,
    published_display: "11-13",
    created_at: "2026-11-14T02:32:00Z",
    content_state: "ready",
    customer_visible: true,
    homepage_featured: true,
    homepage_rank: 3,
    pushed_at: "2026-11-14T02:40:00Z",
    media_status: "SUCCEEDED",
    storage_uri: "fake://test/v.mp4",
    availability: "AVAILABLE",
    ...overrides,
  };
}

function installFetch(
  overrides: {
    list?: () => unknown;
    write?: (
      url: string,
      init: RequestInit,
    ) => { body: unknown; status?: number } | undefined;
  } = {},
) {
  const fetchMock = vi.fn(async (url: string | URL, init?: RequestInit) => {
    const path = String(url);
    const method = (init?.method ?? "GET").toUpperCase();
    if (method !== "GET") {
      const custom = overrides.write?.(path, (init ?? {}) as RequestInit);
      if (custom) return respond(custom.body, custom.status ?? 200);
      if (path.includes("link-import")) {
        return respond(
          {
            items: [
              row({
                video_id: "link-new",
                title: "新采集视频",
                content_state: "pending_prepare",
                customer_visible: false,
                homepage_featured: false,
                homepage_rank: null,
                pushed_at: null,
                cover_url: null,
              }),
            ],
            platform: "douyin",
            videoId: "link-new",
            archiveQueued: true,
          },
          202,
        );
      }
      if (path.includes("/uploads")) {
        return respond(
          {
            platform: "wechat_channels",
            videoId: "upload-new",
            autoPublished: true,
          },
          201,
        );
      }
      if (path.includes("curation:batch")) {
        return respond({ action: "hide", count: 1, items: [] }, 200);
      }
      return respond({ ok: true });
    }
    if (path.includes("/api/control/viral/videos?")) {
      return respond(
        overrides.list?.() ?? { items: [row()], total: 1, categories: [] },
      );
    }
    return respond({});
  });
  vi.stubGlobal("fetch", fetchMock);
  setAdminCsrfToken("csrf-acquire-test");
  return fetchMock;
}

it("挂载即读取最近入库并渲染卡片；转存中卡片可补转存+删除", async () => {
  installFetch({
    list: () => ({
      items: [
        row(),
        row({
          platform: "xiaohongshu",
          video_id: "acquire-2",
          title: "家装避坑指南 · 第 3 集",
          content_state: "pending_prepare",
          customer_visible: false,
          homepage_featured: false,
          homepage_rank: null,
          pushed_at: null,
          media_status: "NOT_STARTED",
        }),
      ],
      total: 2,
      categories: [],
    }),
  });
  render(<ViralAcquirePage readOnly={false} />);
  await waitFor(() =>
    expect(screen.getAllByText(/秋天第一杯奶茶/).length).toBeGreaterThan(0),
  );
  expect(screen.getAllByText("抖音").length).toBeGreaterThan(0);
  expect(screen.getAllByText("小红书").length).toBeGreaterThan(0);
  expect(screen.getByText("已就绪")).toBeTruthy();
  expect(screen.getByText("转存中")).toBeTruthy();
  // 修复后：转存中的条目提供「补转存」（修复永远卡在转存中的死状态）
  // 与「删除」（软删出池，用户报告的"无法删除"）。
  expect(screen.getByRole("button", { name: "补转存" })).toBeTruthy();
  expect(screen.getAllByRole("button", { name: "删除" }).length).toBe(2);
  // 已就绪且已上架：显示「下架」；就绪未上架的卡片显示「上架」。
  expect(screen.getByRole("button", { name: "下架" })).toBeTruthy();
});

it("粘贴链接采集成功：调用 link-import、提示入库并刷新列表", async () => {
  const fetchMock = installFetch();
  render(<ViralAcquirePage readOnly={false} />);
  await waitFor(() => expect(screen.getByRole("status")).toBeTruthy());
  fireEvent.change(screen.getByLabelText("视频链接"), {
    target: { value: "https://v.douyin.com/acquire-one" },
  });
  fireEvent.click(screen.getByRole("button", { name: "采集入库" }));
  await waitFor(() =>
    expect(screen.getByText(/已入库（抖音 · link-new）/).textContent).toContain(
      "自动转存",
    ),
  );
  const importCall = fetchMock.mock.calls.find(([url]) =>
    String(url).includes("link-import"),
  ) as unknown as [string, RequestInit];
  expect(JSON.parse(String(importCall[1].body)).url).toBe(
    "https://v.douyin.com/acquire-one",
  );
  // 首次挂载 + 成功后刷新：列表至少请求两次。
  const listCalls = fetchMock.mock.calls.filter(([url]) =>
    String(url).includes("/api/control/viral/videos?"),
  );
  expect(listCalls.length).toBeGreaterThanOrEqual(2);
});

it("链接采集失败：显示服务端错误，重试换用新的幂等键", async () => {
  let attempts = 0;
  const keys: string[] = [];
  installFetch({
    write: (url, init) => {
      if (!url.includes("link-import")) return undefined;
      attempts += 1;
      keys.push(String(new Headers(init.headers).get("Idempotency-Key")));
      return attempts === 1
        ? {
            body: {
              detail: { message: "平台不支持", code: "VIRAL_PLATFORM_INVALID" },
            },
            status: 422,
          }
        : {
            body: {
              items: [],
              platform: "douyin",
              videoId: "retry-ok",
              archiveQueued: true,
            },
            status: 202,
          };
    },
  });
  render(<ViralAcquirePage readOnly={false} />);
  await waitFor(() => expect(screen.getByRole("status")).toBeTruthy());
  fireEvent.change(screen.getByLabelText("视频链接"), {
    target: { value: "https://v.douyin.com/retry-me" },
  });
  fireEvent.click(screen.getByRole("button", { name: "采集入库" }));
  await waitFor(() => expect(screen.getByText(/平台不支持/)).toBeTruthy());
  fireEvent.click(screen.getByRole("button", { name: "采集入库" }));
  await waitFor(() => expect(screen.getByText(/已入库/)).toBeTruthy());
  expect(attempts).toBe(2);
  expect(keys[0]).not.toBe(keys[1]);
});

it("上传弹窗：本地预读失败不阻塞补填，提交携带平台与文件", async () => {
  const fetchMock = installFetch();
  render(<ViralAcquirePage readOnly={false} />);
  await waitFor(() => expect(screen.getByRole("status")).toBeTruthy());
  fireEvent.click(screen.getByRole("button", { name: "上传视频" }));
  fireEvent.change(screen.getByLabelText("选择视频文件"), {
    target: {
      files: [new File(["video-bytes"], "case.mp4", { type: "video/mp4" })],
    },
  });
  const dialog = await screen.findByRole("dialog", {
    name: "上传视频 · 补充信息",
  });
  expect(dialog).toBeTruthy();
  // jsdom 不解码视频：等预读降级文案出现（4s 超时守卫）。
  await waitFor(
    () => expect(screen.getAllByText(/本地预读失败/).length).toBeGreaterThan(0),
    { timeout: 8000 },
  );
  fireEvent.change(screen.getByLabelText("视频标题"), {
    target: { value: "别墅设计干货" },
  });
  fireEvent.change(screen.getByLabelText("点赞"), { target: { value: "120" } });
  fireEvent.click(screen.getByRole("button", { name: "确认上传入库" }));
  await waitFor(() =>
    expect(screen.getByText(/已入库并自动上架/)).toBeTruthy(),
  );
  const uploadCall = fetchMock.mock.calls.find(([url]) =>
    String(url).includes("/uploads"),
  ) as unknown as [string, RequestInit];
  const form = uploadCall[1].body as FormData;
  expect(form.get("platform")).toBe("wechat_channels");
  expect(form.get("title")).toBe("别墅设计干货");
  expect(form.get("likes")).toBe("120");
  expect(form.get("file")).toBeInstanceOf(File);
  expect(screen.queryByRole("dialog")).toBeNull();
});

it("下架：对已就绪卡片调用 curation:batch hide 并刷新列表", async () => {
  const fetchMock = installFetch();
  render(<ViralAcquirePage readOnly={false} />);
  await waitFor(() =>
    expect(screen.getAllByText(/秋天第一杯奶茶/).length).toBeGreaterThan(0),
  );
  fireEvent.click(screen.getByRole("button", { name: "下架" }));
  await waitFor(() => {
    const curation = fetchMock.mock.calls.find(([url]) =>
      String(url).includes("curation:batch"),
    ) as unknown as [string, RequestInit] | undefined;
    expect(curation).toBeTruthy();
    const body = JSON.parse(String(curation?.[1].body));
    expect(body.action).toBe("hide");
    expect(body.items).toEqual([{ platform: "douyin", video_id: "acquire-1" }]);
  });
});

it("补转存：对转存中卡片调用 archive 端点入队媒体转存", async () => {
  const fetchMock = installFetch({
    list: () => ({
      items: [
        row({
          content_state: "pending_prepare",
          customer_visible: false,
          homepage_featured: false,
          homepage_rank: null,
          pushed_at: null,
          media_status: "NOT_STARTED",
        }),
      ],
      total: 1,
      categories: [],
    }),
  });
  render(<ViralAcquirePage readOnly={false} />);
  await waitFor(() => expect(screen.getByText("转存中")).toBeTruthy());
  fireEvent.click(screen.getByRole("button", { name: "补转存" }));
  await waitFor(() => expect(screen.getByText(/已排队媒体转存/)).toBeTruthy());
  const archive = fetchMock.mock.calls.find(([url]) =>
    String(url).includes("/archive"),
  ) as unknown as [string, RequestInit] | undefined;
  expect(archive).toBeTruthy();
  expect(String(archive?.[0])).toContain(
    "/api/control/viral/videos/douyin/acquire-1/archive",
  );
});

it("删除：确认后对卡片调用 curation:batch delete，条目移出内容池", async () => {
  const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(true);
  const fetchMock = installFetch();
  render(<ViralAcquirePage readOnly={false} />);
  await waitFor(() =>
    expect(screen.getAllByText(/秋天第一杯奶茶/).length).toBeGreaterThan(0),
  );
  fireEvent.click(screen.getByRole("button", { name: "删除" }));
  expect(confirmSpy).toHaveBeenCalled();
  await waitFor(() => expect(screen.getByText(/已删除/)).toBeTruthy());
  const curation = fetchMock.mock.calls.find(([url]) =>
    String(url).includes("curation:batch"),
  ) as unknown as [string, RequestInit];
  const body = JSON.parse(String(curation?.[1].body));
  expect(body.action).toBe("delete");
  expect(body.items).toEqual([{ platform: "douyin", video_id: "acquire-1" }]);
});

it("删除需确认：取消 confirm 不发起请求", async () => {
  vi.spyOn(window, "confirm").mockReturnValue(false);
  const fetchMock = installFetch();
  render(<ViralAcquirePage readOnly={false} />);
  await waitFor(() =>
    expect(screen.getAllByText(/秋天第一杯奶茶/).length).toBeGreaterThan(0),
  );
  fireEvent.click(screen.getByRole("button", { name: "删除" }));
  expect(
    fetchMock.mock.calls.filter(([url]) =>
      String(url).includes("curation:batch"),
    ).length,
  ).toBe(0);
});

it("上架：对已就绪但未上架卡片调用 curation:batch push", async () => {
  const fetchMock = installFetch({
    list: () => ({
      items: [
        row({
          customer_visible: false,
          homepage_featured: false,
          homepage_rank: null,
          pushed_at: null,
          collection_published: false,
        }),
      ],
      total: 1,
      categories: [],
    }),
  });
  render(<ViralAcquirePage readOnly={false} />);
  await waitFor(() =>
    expect(screen.getAllByText(/秋天第一杯奶茶/).length).toBeGreaterThan(0),
  );
  expect(screen.getByRole("button", { name: "上架" })).toBeTruthy();
  expect(screen.queryByRole("button", { name: "下架" })).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "上架" }));
  await waitFor(() => expect(screen.getByText(/已上架/)).toBeTruthy());
  const curation = fetchMock.mock.calls.find(([url]) =>
    String(url).includes("curation:batch"),
  ) as unknown as [string, RequestInit];
  expect(JSON.parse(String(curation?.[1].body)).action).toBe("push");
});

it("重新上架：对已下架卡片调用 curation:batch push", async () => {
  const fetchMock = installFetch({
    list: () => ({
      items: [
        row({
          availability: "HIDDEN",
          customer_visible: false,
          homepage_featured: false,
          homepage_rank: null,
          pushed_at: null,
        }),
      ],
      total: 1,
      categories: [],
    }),
  });
  render(<ViralAcquirePage readOnly={false} />);
  await waitFor(() => expect(screen.getByText("已下架")).toBeTruthy());
  expect(screen.queryByRole("button", { name: "下架" })).toBeNull();
  expect(screen.getByRole("button", { name: "删除" })).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "重新上架" }));
  await waitFor(() => expect(screen.getByText(/已上架/)).toBeTruthy());
  const curation = fetchMock.mock.calls.find(([url]) =>
    String(url).includes("curation:batch"),
  ) as unknown as [string, RequestInit];
  expect(JSON.parse(String(curation?.[1].body)).action).toBe("push");
});

it("只读视图：采集与上传入口禁用，卡片不提供下架", async () => {
  installFetch();
  render(<ViralAcquirePage readOnly={true} />);
  await waitFor(() =>
    expect(screen.getAllByText(/秋天第一杯奶茶/).length).toBeGreaterThan(0),
  );
  const go = screen.getByRole("button", {
    name: "采集入库",
  }) as HTMLButtonElement;
  expect(go.disabled).toBe(true);
  expect(
    (screen.getByRole("button", { name: "上传视频" }) as HTMLButtonElement)
      .disabled,
  ).toBe(true);
  expect(screen.queryByRole("button", { name: "下架" })).toBeNull();
  expect((screen.getByLabelText("视频链接") as HTMLInputElement).disabled).toBe(
    true,
  );
});
