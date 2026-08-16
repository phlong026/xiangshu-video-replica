import { afterEach, describe, expect, it, vi } from "vitest";

import {
  chooseProjectMainCharacterVersion,
  compileGenerationPrompt,
  completeVideoUpload,
  confirmSourceFrame,
  createGenerationBatch,
  createProject,
  createScriptVersion,
  generateFirstFrames,
  getCharacterReferenceRecommendation,
  getCurrentUser,
  getGenerationBatch,
  getGenerationResultDownloadUrl,
  getGenerationRuntimeLimits,
  getHealth,
  getLatestGenerationPrompt,
  getLatestProjectFirstFrames,
  getLatestScriptVersion,
  getSettings,
  listGenerationBatches,
  listProjectCharacterVersions,
  lockGenerationPrompt,
  regenerateGenerationBatch,
  regenerateGenerationTask,
  retryGenerationTask,
  reviseGenerationPrompt,
  SESSION_EXPIRED_EVENT,
  selectCharacterReferences,
  startVideoAnalysis,
  uploadReferenceVideo,
} from "./api";

const generationVersion = {
  id: "version-1",
  project_id: "project-1",
  asset_id: null,
  kind: "script",
  version_number: 1,
  payload: {},
  created_by_user_id: "employee_1",
  created_at: "2030-01-01T00:00:00Z",
};

describe("generation workflow API", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("covers script, prompt, runtime, batch, retry and result download routes", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce({ ok: true, json: async () => generationVersion })
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({
          version: generationVersion,
          stale: false,
          stale_reasons: [],
        }),
      })
      .mockResolvedValueOnce({ ok: true, json: async () => generationVersion })
      .mockResolvedValueOnce({ ok: true, json: async () => generationVersion })
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({
          version: generationVersion,
          stale: false,
          stale_reasons: [],
        }),
      })
      .mockResolvedValueOnce({ ok: true, json: async () => generationVersion })
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({
          min_quantity: 1,
          max_quantity: 4,
          estimated_cost_per_task: null,
        }),
      })
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({
          id: "batch-1",
          project_id: "project-1",
          prompt_version_id: "prompt-1",
          status: "QUEUED",
          quantity: 2,
          stale: false,
          progress: {
            total_count: 2,
            terminal_count: 0,
            progress_percent: 0,
            counts: {},
          },
          tasks: [],
        }),
      })
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({ status: "accepted" }),
      })
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({ url: "https://download.example/result.mp4" }),
      });
    vi.stubGlobal("fetch", fetchMock);

    await createScriptVersion("project 1", {
      source: "custom",
      text: "口播稿",
      shot_card_version_id: "shot-1",
    });
    await getLatestScriptVersion("project 1");
    await compileGenerationPrompt("project 1", {
      script_version_id: "script-1",
      shot_card_version_id: "shot-1",
      first_frame_asset_id: "frame-1",
      output_duration_seconds: 10,
      resolution: "768P",
    });
    await reviseGenerationPrompt("project 1", {
      base_prompt_version_id: "prompt-1",
      prompt_text: "修订 Prompt",
    });
    await getLatestGenerationPrompt("project 1");
    await lockGenerationPrompt("project 1", "prompt 1");
    await getGenerationRuntimeLimits();
    await createGenerationBatch("project 1", {
      quantity: 2,
      prompt_version_id: "prompt-1",
      first_frame_asset_id: "frame-1",
      output_duration_seconds: 10,
      resolution: "768P",
      idempotency_key: "key-1",
      provider: "fake_h3",
      fake_audio_quality: "ok",
    });
    await retryGenerationTask("task 1", {
      idempotency_key: "retry-key-1",
      retry_reason: "重新归档",
    });
    await getGenerationResultDownloadUrl("asset 1");

    expect(fetchMock).toHaveBeenNthCalledWith(
      1,
      "http://127.0.0.1:8000/api/projects/project%201/scripts",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({
          source: "custom",
          text: "口播稿",
          shot_card_version_id: "shot-1",
        }),
      }),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      4,
      "http://127.0.0.1:8000/api/projects/project%201/prompts/revise",
      expect.objectContaining({ method: "POST" }),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      6,
      "http://127.0.0.1:8000/api/projects/project%201/prompts/prompt%201/lock",
      expect.objectContaining({ method: "POST" }),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      8,
      "http://127.0.0.1:8000/api/projects/project%201/generation-batches",
      expect.objectContaining({ method: "POST" }),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      9,
      "http://127.0.0.1:8000/api/generation-tasks/task%201/retry",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({
          idempotency_key: "retry-key-1",
          retry_reason: "重新归档",
        }),
      }),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      10,
      "http://127.0.0.1:8000/api/assets/asset%201/download-url",
      expect.objectContaining({ method: "POST" }),
    );
  });

  it("posts explicit paid regeneration contracts for batches and tasks", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ id: "replacement-batch" }),
    });
    vi.stubGlobal("fetch", fetchMock);
    const input = {
      idempotency_key: "paid-regeneration-key",
      payment_confirmed: true as const,
      payment_confirmation_version: "V1" as const,
      estimated_cost_snapshot: 2.5,
      generation_reason: "人工确认重新生成",
    };

    await regenerateGenerationBatch("batch 1", input);
    await regenerateGenerationTask("task 1", input);

    expect(fetchMock).toHaveBeenNthCalledWith(
      1,
      "http://127.0.0.1:8000/api/generation-batches/batch%201/regenerate",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify(input),
      }),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      "http://127.0.0.1:8000/api/generation-tasks/task%201/regenerate",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify(input),
      }),
    );
  });

  it.each([
    [401, "登录已失效，请重新进入工作台"],
    [403, "当前账号无权执行此操作"],
    [409, "上游内容已变化，请重新确认后再试"],
    [422, "生成参数无效，请检查后重试"],
    [429, "请求过于频繁，请稍后重试"],
    [500, "生成服务暂不可用，请稍后重试"],
  ])("maps generation HTTP %s to a Chinese error", async (status, message) => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: false,
        status,
        json: async () => ({}),
      }),
    );

    await expect(
      createScriptVersion("project-1", {
        source: "custom",
        text: "口播稿",
        shot_card_version_id: "shot-1",
      }),
    ).rejects.toThrow(message);
  });

  it("maps generation timeout and offline failures to Chinese errors", async () => {
    const fetchMock = vi
      .fn()
      .mockRejectedValueOnce(new DOMException("aborted", "AbortError"))
      .mockRejectedValueOnce(new TypeError("Failed to fetch"));
    vi.stubGlobal("fetch", fetchMock);
    const input = {
      source: "custom" as const,
      text: "口播稿",
      shot_card_version_id: "shot-1",
    };

    await expect(createScriptVersion("project-1", input)).rejects.toThrow(
      "保存口播稿失败：请求超时，请重试",
    );
    await expect(createScriptVersion("project-1", input)).rejects.toThrow(
      "保存口播稿失败：网络连接失败，请检查本地服务",
    );
  });

  it("preserves the server error code on generation failures", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: false,
        status: 409,
        json: async () => ({
          detail: {
            code: "PROMPT_STALE",
            message: "Upstream inputs changed.",
          },
        }),
      }),
    );

    const error = await createGenerationBatch("project-1", {
      quantity: 1,
      prompt_version_id: "prompt-1",
      first_frame_asset_id: "frame-1",
      output_duration_seconds: 10,
      resolution: "768P",
      idempotency_key: "key-1",
      provider: "fake_h3",
      fake_audio_quality: "ok",
    }).catch((requestError: unknown) => requestError);

    expect(error).toMatchObject({
      status: 409,
      code: "PROMPT_STALE",
      message: "上游内容已变化，请重新确认后再试",
    });
  });
});

describe("character reference and first-frame binding", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("reads recommendations without creating a selection", async () => {
    const recommendation = { recommended_asset_ids_json: ["asset-1"] };
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => recommendation,
    });
    vi.stubGlobal("fetch", fetchMock);

    await expect(
      getCharacterReferenceRecommendation("project 1"),
    ).resolves.toEqual(recommendation);
    expect(fetchMock).toHaveBeenCalledWith(
      "http://127.0.0.1:8000/api/projects/project%201/character-reference-recommendation",
      expect.objectContaining({
        headers: expect.any(Headers),
        signal: expect.any(AbortSignal),
      }),
    );
  });

  it("sends explicit source features and selected character references", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ id: "saved" }),
    });
    vi.stubGlobal("fetch", fetchMock);
    const features = {
      orientation: "FRONT" as const,
      shot_size: "HALF_BODY" as const,
      face_visible: true,
      body_completeness: "UPPER_BODY" as const,
    };

    await confirmSourceFrame("project-1", "source-1", features);
    await selectCharacterReferences("project-1", {
      selected_asset_ids: ["reference-1"],
      source_frame_selection_version_id: "source-selection-1",
      character_version_id: "character-version-1",
    });

    expect(fetchMock).toHaveBeenNthCalledWith(
      1,
      "http://127.0.0.1:8000/api/projects/project-1/source-frames/confirm",
      expect.objectContaining({
        body: JSON.stringify({
          source_frame_asset_id: "source-1",
          character_features: features,
        }),
        method: "POST",
      }),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      "http://127.0.0.1:8000/api/projects/project-1/character-reference-selection",
      expect.objectContaining({
        body: JSON.stringify({
          selected_asset_ids: ["reference-1"],
          source_frame_selection_version_id: "source-selection-1",
          character_version_id: "character-version-1",
        }),
        method: "POST",
      }),
    );
  });

  it("maps a stale latest generation and sends the frozen binding on regeneration", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce({ ok: false, status: 409 })
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({ id: "first-frame-candidates-1" }),
      });
    vi.stubGlobal("fetch", fetchMock);

    await expect(getLatestProjectFirstFrames("project-1")).resolves.toEqual({
      version: null,
      stale: true,
    });
    await generateFirstFrames("project-1", {
      model: "nano-banana-pro-2k",
      prompt: "replace",
      quantity: 1,
      character_version_id: "character-version-1",
      character_reference_selection_id: "reference-selection-1",
    });

    expect(fetchMock).toHaveBeenLastCalledWith(
      "http://127.0.0.1:8000/api/projects/project-1/first-frames/generate",
      expect.objectContaining({
        body: JSON.stringify({
          model: "nano-banana-pro-2k",
          prompt: "replace",
          quantity: 1,
          character_version_id: "character-version-1",
          character_reference_selection_id: "reference-selection-1",
        }),
        method: "POST",
      }),
    );
  });
});

describe("project character version selection", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("loads only project-approved immutable character versions", async () => {
    const versions = [{ character_version_id: "character-version-3" }];
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => versions,
    });
    vi.stubGlobal("fetch", fetchMock);

    await expect(listProjectCharacterVersions("project 1")).resolves.toEqual(
      versions,
    );
    expect(fetchMock).toHaveBeenCalledWith(
      "http://127.0.0.1:8000/api/projects/project%201/character-versions/available",
      expect.objectContaining({
        headers: expect.any(Headers),
        signal: expect.any(AbortSignal),
      }),
    );
  });

  it("selects by immutable character version id", async () => {
    const selection = {
      project_id: "project-1",
      character_version_id: "character-version-3",
    };
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => selection,
    });
    vi.stubGlobal("fetch", fetchMock);

    await expect(
      chooseProjectMainCharacterVersion("project-1", "character-version-3"),
    ).resolves.toEqual(selection);
    expect(fetchMock).toHaveBeenCalledWith(
      "http://127.0.0.1:8000/api/projects/project-1/main-character",
      expect.objectContaining({
        body: JSON.stringify({ character_version_id: "character-version-3" }),
        headers: expect.any(Headers),
        method: "PUT",
        signal: expect.any(AbortSignal),
      }),
    );
  });
});

describe("getHealth", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("rejects a non-success response from the local API", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({ ok: false, status: 503 }),
    );

    await expect(getHealth()).rejects.toThrow("本地服务暂不可用（503）");
  });
});

describe("API error details", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("shows the server precheck message instead of only the HTTP status", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: false,
        status: 422,
        json: async () => ({
          detail: {
            code: "VIDEO_DURATION_OUT_OF_RANGE",
            message: "检测到 16.20 秒，参考视频需为 4–15 秒。",
          },
        }),
      }),
    );

    await expect(completeVideoUpload("asset-1")).rejects.toThrow(
      "参考视频预检失败：检测到 16.20 秒，参考视频需为 4–15 秒。（422）",
    );
  });
});

describe("getGenerationBatch", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("loads a generation batch by id from the local API", async () => {
    const batch = {
      id: "batch 1",
      status: "RUNNING",
      quantity: 2,
      progress: {
        total_count: 2,
        terminal_count: 1,
        progress_percent: 50,
        counts: { succeeded: 1, running: 1, needs_attention: 0 },
      },
      tasks: [],
    };
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => batch,
    });
    vi.stubGlobal("fetch", fetchMock);

    await expect(getGenerationBatch("batch 1")).resolves.toEqual(batch);
    expect(fetchMock).toHaveBeenCalledWith(
      "http://127.0.0.1:8000/api/generation-batches/batch%201",
      expect.objectContaining({
        headers: expect.any(Headers),
        signal: expect.any(AbortSignal),
      }),
    );
  });

  it("rejects a non-success batch response from the local API", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({ ok: false, status: 404 }),
    );

    await expect(getGenerationBatch("missing")).rejects.toThrow(
      "任务批次暂不可用（404）",
    );
  });
});

describe("listGenerationBatches", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("loads a filtered cursor page without sending a request body", async () => {
    const page = {
      items: [],
      next_cursor: "next-cursor",
    };
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => page,
    });
    vi.stubGlobal("fetch", fetchMock);

    await expect(
      listGenerationBatches({
        projectId: "project 1",
        createdByUserId: "admin 1",
        status: "NEEDS_ATTENTION",
        needsAttention: true,
        limit: 10,
        cursor: "cursor/value",
      }),
    ).resolves.toEqual(page);

    expect(fetchMock).toHaveBeenCalledWith(
      "http://127.0.0.1:8000/api/generation-batches?project_id=project+1&created_by_user_id=admin+1&status=NEEDS_ATTENTION&needs_attention=true&limit=10&cursor=cursor%2Fvalue",
      expect.objectContaining({
        headers: expect.any(Headers),
        signal: expect.any(AbortSignal),
      }),
    );
    const request = fetchMock.mock.calls[0][1] as RequestInit;
    expect(request.method).toBeUndefined();
    expect(request.body).toBeUndefined();
  });
});

describe("createProject", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("creates a project through the authenticated local API", async () => {
    const project = {
      id: "project-1",
      owner_user_id: "employee_1",
      name: "参考视频复刻",
      status: "ACTIVE",
    };
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => project,
    });
    vi.stubGlobal("fetch", fetchMock);

    await expect(createProject("参考视频复刻")).resolves.toEqual(project);
    expect(fetchMock).toHaveBeenCalledWith(
      "http://127.0.0.1:8000/api/projects",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ name: "参考视频复刻" }),
        headers: expect.any(Headers),
        signal: expect.any(AbortSignal),
      }),
    );
    const options = fetchMock.mock.calls[0]?.[1] as RequestInit;
    expect((options.headers as Headers).get("X-Dev-User-Id")).toBe(
      "employee_1",
    );
  });
});

describe("startVideoAnalysis", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it("uses the provider-sized timeout and marks automatic starts as recoverable", async () => {
    const timeoutSpy = vi.spyOn(window, "setTimeout");
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ id: "analysis-1" }),
    });
    vi.stubGlobal("fetch", fetchMock);

    await startVideoAnalysis("project-1", "asset-1", 8);

    expect(timeoutSpy).toHaveBeenCalledWith(expect.any(Function), 120_000);
    expect(fetchMock).toHaveBeenCalledWith(
      "http://127.0.0.1:8000/api/projects/project-1/analysis",
      expect.objectContaining({
        body: JSON.stringify({
          asset_id: "asset-1",
          duration_seconds: 8,
          reuse_existing: true,
        }),
      }),
    );
  });
});

describe("getCurrentUser", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.unstubAllEnvs();
  });

  it("loads the current user from auth/me using the unified development identity", async () => {
    const user = {
      id: "employee_1",
      username: "employee_1",
      display_name: "林夏",
      role: "employee",
    };
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => user,
    });
    vi.stubGlobal("fetch", fetchMock);

    await expect(getCurrentUser()).resolves.toEqual(user);
    expect(fetchMock).toHaveBeenCalledWith(
      "http://127.0.0.1:8000/api/auth/me",
      expect.objectContaining({
        headers: expect.any(Headers),
        signal: expect.any(AbortSignal),
      }),
    );
    const options = fetchMock.mock.calls[0]?.[1] as RequestInit;
    expect((options.headers as Headers).get("X-Dev-User-Id")).toBe(
      "employee_1",
    );
  });

  it("does not fallback to a development identity in production builds", async () => {
    vi.stubEnv("DEV", false);
    vi.stubEnv("PROD", true);
    const fetchMock = vi.fn().mockResolvedValue({
      ok: false,
      status: 401,
      json: async () => ({ detail: { message: "missing identity" } }),
    });
    vi.stubGlobal("fetch", fetchMock);

    await expect(getCurrentUser()).rejects.toThrow(
      "身份验证失败：missing identity（401）",
    );
    const options = fetchMock.mock.calls[0]?.[1] as RequestInit;
    expect((options.headers as Headers).has("X-Dev-User-Id")).toBe(false);
  });

  it("ignores an explicitly configured development identity in production builds", async () => {
    vi.stubEnv("DEV", false);
    vi.stubEnv("PROD", true);
    vi.stubEnv("VITE_DEV_USER_ID", "admin_1");
    const fetchMock = vi.fn().mockResolvedValue({
      ok: false,
      status: 401,
      json: async () => ({ detail: { message: "missing identity" } }),
    });
    vi.stubGlobal("fetch", fetchMock);

    await expect(getCurrentUser()).rejects.toThrow(
      "身份验证失败：missing identity（401）",
    );
    const options = fetchMock.mock.calls[0]?.[1] as RequestInit;
    expect((options.headers as Headers).has("X-Dev-User-Id")).toBe(false);
  });
});

describe("admin API authentication", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.unstubAllEnvs();
  });

  it("uses the same development identity for settings unless explicitly overridden", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ providers: {}, runtime: {} }),
    });
    vi.stubGlobal("fetch", fetchMock);

    await getSettings();

    const options = fetchMock.mock.calls[0]?.[1] as RequestInit;
    expect((options.headers as Headers).get("X-Dev-User-Id")).toBe(
      "employee_1",
    );
  });

  it("allows an explicit VITE_DEV_USER_ID to switch the local identity", async () => {
    vi.stubEnv("VITE_DEV_USER_ID", "admin_1");
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ providers: {}, runtime: {} }),
    });
    vi.stubGlobal("fetch", fetchMock);

    await getSettings();

    const options = fetchMock.mock.calls[0]?.[1] as RequestInit;
    expect((options.headers as Headers).get("X-Dev-User-Id")).toBe("admin_1");
  });
});

describe("uploadReferenceVideo", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.unstubAllEnvs();
  });

  it("does not send the development identity header to a cloud presigned URL", async () => {
    class CloudUploadRequest {
      static latest: CloudUploadRequest | null = null;
      headers = new Map<string, string>();
      onerror: (() => void) | null = null;
      onload: (() => void) | null = null;
      ontimeout: (() => void) | null = null;
      status = 200;
      timeout = 0;
      upload: { onprogress: ((event: ProgressEvent) => void) | null } = {
        onprogress: null,
      };

      constructor() {
        CloudUploadRequest.latest = this;
      }

      open() {}
      setRequestHeader(name: string, value: string) {
        this.headers.set(name, value);
      }
      send() {
        this.onload?.();
      }
    }

    vi.stubGlobal("XMLHttpRequest", CloudUploadRequest);

    await uploadReferenceVideo(
      {
        asset_id: "asset-1",
        project_id: "project-1",
        storage_key: "projects/project-1/reference.mp4",
        method: "PUT",
        url: "https://cos.example.com/presigned-upload",
        headers: { "Content-Type": "video/mp4" },
        expires_at: "2030-01-01T00:00:00Z",
      },
      new File(["video"], "reference.mp4", { type: "video/mp4" }),
      vi.fn(),
    );

    expect(
      CloudUploadRequest.latest?.headers.get("X-Dev-User-Id"),
    ).toBeUndefined();
    expect(CloudUploadRequest.latest?.headers.get("Content-Type")).toBe(
      "video/mp4",
    );
  });

  it("keeps the development identity header for the local upload endpoint", async () => {
    class LocalUploadRequest {
      static latest: LocalUploadRequest | null = null;
      headers = new Map<string, string>();
      onerror: (() => void) | null = null;
      onload: (() => void) | null = null;
      ontimeout: (() => void) | null = null;
      status = 204;
      timeout = 0;
      upload: { onprogress: ((event: ProgressEvent) => void) | null } = {
        onprogress: null,
      };

      constructor() {
        LocalUploadRequest.latest = this;
      }

      open() {}
      setRequestHeader(name: string, value: string) {
        this.headers.set(name, value);
      }
      send() {
        this.onload?.();
      }
    }

    vi.stubGlobal("XMLHttpRequest", LocalUploadRequest);

    await uploadReferenceVideo(
      {
        asset_id: "asset-1",
        project_id: "project-1",
        storage_key: "projects/project-1/reference.mp4",
        method: "PUT",
        url: "http://127.0.0.1:8000/api/assets/local-objects/projects/project-1/reference.mp4",
        headers: { "Content-Type": "video/mp4" },
        expires_at: "2030-01-01T00:00:00Z",
      },
      new File(["video"], "reference.mp4", { type: "video/mp4" }),
      vi.fn(),
    );

    expect(LocalUploadRequest.latest?.headers.get("X-Dev-User-Id")).toBe(
      "employee_1",
    );
  });

  it("emits the unified session-expired event when a local upload returns 401", async () => {
    class UnauthorizedUploadRequest {
      static latest: UnauthorizedUploadRequest | null = null;
      headers = new Map<string, string>();
      onerror: (() => void) | null = null;
      onload: (() => void) | null = null;
      ontimeout: (() => void) | null = null;
      status = 401;
      timeout = 0;
      upload: { onprogress: ((event: ProgressEvent) => void) | null } = {
        onprogress: null,
      };

      constructor() {
        UnauthorizedUploadRequest.latest = this;
      }

      open() {}
      setRequestHeader(name: string, value: string) {
        this.headers.set(name, value);
      }
      send() {
        this.onload?.();
      }
    }

    const onSessionExpired = vi.fn();
    window.addEventListener(SESSION_EXPIRED_EVENT, onSessionExpired);
    vi.stubGlobal("XMLHttpRequest", UnauthorizedUploadRequest);

    await expect(
      uploadReferenceVideo(
        {
          asset_id: "asset-1",
          project_id: "project-1",
          storage_key: "projects/project-1/reference.mp4",
          method: "PUT",
          url: "http://127.0.0.1:8000/api/assets/local-objects/projects/project-1/reference.mp4",
          headers: { "Content-Type": "video/mp4" },
          expires_at: "2030-01-01T00:00:00Z",
        },
        new File(["video"], "reference.mp4", { type: "video/mp4" }),
        vi.fn(),
      ),
    ).rejects.toThrow("登录已失效，请重新进入工作台。");

    expect(onSessionExpired).toHaveBeenCalledOnce();
    window.removeEventListener(SESSION_EXPIRED_EVENT, onSessionExpired);
  });
});
