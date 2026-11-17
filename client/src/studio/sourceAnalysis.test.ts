import { describe, expect, it } from "vitest";
import {
  sourceAnalysisPurpose,
  validateSourceAnalysis,
} from "./sourceAnalysis";

describe("来源分析分流", () => {
  it.each([3.9, 4, 15, 20, 30, 30.1])("%s 秒进入普通复刻", (duration) => {
    expect(sourceAnalysisPurpose(duration)).toBe("replica");
  });
  it.each([30.11, 60, 180, 180.1])("%s 秒进入长视频拆解", (duration) => {
    expect(sourceAnalysisPurpose(duration)).toBe("replica_prompt");
  });
  it.each([NaN, Infinity, -Infinity, -1, 0, 3.8, 180.11])(
    "拒绝无效或超范围时长 %s",
    (duration) => {
      expect(() => sourceAnalysisPurpose(duration)).toThrow();
    },
  );
  it("大小不能改变时长的路由", () => {
    expect(() => validateSourceAnalysis(20, 300 * 1024 * 1024)).toThrow(
      "200 MB",
    );
    expect(validateSourceAnalysis(60, 600 * 1024 * 1024)).toBe(
      "replica_prompt",
    );
    expect(() => validateSourceAnalysis(60, 600 * 1024 * 1024 + 1)).toThrow(
      "600 MB",
    );
  });
});
