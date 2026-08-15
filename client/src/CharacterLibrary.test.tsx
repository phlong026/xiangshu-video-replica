import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import * as api from "./api";
import { CharacterLibrary } from "./CharacterLibrary";

vi.mock("./api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./api")>();
  return {
    ...actual,
    completeIdentityAuthorizationUpload: vi.fn(),
    completeIdentitySourceUpload: vi.fn(),
    createCharacterPersona: vi.fn(),
    createCharacterVersion: vi.fn(),
    createIdentityUploadIntent: vi.fn(),
    createPersonIdentity: vi.fn(),
    generateCharacterAssets: vi.fn(),
    getAssetDownloadUrl: vi.fn(),
    listCharacterAssets: vi.fn(),
    listCharacterGenerationTasks: vi.fn(),
    listCharacterPersonas: vi.fn(),
    listCharacterVersions: vi.fn(),
    listPersonIdentities: vi.fn(),
    publishCharacterVersion: vi.fn(),
    regenerateCharacterAsset: vi.fn(),
    reviewCharacterAsset: vi.fn(),
    updateCharacterPersona: vi.fn(),
    uploadIdentityAsset: vi.fn(),
  };
});

const identity: api.PersonIdentity = {
  id: "identity-1",
  owner_user_id: "employee_1",
  display_name: "林夏",
  authorization_status: "AUTHORIZED" as const,
  authorization_asset_id: "authorization-1",
  authorization_scope: ["内部短视频"],
  authorization_expires_at: "2035-01-01T00:00:00Z",
  source_asset_id: "source-1",
  source_quality_status: "PASSED" as const,
  status: "ACTIVE" as const,
  created_by: "admin_1",
  created_at: "2026-08-16T00:00:00Z",
  updated_at: "2026-08-16T00:00:00Z",
};

const persona: api.CharacterPersona = {
  id: "persona-1",
  identity_id: identity.id,
  name: "乡墅项目管理专家",
  occupation: "项目管理",
  scene_description: "乡村别墅施工现场",
  appearance_constraints_json: {},
  costume_description: "工程马甲",
  default_background: "施工现场",
  positive_prompt: "真实自然",
  negative_prompt: "不要卡通",
  usage_scope_json: ["内部短视频"],
  created_by: "admin_1",
  created_at: "2026-08-16T00:00:00Z",
  updated_at: "2026-08-16T00:00:00Z",
};

const version: api.CharacterVersion = {
  id: "version-1",
  persona_id: persona.id,
  version_number: 1,
  status: "DRAFT" as const,
  source_asset_id: identity.source_asset_id,
  source_sha256: "source-sha",
  persona_snapshot_json: { name: persona.name },
  provider: "fake_character",
  model: "fake-character-v1",
  generation_params_json: {},
  template_version: "character-assets-v1",
  template_hash: "template-sha",
  required_view_types_json: [
    "FRONT_FACE",
    "FRONT_HALF",
    "FRONT_FULL",
    "LEFT_45",
    "RIGHT_45",
    "LEFT_SIDE",
    "RIGHT_SIDE",
  ],
  published_by: null,
  published_at: null,
  publication_snapshot_json: null,
  publication_hash: null,
  created_by: "admin_1",
  created_at: "2026-08-16T00:00:00Z",
};

const publishedVersion: api.CharacterVersion = {
  ...version,
  status: "PUBLISHED" as const,
  published_by: "admin_1",
  published_at: "2026-08-16T01:00:00Z",
  publication_snapshot_json: {},
  publication_hash: "publication-sha",
};

const viewTypes = [
  "FRONT_FACE",
  "FRONT_HALF",
  "FRONT_FULL",
  "LEFT_45",
  "RIGHT_45",
  "LEFT_SIDE",
  "RIGHT_SIDE",
] as const;

function candidate(
  viewType: (typeof viewTypes)[number],
  index: number,
): api.CharacterAsset {
  return {
    id: `candidate-${index}`,
    character_version_id: version.id,
    asset_id: `asset-${index}`,
    view_type: viewType,
    candidate_number: 1,
    generation_task_id: `generation-${index}`,
    auto_quality_json: {
      simulated: true,
      scores: { identity_consistency: 0.86 },
      issue_codes: [],
    },
    review_status: "NOT_REVIEWED" as const,
    is_published_selection: false,
    created_at: "2026-08-16T00:00:00Z",
  };
}

let assets: api.CharacterAsset[] = viewTypes.map(candidate);

function prepareLoadedLibrary() {
  vi.mocked(api.listPersonIdentities).mockResolvedValue([identity]);
  vi.mocked(api.listCharacterPersonas).mockResolvedValue([persona]);
  vi.mocked(api.listCharacterVersions).mockResolvedValue([version]);
  vi.mocked(api.listCharacterAssets).mockImplementation(async () => assets);
  vi.mocked(api.listCharacterGenerationTasks).mockResolvedValue([]);
  vi.mocked(api.getAssetDownloadUrl).mockImplementation(async (assetId) => ({
    url: `blob:${assetId}`,
  }));
}

function preparePublishedLibrary() {
  assets = viewTypes.map((viewType, index) => ({
    ...candidate(viewType, index),
    character_version_id: publishedVersion.id,
    review_status: "APPROVED" as const,
    is_published_selection: true,
  }));
  vi.mocked(api.listPersonIdentities).mockResolvedValue([identity]);
  vi.mocked(api.listCharacterPersonas).mockResolvedValue([persona]);
  vi.mocked(api.listCharacterVersions).mockResolvedValue([publishedVersion]);
  vi.mocked(api.listCharacterAssets).mockImplementation(async () => assets);
  vi.mocked(api.getAssetDownloadUrl).mockImplementation(async (assetId) => ({
    url: `blob:${assetId}`,
  }));
}

describe("CharacterLibrary", () => {
  beforeEach(() => {
    assets = viewTypes.map(candidate);
    vi.clearAllMocks();
    window.localStorage.clear();
  });

  afterEach(() => {
    vi.restoreAllMocks();
    window.localStorage.clear();
  });

  it("renders identity, persona, version history and seven fixed review slots", async () => {
    prepareLoadedLibrary();

    render(<CharacterLibrary userRole="admin" />);

    expect(await screen.findAllByText("林夏")).toHaveLength(2);
    expect(screen.getByText("授权有效至 2035-01-01")).toBeInTheDocument();
    expect(await screen.findByText("乡墅项目管理专家")).toBeInTheDocument();
    expect(await screen.findByRole("tab", { name: /V1/ })).toBeInTheDocument();
    expect(await screen.findAllByText("自动提示（仅供参考）")).toHaveLength(7);
    expect(screen.getAllByText("人工审核：待审核")).toHaveLength(7);
    for (const name of [
      "正面头像",
      "正面半身",
      "正面全身",
      "左 45°",
      "右 45°",
      "左侧面",
      "右侧面",
    ]) {
      expect(screen.getByRole("heading", { name })).toBeInTheDocument();
    }
  });

  it("lets an employee read published assets without requesting restricted generation tasks", async () => {
    preparePublishedLibrary();
    vi.mocked(api.listCharacterGenerationTasks).mockRejectedValue(
      new Error("forbidden"),
    );

    render(<CharacterLibrary userRole="employee" />);

    expect(await screen.findAllByText("当前发布资产")).toHaveLength(7);
    expect(api.listCharacterGenerationTasks).not.toHaveBeenCalled();
    expect(screen.queryAllByRole("radio")).toHaveLength(0);
    for (const action of [
      "创建人物身份",
      "新建人设",
      "编辑人设",
      "创建 DRAFT 版本",
      "开始生成 7 类视角",
      "发布角色版本",
    ]) {
      expect(screen.queryByRole("button", { name: action })).toBeNull();
    }
  });

  it("keeps auditor character metadata read-only while exposing task status", async () => {
    preparePublishedLibrary();
    vi.mocked(api.listCharacterGenerationTasks).mockResolvedValue([]);

    render(<CharacterLibrary userRole="auditor" />);

    expect(await screen.findAllByText("当前发布资产")).toHaveLength(7);
    expect(api.listCharacterGenerationTasks).toHaveBeenCalledWith(version.id);
    expect(api.getAssetDownloadUrl).not.toHaveBeenCalled();
    expect(screen.queryAllByRole("radio")).toHaveLength(0);
    expect(
      screen.getByText("审计身份可读元数据，但不能创建源图下载链接。"),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "发布角色版本" })).toBeNull();
  });

  it("ignores an older asset response after the selected version changes", async () => {
    const newerVersion: api.CharacterVersion = {
      ...version,
      id: "version-2",
      version_number: 2,
    };
    const currentAsset: api.CharacterAsset = {
      ...candidate("FRONT_FACE", 1),
      character_version_id: version.id,
    };
    const staleAsset: api.CharacterAsset = {
      ...candidate("FRONT_FACE", 2),
      id: "candidate-stale",
      asset_id: "asset-stale",
      character_version_id: newerVersion.id,
      candidate_number: 2,
    };
    let resolveOlderRequest: (value: api.CharacterAsset[]) => void = () => {};
    const olderRequest = new Promise<api.CharacterAsset[]>((resolve) => {
      resolveOlderRequest = resolve;
    });

    vi.mocked(api.listPersonIdentities).mockResolvedValue([identity]);
    vi.mocked(api.listCharacterPersonas).mockResolvedValue([persona]);
    vi.mocked(api.listCharacterVersions).mockResolvedValue([
      version,
      newerVersion,
    ]);
    vi.mocked(api.listCharacterAssets).mockImplementation((versionId) =>
      versionId === newerVersion.id
        ? olderRequest
        : Promise.resolve([currentAsset]),
    );
    vi.mocked(api.listCharacterGenerationTasks).mockResolvedValue([]);
    vi.mocked(api.getAssetDownloadUrl).mockImplementation(async (assetId) => ({
      url: `blob:${assetId}`,
    }));

    render(<CharacterLibrary userRole="admin" />);
    await screen.findByRole("tab", { name: /V2/ });
    fireEvent.click(screen.getByRole("tab", { name: /V1/ }));
    expect(await screen.findByText("候选 1")).toBeInTheDocument();

    await act(async () => {
      resolveOlderRequest([staleAsset]);
      await olderRequest;
    });

    await waitFor(() => {
      expect(screen.queryByText("候选 2")).not.toBeInTheDocument();
      expect(screen.getByText("候选 1")).toBeInTheDocument();
    });
  });

  it("shows a recoverable placeholder when an asset image cannot render", async () => {
    prepareLoadedLibrary();

    render(<CharacterLibrary userRole="admin" />);
    const preview = await screen.findByAltText("林夏 真人源图预览");
    fireEvent.error(preview);

    expect(await screen.findByText("预览失败")).toBeInTheDocument();
  });

  it("reuses the batch idempotency key after an uncertain generation response", async () => {
    prepareLoadedLibrary();
    vi.mocked(api.generateCharacterAssets)
      .mockRejectedValueOnce(new Error("请求超时，提交结果未知"))
      .mockResolvedValueOnce([]);

    render(<CharacterLibrary userRole="admin" />);
    const generateButton = await screen.findByRole("button", {
      name: "开始生成 7 类视角",
    });
    fireEvent.click(generateButton);
    expect(
      await screen.findByText("请求超时，提交结果未知"),
    ).toBeInTheDocument();
    fireEvent.click(generateButton);

    await waitFor(() =>
      expect(api.generateCharacterAssets).toHaveBeenCalledTimes(2),
    );
    const firstInput = vi.mocked(api.generateCharacterAssets).mock.calls[0][1];
    const retryInput = vi.mocked(api.generateCharacterAssets).mock.calls[1][1];
    expect(retryInput.idempotency_key).toBe(firstInput.idempotency_key);
  });

  it("lets an admin create an active identity, persona and draft version without internal ids", async () => {
    vi.mocked(api.listPersonIdentities).mockResolvedValue([]);
    vi.mocked(api.createPersonIdentity).mockResolvedValue({
      ...identity,
      authorization_status: "PENDING",
      authorization_asset_id: null,
      source_asset_id: null,
      source_quality_status: "PENDING",
      status: "DRAFT",
    });
    vi.mocked(api.createIdentityUploadIntent)
      .mockResolvedValueOnce({
        asset_id: "authorization-1",
        identity_id: identity.id,
        purpose: "authorization",
        storage_key: "authorization.pdf",
        method: "PUT",
        url: "http://127.0.0.1:8000/upload/authorization",
        headers: {},
        expires_at: "2030-01-01T00:00:00Z",
      })
      .mockResolvedValueOnce({
        asset_id: "source-1",
        identity_id: identity.id,
        purpose: "source",
        storage_key: "source.png",
        method: "PUT",
        url: "http://127.0.0.1:8000/upload/source",
        headers: {},
        expires_at: "2030-01-01T00:00:00Z",
      });
    vi.mocked(api.uploadIdentityAsset).mockResolvedValue(undefined);
    vi.mocked(api.completeIdentityAuthorizationUpload).mockResolvedValue({
      ...identity,
      source_asset_id: null,
      source_quality_status: "PENDING",
      status: "DRAFT",
    });
    vi.mocked(api.completeIdentitySourceUpload).mockResolvedValue({
      identity,
      asset_id: "source-1",
      sha256: "source-sha",
      size_bytes: 2048,
      content_type: "image/png",
      quality: {
        passed: true,
        width: 1024,
        height: 1536,
        person_count: 1,
        face_count: 1,
        face_visible: true,
        sharpness_score: 0.88,
        occlusion_detected: false,
        watermark_detected: false,
        issue_codes: [],
        notes: ["单人正面清晰"],
        provider: "fake-source-inspector",
        model: "fake-source-v1",
      },
    });
    vi.mocked(api.listCharacterPersonas).mockResolvedValue([]);
    vi.mocked(api.createCharacterPersona).mockResolvedValue(persona);
    vi.mocked(api.listCharacterVersions).mockResolvedValue([]);
    vi.mocked(api.createCharacterVersion).mockResolvedValue(version);
    vi.mocked(api.listCharacterAssets).mockResolvedValue([]);
    vi.mocked(api.listCharacterGenerationTasks).mockResolvedValue([]);
    vi.mocked(api.getAssetDownloadUrl).mockResolvedValue({
      url: "blob:source",
    });

    render(<CharacterLibrary userRole="admin" />);
    fireEvent.click(
      await screen.findByRole("button", { name: "创建人物身份" }),
    );
    fireEvent.change(screen.getByLabelText("人物显示名"), {
      target: { value: "林夏" },
    });
    fireEvent.change(screen.getByLabelText("授权使用范围"), {
      target: { value: "内部短视频" },
    });
    fireEvent.change(screen.getByLabelText("授权到期日"), {
      target: { value: "2035-01-01" },
    });
    fireEvent.change(screen.getByLabelText("肖像授权文件"), {
      target: {
        files: [
          new File(["pdf"], "authorization.pdf", { type: "application/pdf" }),
        ],
      },
    });
    fireEvent.click(screen.getByRole("button", { name: "创建并上传授权" }));

    expect(await screen.findByText("上传真人源图")).toBeInTheDocument();
    expect(api.createPersonIdentity).toHaveBeenCalledWith(
      expect.objectContaining({
        authorization_expires_at: new Date(
          2035,
          0,
          1,
          23,
          59,
          59,
          999,
        ).toISOString(),
      }),
    );
    fireEvent.click(screen.getByRole("button", { name: "关闭" }));
    fireEvent.click(
      await screen.findByRole("button", { name: "继续完成人物身份" }),
    );
    expect(await screen.findByText("上传真人源图")).toBeInTheDocument();
    expect(api.createPersonIdentity).toHaveBeenCalledTimes(1);
    fireEvent.change(screen.getByLabelText("真人源图"), {
      target: {
        files: [new File(["png"], "source.png", { type: "image/png" })],
      },
    });
    fireEvent.click(screen.getByRole("button", { name: "上传并检查源图" }));

    expect(await screen.findByText("源图质量通过")).toBeInTheDocument();
    expect(
      screen.getByText("模拟检查结果，仅用于本地流程验证"),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "确认质检结果" }));
    fireEvent.click(
      await screen.findByRole("button", { name: "完成并返回人物库" }),
    );

    fireEvent.click(await screen.findByRole("button", { name: "新建人设" }));
    fireEvent.change(screen.getByLabelText("人设名称"), {
      target: { value: persona.name },
    });
    fireEvent.change(screen.getByLabelText("职业定位"), {
      target: { value: persona.occupation },
    });
    fireEvent.click(screen.getByRole("button", { name: "保存人设" }));

    fireEvent.click(
      await screen.findByRole("button", { name: "创建 DRAFT 版本" }),
    );
    expect(await screen.findByRole("tab", { name: /V1/ })).toBeInTheDocument();
    expect(api.createCharacterVersion).toHaveBeenCalledWith(persona.id, {
      provider: "fake_character",
      model: "fake-character-v1",
      generation_params_json: {},
    });
  });

  it("supports batch generation, reject and regenerate, human approval and immutable publish", async () => {
    prepareLoadedLibrary();
    vi.mocked(api.generateCharacterAssets).mockResolvedValue([]);
    vi.mocked(api.regenerateCharacterAsset).mockResolvedValue([]);
    vi.mocked(api.reviewCharacterAsset).mockImplementation(
      async (assetId, decision, comment) => {
        assets = assets.map((asset) =>
          asset.id === assetId ? { ...asset, review_status: decision } : asset,
        );
        return {
          id: `review-${assetId}`,
          character_asset_id: assetId,
          reviewer_user_id: "admin_1",
          decision,
          issue_codes_json: decision === "REJECTED" ? ["MANUAL_REJECT"] : [],
          comment,
          created_at: "2026-08-16T00:00:00Z",
        };
      },
    );
    vi.mocked(api.publishCharacterVersion).mockResolvedValue({
      ...version,
      status: "PUBLISHED",
      published_by: "admin_1",
      published_at: "2026-08-16T01:00:00Z",
      publication_snapshot_json: {},
      publication_hash: "publication-sha",
    });
    vi.spyOn(window, "confirm").mockReturnValue(true);

    render(<CharacterLibrary userRole="admin" />);
    await screen.findByText("乡墅项目管理专家");
    await screen.findAllByLabelText("审核说明");
    fireEvent.click(screen.getByRole("button", { name: "开始生成 7 类视角" }));
    expect(api.generateCharacterAssets).toHaveBeenCalledWith(
      version.id,
      expect.objectContaining({ candidates_per_view: 1 }),
    );

    fireEvent.change((await screen.findAllByLabelText("审核说明"))[0], {
      target: { value: "脸部角度不稳定" },
    });
    fireEvent.click(screen.getAllByRole("button", { name: "驳回" })[0]);
    await waitFor(() =>
      expect(api.reviewCharacterAsset).toHaveBeenCalledWith(
        "candidate-0",
        "REJECTED",
        "脸部角度不稳定",
      ),
    );
    fireEvent.click(
      await screen.findByRole("button", { name: "重新生成正面头像" }),
    );
    expect(api.regenerateCharacterAsset).toHaveBeenCalledWith(
      "candidate-0",
      expect.any(String),
    );

    for (const button of screen.getAllByRole("button", { name: "批准" })) {
      fireEvent.click(button);
    }
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "发布角色版本" }),
      ).toBeEnabled(),
    );
    fireEvent.click(screen.getByRole("button", { name: "发布角色版本" }));

    await waitFor(() => expect(api.publishCharacterVersion).toHaveBeenCalled());
    expect(window.confirm).toHaveBeenCalledWith(
      "发布后角色版本与七类资产选择不可修改。确认发布？",
    );
    expect(await screen.findAllByText("版本已发布，内容不可修改")).toHaveLength(
      2,
    );
  });
});
