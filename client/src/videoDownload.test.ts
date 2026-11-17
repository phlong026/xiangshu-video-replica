import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  archiveGenerationTask,
  downloadGenerationTaskResult,
  downloadVideoToFile,
  isResultUrlExpired,
  openVideoDownloadFolder,
  VideoDownloadUnconfirmedError,
} from "./api";

const native = vi.hoisted(() => ({
  invoke: vi.fn(),
  listen: vi.fn(),
  isTauri: vi.fn(() => true),
}));
vi.mock("@tauri-apps/api/core", () => ({
  invoke: native.invoke,
  isTauri: native.isTauri,
}));
vi.mock("@tauri-apps/api/event", () => ({ listen: native.listen }));

type Finished = {
  download_id: string;
  success: boolean;
  path: string | null;
  error: string | null;
};

// Container-header fixture for download lifecycle tests; not a decodable video.
const mp4HeaderFixture = Uint8Array.from([
  0, 0, 0, 24, 102, 116, 121, 112, 105, 115, 111, 109, 0, 0, 0, 0, 105, 115,
  111, 109, 109, 112, 52, 50, 0, 0, 0, 9, 109, 100, 97, 116, 0,
]);

describe("desktop video download feedback", () => {
  let finish: (event: { payload: Finished }) => void;
  const unlisten = vi.fn();
  const fetchMock = vi.fn();
  const revoke = vi.fn();
  const path = "C:\\Downloads\\chosen.mp4";

  beforeEach(() => {
    vi.clearAllMocks();
    native.isTauri.mockReturnValue(true);
    native.invoke.mockImplementation(async (command: string) =>
      command === "choose_video_download"
        ? { download_id: "download-1", path }
        : undefined,
    );
    native.listen.mockImplementation(async (_event: string, callback) => {
      finish = callback;
      return unlisten;
    });
    fetchMock
      .mockReset()
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({ url: "https://provider.example/video.mp4" }),
      })
      .mockResolvedValueOnce({
        ok: true,
        blob: async () => new Blob([mp4HeaderFixture], { type: "video/mp4" }),
      });
    vi.stubGlobal("fetch", fetchMock);
    vi.stubGlobal("URL", {
      createObjectURL: vi.fn(() => "blob:http://tauri.localhost/video-1"),
      revokeObjectURL: revoke,
    });
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  it("waits for result normalization beyond the old one-minute cloud timeout", async () => {
    vi.useFakeTimers();
    fetchMock.mockReset().mockImplementationOnce(
      (_url, init: RequestInit) =>
        new Promise((resolve, reject) => {
          init.signal?.addEventListener("abort", () =>
            reject(new DOMException("Aborted", "AbortError")),
          );
          setTimeout(
            () =>
              resolve({
                ok: true,
                json: async () => ({ result_asset_id: "saved-asset" }),
              }),
            90_000,
          );
        }),
    );
    const archived = archiveGenerationTask("slow-archive");
    await vi.advanceTimersByTimeAsync(90_001);
    await expect(archived).resolves.toMatchObject({
      result_asset_id: "saved-asset",
    });
    expect(fetchMock).toHaveBeenCalledOnce();
  });

  it("reports an unconfirmed archive on timeout without submitting again", async () => {
    vi.useFakeTimers();
    fetchMock.mockReset().mockImplementationOnce(
      (_url, init: RequestInit) =>
        new Promise((_resolve, reject) => {
          init.signal?.addEventListener("abort", () =>
            reject(new DOMException("Aborted", "AbortError")),
          );
        }),
    );
    const rejected = expect(
      archiveGenerationTask("timed-out-archive"),
    ).rejects.toThrow("保存结果暂未确认，请刷新任务核对");
    await vi.advanceTimersByTimeAsync(300_001);
    await rejected;
    expect(fetchMock).toHaveBeenCalledOnce();
  });

  it("downloads the verified 30-second asset after normalization", async () => {
    fetchMock
      .mockReset()
      .mockResolvedValueOnce(
        Response.json(
          {
            detail: {
              code: "RESULT_DURATION_PREPARATION_REQUIRED",
              message: "需完成交付校验",
            },
          },
          { status: 409 },
        ),
      )
      .mockResolvedValueOnce(Response.json({ result_asset_id: "strict-asset" }))
      .mockResolvedValueOnce(
        Response.json({ url: "https://storage.example/strict-30.mp4" }),
      )
      .mockResolvedValueOnce({
        ok: true,
        blob: async () => new Blob([mp4HeaderFixture], { type: "video/mp4" }),
      });
    const download = downloadGenerationTaskResult("task-30", "video.mp4");
    await vi.waitFor(() =>
      expect(native.invoke).toHaveBeenCalledWith(
        "start_video_download",
        expect.any(Object),
      ),
    );
    expect(fetchMock.mock.calls.map(([url]) => String(url))).toEqual([
      expect.stringContaining("/generation-tasks/task-30/preview-url"),
      expect.stringContaining("/generation-tasks/task-30/archive"),
      expect.stringContaining("/assets/strict-asset/download-url"),
      "https://storage.example/strict-30.mp4",
    ]);
    finish({
      payload: { download_id: "download-1", success: true, path, error: null },
    });
    await expect(download).resolves.toMatchObject({ status: "saved", path });
  });

  it("allows choosing a destination before fetching and cancelling without a request", async () => {
    native.invoke.mockResolvedValueOnce(null);
    expect(await downloadGenerationTaskResult("task-1", "video.mp4")).toEqual({
      status: "cancelled",
    });
    expect(native.invoke).toHaveBeenCalledWith("choose_video_download", {
      filename: "video.mp4",
    });
    expect(fetchMock).not.toHaveBeenCalled();
    expect(HTMLAnchorElement.prototype.click).not.toHaveBeenCalled();
  });

  it("reports saved only after the matching native completion, retaining the chosen path", async () => {
    const settled = vi.fn();
    const download = downloadGenerationTaskResult("task-1", "video.mp4");
    void download.then(settled);
    await vi.waitFor(() =>
      expect(native.invoke).toHaveBeenCalledWith("start_video_download", {
        downloadId: "download-1",
        url: "blob:http://tauri.localhost/video-1",
      }),
    );
    expect(HTMLAnchorElement.prototype.click).toHaveBeenCalledOnce();
    expect(native.listen).toHaveBeenCalledWith(
      "video-download-finished",
      expect.any(Function),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      "https://provider.example/video.mp4",
      { signal: expect.any(AbortSignal), credentials: "omit" },
    );
    expect(settled).not.toHaveBeenCalled();
    expect(revoke).not.toHaveBeenCalled();
    finish({
      payload: {
        download_id: "unrelated",
        success: true,
        path: "other.mp4",
        error: null,
      },
    });
    await Promise.resolve();
    expect(settled).not.toHaveBeenCalled();
    finish({
      payload: {
        download_id: "download-1",
        success: true,
        path,
        error: null,
      },
    });
    expect(await download).toEqual({
      status: "saved",
      downloadId: "download-1",
      path,
    });
    expect(unlisten).toHaveBeenCalledOnce();
    expect(revoke).toHaveBeenCalledWith("blob:http://tauri.localhost/video-1");
  });

  it("shows a write failure instead of claiming the file was saved", async () => {
    const download = downloadGenerationTaskResult("task-1", "video.mp4");
    const rejected = expect(download).rejects.toThrow("保存视频失败");
    await vi.waitFor(() =>
      expect(HTMLAnchorElement.prototype.click).toHaveBeenCalledOnce(),
    );
    finish({
      payload: {
        download_id: "download-1",
        success: false,
        path: null,
        error: "DOWNLOAD_FAILED",
      },
    });
    await rejected;
    expect(unlisten).toHaveBeenCalledOnce();
    expect(revoke).toHaveBeenCalledOnce();
  });

  it("releases a chosen destination if fetching the video fails", async () => {
    fetchMock.mockReset().mockRejectedValue(new TypeError("Failed to fetch"));
    await expect(
      downloadGenerationTaskResult("task-1", "video.mp4"),
    ).rejects.toThrow();
    expect(native.invoke).toHaveBeenCalledWith("cancel_video_download", {
      downloadId: "download-1",
    });
    expect(HTMLAnchorElement.prototype.click).not.toHaveBeenCalled();
  });

  it("does not claim an unconfirmed native download succeeded", async () => {
    vi.useFakeTimers();
    const download = downloadGenerationTaskResult("task-1", "video.mp4");
    const rejected = expect(download).rejects.toBeInstanceOf(
      VideoDownloadUnconfirmedError,
    );
    await vi.waitFor(() =>
      expect(HTMLAnchorElement.prototype.click).toHaveBeenCalledOnce(),
    );
    await vi.advanceTimersByTimeAsync(300_001);
    await rejected;
    expect(unlisten).toHaveBeenCalledOnce();
  });

  it("recovers an actual saved result when the native completion event is lost", async () => {
    vi.useFakeTimers();
    native.invoke.mockImplementation(async (command: string) => {
      if (command === "choose_video_download")
        return { download_id: "download-1", path };
      if (command === "get_video_download_status") {
        return { download_id: "download-1", path, success: true, error: null };
      }
    });
    const download = downloadGenerationTaskResult("task-1", "video.mp4");
    await vi.waitFor(() =>
      expect(HTMLAnchorElement.prototype.click).toHaveBeenCalledOnce(),
    );
    await vi.advanceTimersByTimeAsync(2_001);
    expect(native.invoke).toHaveBeenCalledWith("get_video_download_status", {
      downloadId: "download-1",
    });
    expect(await download).toEqual({
      status: "saved",
      downloadId: "download-1",
      path,
    });
    const callCount = native.invoke.mock.calls.length;
    await vi.advanceTimersByTimeAsync(6_000);
    expect(native.invoke).toHaveBeenCalledTimes(callCount);
  });

  it("reports an unavailable native status channel without claiming save failure or success", async () => {
    vi.useFakeTimers();
    native.invoke.mockImplementation(async (command: string) => {
      if (command === "choose_video_download")
        return { download_id: "download-1", path };
      if (command === "get_video_download_status")
        throw new Error("native unavailable");
    });
    const rejected = expect(
      downloadGenerationTaskResult("task-1", "video.mp4"),
    ).rejects.toThrow("尚未确认");
    await vi.waitFor(() =>
      expect(HTMLAnchorElement.prototype.click).toHaveBeenCalledOnce(),
    );
    await vi.advanceTimersByTimeAsync(2_001);
    expect(native.invoke).toHaveBeenCalledWith("get_video_download_status", {
      downloadId: "download-1",
    });
    await rejected;
    expect(unlisten).toHaveBeenCalledOnce();
  });

  it("opens only a native recorded download id, without sending an arbitrary path", async () => {
    await openVideoDownloadFolder("download-1");
    expect(native.invoke).toHaveBeenCalledWith("open_video_download_folder", {
      downloadId: "download-1",
    });
  });

  it("keeps browser downloads explicit about being started rather than saved", async () => {
    native.isTauri.mockReturnValue(false);
    fetchMock
      .mockReset()
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({ result_asset_id: "asset-1" }),
      })
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({ url: "https://signed.example/video.mp4" }),
      });
    expect(await downloadGenerationTaskResult("task-1", "video.mp4")).toEqual({
      status: "started",
    });
    expect(native.invoke).not.toHaveBeenCalled();
    expect(HTMLAnchorElement.prototype.click).toHaveBeenCalledOnce();
  });

  it("Web 通用下载验证签名地址的 MP4 后用 blob 链接启动下载，不调用桌面保存桥", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    native.isTauri.mockReturnValue(false);
    const blob = new Blob([mp4HeaderFixture], { type: "video/mp4" });
    fetchMock
      .mockReset()
      .mockResolvedValue({ ok: true, blob: async () => blob });
    const getUrl = vi.fn(
      async () => "https://signed.example/archived-viral.mp4",
    );
    const links: { href: string; download: string; attached: boolean }[] = [];
    vi.mocked(HTMLAnchorElement.prototype.click).mockImplementation(function (
      this: HTMLAnchorElement,
    ) {
      links.push({
        href: this.href,
        download: this.download,
        attached: this.isConnected,
      });
    });

    await expect(
      downloadVideoToFile(getUrl, "管理员发布.mp4"),
    ).resolves.toEqual({ status: "started" });

    expect(getUrl).toHaveBeenCalledOnce();
    expect(fetchMock).toHaveBeenCalledExactlyOnceWith(
      "https://signed.example/archived-viral.mp4",
      {
        signal: expect.any(AbortSignal),
        credentials: "omit",
      },
    );
    expect(URL.createObjectURL).toHaveBeenCalledWith(blob);
    expect(links).toEqual([
      {
        href: "blob:http://tauri.localhost/video-1",
        download: "管理员发布.mp4",
        attached: true,
      },
    ]);
    expect(document.querySelector('a[download="管理员发布.mp4"]')).toBeNull();
    expect(native.invoke).not.toHaveBeenCalled();
    expect(native.listen).not.toHaveBeenCalled();
    expect(revoke).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(1_000);
    expect(revoke).toHaveBeenCalledWith("blob:http://tauri.localhost/video-1");
  });

  it.each(["错误页", "伪装MP4", "签名失效"])(
    "Web 下载拒绝%s且不创建下载或调用桌面桥",
    async (failure) => {
      native.isTauri.mockReturnValue(false);
      fetchMock.mockReset().mockResolvedValue(
        failure === "签名失效"
          ? { ok: false, status: 403 }
          : {
              ok: true,
              blob: async () =>
                new Blob(["<html>文件暂不可用</html>"], {
                  type: failure === "错误页" ? "text/html" : "video/mp4",
                }),
            },
      );

      await expect(
        downloadVideoToFile(
          async () => "https://signed.example/unusable.mp4",
          "video.mp4",
        ),
      ).rejects.toThrow();

      expect(fetchMock).toHaveBeenCalledOnce();
      expect(URL.createObjectURL).not.toHaveBeenCalled();
      expect(HTMLAnchorElement.prototype.click).not.toHaveBeenCalled();
      expect(native.invoke).not.toHaveBeenCalled();
      expect(native.listen).not.toHaveBeenCalled();
    },
  );

  it("downloads an authorized inline fixture without fetching a data URL", async () => {
    fetchMock.mockReset().mockResolvedValueOnce({
      ok: true,
      json: async () => ({
        url: `data:video/mp4;base64,${btoa(String.fromCharCode(...mp4HeaderFixture))}`,
      }),
    });
    const download = downloadGenerationTaskResult("task-inline", "inline.mp4");
    await vi.waitFor(() =>
      expect(HTMLAnchorElement.prototype.click).toHaveBeenCalledOnce(),
    );
    finish({
      payload: { download_id: "download-1", success: true, path, error: null },
    });
    await expect(download).resolves.toMatchObject({ status: "saved" });
    expect(fetchMock).toHaveBeenCalledOnce();
  });

  it.each([
    "data:video/mp4;base64,%%%invalid%%%",
    "https://provider.example/expired.mp4",
  ])(
    "does not save invalid inline data or an unavailable provider response: %s",
    async (url) => {
      fetchMock
        .mockReset()
        .mockResolvedValueOnce({ ok: true, json: async () => ({ url }) })
        .mockResolvedValueOnce({ ok: false, status: 403 });
      await expect(
        downloadGenerationTaskResult("task-failed", "video.mp4"),
      ).rejects.toThrow();
      expect(HTMLAnchorElement.prototype.click).not.toHaveBeenCalled();
      expect(native.invoke).toHaveBeenCalledWith("cancel_video_download", {
        downloadId: "download-1",
      });
    },
  );

  it("times out fetching a native result without retrying generation", async () => {
    vi.useFakeTimers();
    fetchMock
      .mockReset()
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({ url: "https://provider.example/slow.mp4" }),
      })
      .mockImplementationOnce(
        (_url, init: RequestInit) =>
          new Promise((_resolve, reject) => {
            init.signal?.addEventListener("abort", () =>
              reject(new DOMException("Aborted", "AbortError")),
            );
          }),
      );
    // 下载失败后复查一次直链（FIX-02）：直链仍有效时保留原始下载错误。
    fetchMock.mockResolvedValueOnce({
      ok: true,
      json: async () => ({ url: "https://provider.example/slow.mp4" }),
    });
    const rejected = expect(
      downloadGenerationTaskResult("task-slow", "video.mp4"),
    ).rejects.toThrow();
    await vi.advanceTimersByTimeAsync(60_001);
    await rejected;
    expect(fetchMock).toHaveBeenCalledTimes(3);
    expect(String(fetchMock.mock.calls[2][0])).toContain(
      "/api/generation-tasks/task-slow/preview-url?verify=true",
    );
    expect(HTMLAnchorElement.prototype.click).not.toHaveBeenCalled();
  });

  it("直链已失效时把下载失败改为明确的「文件已失效」（FIX-02）", async () => {
    fetchMock
      .mockReset()
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({ url: "https://provider.example/gone.mp4" }),
      })
      .mockResolvedValueOnce({ ok: false, status: 404, json: async () => ({}) })
      .mockResolvedValueOnce({
        ok: false,
        status: 410,
        json: async () => ({
          detail: {
            code: "RESULT_URL_EXPIRED",
            message: "成片文件已失效：生成服务提供的下载链接已过期。",
          },
        }),
      });
    const error = await downloadGenerationTaskResult(
      "task-gone",
      "video.mp4",
    ).catch((cause: unknown) => cause);
    expect(isResultUrlExpired(error)).toBe(true);
    expect((error as Error).message).toContain("成片文件已失效");
  });
});
