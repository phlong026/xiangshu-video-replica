import { afterEach, describe, expect, it, vi } from "vitest";
import type { NativeSourceFile } from "../nativeSource";

const bridge = vi.hoisted(() => ({ restore: vi.fn(), choose: vi.fn() }));
vi.mock("../nativeSource", async (original) => ({
  ...(await original<object>()),
  restoreNativeSource: bridge.restore,
  chooseNativeSource: bridge.choose,
}));
const source: NativeSourceFile = {
  native_source_id: "source-1",
  account_id: "one",
  name: "原片.mp4",
  size: 1024,
  type: "video/mp4",
  duration_seconds: 60,
  sha256: "a".repeat(64),
};
afterEach(() => {
  localStorage.clear();
  vi.resetAllMocks();
  vi.resetModules();
});
describe("原片恢复", () => {
  it("重启后通过不透明 ID 恢复，存储中没有路径或媒体字节", async () => {
    let cache = await import("./localSourceSession");
    cache.rememberLocalSource("one", "asset", { file: source, duration: 60 });
    expect(
      JSON.parse(localStorage.getItem("native-source:one:asset")!),
    ).toEqual({ native_source_id: "source-1" });
    vi.resetModules();
    cache = await import("./localSourceSession");
    bridge.restore.mockResolvedValue(source);
    expect((await cache.restoreLocalSource("one", "asset"))?.file).toEqual(
      source,
    );
    expect(bridge.restore).toHaveBeenCalledWith("one", "source-1");
    expect(await cache.restoreLocalSource("two", "asset")).toBeUndefined();
  });
  it("文件移动时由用户重新定位，同一原片 ID 送往原生校验", async () => {
    localStorage.setItem(
      "native-source:one:asset",
      JSON.stringify({ native_source_id: "source-1" }),
    );
    bridge.restore.mockRejectedValue(new Error("原视频不存在"));
    bridge.choose.mockResolvedValue(source);
    const cache = await import("./localSourceSession");
    expect(
      (await cache.restoreLocalSource("one", "asset", true))?.file,
    ).toEqual(source);
    expect(bridge.choose).toHaveBeenCalledWith("one", "source-1");
  });
  it("用户取消定位时保留原来源，不向音轨发起视觉拆解", async () => {
    localStorage.setItem(
      "native-source:one:asset",
      JSON.stringify({ native_source_id: "source-1" }),
    );
    bridge.restore.mockRejectedValue(new Error("原视频不存在"));
    bridge.choose.mockResolvedValue(null);
    const cache = await import("./localSourceSession");
    await expect(
      cache.restoreLocalSource("one", "asset", true),
    ).rejects.toThrow("未重新定位");
    expect(localStorage.getItem("native-source:one:asset")).not.toBeNull();
  });
});
