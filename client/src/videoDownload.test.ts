import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  downloadGenerationTaskResult,
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
        blob: async () => new Blob(["video"], { type: "video/mp4" }),
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
    expect(await downloadGenerationTaskResult("task-1", "video.mp4")).toEqual({
      status: "started",
    });
    expect(native.invoke).not.toHaveBeenCalled();
    expect(HTMLAnchorElement.prototype.click).toHaveBeenCalledOnce();
  });
});
