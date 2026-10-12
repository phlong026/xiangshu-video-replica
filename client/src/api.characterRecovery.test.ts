import { webcrypto } from "node:crypto";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { regenerateContactSheet, setInternalAccessToken } from "./api";

const result = {
  identity_id: "identity-1",
  persona_id: "persona-1",
  character_version_id: "v2",
  previous_version_id: "v1",
  version_number: 2,
  publication_hash: "hash",
  contact_sheet_asset_id: "sheet",
  generation_source: "image_provider",
  views: [],
};
const task = (status: string) => ({
  id: "task-1",
  project_id: null,
  identity_id: "identity-1",
  operation: "REGENERATE",
  display_name: "人物",
  status,
  result: status === "SUCCEEDED" ? result : null,
  error_message: status === "SUBMISSION_UNCERTAIN" ? "结果待核对" : "明确失败",
});

beforeEach(() => {
  localStorage.clear();
  setInternalAccessToken("test-credential");
  vi.stubGlobal("crypto", webcrypto);
});

describe("人物生成同次恢复", () => {
  it("未知结果后只查询原任务，不再新建任务或预算", async () => {
    const postKeys: string[] = [];
    let current = "SUBMISSION_UNCERTAIN";
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string, init?: RequestInit) => {
        if (String(url).endsWith("/api/auth/me"))
          return Response.json({
            id: "owner",
            username: "owner",
            display_name: "owner",
            role: "employee",
          });
        if (init?.method === "POST") {
          postKeys.push(String((init.body as FormData).get("idempotency_key")));
          return Response.json(task(current));
        }
        return Response.json(task(current));
      }),
    );
    await expect(regenerateContactSheet("identity-1")).rejects.toThrow(
      "结果待核对",
    );
    await expect(regenerateContactSheet("identity-1")).rejects.toThrow(
      "结果待核对",
    );
    expect(postKeys).toHaveLength(1);
    current = "SUCCEEDED";
    await expect(regenerateContactSheet("identity-1")).resolves.toEqual(result);
    expect(postKeys).toHaveLength(1);
  });

  it("创建响应丢失时保留同次 key，明确完成后才允许新轮 key", async () => {
    const postKeys: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string, init?: RequestInit) => {
        if (String(url).endsWith("/api/auth/me"))
          return Response.json({
            id: "owner",
            username: "owner",
            display_name: "owner",
            role: "employee",
          });
        if (init?.method === "POST") {
          postKeys.push(String((init.body as FormData).get("idempotency_key")));
          if (postKeys.length === 1) throw new TypeError("连接中断");
        }
        return Response.json(task("SUCCEEDED"));
      }),
    );
    await expect(regenerateContactSheet("identity-1")).rejects.toThrow();
    await expect(regenerateContactSheet("identity-1")).resolves.toEqual(result);
    expect(postKeys[1]).toBe(postKeys[0]);
    await regenerateContactSheet("identity-1");
    expect(postKeys[2]).not.toBe(postKeys[1]);
  });

  it("明确失败后允许同参数的新一轮", async () => {
    const postKeys: string[] = [];
    let current = "FAILED";
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string, init?: RequestInit) => {
        if (String(url).endsWith("/api/auth/me"))
          return Response.json({
            id: "owner",
            username: "owner",
            display_name: "owner",
            role: "employee",
          });
        if (init?.method === "POST")
          postKeys.push(String((init.body as FormData).get("idempotency_key")));
        return Response.json(task(current));
      }),
    );
    await expect(regenerateContactSheet("identity-1")).rejects.toThrow(
      "明确失败",
    );
    current = "SUCCEEDED";
    await regenerateContactSheet("identity-1");
    expect(postKeys).toHaveLength(2);
    expect(postKeys[1]).not.toBe(postKeys[0]);
  });
});
