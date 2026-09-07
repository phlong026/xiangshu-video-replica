import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { PeoplePage, PersonPage } from "./PeoplePages";

const api = vi.hoisted(() => ({
  customerVisibleErrorMessage: vi.fn(
    (_cause: unknown, fallback: string) => fallback,
  ),
  completeMaterialUpload: vi.fn(),
  confirmOralVoice: vi.fn(),
  createMaterialUploadIntent: vi.fn(),
  createOralAvatarClone: vi.fn(),
  createOralConsent: vi.fn(),
  createOralVoiceClone: vi.fn(),
  refreshOralAvatar: vi.fn(),
  refreshOralVoice: vi.fn(),
  updateSimpleCharacterProfile: vi.fn(),
  uploadMaterial: vi.fn(),
}));

vi.mock("../api", () => api);

const navigate = vi.fn();
const patchDraft = vi.fn();
const notify = vi.fn();
const refresh = vi.fn();
const updateData = vi.fn();
const openPicker = vi.fn();
let currentPage = "people";
let selectedPersonId: string | undefined = "p1";
let returnTo: string | undefined;
let review = true;
let draft: Record<string, string | undefined> = {};

vi.mock("./context", () => ({
  useStudio: () => ({
    state: { page: currentPage, selectedPersonId, returnTo, draft },
    data: {
      loading: false,
      errors: [],
      people: [
        {
          id: "p1",
          name: "测试人物",
          role: "乡墅设计师",
          portrait: "/people/test-person.png",
          version: 1,
          scope: "乡墅方案",
          audience: "准备建房的家庭",
          expression: "专业",
          photoIds: ["scene"],
          avatars: [
            {
              id: "avatar-1",
              name: "测试分身",
              imageId: "avatar-image",
              ready: true,
              origin: "照片制作",
              duration: "00:30",
            },
            {
              id: "avatar-pending",
              name: "制作中分身",
              imageId: "avatar-image",
              ready: false,
              status: "PENDING",
              origin: "视频制作",
              duration: "制作中",
            },
          ],
          voices: [
            {
              id: "voice-ok",
              name: "已确认音色",
              confirmed: true,
              isDefault: true,
            },
            {
              id: "voice-pending",
              name: "待确认音色",
              confirmed: false,
              isDefault: false,
              status: "READY",
              url: "/voice-preview.mp3",
            },
            {
              id: "voice-running",
              name: "克隆中音色",
              confirmed: false,
              isDefault: false,
              status: "RUNNING",
            },
            {
              id: "voice-archiving",
              name: "待归档音色",
              confirmed: false,
              isDefault: false,
              status: "READY",
            },
          ],
        },
      ],
      assets: [
        {
          id: "avatar-image",
          name: "分身封面",
          kind: "image",
          url: "/avatar.png",
          group: "口播分身",
          source: "人物库",
          saved: true,
        },
        {
          id: "sheet",
          name: "基础五视图",
          kind: "image",
          url: "/five-views.png",
          group: "人物素材",
          personId: "p1",
          composite: true,
          source: "人物库",
          saved: true,
        },
        {
          id: "scene",
          name: "庭院讲解",
          kind: "image",
          url: "/scene.png",
          group: "人物素材",
          personId: "p1",
          source: "AI生成",
          saved: true,
        },
        {
          id: "other-scene",
          name: "其他人物场景照",
          kind: "image",
          url: "/other-scene.png",
          group: "人物素材",
          personId: "p2",
          source: "AI生成",
          saved: true,
        },
        {
          id: "voice-source",
          name: "张工录音样本",
          kind: "audio",
          url: "/voice-source.mp3",
          group: "声音素材",
          personId: "p1",
          source: "用户上传",
          saved: true,
        },
      ],
      videos: [],
      tasks: [],
      projects: [],
    },
    review,
    user: {},
    navigate,
    patchDraft,
    patchState: vi.fn(),
    updateData,
    notify,
    openPicker,
    openLive: vi.fn(),
    requestGeneration: vi.fn(),
    saveDraft: vi.fn(),
    refresh,
  }),
}));

describe("PeoplePages", () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  beforeEach(() => {
    currentPage = "people";
    selectedPersonId = "p1";
    returnTo = undefined;
    review = true;
    draft = {};
    navigate.mockClear();
    patchDraft.mockClear();
    notify.mockClear();
    refresh.mockClear();
    updateData.mockClear();
    openPicker.mockClear();
    vi.clearAllMocks();
  });

  it("renders the people library and its primary empty-safe actions", () => {
    render(<PeoplePage />);
    expect(screen.getByRole("heading", { name: "人物库" })).toBeInTheDocument();
    expect(screen.getByText("测试人物")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "新增人物" }),
    ).toBeInTheDocument();
    expect(screen.getByAltText("测试人物形象")).toHaveAttribute(
      "src",
      "/people/test-person.png",
    );
  });

  it("filters people by role", async () => {
    render(<PeoplePage />);
    await screen.getByRole("button", { name: "客户经理" }).click();
    expect(screen.queryByText("测试人物")).not.toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "人物库" })).toBeInTheDocument();
  });

  it("renders the IP tab without exposing authorization settings", () => {
    render(<PersonPage />);
    expect(
      screen.getByRole("heading", { name: "人物定位" }),
    ).toBeInTheDocument();
    expect(screen.queryByText("授权与状态")).not.toBeInTheDocument();
  });

  it("keeps the five views as one composite image and lists scene photos separately", () => {
    currentPage = "person-photos";
    render(<PersonPage />);
    expect(screen.getByAltText("五视图合成图")).toBeInTheDocument();
    expect(screen.getByText("基础五视图 · 1 张合成图")).toBeInTheDocument();
  });

  it("把场景照片和所属人物一起带入口播分身制作", () => {
    currentPage = "person-photos";
    render(<PersonPage />);

    screen.getByRole("button", { name: "制作口播分身" }).click();

    expect(patchDraft).toHaveBeenCalledWith({
      ipId: "p1",
      imageId: "scene",
    });
    expect(navigate).toHaveBeenCalledWith("person-avatars", {
      selectedPersonId: "p1",
      selectedAssetId: "scene",
    });
  });

  it.each([
    ["missing-scene", "失效"],
    ["other-scene", "跨人物"],
  ])("%s 的照片显示明确重选入口", (imageId) => {
    currentPage = "person-avatars";
    draft = { ipId: "p1", imageId };
    render(<PersonPage />);

    expect(
      screen.getByText("所选照片已失效或不属于当前人物，请重新选择。"),
    ).toBeInTheDocument();
    screen.getByRole("button", { name: "重新选择形象照片" }).click();
    expect(patchDraft).toHaveBeenCalledWith({
      ipId: "p1",
      imageId: undefined,
    });
    expect(openPicker).toHaveBeenCalledWith("avatar-photo");
  });

  it("does not present an unconfirmed voice as selectable", () => {
    currentPage = "person-voices";
    render(<PersonPage />);
    const card = screen.getByText("待确认音色").closest("article");
    expect(card).not.toBeNull();
    expect(
      within(card as HTMLElement).getByText("待试听确认，暂不可选用"),
    ).toBeInTheDocument();
    expect(
      within(card as HTMLElement).getByRole("button", {
        name: "确认使用此声音",
      }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "用于数字人口播" }),
    ).not.toBeInTheDocument();
  });

  it("试听 does not select a confirmed voice, while 使用此声音 does", () => {
    currentPage = "person-voices";
    render(<PersonPage />);
    screen.getAllByRole("button", { name: "试听" })[0].click();
    expect(patchDraft).not.toHaveBeenCalled();
    expect(navigate).not.toHaveBeenCalled();
    screen.getByRole("button", { name: "使用此声音" }).click();
    expect(patchDraft).toHaveBeenCalledWith({
      ipId: "p1",
      voiceId: "voice-ok",
    });
    expect(navigate).toHaveBeenCalledWith("oral", {
      selectedPersonId: "p1",
      returnTo: undefined,
    });
  });

  it("returns to the originating creation page and clears the return context", () => {
    currentPage = "person-voices";
    returnTo = "oral-audio";
    render(<PersonPage />);
    screen.getByRole("button", { name: /返回创作/ }).click();
    expect(navigate).toHaveBeenCalledWith("oral-audio", {
      returnTo: undefined,
    });
  });

  it("renders each ready avatar from its linked asset URL", () => {
    currentPage = "person-avatars";
    render(<PersonPage />);
    expect(screen.getByAltText("测试分身")).toHaveAttribute(
      "src",
      "/avatar.png",
    );
  });

  it("正式模式会把 IP 定位保存到后端", async () => {
    currentPage = "person-ip";
    review = false;
    api.updateSimpleCharacterProfile.mockResolvedValue({});

    render(<PersonPage />);
    screen.getByRole("button", { name: "保存 IP 定位" }).click();

    expect(api.updateSimpleCharacterProfile).toHaveBeenCalledWith("p1", {
      display_name: "测试人物",
      role: "乡墅设计师",
      service_scope: "乡墅方案",
      target_audience: "准备建房的家庭",
      expression_style: "专业",
    });
  });

  it("用当前人物的场景图提交照片数字人制作", async () => {
    currentPage = "person-avatars";
    review = false;
    draft = { ipId: "p1", imageId: "scene" };
    api.createOralAvatarClone.mockResolvedValue({
      id: "avatar-new",
      status: "RUNNING",
    });
    api.createOralConsent.mockResolvedValue({ id: "consent-avatar" });

    render(<PersonPage />);
    screen.getByRole("checkbox", { name: "确认分身克隆授权" }).click();
    screen.getByRole("button", { name: "开始制作照片分身" }).click();

    expect(api.createOralConsent).toHaveBeenCalledWith({
      identityId: "p1",
      sourceAssetId: "scene",
      purpose: "AVATAR",
    });
    await vi.waitFor(() =>
      expect(api.createOralAvatarClone).toHaveBeenCalledWith({
        identityId: "p1",
        title: "测试人物照片分身",
        sourceAssetId: "scene",
        sourceKind: "IMAGE",
        consentId: "consent-avatar",
        idempotencyKey: expect.any(String),
      }),
    );
    await vi.waitFor(() => expect(refresh).toHaveBeenCalled());
  });

  it("用已选音频提交声音克隆", async () => {
    currentPage = "person-voices";
    review = false;
    draft = { ipId: "p1", audioId: "voice-source" };
    api.createOralVoiceClone.mockResolvedValue({
      id: "voice-new",
      status: "RUNNING",
    });
    api.createOralConsent.mockResolvedValue({ id: "consent-voice" });

    render(<PersonPage />);
    screen.getByRole("checkbox", { name: "确认声音克隆授权" }).click();
    screen.getByRole("button", { name: "开始克隆声音" }).click();

    expect(api.createOralConsent).toHaveBeenCalledWith({
      identityId: "p1",
      sourceAssetId: "voice-source",
      purpose: "VOICE",
    });
    await vi.waitFor(() =>
      expect(api.createOralVoiceClone).toHaveBeenCalledWith({
        identityId: "p1",
        title: "测试人物本人音色",
        sourceAssetId: "voice-source",
        consentId: "consent-voice",
        idempotencyKey: expect.any(String),
      }),
    );
    await vi.waitFor(() => expect(refresh).toHaveBeenCalled());
  });

  it("声音克隆重试复用幂等键，标题变化后生成新键", async () => {
    currentPage = "person-voices";
    review = false;
    draft = { ipId: "p1", audioId: "voice-source" };
    api.createOralConsent
      .mockResolvedValueOnce({ id: "consent-1" })
      .mockResolvedValueOnce({ id: "consent-2" });
    api.createOralVoiceClone
      .mockRejectedValueOnce(new Error("network"))
      .mockResolvedValue({ id: "voice-new", status: "RUNNING" });

    render(<PersonPage />);
    screen.getByRole("checkbox", { name: "确认声音克隆授权" }).click();
    const submit = screen.getByRole("button", { name: "开始克隆声音" });
    submit.click();
    await screen.findByRole("alert");
    submit.click();
    await vi.waitFor(() =>
      expect(api.createOralVoiceClone).toHaveBeenCalledTimes(2),
    );

    const firstKey = api.createOralVoiceClone.mock.calls[0]?.[0].idempotencyKey;
    const retryKey = api.createOralVoiceClone.mock.calls[1]?.[0].idempotencyKey;
    expect(firstKey).toEqual(expect.any(String));
    expect(retryKey).toBe(firstKey);
    expect(api.createOralConsent).toHaveBeenCalledTimes(1);
    await vi.waitFor(() => expect(submit).toBeEnabled());

    fireEvent.change(screen.getByLabelText("声音名称"), {
      target: { value: "测试人物本人音色 V2" },
    });
    submit.click();
    await vi.waitFor(() =>
      expect(api.createOralVoiceClone).toHaveBeenCalledTimes(3),
    );
    expect(api.createOralVoiceClone.mock.calls[2]?.[0].idempotencyKey).not.toBe(
      firstKey,
    );
  });

  it("carries a scene photo into replacement without overwriting the original frame contract", () => {
    currentPage = "person-photos";
    render(<PersonPage />);
    screen.getByRole("button", { name: "用于人物置换" }).click();
    expect(patchDraft).toHaveBeenCalledWith({
      ipId: "p1",
      imageId: "scene",
    });
  });

  it("上传本地视频后提交视频分身", async () => {
    currentPage = "person-avatars";
    review = false;
    api.createMaterialUploadIntent.mockResolvedValue({
      asset_id: "video-asset",
      material_id: "asset:video-asset",
    });
    api.uploadMaterial.mockImplementation(
      (_intent: unknown, _file: File, onProgress: (value: number) => void) => {
        onProgress(45);
        return Promise.resolve();
      },
    );
    api.completeMaterialUpload.mockResolvedValue({ asset_id: "video-asset" });
    api.createOralConsent.mockResolvedValue({ id: "consent-video" });
    api.createOralAvatarClone.mockResolvedValue({
      id: "avatar-video",
      status: "RUNNING",
    });
    render(<PersonPage />);

    fireEvent.change(screen.getByLabelText("选择人物视频"), {
      target: {
        files: [new File(["video"], "avatar.mp4", { type: "video/mp4" })],
      },
    });
    await screen.findByText("已上传：avatar.mp4");
    screen.getByRole("checkbox", { name: "确认分身克隆授权" }).click();
    screen.getByRole("button", { name: "开始制作视频分身" }).click();

    expect(api.completeMaterialUpload).toHaveBeenCalledWith("video-asset");
    await vi.waitFor(() =>
      expect(api.createOralAvatarClone).toHaveBeenCalledWith(
        expect.objectContaining({
          sourceAssetId: "video-asset",
          sourceKind: "VIDEO",
          consentId: "consent-video",
        }),
      ),
    );
  });

  it("上传本地 MP3 后提交声音克隆", async () => {
    currentPage = "person-voices";
    review = false;
    api.createMaterialUploadIntent.mockResolvedValue({
      asset_id: "audio-asset",
      material_id: "asset:audio-asset",
    });
    api.uploadMaterial.mockResolvedValue(undefined);
    api.completeMaterialUpload.mockResolvedValue({ asset_id: "audio-asset" });
    api.createOralConsent.mockResolvedValue({ id: "consent-audio" });
    api.createOralVoiceClone.mockResolvedValue({
      id: "voice-new",
      status: "RUNNING",
    });
    render(<PersonPage />);

    fireEvent.change(screen.getByLabelText("选择声音样本"), {
      target: {
        files: [new File(["audio"], "voice.mp3", { type: "audio/mpeg" })],
      },
    });
    await screen.findByText("已上传：voice.mp3");
    screen.getByRole("checkbox", { name: "确认声音克隆授权" }).click();
    screen.getByRole("button", { name: "开始克隆声音" }).click();

    expect(api.completeMaterialUpload).toHaveBeenCalledWith("audio-asset");
    await vi.waitFor(() =>
      expect(api.createOralVoiceClone).toHaveBeenCalledWith(
        expect.objectContaining({
          sourceAssetId: "audio-asset",
          consentId: "consent-audio",
        }),
      ),
    );
  });

  it("未授权不能提交克隆，且上传格式错误可见", () => {
    currentPage = "person-voices";
    review = false;
    draft = { ipId: "p1", audioId: "voice-source" };
    render(<PersonPage />);

    expect(screen.getByRole("button", { name: "开始克隆声音" })).toBeDisabled();
    fireEvent.change(screen.getByLabelText("选择声音样本"), {
      target: {
        files: [new File(["bad"], "voice.wav", { type: "audio/wav" })],
      },
    });
    expect(screen.getByRole("alert")).toHaveTextContent("仅支持 MP3");
  });

  it("显示上传进度和服务端失败，并拒绝超大文件", async () => {
    currentPage = "person-avatars";
    review = false;
    let finishUpload: () => void = () => {};
    api.createMaterialUploadIntent.mockResolvedValue({
      asset_id: "video-asset",
      material_id: "asset:video-asset",
    });
    api.uploadMaterial.mockImplementation(
      (_intent: unknown, _file: File, onProgress: (value: number) => void) => {
        onProgress(45);
        return new Promise<void>((resolve) => {
          finishUpload = resolve;
        });
      },
    );
    api.completeMaterialUpload.mockRejectedValue(new Error("storage down"));
    render(<PersonPage />);

    fireEvent.change(screen.getByLabelText("选择人物视频"), {
      target: {
        files: [new File(["video"], "avatar.mp4", { type: "video/mp4" })],
      },
    });
    expect(await screen.findByText("上传中 45%")).toBeInTheDocument();
    finishUpload();
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "人物视频上传失败",
    );

    const tooLarge = new File(["video"], "large.mp4", { type: "video/mp4" });
    Object.defineProperty(tooLarge, "size", { value: 50 * 1024 * 1024 + 1 });
    fireEvent.change(screen.getByLabelText("选择人物视频"), {
      target: { files: [tooLarge] },
    });
    expect(screen.getByRole("alert")).toHaveTextContent("不能超过 50 MB");
  });

  it("审核示例模式不上传人物视频", () => {
    currentPage = "person-avatars";
    render(<PersonPage />);

    screen.getByRole("button", { name: "上传人物视频制作" }).click();

    expect(api.createMaterialUploadIntent).not.toHaveBeenCalled();
    expect(notify).toHaveBeenCalledWith("审核模式保留示例素材，不执行真实上传");
  });

  it("READY 声音可试听并通过后端确认使用", async () => {
    currentPage = "person-voices";
    review = false;
    api.confirmOralVoice.mockResolvedValue({
      id: "voice-pending",
      status: "READY",
      confirmed: true,
    });
    render(<PersonPage />);

    const card = screen.getByText("待确认音色").closest("article");
    expect(card).not.toBeNull();
    expect(
      within(card as HTMLElement).getByLabelText("待确认音色试听"),
    ).toHaveAttribute("src", "/voice-preview.mp3");
    within(card as HTMLElement)
      .getByRole("button", { name: "确认使用此声音" })
      .click();

    await vi.waitFor(() =>
      expect(api.confirmOralVoice).toHaveBeenCalledWith("voice-pending"),
    );
    expect(patchDraft).toHaveBeenCalledWith({
      ipId: "p1",
      voiceId: "voice-pending",
    });
  });

  it("READY 但试听样例未归档时不允许确认", () => {
    currentPage = "person-voices";
    review = false;
    render(<PersonPage />);

    const card = screen.getByText("待归档音色").closest("article");
    expect(card).not.toBeNull();
    expect(
      within(card as HTMLElement).getByText("试听样例归档中"),
    ).toBeInTheDocument();
    expect(
      within(card as HTMLElement).queryByRole("button", {
        name: "确认使用此声音",
      }),
    ).not.toBeInTheDocument();
  });

  it("声音样本明确限制为 5–180 秒 MP3 且不超过 50 MB", () => {
    currentPage = "person-voices";
    render(<PersonPage />);

    expect(screen.getByText(/5–180 秒（3 分钟）清晰干声/)).toBeInTheDocument();
    expect(
      screen.getByText(/MP3，时长 5–180 秒（3 分钟），上限 50 MB/),
    ).toBeInTheDocument();
  });

  it("切换声音素材后清空旧授权状态", () => {
    currentPage = "person-voices";
    review = false;
    draft = { ipId: "p1", audioId: "voice-source" };
    const view = render(<PersonPage />);
    const consent = screen.getByRole("checkbox", { name: "确认声音克隆授权" });
    consent.click();
    expect(consent).toBeChecked();

    draft = { ipId: "p1" };
    view.rerender(<PersonPage />);

    expect(consent).not.toBeChecked();
    expect(screen.getByRole("button", { name: "开始克隆声音" })).toBeDisabled();
  });

  it("正式模式自动刷新制作中分身，卸载后停止计时", async () => {
    vi.useFakeTimers();
    currentPage = "person-avatars";
    review = false;
    api.refreshOralAvatar.mockResolvedValue({ status: "RUNNING" });

    const view = render(<PersonPage />);
    await act(async () => vi.advanceTimersByTimeAsync(6_000));

    expect(api.refreshOralAvatar).toHaveBeenCalledWith("avatar-pending");
    view.unmount();
    expect(vi.getTimerCount()).toBe(0);
  });

  it("正式模式自动刷新声音克隆和样例归档，错误可见", async () => {
    vi.useFakeTimers();
    currentPage = "person-voices";
    review = false;
    api.refreshOralVoice.mockRejectedValue(new Error("provider timeout"));

    const view = render(<PersonPage />);
    await act(async () => vi.advanceTimersByTimeAsync(6_000));

    expect(api.refreshOralVoice).toHaveBeenCalledWith("voice-running");
    expect(api.refreshOralVoice).toHaveBeenCalledWith("voice-archiving");
    expect(screen.getByRole("alert")).toHaveTextContent("声音状态自动刷新失败");
    view.unmount();
    expect(vi.getTimerCount()).toBe(0);
  });

  it("审核模式不发起自动刷新", async () => {
    vi.useFakeTimers();
    currentPage = "person-voices";
    render(<PersonPage />);

    await act(async () => vi.advanceTimersByTimeAsync(12_000));

    expect(api.refreshOralVoice).not.toHaveBeenCalled();
    expect(vi.getTimerCount()).toBe(0);
  });

  it("does not silently switch to another person when the selected id is stale", () => {
    currentPage = "person-ip";
    selectedPersonId = "missing-person";
    render(<PersonPage />);
    expect(screen.getByText("未选择人物")).toBeInTheDocument();
    expect(screen.queryByText("测试人物")).not.toBeInTheDocument();
  });
});
