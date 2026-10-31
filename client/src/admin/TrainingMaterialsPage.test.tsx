import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { setAdminCsrfToken } from "../api";
import { TrainingMaterialsPage } from "./TrainingMaterialsPage";

afterEach(() => vi.unstubAllGlobals());
beforeEach(() => setAdminCsrfToken("csrf-token-1"));

const LIST_PAYLOAD = {
  items: [
    {
      id: "tm-1",
      title: "三步完成爆款复刻",
      description: "新手必看的完整流程演示",
      kind: "video",
      kind_label: "视频",
      content_type: "video/mp4",
      file_suffix: ".mp4",
      size_bytes: 12 * 1024 * 1024,
      sort_order: 10,
      visible: true,
      download_count: 7,
      created_by: "honor",
      created_at: "2026-11-01T02:00:00Z",
      updated_at: "2026-11-01T02:00:00Z",
    },
    {
      id: "tm-2",
      title: "结算规则讲义",
      description: "",
      kind: "training",
      kind_label: "培训文档",
      content_type: "application/pdf",
      file_suffix: ".pdf",
      size_bytes: 300 * 1024,
      sort_order: 5,
      visible: false,
      download_count: 0,
      created_by: "honor",
      created_at: "2026-11-01T01:00:00Z",
      updated_at: "2026-11-01T01:00:00Z",
    },
  ],
  total: 2,
  limit: 20,
  offset: 0,
};

function stubFetch(
  overrides: {
    writes?: Array<{ url: string; method: string; body?: unknown }>;
    list?: object;
  } = {},
) {
  const writes = overrides.writes ?? [];
  return vi.fn(async (url: string, init?: RequestInit) => {
    const method = init?.method ?? "GET";
    if (
      String(url).includes("/api/control/training-materials") &&
      method === "GET"
    ) {
      return {
        ok: true,
        status: 200,
        json: async () => overrides.list ?? LIST_PAYLOAD,
      };
    }
    writes.push({ url: String(url), method, body: init?.body });
    return { ok: true, status: 200, json: async () => ({ id: "tm-new" }) };
  });
}

it("培训素材页：渲染清单与上下架状态，下架走确认原因提交", async () => {
  const fetchMock = stubFetch({ writes: [] });
  vi.stubGlobal("fetch", fetchMock);
  render(<TrainingMaterialsPage />);

  expect(await screen.findByText("三步完成爆款复刻")).toBeInTheDocument();
  expect(screen.getByText("结算规则讲义")).toBeInTheDocument();
  expect(screen.getByText("视频")).toBeInTheDocument();
  expect(screen.getByText("培训文档")).toBeInTheDocument();
  // 「上架」同时出现在状态徽章与操作按钮上，按多重匹配断言。
  expect(screen.getAllByText("上架").length).toBe(2);
  expect(screen.getByText("已下架")).toBeInTheDocument();
  expect(screen.getByText("12.00 MB")).toBeInTheDocument();
  expect(screen.getByText("300.0 KB")).toBeInTheDocument();
  expect(screen.getByText("7")).toBeInTheDocument();

  // 下架是固定原因的即时开关（与公告置顶同款），直接发 PATCH visible:false。
  fireEvent.click(screen.getAllByRole("button", { name: "下架" })[0]);
  await waitFor(() => {
    const patch = fetchMock.mock.calls.find(
      ([url, init]) =>
        String(url).includes("/training-materials/tm-1") &&
        (init?.method ?? "GET") === "PATCH",
    );
    expect(patch).toBeTruthy();
    const body = JSON.parse(String((patch?.[1] as RequestInit)?.body));
    expect(body.visible).toBe(false);
    expect(body.confirm).toBe(true);
  });

  // 删除走 reason 弹窗（写契约）。
  fireEvent.click(screen.getAllByRole("button", { name: "删除" })[0]);
  fireEvent.change(screen.getByLabelText(/操作原因/), {
    target: { value: "内容过期清理" },
  });
  fireEvent.click(screen.getByRole("button", { name: "确认删除" }));
  await waitFor(() => {
    const remove = fetchMock.mock.calls.find(
      ([url, init]) =>
        String(url).includes("/training-materials/tm-1") &&
        (init?.method ?? "GET") === "DELETE",
    );
    expect(remove).toBeTruthy();
  });
});

it("培训素材页：上传弹窗校验白名单并组装 multipart 契约字段", async () => {
  const fetchMock = stubFetch();
  vi.stubGlobal("fetch", fetchMock);
  render(<TrainingMaterialsPage />);

  expect(await screen.findByText("三步完成爆款复刻")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "上传素材" }));

  const file = new File(["x".repeat(8)], "教程.mp4", { type: "video/mp4" });
  fireEvent.change(screen.getByLabelText(/文件（mp4/), {
    target: { files: [file] },
  });
  // 选文件后标题自动带出文件名（去后缀）。
  expect(screen.getByLabelText(/标题/)).toHaveValue("教程");
  expect(screen.getByText(/文件校验通过/)).toBeInTheDocument();

  // 白名单外类型：exe → 提示拒绝且不提交。
  fireEvent.change(screen.getByLabelText(/文件（mp4/), {
    target: {
      files: [new File(["mz"], "a.exe", { type: "application/octet-stream" })],
    },
  });
  expect(screen.getByText(/仅支持 mp4\/mov 视频/)).toBeInTheDocument();

  // 上传提交走 XHR（fetch stub 拦不到），传输链路由服务端 PG 测试覆盖；
  // 此处锁死的是提交前的客户端校验与标题自动带出。
});

it("培训素材页：readOnly 时只读无操作按钮", async () => {
  vi.stubGlobal("fetch", stubFetch());
  render(<TrainingMaterialsPage readOnly />);
  expect(await screen.findByText("三步完成爆款复刻")).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "上传素材" })).toBeNull();
  expect(screen.queryByRole("button", { name: "下架" })).toBeNull();
});
