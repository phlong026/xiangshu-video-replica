import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { setAdminCsrfToken } from "../api";
import { ViralRuntimeSection } from "./ViralRuntimeSection";

function response(payload: unknown) {
  return Promise.resolve({
    ok: true,
    status: 200,
    json: async () => payload,
    text: async () => JSON.stringify(payload),
  });
}

const controls = {
  collection_enabled: true,
  import_enabled: true,
  pending_imports: 2,
  running_imports: 1,
  failed_imports: 3,
  pending_refreshes: 1,
  running_refreshes: 1,
  failed_refreshes: 0,
  source_configured: true,
  platforms: [
    {
      platform: "douyin",
      cached_videos: 24,
      last_fetched_at: "2026-09-07 10:00:00",
      refresh_status: "ok",
      last_refresh_error: null,
    },
    {
      platform: "wechat_channels",
      cached_videos: 18,
      last_fetched_at: null,
      refresh_status: "refreshing",
      last_refresh_error: null,
    },
  ],
};

describe("ViralRuntimeSection", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    setAdminCsrfToken("");
  });

  it("展示平台缓存、导入队列与运行开关", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => response(controls)),
    );
    render(<ViralRuntimeSection />);

    expect(await screen.findByText(/导入任务：排队 2/)).toBeInTheDocument();
    expect(screen.getByText(/数据源：已配置/)).toHaveTextContent(
      /刷新任务：\s*排队 1 \/ 执行 1 \/ 失败 0/,
    );
    expect(screen.getByText(/2026-09-07 10:00:00/)).toHaveTextContent(
      "抖音：已缓存 24条",
    );
    expect(screen.getByText(/最后采集 暂无/)).toHaveTextContent(
      "视频号：已缓存 18条",
    );
  });

  it("通过带原因的幂等写暂停采集", async () => {
    setAdminCsrfToken("csrf-viral");
    const fetchMock = vi.fn((_url: string, init?: RequestInit) => {
      if (init?.method === "PATCH") {
        return response({ ...controls, collection_enabled: false });
      }
      return response(controls);
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<ViralRuntimeSection />);

    fireEvent.click(await screen.findByRole("button", { name: "暂停采集" }));
    fireEvent.change(screen.getByLabelText("操作原因"), {
      target: { value: "上游错误率超标" },
    });
    fireEvent.click(screen.getByRole("button", { name: "确认更新" }));

    expect(
      await screen.findByText("爆款视频运行开关已更新。"),
    ).toBeInTheDocument();
    const patch = fetchMock.mock.calls.find(
      ([, init]) => init?.method === "PATCH",
    );
    expect(patch?.[0]).toContain("/api/control/settings/viral");
    expect(JSON.parse(String(patch?.[1]?.body))).toEqual({
      collection_enabled: false,
      import_enabled: true,
      confirm: true,
      reason: "上游错误率超标",
    });
  });
});
