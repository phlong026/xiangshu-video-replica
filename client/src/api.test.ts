import { afterEach, describe, expect, it, vi } from "vitest";

import {
  applySavedGenerationPrompt,
  attachCustomerSessionToken,
  CUSTOMER_SESSION_EXPIRED_EVENT,
  CUSTOMER_SESSION_REPLACED_EVENT,
  CUSTOMER_SESSION_REVOKED_EVENT,
  cancelOralTask,
  cancelSourceFrameTask,
  chooseProjectMainCharacterVersion,
  compileGenerationPrompt,
  completeMaterialUpload,
  completeVideoUpload,
  confirmOralVoice,
  confirmSourceFrame,
  createGenerationBatch,
  createGenerationResultPreviewUrl,
  createMaterialUploadIntent,
  createOralAvatarClone,
  createOralConsent,
  createOralTask,
  createOralVoiceClone,
  createProject,
  createScriptVersion,
  createVideoUploadIntent,
  customerVisibleErrorMessage,
  downloadCharacterAsset,
  downloadGenerationResult,
  downloadGenerationTaskResult,
  downloadMaterialAsset,
  extractSourceFrames,
  generateFirstFrames,
  getCachedCharacterAssetUrl,
  getCharacterReferenceRecommendation,
  getCurrentUser,
  getGenerationBatch,
  getGenerationPriceQuote,
  getGenerationResultDownloadUrl,
  getGenerationRuntimeLimits,
  getHealth,
  getLatestGenerationPrompt,
  getLatestProjectFirstFrames,
  getLatestScriptRewriteTask,
  getLatestScriptVersion,
  getOralTask,
  getScriptFromAudioTask,
  getSettings,
  hideMaterial,
  listCharacterSceneLooksPage,
  listGenerationBatches,
  listMaterials,
  listOralAvatars,
  listOralTasksPage,
  listOralVoices,
  listProjectCharacterVersions,
  listProjects,
  listSavedGenerationPrompts,
  listSimpleCharacterLibraryPage,
  listViralVideos,
  lockGenerationPrompt,
  readAnalysisPayload,
  readFirstFrameCandidates,
  reconcileUncertainTask,
  regenerateGenerationBatch,
  regenerateGenerationTask,
  resolveApiBaseUrl,
  resolveViralLink,
  retryGenerationTask,
  retryOralTaskArchive,
  reviseGenerationPrompt,
  rewriteProjectScript,
  SESSION_EXPIRED_EVENT,
  saveGenerationPrompt,
  selectCharacterReferences,
  setCustomerSessionToken,
  setInternalAccessToken,
  startVideoAnalysis,
  updateMaterial,
  updateSimpleCharacterProfile,
  uploadMaterial,
  uploadReferenceVideo,
  waitForAnalysisTask,
  waitForCharacterSheetTask,
  waitForFirstFrameTask,
  waitForGenerationReconcileOperation,
  waitForScriptRewriteTask,
  waitForSourceFrameTask,
} from "./api";

describe("爆款列表 API", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("将分页游标编码到查询参数并保持 GET 请求", async () => {
    const page = {
      platform: "douyin",
      sort: "latest",
      categories: [],
      items: [],
      hasMore: false,
      nextCursor: null,
      total: 0,
    };
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => page,
    });
    vi.stubGlobal("fetch", fetchMock);

    await expect(
      listViralVideos("douyin", "latest", {
        limit: 12,
        cursor: "page/2+=",
      }),
    ).resolves.toEqual(page);
    expect(fetchMock).toHaveBeenCalledWith(
      "http://127.0.0.1:8000/api/viral/videos?platform=douyin&sort=latest&limit=12&cursor=page%2F2%2B%3D",
      expect.objectContaining({
        headers: expect.any(Headers),
        signal: expect.any(AbortSignal),
      }),
    );
  });

  it("提交链接和用途到受限时长的解析接口", async () => {
    const body = {
      item: { platform: "douyin", videoId: "source-1" },
      importIdempotencyKey: "stable-import-key",
    };
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => body,
    });
    vi.stubGlobal("fetch", fetchMock);

    await expect(
      resolveViralLink("https://v.douyin.com/share/", "replica", "resolve-key"),
    ).resolves.toEqual(body);
    expect(fetchMock).toHaveBeenCalledWith(
      "http://127.0.0.1:8000/api/viral/link-resolutions",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({
          url: "https://v.douyin.com/share/",
          purpose: "replica",
        }),
        signal: expect.any(AbortSignal),
      }),
    );
    const init = fetchMock.mock.calls[0]?.[1] as RequestInit;
    expect(new Headers(init.headers).get("Idempotency-Key")).toBe(
      "resolve-key",
    );
  });
});

describe("素材库 API", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("使用服务端分页筛选并提交素材管理动作", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ items: [], page: 2, page_size: 6, total: 8 }),
    });
    vi.stubGlobal("fetch", fetchMock);

    await listMaterials({ mediaType: "audio", page: 2, pageSize: 6 });
    await updateMaterial("asset:audio 1", { title: "新名称" });
    await hideMaterial("asset:audio 1");

    expect(fetchMock.mock.calls[0]?.[0]).toBe(
      "http://127.0.0.1:8000/api/studio/materials?media_type=audio&page=2&page_size=6",
    );
    expect(fetchMock.mock.calls[1]?.[0]).toBe(
      "http://127.0.0.1:8000/api/studio/materials/asset%3Aaudio%201",
    );
    expect(fetchMock.mock.calls[1]?.[1]).toEqual(
      expect.objectContaining({ method: "PATCH", body: '{"title":"新名称"}' }),
    );
    expect(fetchMock.mock.calls[2]?.[1]).toEqual(
      expect.objectContaining({ method: "DELETE" }),
    );
  });

  it("通过授权下载地址启动素材下载且不把文件整体读入内存", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ url: "http://127.0.0.1:8000/api/assets/signed" }),
    });
    const click = vi
      .spyOn(HTMLAnchorElement.prototype, "click")
      .mockImplementation(() => undefined);
    vi.stubGlobal("fetch", fetchMock);

    await downloadMaterialAsset("asset 1", "庭院.png");

    expect(fetchMock).toHaveBeenCalledWith(
      "http://127.0.0.1:8000/api/assets/asset%201/download-url",
      expect.objectContaining({ method: "POST" }),
    );
    expect(click).toHaveBeenCalledOnce();
    click.mockRestore();
  });

  it("按扩展名规范化上传类型并完成素材上传", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({
          material_id: "asset:image-1",
          asset_id: "image-1",
          storage_key: "materials/user/image-1/original.jpg",
          method: "PUT",
          url: "https://storage.test/upload",
          headers: { "Content-Type": "image/jpeg" },
          expires_at: "2030-01-01T00:00:00Z",
        }),
      })
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({ id: "asset:image-1", status: "ready" }),
      });
    vi.stubGlobal("fetch", fetchMock);
    const file = new File(["image"], "房屋.JPEG", {
      type: "application/octet-stream",
    });

    const intent = await createMaterialUploadIntent(file);
    await completeMaterialUpload(intent.asset_id);

    expect(JSON.parse(String(fetchMock.mock.calls[0]?.[1]?.body))).toEqual(
      expect.objectContaining({
        filename: "房屋.JPEG",
        content_type: "image/jpeg",
        size_bytes: 5,
      }),
    );
    expect(fetchMock.mock.calls[1]?.[0]).toBe(
      "http://127.0.0.1:8000/api/studio/materials/uploads/image-1/complete",
    );
  });

  it("取消完成素材请求时中止底层 fetch 而不误报超时", async () => {
    let requestSignal: AbortSignal | undefined;
    const fetchMock = vi.fn((_url: string, init?: RequestInit) => {
      requestSignal = init?.signal ?? undefined;
      return new Promise<Response>((_resolve, reject) => {
        requestSignal?.addEventListener("abort", () => {
          reject(new DOMException("Aborted", "AbortError"));
        });
      });
    });
    vi.stubGlobal("fetch", fetchMock);
    const controller = new AbortController();

    const request = completeMaterialUpload("image-1", controller.signal);
    controller.abort();

    await expect(request).rejects.toMatchObject({ name: "AbortError" });
    expect(requestSignal?.aborted).toBe(true);
  });

  it("音频上传意图携带用途与浏览器读取的时长", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({
        material_id: "asset:audio-1",
        asset_id: "audio-1",
        storage_key: null,
        method: "PUT",
        url: "https://storage.test/audio",
        headers: { "Content-Type": "audio/mpeg" },
        expires_at: "2030-01-01T00:00:00Z",
      }),
    });
    vi.stubGlobal("fetch", fetchMock);

    await createMaterialUploadIntent(
      new File(["ID3audio"], "完整口播.mp3", { type: "audio/mpeg" }),
      { audioPurpose: "oral_audio", durationSeconds: 42 },
    );

    expect(JSON.parse(String(fetchMock.mock.calls[0]?.[1]?.body))).toEqual(
      expect.objectContaining({
        audio_purpose: "oral_audio",
        duration_seconds: 42,
      }),
    );
  });

  it("复用带本地鉴权和进度处理的上传通道", async () => {
    class MaterialUploadRequest {
      static latest: MaterialUploadRequest | null = null;
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
        MaterialUploadRequest.latest = this;
      }
      open() {}
      setRequestHeader(name: string, value: string) {
        this.headers.set(name, value);
      }
      send() {
        this.upload.onprogress?.({
          lengthComputable: true,
          loaded: 3,
          total: 3,
        } as ProgressEvent);
        this.onload?.();
      }
    }
    vi.stubGlobal("XMLHttpRequest", MaterialUploadRequest);
    const progress = vi.fn();

    await uploadMaterial(
      {
        material_id: "asset:audio-1",
        asset_id: "audio-1",
        storage_key: null,
        method: "PUT",
        url: "http://127.0.0.1:8000/api/studio/materials/uploads/audio-1/content",
        headers: { "Content-Type": "audio/mpeg" },
        expires_at: "2030-01-01T00:00:00Z",
      },
      new File(["ID3"], "voice.mp3", { type: "audio/mpeg" }),
      progress,
    );

    expect(progress).toHaveBeenLastCalledWith(100);
    expect(MaterialUploadRequest.latest?.headers.get("Content-Type")).toBe(
      "audio/mpeg",
    );
  });
});

describe("人物库 API", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("使用服务端查询和不透明游标读取下一页", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ items: [], next_cursor: "next-cursor" }),
    });
    vi.stubGlobal("fetch", fetchMock);

    await listSimpleCharacterLibraryPage({
      limit: 12,
      cursor: "cursor/value",
      query: "林 夏",
    });

    expect(fetchMock.mock.calls[0]?.[0]).toBe(
      "http://127.0.0.1:8000/api/simple-characters/library?limit=12&cursor=cursor%2Fvalue&query=%E6%9E%97+%E5%A4%8F",
    );
  });

  it("场景造型按人物使用 limit 和 offset 分页", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ items: [], total: 13, limit: 12, offset: 12 }),
    });
    vi.stubGlobal("fetch", fetchMock);

    await listCharacterSceneLooksPage("person 1", { limit: 12, offset: 12 });

    expect(fetchMock.mock.calls[0]?.[0]).toBe(
      "http://127.0.0.1:8000/api/simple-characters/identities/person%201/scene-looks?limit=12&offset=12",
    );
  });
});

describe("人物 IP 口播资产 API", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("按人物读取分身和声音记录", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce({ ok: true, json: async () => [] })
      .mockResolvedValueOnce({ ok: true, json: async () => [] });
    vi.stubGlobal("fetch", fetchMock);

    await listOralAvatars("person 1");
    await listOralVoices("person 1");

    expect(fetchMock.mock.calls[0]?.[0]).toBe(
      "http://127.0.0.1:8000/api/oral/avatars?identity_id=person%201",
    );
    expect(fetchMock.mock.calls[1]?.[0]).toBe(
      "http://127.0.0.1:8000/api/oral/voices?identity_id=person%201",
    );
  });

  it("口播任务使用独立 limit 和 offset 历史范围", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ items: [], total: 21, limit: 20, offset: 20 }),
    });
    vi.stubGlobal("fetch", fetchMock);

    await listOralTasksPage({ limit: 20, offset: 20 });

    expect(fetchMock.mock.calls[0]?.[0]).toBe(
      "http://127.0.0.1:8000/api/oral/tasks?limit=20&offset=20",
    );
  });

  it("口播详情按编码后的原生任务 ID 读取", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ id: "oral 1", status: "SUCCEEDED" }),
    });
    vi.stubGlobal("fetch", fetchMock);

    await getOralTask("oral 1");

    expect(fetchMock.mock.calls[0]?.[0]).toBe(
      "http://127.0.0.1:8000/api/oral/tasks/oral%201",
    );
  });

  it("先存证授权，再带 consent_id 提交分身和声音克隆", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({ id: "consent-avatar" }),
      })
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({ id: "avatar-1", status: "RUNNING" }),
      })
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({ id: "consent-voice" }),
      })
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({ id: "voice-1", status: "RUNNING" }),
      });
    vi.stubGlobal("fetch", fetchMock);

    const avatarConsent = await createOralConsent({
      identityId: "person-1",
      sourceAssetId: "scene-1",
      purpose: "AVATAR",
    });
    await createOralAvatarClone({
      identityId: "person-1",
      title: "庭院讲解分身",
      sourceAssetId: "scene-1",
      sourceKind: "IMAGE",
      consentId: avatarConsent.id,
      idempotencyKey: "avatar-clone-key",
    });
    const voiceConsent = await createOralConsent({
      identityId: "person-1",
      sourceAssetId: "audio-1",
      purpose: "VOICE",
    });
    await createOralVoiceClone({
      identityId: "person-1",
      title: "张工音色",
      sourceAssetId: "audio-1",
      consentId: voiceConsent.id,
      idempotencyKey: "voice-clone-key",
    });

    expect(fetchMock.mock.calls[0]?.[0]).toBe(
      "http://127.0.0.1:8000/api/oral/consents",
    );
    expect(JSON.parse(String(fetchMock.mock.calls[0]?.[1]?.body))).toEqual({
      identity_id: "person-1",
      source_asset_id: "scene-1",
      purpose: "AVATAR",
    });
    expect(fetchMock.mock.calls[1]?.[0]).toBe(
      "http://127.0.0.1:8000/api/oral/avatars",
    );
    expect(JSON.parse(String(fetchMock.mock.calls[1]?.[1]?.body))).toEqual({
      identity_id: "person-1",
      title: "庭院讲解分身",
      source_asset_id: "scene-1",
      source_kind: "IMAGE",
      consent_id: "consent-avatar",
      idempotency_key: "avatar-clone-key",
    });
    expect(fetchMock.mock.calls[2]?.[0]).toBe(
      "http://127.0.0.1:8000/api/oral/consents",
    );
    expect(fetchMock.mock.calls[3]?.[0]).toBe(
      "http://127.0.0.1:8000/api/oral/voices",
    );
    expect(JSON.parse(String(fetchMock.mock.calls[3]?.[1]?.body))).toEqual({
      identity_id: "person-1",
      title: "张工音色",
      source_asset_id: "audio-1",
      consent_id: "consent-voice",
      idempotency_key: "voice-clone-key",
    });
  });

  it("显式确认 READY 声音", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ id: "voice-1", status: "READY", confirmed: true }),
    });
    vi.stubGlobal("fetch", fetchMock);

    await confirmOralVoice("voice 1");

    expect(fetchMock).toHaveBeenCalledWith(
      "http://127.0.0.1:8000/api/oral/voices/voice%201/confirm",
      expect.objectContaining({ method: "POST" }),
    );
  });

  it("口播任务按选择序列化供应商原生字幕配置", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ id: "oral-1", status: "QUEUED" }),
    });
    vi.stubGlobal("fetch", fetchMock);
    const common = {
      identityId: "person-1",
      avatarId: "avatar-1",
      voiceId: "voice-1",
      mode: "TTS" as const,
      title: "口播",
      scriptText: "文案",
    };

    await createOralTask({ ...common, idempotencyKey: "oral-key-off" });
    await createOralTask({
      ...common,
      subtitle: { st_show: true, st_font_size: 30 },
      idempotencyKey: "oral-key-on",
    });

    const disabledBody = JSON.parse(String(fetchMock.mock.calls[0]?.[1]?.body));
    const enabledBody = JSON.parse(String(fetchMock.mock.calls[1]?.[1]?.body));
    expect(disabledBody.subtitle).toBeNull();
    expect(enabledBody.subtitle).toEqual({ st_show: true, st_font_size: 30 });
  });

  it("调用口播任务取消与归档重试合同", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ id: "oral-1", status: "QUEUED" }),
    });
    vi.stubGlobal("fetch", fetchMock);

    await cancelOralTask("oral 1");
    await retryOralTaskArchive("oral 1");

    expect(fetchMock.mock.calls.map((call) => call[0])).toEqual([
      "http://127.0.0.1:8000/api/oral/tasks/oral%201/cancel",
      "http://127.0.0.1:8000/api/oral/tasks/oral%201/archive-retry",
    ]);
    for (const call of fetchMock.mock.calls) {
      expect(call[1]).toEqual(expect.objectContaining({ method: "POST" }));
    }
  });

  it("保存人物 IP 定位合同", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ identity_id: "person-1" }),
    });
    vi.stubGlobal("fetch", fetchMock);
    const profile = {
      display_name: "张工",
      role: "乡墅项目经理",
      service_scope: "建房全流程",
      target_audience: "返乡建房家庭",
      expression_style: "专业直白",
    };

    await updateSimpleCharacterProfile("person-1", profile);

    expect(fetchMock.mock.calls[0]?.[0]).toBe(
      "http://127.0.0.1:8000/api/simple-characters/identities/person-1/profile",
    );
    expect(JSON.parse(String(fetchMock.mock.calls[0]?.[1]?.body))).toEqual(
      profile,
    );
  });
});

describe("generation payload readers", () => {
  it("uses owner-scoped saved prompt and external quote endpoints", async () => {
    const version = { id: "saved-1" };
    const quote = {
      resolution: "2K",
      duration_seconds: 15,
      quantity: 4,
      unit_price_fen_per_second: 25,
      estimated_seconds: 60,
      estimated_price_fen: 1500,
    };
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce({ ok: true, json: async () => version })
      .mockResolvedValueOnce({ ok: true, json: async () => [version] })
      .mockResolvedValueOnce({ ok: true, json: async () => version })
      .mockResolvedValueOnce({ ok: true, json: async () => quote });
    vi.stubGlobal("fetch", fetchMock);

    await saveGenerationPrompt("project 1", {
      name: "庭院推镜",
      prompt_text: "庭院日景，镜头缓慢推进。",
      base_prompt_version_id: "prompt-1",
    });
    await listSavedGenerationPrompts("project 1");
    await applySavedGenerationPrompt("project 1", "saved 1", "prompt-1");
    await expect(
      getGenerationPriceQuote({
        resolution: "2K",
        duration_seconds: 15,
        quantity: 4,
      }),
    ).resolves.toEqual(quote);

    expect(fetchMock).toHaveBeenNthCalledWith(
      3,
      "http://127.0.0.1:8000/api/projects/project%201/saved-prompts/saved%201/apply",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ base_prompt_version_id: "prompt-1" }),
      }),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      4,
      "http://127.0.0.1:8000/api/generation/price-quote?resolution=2K&duration_seconds=15&quantity=4",
      expect.any(Object),
    );
  });

  it("reads project appearance and reconstruction metadata compatibly", () => {
    const parsed = readFirstFrameCandidates({
      id: "first-frame-v1",
      project_id: "project-1",
      asset_id: "source-1",
      kind: "first_frame_candidates",
      version_number: 1,
      payload: {
        provider: "fake",
        model: "gpt-image-2",
        prompt: "完整人物重构",
        reconstruction_mode: "full_person_replace.v1",
        character_contract: { body_reconstruction: true },
        project_character_appearance_version_id: "appearance-v1",
        project_appearance: {
          category: "BUSINESS",
          scene: "商务会议室",
          subject: "企业负责人",
          outfit_description: "简洁商务休闲装",
          selection_reason: "按场景自动匹配",
        },
        candidates: [
          {
            asset_id: "first-frame-1",
            storage_key: "projects/project-1/first-frame-1.png",
            storage_uri: "cos://bucket/first-frame-1.png",
            sha256: "hash",
            size_bytes: 123,
            content_type: "image/png",
            quality: {
              passed: true,
              attempt: 2,
              issue_codes: [],
              inspection: { head_only_replacement_detected: false },
            },
          },
        ],
      },
      created_by_user_id: "employee-1",
      created_at: "2030-01-01T00:00:00Z",
    });

    expect(parsed?.reconstruction_mode).toBe("full_person_replace.v1");
    expect(parsed?.project_appearance?.category).toBe("BUSINESS");
    expect(parsed?.candidates[0]?.quality?.passed).toBe(true);
    expect(parsed?.candidates[0]?.quality?.attempt).toBe(2);
  });

  it("reads action-beat metadata while keeping the shots compatibility field", () => {
    const parsed = readAnalysisPayload({
      id: "analysis-v1",
      project_id: "project-1",
      asset_id: "video-1",
      kind: "analysis",
      version_number: 1,
      payload: {
        analysis: {
          summary: "连续镜头动作拆解",
          duration_seconds: 12,
          original_script: "",
          shots: [
            {
              shot_id: "S01",
              start_time: 0,
              end_time: 12,
              shot_type: "中景",
              composition: "人物居中",
              camera_motion: "固定",
              subject: "主讲人",
              action: "口播",
              scene: "室内",
              spoken_text: "",
              transition: "连续",
              segment_kind: "ACTION_BEAT",
              boundary_reason: "表达重点变化",
            },
          ],
        },
      },
      created_by_user_id: "employee-1",
      created_at: "2030-01-01T00:00:00Z",
    });

    expect(parsed?.shots[0]?.segment_kind).toBe("ACTION_BEAT");
    expect(parsed?.shots[0]?.boundary_reason).toBe("表达重点变化");
  });
});

describe("API base URL resolution", () => {
  it("uses the serving HTTPS origin for a production web build", () => {
    expect(
      resolveApiBaseUrl(undefined, true, {
        origin: "https://video.example.com",
        protocol: "https:",
      }),
    ).toBe("https://video.example.com");
  });

  it("keeps the local API fallback for desktop and development runtimes", () => {
    expect(
      resolveApiBaseUrl(undefined, true, {
        origin: "tauri://localhost",
        protocol: "tauri:",
      }),
    ).toBe("http://127.0.0.1:8000");
    expect(
      resolveApiBaseUrl(undefined, false, {
        origin: "http://127.0.0.1:5173",
        protocol: "http:",
      }),
    ).toBe("http://127.0.0.1:8000");
  });

  it("prefers and normalizes an explicitly configured API origin", () => {
    expect(
      resolveApiBaseUrl(" https://api.example.com/ ", true, {
        origin: "https://video.example.com",
        protocol: "https:",
      }),
    ).toBe("https://api.example.com");
  });
});

describe("customer-visible service errors", () => {
  it.each([
    [
      { code: "METASO_UPSTREAM_TIMEOUT", message: "MiniMax H3 timeout" },
      "视频生成服务暂时不可用，请稍后重试；如持续失败，请联系客服。",
    ],
    [
      { code: "APILIO_REQUEST_FAILED", message: "Gemini unavailable" },
      "视频拆解服务暂时不可用，请稍后重试；如持续失败，请联系客服。",
    ],
    [
      {
        code: "APILIO_SETTINGS_UNAVAILABLE",
        message: "生成人物置换首帧失败：Apilio key missing",
      },
      "首帧生成服务暂时不可用，请稍后重试；如持续失败，请联系客服。",
    ],
    [
      { code: "IMAGE_FAILED", message: "GPT Image returned no output" },
      "首帧生成服务暂时不可用，请稍后重试；如持续失败，请联系客服。",
    ],
    [
      { code: "COS_UPLOAD_FAILED", message: "腾讯云 COS denied" },
      "素材库暂时不可用，请稍后重试；如持续失败，请联系客服。",
    ],
    [
      { code: "SCRIPT_FAILED", message: "DeepSeek timeout" },
      "文案优化服务暂时不可用，请稍后重试；如持续失败，请联系客服。",
    ],
    [
      { code: "ZPAY_UNAVAILABLE", message: "gateway rejected" },
      "在线支付暂时不可用，请稍后重试；如已扣款，请勿重复支付并联系客服。",
    ],
  ])("maps a branded provider failure to neutral copy", (error, expected) => {
    expect(customerVisibleErrorMessage(error)).toBe(expected);
  });

  it("keeps actionable non-branded errors and adds request ids only after mapping", () => {
    expect(
      customerVisibleErrorMessage({
        code: "METASO_FAILED",
        message: "upstream failed",
        requestId: "request-123",
      }),
    ).toBe(
      "视频生成服务暂时不可用，请稍后重试；如持续失败，请联系客服。 问题编号：request-123",
    );
    expect(customerVisibleErrorMessage("参考视频时长必须为 4–15 秒")).toBe(
      "参考视频时长必须为 4–15 秒",
    );
  });

  it("turns transport failures into the caller's customer-safe fallback", () => {
    expect(
      customerVisibleErrorMessage(
        new TypeError("Failed to fetch"),
        "项目列表暂不可用，请检查网络连接后重试。",
      ),
    ).toBe("项目列表暂不可用，请检查网络连接后重试。");
  });

  it.each([
    [
      "ACTIVATION_UNAVAILABLE",
      "该激活码当前无法使用，请确认激活码仍在有效期内。",
    ],
    [
      "PAIRING_UNAVAILABLE",
      "该激活码当前无法用于设备配对，请联系服务人员处理。",
    ],
  ])("localizes customer account error %s", (code, expected) => {
    expect(
      customerVisibleErrorMessage({
        code,
        message: "The activation code cannot be used.",
      }),
    ).toBe(expected);
  });
});

describe("customer workspace session lifecycle", () => {
  const customerSessionText = "customer-session-fixture";

  afterEach(() => {
    setCustomerSessionToken(null);
    vi.unstubAllGlobals();
  });

  it.each([
    ["SESSION_REPLACED", CUSTOMER_SESSION_REPLACED_EVENT],
    ["DEVICE_REVOKED", CUSTOMER_SESSION_REVOKED_EVENT],
  ])(
    "dispatches the exact %s lifecycle event for shared workspace requests",
    async (code, eventName) => {
      const detail = { code, message: "session unavailable" };
      const response = () => ({
        ok: false,
        status: 401,
        headers: new Headers({ "X-Request-Id": "request-lifecycle-1" }),
        json: vi.fn().mockResolvedValue({ detail }),
      });
      const fetchResponse = {
        ...response(),
        clone: vi.fn(() => response()),
      };
      const listener = vi.fn();
      window.addEventListener(eventName, listener, { once: true });
      setCustomerSessionToken(customerSessionText);
      vi.stubGlobal("fetch", vi.fn().mockResolvedValue(fetchResponse));

      await expect(listProjects()).rejects.toThrow();

      expect(listener).toHaveBeenCalledTimes(1);
      window.removeEventListener(eventName, listener);
    },
  );

  it("ignores an older workspace 401 whose body finishes after a new session attaches", async () => {
    let resolveErrorBody:
      | ((value: { detail: { code: string; message: string } }) => void)
      | undefined;
    const delayedErrorBody = new Promise<{
      detail: { code: string; message: string };
    }>((resolve) => {
      resolveErrorBody = resolve;
    });
    const cloneJson = vi.fn(() => delayedErrorBody);
    const fetchResponse = {
      ok: false,
      status: 401,
      headers: new Headers({ "X-Request-Id": "request-delayed-401" }),
      json: vi.fn().mockResolvedValue({
        detail: { code: "SESSION_EXPIRED", message: "older session expired" },
      }),
      clone: vi.fn(() => ({
        ok: false,
        status: 401,
        headers: new Headers({ "X-Request-Id": "request-delayed-401" }),
        json: cloneJson,
      })),
    };
    const listener = vi.fn();
    window.addEventListener(CUSTOMER_SESSION_EXPIRED_EVENT, listener);
    const releaseOlder = attachCustomerSessionToken("older-session");
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(fetchResponse));

    const pendingRequest = listProjects();
    await vi.waitFor(() => expect(cloneJson).toHaveBeenCalledTimes(1));
    const releaseCurrent = attachCustomerSessionToken("current-session");
    resolveErrorBody?.({
      detail: { code: "SESSION_EXPIRED", message: "older session expired" },
    });

    await expect(pendingRequest).rejects.toThrow();
    expect(listener).not.toHaveBeenCalled();

    releaseOlder();
    releaseCurrent();
    window.removeEventListener(CUSTOMER_SESSION_EXPIRED_EVENT, listener);
  });
});

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
    setCustomerSessionToken(null);
    setInternalAccessToken(null);
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  it("downloads a direct result using task authorization without forwarding credentials", async () => {
    vi.useFakeTimers();
    setCustomerSessionToken("test-customer-session");
    const video = new Blob(["provider video"], { type: "video/mp4" });
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({ url: "https://provider.example/video.mp4" }),
      })
      .mockResolvedValueOnce({ ok: true, blob: async () => video });
    const createObjectURL = vi.fn(() => "blob:direct-result");
    const revokeObjectURL = vi.fn();
    const click = vi
      .spyOn(HTMLAnchorElement.prototype, "click")
      .mockImplementation(function (this: HTMLAnchorElement) {
        expect(this.download).toBe("direct-task.mp4");
        expect(this.href).toBe("blob:direct-result");
      });
    vi.stubGlobal("fetch", fetchMock);
    vi.stubGlobal("URL", { createObjectURL, revokeObjectURL });

    await downloadGenerationTaskResult("task 1", "direct-task.mp4");

    expect(fetchMock.mock.calls[0][0]).toBe(
      "http://127.0.0.1:8000/api/generation-tasks/task%201/preview-url",
    );
    expect(
      new Headers(fetchMock.mock.calls[0][1].headers).get("Authorization"),
    ).toBe("Bearer test-customer-session");
    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      "https://provider.example/video.mp4",
      {
        signal: expect.any(AbortSignal),
        credentials: "omit",
      },
    );
    expect(createObjectURL).toHaveBeenCalledWith(video);
    expect(click).toHaveBeenCalledOnce();
    await vi.advanceTimersByTimeAsync(1_000);
    expect(revokeObjectURL).toHaveBeenCalledWith("blob:direct-result");
  });

  it("downloads authorized inline MP4 bytes without a data URL fetch", async () => {
    setCustomerSessionToken("test-inline-session");
    const fetchMock = vi.fn().mockResolvedValueOnce({
      ok: true,
      json: async () => ({ url: "data:video/mp4;base64,AP+AQQ==" }),
    });
    const createObjectURL = vi.fn((_blob: Blob) => "blob:inline-result");
    const click = vi
      .spyOn(HTMLAnchorElement.prototype, "click")
      .mockImplementation(function (this: HTMLAnchorElement) {
        expect(this.download).toBe("inline.mp4");
        expect(this.href).toBe("blob:inline-result");
      });
    vi.stubGlobal("fetch", fetchMock);
    vi.stubGlobal("URL", { createObjectURL, revokeObjectURL: vi.fn() });

    await downloadGenerationTaskResult("task-inline", "inline.mp4");

    expect(fetchMock).toHaveBeenCalledOnce();
    expect(fetchMock.mock.calls[0][0]).toBe(
      "http://127.0.0.1:8000/api/generation-tasks/task-inline/preview-url",
    );
    expect(
      new Headers(fetchMock.mock.calls[0][1].headers).get("Authorization"),
    ).toBe("Bearer test-inline-session");
    const blob = createObjectURL.mock.calls[0][0] as Blob;
    expect(blob.type).toBe("video/mp4");
    const bytes = await new Promise<Uint8Array>((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () =>
        resolve(new Uint8Array(reader.result as ArrayBuffer));
      reader.onerror = () => reject(reader.error);
      reader.readAsArrayBuffer(blob);
    });
    expect([...bytes]).toEqual([0, 255, 128, 65]);
    expect(click).toHaveBeenCalledOnce();
  });

  it("rejects malformed inline MP4 base64 without saving or fetching it", async () => {
    const fetchMock = vi.fn().mockResolvedValueOnce({
      ok: true,
      json: async () => ({ url: "data:video/mp4;base64,%%%invalid%%%" }),
    });
    const createObjectURL = vi.fn();
    const click = vi
      .spyOn(HTMLAnchorElement.prototype, "click")
      .mockImplementation(() => undefined);
    vi.stubGlobal("fetch", fetchMock);
    vi.stubGlobal("URL", { createObjectURL, revokeObjectURL: vi.fn() });

    await expect(
      downloadGenerationTaskResult("task-inline", "inline.mp4"),
    ).rejects.toThrow("下载生成结果失败：内联视频数据无效。");

    expect(fetchMock).toHaveBeenCalledOnce();
    expect(createObjectURL).not.toHaveBeenCalled();
    expect(click).not.toHaveBeenCalled();
  });

  it.each([403, 404, 409])(
    "does not fetch a provider file when task authorization returns %i",
    async (status) => {
      const fetchMock = vi.fn().mockResolvedValue({
        ok: false,
        status,
        json: async () => ({ detail: { code: "RESULT_NOT_AVAILABLE" } }),
      });
      vi.stubGlobal("fetch", fetchMock);
      await expect(
        downloadGenerationTaskResult("task-other", "video.mp4"),
      ).rejects.toThrow();
      expect(fetchMock).toHaveBeenCalledOnce();
    },
  );

  it("does not save an unavailable provider response as an MP4", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({ url: "https://provider.example/expired.mp4" }),
      })
      .mockResolvedValueOnce({ ok: false, status: 403 });
    const click = vi
      .spyOn(HTMLAnchorElement.prototype, "click")
      .mockImplementation(() => undefined);
    vi.stubGlobal("fetch", fetchMock);
    await expect(
      downloadGenerationTaskResult("task-expired", "video.mp4"),
    ).rejects.toThrow("403");
    expect(click).not.toHaveBeenCalled();
  });

  it("times out a direct file download without retrying a paid generation", async () => {
    vi.useFakeTimers();
    const fetchMock = vi
      .fn()
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
    vi.stubGlobal("fetch", fetchMock);
    const download = expect(
      downloadGenerationTaskResult("task-slow", "video.mp4"),
    ).rejects.toThrow();
    await vi.advanceTimersByTimeAsync(60_001);
    await download;
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("downloads a signed cross-origin result through a local blob URL", async () => {
    vi.useFakeTimers();
    const resultBlob = new Blob(["video"], { type: "video/mp4" });
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({ url: "https://signed.example/result.mp4" }),
      })
      .mockResolvedValueOnce({
        ok: true,
        blob: async () => resultBlob,
      });
    const createObjectUrl = vi.fn(() => "blob:generation-result");
    const revokeObjectUrl = vi.fn();
    const anchorClick = vi
      .spyOn(HTMLAnchorElement.prototype, "click")
      .mockImplementation(() => undefined);
    vi.stubGlobal("fetch", fetchMock);
    vi.stubGlobal("URL", {
      createObjectURL: createObjectUrl,
      revokeObjectURL: revokeObjectUrl,
    });

    await downloadGenerationResult("asset 1", "task-1.mp4");

    expect(fetchMock).toHaveBeenNthCalledWith(
      1,
      "http://127.0.0.1:8000/api/assets/asset%201/download-url",
      expect.objectContaining({ method: "POST" }),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      "https://signed.example/result.mp4",
      expect.objectContaining({ signal: expect.any(AbortSignal) }),
    );
    expect(createObjectUrl).toHaveBeenCalledWith(resultBlob);
    expect(anchorClick).toHaveBeenCalledOnce();

    await vi.advanceTimersByTimeAsync(1_000);
    expect(revokeObjectUrl).toHaveBeenCalledWith("blob:generation-result");
  });

  it("uses the local character cache for previews and manual downloads", async () => {
    vi.useFakeTimers();
    const characterBlob = new Blob(["character"], { type: "image/png" });
    const cachedUrl =
      "http://127.0.0.1:8000/api/assets/character-cache/cache.png?expires=1&sig=test";
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({ url: cachedUrl }),
      })
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({ url: cachedUrl }),
      })
      .mockResolvedValueOnce({
        ok: true,
        blob: async () => characterBlob,
      });
    const createObjectUrl = vi.fn(() => "blob:character");
    const revokeObjectUrl = vi.fn();
    const anchorClick = vi
      .spyOn(HTMLAnchorElement.prototype, "click")
      .mockImplementation(() => undefined);
    vi.stubGlobal("fetch", fetchMock);
    vi.stubGlobal("URL", {
      createObjectURL: createObjectUrl,
      revokeObjectURL: revokeObjectUrl,
    });

    await expect(getCachedCharacterAssetUrl("asset 1")).resolves.toEqual({
      url: cachedUrl,
    });
    await downloadCharacterAsset("asset 1", "人物.png");

    expect(fetchMock).toHaveBeenNthCalledWith(
      1,
      "http://127.0.0.1:8000/api/assets/asset%201/cached-url",
      expect.objectContaining({ method: "POST" }),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      "http://127.0.0.1:8000/api/assets/asset%201/cached-url",
      expect.objectContaining({ method: "POST" }),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      3,
      cachedUrl,
      expect.objectContaining({ signal: expect.any(AbortSignal) }),
    );
    expect(createObjectUrl).toHaveBeenCalledWith(characterBlob);
    expect(anchorClick).toHaveBeenCalledOnce();

    await vi.advanceTimersByTimeAsync(1_000);
    expect(revokeObjectUrl).toHaveBeenCalledWith("blob:character");
  });

  it("returns the signed streaming url directly for in-player preview", async () => {
    const fetchMock = vi.fn().mockResolvedValueOnce({
      ok: true,
      json: async () => ({ url: "https://signed.example/preview.mp4" }),
    });
    vi.stubGlobal("fetch", fetchMock);

    await expect(createGenerationResultPreviewUrl("asset 1")).resolves.toBe(
      "https://signed.example/preview.mp4",
    );

    // 预签名 URL 直连 video src：只签发地址，不再二次拉取 blob。
    expect(fetchMock).toHaveBeenCalledOnce();
    expect(fetchMock).toHaveBeenCalledWith(
      "http://127.0.0.1:8000/api/assets/asset%201/download-url",
      expect.objectContaining({ method: "POST" }),
    );
  });

  it("rejects a preview response without a signed url", async () => {
    const fetchMock = vi.fn().mockResolvedValueOnce({
      ok: true,
      json: async () => ({}),
    });
    vi.stubGlobal("fetch", fetchMock);

    await expect(createGenerationResultPreviewUrl("asset 1")).rejects.toThrow(
      "预览链接获取失败，请重试。",
    );
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
      ratio: "adaptive",
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
      ratio: "adaptive",
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

  it("posts wallet-backed regeneration contracts for batches and tasks", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ id: "replacement-batch" }),
    });
    vi.stubGlobal("fetch", fetchMock);
    const input = {
      idempotency_key: "paid-regeneration-key",
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
      ratio: "adaptive",
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

  it("lets the server apply safe source-frame defaults during automatic confirmation", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ id: "saved" }),
    });
    vi.stubGlobal("fetch", fetchMock);

    await confirmSourceFrame("project-1", "source-1");

    expect(fetchMock).toHaveBeenCalledWith(
      "http://127.0.0.1:8000/api/projects/project-1/source-frames/confirm",
      expect.objectContaining({
        body: JSON.stringify({ source_frame_asset_id: "source-1" }),
        method: "POST",
      }),
    );
  });

  it("maps a stale latest generation and sends the frozen binding on regeneration", async () => {
    const generatedVersion = {
      id: "first-frame-candidates-1",
      project_id: "project-1",
      asset_id: "source-1",
      kind: "first_frame_candidates",
      version_number: 1,
      payload: { candidates: [] },
      created_by_user_id: "employee-1",
      created_at: "2030-01-01T00:00:00Z",
    };
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce({ ok: false, status: 409 })
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({ id: "first-frame-task-1" }),
      })
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({
          id: "first-frame-task-1",
          status: "SUCCEEDED",
          result_version_id: generatedVersion.id,
        }),
      })
      .mockResolvedValueOnce({
        ok: true,
        json: async () => generatedVersion,
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

    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      "http://127.0.0.1:8000/api/projects/project-1/first-frame-tasks",
      expect.objectContaining({
        method: "POST",
      }),
    );
    const submitted = JSON.parse(
      String(fetchMock.mock.calls[1]?.[1]?.body),
    ) as Record<string, unknown>;
    expect(submitted).toMatchObject({
      model: "nano-banana-pro-2k",
      prompt: "replace",
      quantity: 1,
      character_version_id: "character-version-1",
      character_reference_selection_id: "reference-selection-1",
    });
    expect(submitted.idempotency_key).toMatch(/^first-frame-/);
  });

  it("shares one character task poller across concurrent recovery callers", async () => {
    vi.useFakeTimers();
    const pending = { id: "character-task-shared", status: "RUNNING" };
    const succeeded = {
      id: "character-task-shared",
      status: "SUCCEEDED",
      result: { persona_id: "persona-1" },
    };
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce({ ok: true, json: async () => pending })
      .mockResolvedValueOnce({ ok: true, json: async () => succeeded });
    vi.stubGlobal("fetch", fetchMock);

    const first = waitForCharacterSheetTask("character-task-shared");
    const recovered = waitForCharacterSheetTask("character-task-shared");
    await vi.advanceTimersByTimeAsync(1_500);

    await expect(Promise.all([first, recovered])).resolves.toEqual([
      succeeded,
      succeeded,
    ]);
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("shares one first-frame task poller and broadcasts progress to recovery callers", async () => {
    vi.useFakeTimers();
    const pending = {
      id: "first-frame-task-shared",
      status: "PENDING",
      stage: "QUEUED",
    };
    const succeeded = {
      id: "first-frame-task-shared",
      status: "SUCCEEDED",
      stage: "SUCCEEDED",
      result_version_id: "version-shared",
    };
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce({ ok: true, json: async () => pending })
      .mockResolvedValueOnce({ ok: true, json: async () => succeeded });
    vi.stubGlobal("fetch", fetchMock);

    const firstUpdates: string[] = [];
    const recoveredUpdates: string[] = [];
    const first = waitForFirstFrameTask("first-frame-task-shared", (task) =>
      firstUpdates.push(task.stage),
    );
    const recovered = waitForFirstFrameTask("first-frame-task-shared", (task) =>
      recoveredUpdates.push(task.stage),
    );
    await vi.advanceTimersByTimeAsync(1_500);

    await expect(Promise.all([first, recovered])).resolves.toEqual([
      succeeded,
      succeeded,
    ]);
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(firstUpdates).toEqual(["QUEUED", "SUCCEEDED"]);
    expect(recoveredUpdates).toEqual(["QUEUED", "SUCCEEDED"]);
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

  it("only enqueues analysis with the normal API timeout", async () => {
    const timeoutSpy = vi.spyOn(window, "setTimeout");
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ id: "analysis-1" }),
    });
    vi.stubGlobal("fetch", fetchMock);

    await startVideoAnalysis("project-1", "asset-1");

    expect(timeoutSpy).toHaveBeenCalledWith(expect.any(Function), 5_000);
    expect(fetchMock).toHaveBeenCalledWith(
      "http://127.0.0.1:8000/api/projects/project-1/analysis-tasks",
      expect.objectContaining({
        body: JSON.stringify({
          asset_id: "asset-1",
          reuse_existing: false,
        }),
      }),
    );
  });

  it("shares one analysis task poller across concurrent recovery callers", async () => {
    vi.useFakeTimers();
    const pending = { id: "analysis-task-shared", status: "RUNNING" };
    const succeeded = {
      id: "analysis-task-shared",
      status: "SUCCEEDED",
      result_version_id: "analysis-version-shared",
    };
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce({ ok: true, json: async () => pending })
      .mockResolvedValueOnce({ ok: true, json: async () => succeeded });
    vi.stubGlobal("fetch", fetchMock);

    const first = waitForAnalysisTask("analysis-task-shared");
    const recovered = waitForAnalysisTask("analysis-task-shared");
    await vi.advanceTimersByTimeAsync(1_500);

    await expect(Promise.all([first, recovered])).resolves.toEqual([
      succeeded,
      succeeded,
    ]);
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("enqueues source-frame extraction and shares its durable poller", async () => {
    vi.useFakeTimers();
    const queued = {
      id: "source-frame-task-shared",
      project_id: "project-1",
      asset_id: "asset-1",
      timestamps_seconds: [2.4, 6, 9.6],
      status: "PENDING",
    };
    const running = { ...queued, status: "RUNNING" };
    const succeeded = {
      ...queued,
      status: "SUCCEEDED",
      result_version_id: "source-frame-version-shared",
    };
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce({ ok: true, json: async () => queued })
      .mockResolvedValueOnce({ ok: true, json: async () => running })
      .mockResolvedValueOnce({ ok: true, json: async () => succeeded });
    vi.stubGlobal("fetch", fetchMock);

    const task = await extractSourceFrames(
      "project-1",
      "asset-1",
      queued.timestamps_seconds,
    );
    const first = waitForSourceFrameTask(task.id);
    const recovered = waitForSourceFrameTask(task.id);
    await vi.advanceTimersByTimeAsync(1_500);

    await expect(Promise.all([first, recovered])).resolves.toEqual([
      succeeded,
      succeeded,
    ]);
    expect(fetchMock).toHaveBeenCalledTimes(3);
    const enqueueBody = JSON.parse(String(fetchMock.mock.calls[0][1]?.body));
    expect(enqueueBody).toEqual(
      expect.objectContaining({
        asset_id: "asset-1",
        timestamps_seconds: [2.4, 6, 9.6],
        idempotency_key: expect.any(String),
      }),
    );
  });

  it("preserves the failed source-frame task for safe UI recovery", async () => {
    const failed = {
      id: "source-frame-task-failed",
      project_id: "project-1",
      asset_id: "asset-1",
      timestamps_seconds: [2.4],
      status: "FAILED",
      attempt: 1,
      result_version_id: null,
      error_code: "SOURCE_FRAME_TASK_RECOVERY_REQUIRED",
      error_message: "取帧任务执行中断，请重新开始。",
      retryable: true,
      created_at: "2026-09-02T00:00:00Z",
      updated_at: "2026-09-02T00:01:00Z",
      started_at: "2026-09-02T00:00:01Z",
      completed_at: "2026-09-02T00:01:00Z",
    } as const;
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({ ok: true, json: async () => failed }),
    );

    await expect(waitForSourceFrameTask(failed.id)).rejects.toMatchObject({
      name: "SourceFrameTaskFailedError",
      task: failed,
    });
  });

  it("cancels a source-frame task through its recovery endpoint", async () => {
    const cancelled = {
      id: "source-frame-task-cancelled",
      project_id: "project-1",
      asset_id: "asset-1",
      timestamps_seconds: [2.4],
      status: "FAILED",
      error_code: "SOURCE_FRAME_TASK_CANCELLED",
      retryable: true,
    };
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => cancelled,
    });
    vi.stubGlobal("fetch", fetchMock);

    await expect(
      cancelSourceFrameTask("source-frame-task-cancelled"),
    ).resolves.toEqual(cancelled);
    expect(fetchMock).toHaveBeenCalledWith(
      "http://127.0.0.1:8000/api/source-frame-tasks/source-frame-task-cancelled/cancel",
      expect.objectContaining({ method: "POST" }),
    );
  });

  it("enqueues script rewrite and shares its durable recovery poller", async () => {
    vi.useFakeTimers();
    const queued = {
      id: "script-rewrite-task-shared",
      project_id: "project-1",
      status: "PENDING",
      result: null,
    };
    const running = { ...queued, status: "RUNNING" };
    const succeeded = {
      ...queued,
      status: "SUCCEEDED",
      result: {
        rewritten_text: "新的二创稿。",
        provider: "deepseek",
        model: "deepseek-chat",
      },
    };
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce({ ok: true, json: async () => queued })
      .mockResolvedValueOnce({ ok: true, json: async () => running })
      .mockResolvedValueOnce({ ok: true, json: async () => succeeded });
    vi.stubGlobal("fetch", fetchMock);

    const task = await rewriteProjectScript(
      "project-1",
      "待改写原稿。",
      "identity-1",
      "source-1",
      "rewrite-key-1",
    );
    const first = waitForScriptRewriteTask(task.id);
    const recovered = waitForScriptRewriteTask(task.id);
    await vi.advanceTimersByTimeAsync(1_500);

    await expect(Promise.all([first, recovered])).resolves.toEqual([
      succeeded,
      succeeded,
    ]);
    expect(fetchMock).toHaveBeenCalledTimes(3);
    const enqueueBody = JSON.parse(String(fetchMock.mock.calls[0][1]?.body));
    expect(enqueueBody).toEqual({
      text: "待改写原稿。",
      identity_id: "identity-1",
      source_asset_id: "source-1",
      idempotency_key: "rewrite-key-1",
    });
  });

  it("读取本次音频转写任务使用任务 ID 并编码路径", async () => {
    const task = { id: "asr/one", status: "RUNNING" };
    const fetchMock = vi
      .fn()
      .mockResolvedValue({ ok: true, json: async () => task });
    vi.stubGlobal("fetch", fetchMock);
    await expect(getScriptFromAudioTask("asr/one")).resolves.toEqual(task);
    expect(fetchMock.mock.calls[0][0]).toBe(
      "http://127.0.0.1:8000/api/script-from-audio-tasks/asr%2Fone",
    );
  });

  it("按人物或无人物作用域读取最新改写任务", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValue({ ok: true, json: async () => null });
    vi.stubGlobal("fetch", fetchMock);

    await getLatestScriptRewriteTask("project-1", "identity 1", "source/1");
    await getLatestScriptRewriteTask("project-1");

    expect(fetchMock.mock.calls.map((call) => call[0])).toEqual([
      "http://127.0.0.1:8000/api/projects/project-1/script-rewrite-tasks/latest?identity_scope=identity&identity_id=identity+1&source_asset_id=source%2F1",
      "http://127.0.0.1:8000/api/projects/project-1/script-rewrite-tasks/latest?identity_scope=none",
    ]);
  });

  it("enqueues H3 reconciliation and shares its durable recovery poller", async () => {
    vi.useFakeTimers();
    const queued = {
      id: "reconcile-operation-shared",
      task_id: "generation-task-1",
      status: "PENDING",
    };
    const running = { ...queued, status: "RUNNING" };
    const succeeded = { ...queued, status: "SUCCEEDED" };
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce({ ok: true, json: async () => queued })
      .mockResolvedValueOnce({ ok: true, json: async () => running })
      .mockResolvedValueOnce({ ok: true, json: async () => succeeded });
    vi.stubGlobal("fetch", fetchMock);

    const operation = await reconcileUncertainTask("generation-task-1", {
      idempotency_key: "reconcile-operation-key",
    });
    const first = waitForGenerationReconcileOperation(operation.id);
    const recovered = waitForGenerationReconcileOperation(operation.id);
    await vi.advanceTimersByTimeAsync(1_500);

    await expect(Promise.all([first, recovered])).resolves.toEqual([
      succeeded,
      succeeded,
    ]);
    expect(fetchMock).toHaveBeenCalledTimes(3);
    expect(fetchMock.mock.calls[0][0]).toBe(
      "http://127.0.0.1:8000/api/generation-tasks/generation-task-1/reconcile",
    );
  });

  it("tells a temporary network failure apart from an unusable model response", async () => {
    const unreachable = vi.fn().mockResolvedValue({
      ok: false,
      status: 503,
      json: async () => ({
        detail: {
          code: "ANALYSIS_PROVIDER_UNREACHABLE",
          message: "无法连接视频拆解服务，请检查网络后重试。",
          failure_phase: "network",
          retryable: true,
        },
      }),
    });
    vi.stubGlobal("fetch", unreachable);

    await expect(startVideoAnalysis("project-1", "asset-1")).rejects.toThrow(
      /网络/,
    );

    const invalidResponse = vi.fn().mockResolvedValue({
      ok: false,
      status: 502,
      json: async () => ({
        detail: {
          code: "ANALYSIS_PROVIDER_FAILED",
          message: "视频拆解服务返回了无法解析的结果，请重试或更换参考视频。",
          failure_phase: "response",
          retryable: false,
        },
      }),
    });
    vi.stubGlobal("fetch", invalidResponse);

    await expect(startVideoAnalysis("project-1", "asset-1")).rejects.toThrow(
      /无法解析的结果/,
    );
  });

  it("explains a request rejected by server-side validation instead of showing a bare status", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: false,
      status: 422,
      json: async () => ({
        detail: [
          {
            type: "less_than_equal",
            loc: ["body", "duration_seconds"],
            msg: "Input should be less than or equal to 15.1",
          },
        ],
      }),
    });
    vi.stubGlobal("fetch", fetchMock);

    await expect(startVideoAnalysis("project-1", "asset-1")).rejects.toThrow(
      /参考视频不满足拆解要求/,
    );
  });
});

describe("createVideoUploadIntent", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("sends a SHA-256 fingerprint so the server can skip duplicate uploads", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({
        asset_id: "asset-1",
        project_id: "project-1",
        storage_key: "projects/project-1/video.mp4",
        method: null,
        url: null,
        headers: {},
        expires_at: null,
        upload_required: false,
      }),
    });
    vi.stubGlobal("fetch", fetchMock);
    const file = new File(["same-video"], "reference.mp4", {
      type: "video/mp4",
    });

    await createVideoUploadIntent("project-1", file);

    const options = fetchMock.mock.calls[0]?.[1] as RequestInit;
    const body = JSON.parse(String(options.body));
    expect(body.sha256).toMatch(/^[0-9a-f]{64}$/);
    expect(body.size_bytes).toBe(file.size);
  });
});

describe("getCurrentUser", () => {
  afterEach(() => {
    setCustomerSessionToken(null);
    vi.unstubAllGlobals();
    vi.unstubAllEnvs();
  });

  it("uses the customer session for shared workspace requests", async () => {
    vi.stubEnv("DEV", false);
    vi.stubEnv("PROD", true);
    const user = {
      id: "customer-1",
      username: "customer-1",
      display_name: "客户",
      role: "customer",
    };
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => user,
    });
    vi.stubGlobal("fetch", fetchMock);
    setCustomerSessionToken("customer-session-1");

    await expect(getCurrentUser()).resolves.toEqual(user);

    const options = fetchMock.mock.calls[0]?.[1] as RequestInit;
    const headers = options.headers as Headers;
    expect(headers.get("Authorization")).toBe("Bearer customer-session-1");
    expect(headers.has("X-Dev-User-Id")).toBe(false);
  });

  it("does not let an older workspace cleanup clear the active session", async () => {
    const releaseOlder = attachCustomerSessionToken("older-session");
    const releaseCurrent = attachCustomerSessionToken("current-session");
    releaseOlder();
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({
        id: "customer-1",
        username: "customer-1",
        display_name: "Customer One",
        role: "customer",
      }),
    });
    vi.stubGlobal("fetch", fetchMock);

    await getCurrentUser();

    const options = fetchMock.mock.calls[0]?.[1] as RequestInit;
    expect(new Headers(options.headers).get("Authorization")).toBe(
      "Bearer current-session",
    );
    releaseCurrent();
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

  it("forwards the internal Bearer token instead of the dev header for local uploads", async () => {
    class ManagedUploadRequest {
      static latest: ManagedUploadRequest | null = null;
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
        ManagedUploadRequest.latest = this;
      }

      open() {}
      setRequestHeader(name: string, value: string) {
        this.headers.set(name, value);
      }
      send() {
        this.onload?.();
      }
    }

    vi.stubGlobal("XMLHttpRequest", ManagedUploadRequest);
    setInternalAccessToken("internal-token-1");

    try {
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
    } finally {
      setInternalAccessToken(null);
    }

    expect(ManagedUploadRequest.latest?.headers.get("Authorization")).toBe(
      "Bearer internal-token-1",
    );
    expect(ManagedUploadRequest.latest?.headers.has("X-Dev-User-Id")).toBe(
      false,
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
