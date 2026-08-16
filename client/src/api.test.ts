import { afterEach, describe, expect, it, vi } from "vitest";

import {
  chooseProjectMainCharacterVersion,
  completeVideoUpload,
  confirmSourceFrame,
  createProject,
  generateFirstFrames,
  getCharacterReferenceRecommendation,
  getCurrentUser,
  getGenerationBatch,
  getHealth,
  getLatestProjectFirstFrames,
  getSettings,
  listProjectCharacterVersions,
  SESSION_EXPIRED_EVENT,
  selectCharacterReferences,
  startVideoAnalysis,
  uploadReferenceVideo,
} from "./api";

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
    await selectCharacterReferences("project-1", ["reference-1"]);

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
        body: JSON.stringify({ selected_asset_ids: ["reference-1"] }),
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
