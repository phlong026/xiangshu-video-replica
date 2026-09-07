import { beforeEach, describe, expect, it } from "vitest";
import {
  clearScriptRewriteIdempotencyKey,
  type ScriptRewriteScope,
  scriptRewriteIdempotencyKey,
  shouldClearScriptRewriteIdempotencyKey,
} from "./scriptRewrite";

const scope: ScriptRewriteScope = {
  accountId: "account-a",
  projectId: "project-a",
  sourceAssetId: "source-a",
  identityId: "identity-a",
  scriptId: "script-a",
  scriptVersion: 3,
  text: "已保存正文",
};

describe("文案改写幂等键生命周期", () => {
  beforeEach(() => window.sessionStorage.clear());

  it("同一账号和完整来源稿件范围跨重挂载复用，任一输入变化即换键", () => {
    const first = scriptRewriteIdempotencyKey(scope);
    expect(scriptRewriteIdempotencyKey({ ...scope })).toBe(first);
    expect(
      scriptRewriteIdempotencyKey({ ...scope, accountId: "account-b" }),
    ).not.toBe(first);
    expect(
      scriptRewriteIdempotencyKey({ ...scope, sourceAssetId: "source-b" }),
    ).not.toBe(first);
    expect(
      scriptRewriteIdempotencyKey({ ...scope, text: "另一篇正文" }),
    ).not.toBe(first);
    expect(
      scriptRewriteIdempotencyKey({ ...scope, scriptVersion: 4 }),
    ).not.toBe(first);
  });

  it("只清理仍与终态请求匹配的键", () => {
    const first = scriptRewriteIdempotencyKey(scope);
    clearScriptRewriteIdempotencyKey(scope, first);
    const second = scriptRewriteIdempotencyKey(scope);
    expect(second).not.toBe(first);
    clearScriptRewriteIdempotencyKey(scope, first);
    expect(scriptRewriteIdempotencyKey(scope)).toBe(second);
  });

  it("网络、结果未知、卸载和可重试失败保留，同步永久4xx或终态永久失败清理", () => {
    expect(shouldClearScriptRewriteIdempotencyKey(new Error("network"))).toBe(
      false,
    );
    expect(
      shouldClearScriptRewriteIdempotencyKey({
        code: "SCRIPT_REWRITE_SUBMISSION_UNCERTAIN",
        retryable: false,
      }),
    ).toBe(false);
    expect(shouldClearScriptRewriteIdempotencyKey({ retryable: true })).toBe(
      false,
    );
    expect(shouldClearScriptRewriteIdempotencyKey({ status: 503 })).toBe(false);
    expect(shouldClearScriptRewriteIdempotencyKey({ status: 422 })).toBe(true);
    expect(shouldClearScriptRewriteIdempotencyKey({ retryable: false })).toBe(
      true,
    );
  });
});
