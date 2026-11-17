import { webcrypto } from "node:crypto";
import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  CharacterSheetTaskError,
  createCharacterSceneLook,
  getLatestSceneLookTask,
  regenerateContactSheet,
  setInternalAccessToken,
} from "./api";
import { apiBaseUrl } from "./apiBase";
import {
  openCharacterOperation,
  saveCharacterOperation,
} from "./characterTaskOperation";

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

const sceneInput = {
  scene_name: "工地巡检",
  scene_description: "白天施工现场",
  costume_description: "安全帽和工装",
};
const sceneResult = {
  ...result,
  ...sceneInput,
};
const sceneTask = (id = "scene-task") => ({
  ...task("SUCCEEDED"),
  id,
  operation: "SCENE",
  result: sceneResult,
});

async function savedSceneOperation() {
  const operation = await openCharacterOperation(`${apiBaseUrl()}/owner`, [
    "SCENE",
    "identity-1",
    sceneInput.scene_name,
    sceneInput.scene_description,
    sceneInput.costume_description,
  ]);
  operation.taskId = "stale-task";
  saveCharacterOperation(operation);
  return operation;
}

function ownerResponse(url: string) {
  return url.endsWith("/api/auth/me")
    ? Response.json({
        id: "owner",
        username: "owner",
        display_name: "用户",
        role: "employee",
      })
    : null;
}

function missingTaskResponse() {
  return Response.json(
    { detail: { code: "IMAGE_TASK_NOT_FOUND", message: "生成任务不存在。" } },
    { status: 404 },
  );
}

describe("场景生成恢复与受理边界", () => {
  it.each(["FAILED", "SUCCEEDED"])(
    "恢复面板已读到 %s 后，明确的新生成不再重放历史终态",
    async (status) => {
      const operation = await savedSceneOperation();
      const unrelated = await openCharacterOperation(`${apiBaseUrl()}/owner`, [
        "SCENE",
        "another-person",
      ]);
      unrelated.taskId = "unrelated-task";
      saveCharacterOperation(unrelated);
      const postKeys: string[] = [];
      vi.stubGlobal(
        "fetch",
        vi.fn(async (url: string, init?: RequestInit) => {
          const owner = ownerResponse(String(url));
          if (owner) return owner;
          if (String(url).includes("/api/task-references/"))
            return Response.json({ short_ref: "A1B2C3D4" });
          if (String(url).endsWith("/active-or-latest"))
            return Response.json({
              ...sceneTask("stale-task"),
              status,
              error_message: status === "FAILED" ? "上次生成未完成。" : null,
            });
          if (init?.method === "POST")
            postKeys.push(JSON.parse(String(init.body)).idempotency_key);
          return Response.json(sceneTask());
        }),
      );
      const latest = await getLatestSceneLookTask("identity-1");
      expect(latest?.status).toBe(status);
      if (status === "FAILED")
        expect(latest?.error_message).toContain("错误编号：A1B2C3D4");
      await expect(
        createCharacterSceneLook("identity-1", sceneInput),
      ).resolves.toEqual(sceneResult);
      expect(postKeys).toHaveLength(1);
      expect(postKeys[0]).not.toBe(operation.key);
      expect(
        JSON.parse(localStorage.getItem(unrelated.storageKey) ?? "null").key,
      ).toBe(unrelated.key);
    },
  );

  it("恢复面板读到待核对状态时保留原任务，不开启新一轮", async () => {
    const operation = await savedSceneOperation();
    let posts = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string, init?: RequestInit) => {
        const owner = ownerResponse(String(url));
        if (owner) return owner;
        if (String(url).includes("/api/task-references/"))
          return Response.json({ short_ref: "A1B2C3D4" });
        if (init?.method === "POST") posts += 1;
        return Response.json({
          ...sceneTask("stale-task"),
          status: "SUBMISSION_UNCERTAIN",
          result: null,
          error_message: "提交结果待核对。",
        });
      }),
    );
    await getLatestSceneLookTask("identity-1");
    await expect(
      createCharacterSceneLook("identity-1", sceneInput),
    ).rejects.toMatchObject({
      status: "SUBMISSION_UNCERTAIN",
      recoveryRequired: true,
    });
    expect(posts).toBe(0);
    expect(
      JSON.parse(localStorage.getItem(operation.storageKey) ?? "null").key,
    ).toBe(operation.key);
  });

  it("失效本地任务号只用原幂等键恢复一次并保存新任务号", async () => {
    const operation = await savedSceneOperation();
    const postKeys: string[] = [];
    const checked: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string, init?: RequestInit) => {
        const owner = ownerResponse(String(url));
        if (owner) return owner;
        if (init?.method === "POST") {
          postKeys.push(JSON.parse(String(init.body)).idempotency_key);
          return Response.json(sceneTask());
        }
        checked.push(String(url));
        return String(url).endsWith("/stale-task")
          ? missingTaskResponse()
          : Response.json(sceneTask());
      }),
    );
    const observed: string[] = [];
    await expect(
      createCharacterSceneLook("identity-1", sceneInput, (next) => {
        observed.push(next.id);
      }),
    ).resolves.toEqual(sceneResult);
    expect(postKeys).toEqual([operation.key]);
    expect(checked.map((url) => url.split("/").at(-1))).toEqual([
      "stale-task",
      "scene-task",
    ]);
    expect(observed).toEqual(["scene-task", "scene-task"]);
    expect(localStorage.getItem(operation.storageKey)).toBeNull();
  });

  it("原键恢复后任务仍不存在时停止，不循环创建或丢失幂等键", async () => {
    const operation = await savedSceneOperation();
    const postKeys: string[] = [];
    let reads = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string, init?: RequestInit) => {
        const owner = ownerResponse(String(url));
        if (owner) return owner;
        if (init?.method === "POST") {
          postKeys.push(JSON.parse(String(init.body)).idempotency_key);
          return Response.json(sceneTask("stale-task"));
        }
        reads += 1;
        return missingTaskResponse();
      }),
    );
    await expect(
      createCharacterSceneLook("identity-1", sceneInput),
    ).rejects.toMatchObject({
      status: "UNKNOWN",
      taskId: "stale-task",
      recoveryRequired: true,
    });
    expect(postKeys).toEqual([operation.key]);
    expect(reads).toBe(2);
    expect(
      JSON.parse(localStorage.getItem(operation.storageKey) ?? "null"),
    ).toEqual({
      key: operation.key,
      taskId: "stale-task",
    });
  });

  it.each(["network", "server", "unrelated-404"])(
    "%s 查询失败保留原任务，不回放收费请求",
    async (kind) => {
      const operation = await savedSceneOperation();
      let posts = 0;
      vi.stubGlobal(
        "fetch",
        vi.fn(async (url: string, init?: RequestInit) => {
          const owner = ownerResponse(String(url));
          if (owner) return owner;
          if (init?.method === "POST") posts += 1;
          if (kind === "network") throw new TypeError("连接中断");
          return Response.json(
            { detail: { code: "SERVICE_UNAVAILABLE", message: "暂不可读" } },
            { status: kind === "server" ? 503 : 404 },
          );
        }),
      );
      await expect(
        createCharacterSceneLook("identity-1", sceneInput),
      ).rejects.toMatchObject({
        status: "UNKNOWN",
        taskId: "stale-task",
        recoveryRequired: true,
      });
      expect(posts).toBe(0);
      expect(
        JSON.parse(localStorage.getItem(operation.storageKey) ?? "null").key,
      ).toBe(operation.key);
    },
  );

  it("受理前明确拒绝可重试，并继续使用原键直到确认完成", async () => {
    const postKeys: string[] = [];
    let checked = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string, init?: RequestInit) => {
        const owner = ownerResponse(String(url));
        if (owner) return owner;
        if (init?.method === "POST") {
          postKeys.push(JSON.parse(String(init.body)).idempotency_key);
          if (postKeys.length === 1)
            return Response.json(
              {
                detail: {
                  code: "INSUFFICIENT_BALANCE",
                  message: "积分不足，请充值后重试。",
                },
              },
              { status: 402 },
            );
        } else checked += 1;
        return Response.json(sceneTask());
      }),
    );
    await expect(
      createCharacterSceneLook("identity-1", sceneInput),
    ).rejects.toMatchObject({
      name: "CharacterSheetTaskError",
      message: expect.stringContaining("积分不足，请充值后重试。"),
      recoveryRequired: false,
      taskId: null,
    });
    expect(checked).toBe(0);
    await expect(
      createCharacterSceneLook("identity-1", sceneInput),
    ).resolves.toEqual(sceneResult);
    expect(postKeys).toHaveLength(2);
    expect(postKeys[1]).toBe(postKeys[0]);
  });

  it("失败任务保留可展示诊断号，下一次明确生成才使用新键", async () => {
    const postKeys: string[] = [];
    let failed = true;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string, init?: RequestInit) => {
        const owner = ownerResponse(String(url));
        if (owner) return owner;
        if (String(url).includes("/api/task-references/"))
          return Response.json({ short_ref: "A1B2C3D4" });
        if (init?.method === "POST")
          postKeys.push(JSON.parse(String(init.body)).idempotency_key);
        return Response.json(
          failed
            ? {
                ...sceneTask(),
                status: "FAILED",
                result: null,
                error_message: "本次生成失败。",
              }
            : sceneTask(),
        );
      }),
    );
    let failure: unknown;
    try {
      await createCharacterSceneLook("identity-1", sceneInput);
    } catch (cause) {
      failure = cause;
    }
    expect(failure).toBeInstanceOf(CharacterSheetTaskError);
    expect(failure).toMatchObject({
      status: "FAILED",
      recoveryRequired: false,
    });
    expect((failure as Error).message).toContain("错误编号：A1B2C3D4");
    failed = false;
    await expect(
      createCharacterSceneLook("identity-1", sceneInput),
    ).resolves.toEqual(sceneResult);
    expect(postKeys).toHaveLength(2);
    expect(postKeys[1]).not.toBe(postKeys[0]);
  });
});
