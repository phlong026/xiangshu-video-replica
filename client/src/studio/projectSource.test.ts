import { describe, expect, it } from "vitest";
import { projectSourceAssetId } from "./projectSource";

describe("projectSourceAssetId（FIX-05）", () => {
  it("优先参考视频，只有音轨时回退到音轨", () => {
    expect(
      projectSourceAssetId({
        reference_asset_id: "video",
        script_source_asset_id: "audio",
      }),
    ).toBe("video");
    expect(
      projectSourceAssetId({
        reference_asset_id: null,
        script_source_asset_id: "audio",
      }),
    ).toBe("audio");
    expect(projectSourceAssetId({ reference_asset_id: null })).toBeUndefined();
    expect(projectSourceAssetId(undefined)).toBeUndefined();
  });
});
