import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { MaterialItem } from "../api";
import type { StudioContextValue, StudioState } from "./types";

const {
  useStudio,
  fetchViralVideoMedia,
  fetchViralVideoStatistics,
  listViralVideos,
  listMaterials,
  createMaterialUploadIntent,
  uploadMaterial,
  completeMaterialUpload,
  updateMaterial,
  hideMaterial,
  downloadMaterialAsset,
  getAssetDownloadUrl,
  createGenerationTaskPreviewUrl,
} = vi.hoisted(() => ({
  useStudio: vi.fn(),
  fetchViralVideoMedia: vi.fn(),
  fetchViralVideoStatistics: vi.fn(),
  listViralVideos: vi.fn(),
  listMaterials: vi.fn(),
  createMaterialUploadIntent: vi.fn(),
  uploadMaterial: vi.fn(),
  completeMaterialUpload: vi.fn(),
  updateMaterial: vi.fn(),
  hideMaterial: vi.fn(),
  downloadMaterialAsset: vi.fn(),
  getAssetDownloadUrl: vi.fn(),
  createGenerationTaskPreviewUrl: vi.fn(),
}));
vi.mock("./context", () => ({ useStudio }));
vi.mock("../api", () => ({
  fetchViralVideoMedia,
  fetchViralVideoStatistics,
  listViralVideos,
  listMaterials,
  createMaterialUploadIntent,
  uploadMaterial,
  completeMaterialUpload,
  updateMaterial,
  hideMaterial,
  downloadMaterialAsset,
  getAssetDownloadUrl,
  createGenerationTaskPreviewUrl,
}));

class IntersectionObserverStub {
  observe() {}
  disconnect() {}
  unobserve() {}
  takeRecords() {
    return [];
  }
}
vi.stubGlobal("IntersectionObserver", IntersectionObserverStub);

import {
  MaterialsPage,
  PublishPage,
  ViralDetailPage,
  ViralPage,
} from "./ContentPages";

function studio(
  overrides: Partial<StudioContextValue> = {},
): StudioContextValue {
  return {
    state: {
      page: "viral",
      draft: {
        id: "draft-1",
        selectedShotId: "shot-1",
        script: {
          id: "script-1",
          title: "",
          original: "",
          text: "",
          version: 1,
          confirmed: false,
        },
        prompt: "",
        referenceIds: [],
        resolution: "768P",
        ratio: "16:9",
        duration: 4,
        count: 1,
        frameConfirmed: false,
        style: "standard",
        subtitles: false,
        quoteRevision: 1,
      },
      savedScripts: [],
      favorites: [],
    },
    data: {
      people: [],
      assets: [
        {
          id: "audio-1",
          name: "张工讲预算.wav",
          kind: "audio",
          group: "口播音频",
          source: "素材库",
          saved: true,
          personId: "person-1",
        },
      ],
      videos: [
        {
          id: "dy-1",
          title: "农村建房预算，别只盯着主体",
          author: "乡墅建房笔记",
          platform: "抖音",
          category: "建房预算",
          poster: "/studio/demo.jpg",
          duration: "01:28",
          likes: 18000,
          collections: 842,
          shares: 326,
          description: "主体之外，门窗、水电、防水和庭院也要列进预算清单。",
          platformKey: "douyin",
          nativeId: "native-dy-1",
          verified: true,
          tags: ["农村自建房", "建房预算"],
          hasPlayableAudio: true,
        },
        {
          id: "wx-1",
          title: "新中式庭院的三个细节",
          author: "庭院设计老周",
          platform: "视频号",
          category: "庭院案例",
          poster: "/studio/demo-2.jpg",
          duration: "01:12",
          likes: 9800,
          collections: 430,
          shares: 92,
          description: "从动线、植物和夜景灯光说庭院。",
          platformKey: "wechat_channels",
          nativeId: "native-wx-1",
          publishedDisplay: "3天前",
          likeDisplay: "1.2万",
          tags: ["庭院案例", "别墅设计"],
          hasPlayableAudio: false,
        },
      ],
      tasks: [],
      projects: [],
      errors: [],
      materials: [],
      loading: false,
      stats: null,
      analytics7: null,
      analytics30: null,
    },
    review: true,
    user: {} as StudioContextValue["user"],
    navigate: vi.fn(),
    patchDraft: vi.fn(),
    patchState: vi.fn(),
    updateData: vi.fn(),
    notify: vi.fn(),
    openPicker: vi.fn(),
    openLive: vi.fn(),
    requestGeneration: vi.fn(),
    saveDraft: vi.fn(),
    confirmFinalDraft: vi.fn(),
    extractScriptFromUpload: vi.fn(),
    refresh: vi.fn(),
    ...overrides,
  };
}

function material(
  id: string,
  overrides: Partial<MaterialItem> = {},
): MaterialItem {
  return {
    id: `asset:${id}`,
    owner_user_id: "employee_1",
    asset_id: id,
    generation_task_id: null,
    project_id: null,
    person_id: null,
    title: `${id}.png`,
    group: "我的上传",
    media_type: "image",
    source: "upload",
    status: "ready",
    delivery: "stored",
    content_type: "image/png",
    size_bytes: 1024,
    duration_seconds: null,
    created_at: "2026-09-08 10:00:00",
    hidden: false,
    saved: true,
    allowed_uses: [],
    allowed_actions: ["preview", "download", "rename", "hide"],
    ...overrides,
  };
}

describe("V1.4 内容与运营页面", () => {
  beforeEach(() => {
    useStudio.mockReset();
    fetchViralVideoMedia.mockReset();
    fetchViralVideoStatistics.mockReset();
    listViralVideos.mockReset();
    listViralVideos.mockResolvedValue({
      platform: "douyin",
      sort: "hot",
      categories: [],
      items: [],
      fetchedAt: null,
    });
    listMaterials.mockReset();
    createMaterialUploadIntent.mockReset();
    uploadMaterial.mockReset();
    completeMaterialUpload.mockReset();
    updateMaterial.mockReset();
    hideMaterial.mockReset();
    downloadMaterialAsset.mockReset();
    getAssetDownloadUrl.mockReset();
    createGenerationTaskPreviewUrl.mockReset();
    getAssetDownloadUrl.mockResolvedValue({
      url: "https://storage.test/material",
    });
    createGenerationTaskPreviewUrl.mockResolvedValue(
      "https://provider.test/direct-result.mp4",
    );
  });

  it("爆款视频按平台过滤、收藏并把来源带入复刻", () => {
    const value = studio();
    useStudio.mockReturnValue(value);
    render(<ViralPage />);
    fireEvent.click(screen.getByRole("tab", { name: "视频号 30" }));
    expect(screen.getByText("新中式庭院的三个细节")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("tab", { name: "抖音 20" }));
    fireEvent.click(
      screen.getByRole("button", { name: "收藏 农村建房预算，别只盯着主体" }),
    );
    expect(value.patchState).toHaveBeenCalledWith({ favorites: ["dy-1"] });
    fireEvent.click(
      screen.getByRole("button", { name: "复刻 农村建房预算，别只盯着主体" }),
    );
    expect(value.patchDraft).toHaveBeenCalledWith({ sourceId: "dy-1" });
    expect(value.navigate).toHaveBeenCalledWith("replica", {
      selectedVideoId: "dy-1",
      returnTo: "viral",
    });
  });

  it("爆款详情展示平台字段，文案先备料再跳转", async () => {
    fetchViralVideoMedia.mockResolvedValue({
      kind: "audio",
      url: "https://storage.test/viral/douyin/native-dy-1.mp3",
      contentType: "audio/mpeg",
      cacheHit: false,
    });
    const value = studio({
      state: {
        ...studio().state,
        page: "viral-detail",
        selectedVideoId: "dy-1",
      },
    });
    useStudio.mockReturnValue(value);
    const view = render(<ViralDetailPage />);
    expect(
      screen.getByRole("heading", { name: "爆款视频 / 视频详情" }),
    ).toBeInTheDocument();
    expect(screen.getByText("乡墅建房笔记")).toBeInTheDocument();
    expect(screen.getByText("抖音 · 建房预算 · 有原声")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "播放 农村建房预算，别只盯着主体" }),
    ).toBeInTheDocument();
    expect(view.container.querySelector("main")).toBeNull();
    expect(screen.queryByText("内容浏览示例审核")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "提取文案" }));
    expect(fetchViralVideoMedia).toHaveBeenCalledWith("douyin", "native-dy-1");
    await waitFor(() => {
      expect(value.patchDraft).toHaveBeenCalledWith({ sourceId: "dy-1" });
    });
    expect(value.navigate).toHaveBeenCalledWith("copy", {
      selectedVideoId: "dy-1",
      returnTo: "viral-detail",
    });
    expect(screen.getByText("原声音频已就绪")).toBeInTheDocument();
  });

  it("爆款备料失败时保持详情页并展示错误", async () => {
    fetchViralVideoMedia.mockRejectedValue(new Error("素材暂时无法获取"));
    const value = studio({
      state: {
        ...studio().state,
        page: "viral-detail",
        selectedVideoId: "dy-1",
      },
    });
    useStudio.mockReturnValue(value);
    const view = render(<ViralDetailPage />);
    expect(view.container.querySelector(".content-detail-grid")).toHaveClass(
      "content-detail-grid-viral",
    );
    expect(screen.getByText("▶ 播放")).toHaveClass("content-player-viral-play");
    fireEvent.click(screen.getByRole("button", { name: "视频复刻" }));
    await waitFor(() => {
      expect(screen.getByText(/素材准备失败/)).toBeInTheDocument();
    });
    expect(value.navigate).not.toHaveBeenCalled();
  });

  it("过期的视频选择显示空态，不回退到任意视频", () => {
    const base = studio();
    useStudio.mockReturnValue(
      studio({
        review: false,
        state: {
          ...base.state,
          page: "viral-detail",
          selectedVideoId: "removed-video",
        },
      }),
    );
    render(<ViralDetailPage />);

    expect(screen.getByText("暂未选择参考视频")).toBeInTheDocument();
    expect(screen.queryByText("农村建房预算，别只盯着主体")).toBeNull();
  });

  it("爆款列表不向用户展示采集参数后台提示", () => {
    const value = studio();
    useStudio.mockReturnValue(value);
    render(<ViralPage />);

    expect(screen.queryByText("采集参数仅在管理后台配置。")).toBeNull();
    expect(screen.queryByText("内容浏览示例审核")).toBeNull();
  });

  it("爆款封面失败状态随封面地址更新而恢复", () => {
    const base = studio();
    const value = studio({
      data: { ...base.data, videos: [base.data.videos[0]] },
    });
    useStudio.mockReturnValue(value);
    const view = render(<ViralPage />);

    const brokenPoster = view.container.querySelector(".viral-card-cover-img");
    expect(brokenPoster).not.toBeNull();
    if (!brokenPoster) return;
    fireEvent.error(brokenPoster);
    expect(view.container.querySelector(".viral-card-cover-img")).toBeNull();

    const refreshed = {
      ...value,
      data: {
        ...value.data,
        videos: [{ ...value.data.videos[0], poster: "/studio/fresh.jpg" }],
      },
    };
    useStudio.mockReturnValue(refreshed);
    view.rerender(<ViralPage />);

    expect(
      view.container.querySelector(".viral-card-cover-img"),
    ).toHaveAttribute("src", "/studio/fresh.jpg");
  });

  it("真实爆款首次点击即通过服务端媒体地址在原卡片播放", async () => {
    let resolveMedia: ((value: unknown) => void) | undefined;
    fetchViralVideoMedia.mockReturnValue(
      new Promise((resolve) => {
        resolveMedia = resolve;
      }),
    );
    const base = studio();
    const value = studio({
      data: {
        ...base.data,
        videos: [
          {
            ...base.data.videos[0],
            playUrl: "https://source.test/native-dy-1.mp4",
          },
        ],
      },
    });
    useStudio.mockReturnValue(value);
    const view = render(<ViralPage />);

    fireEvent.click(
      screen.getByRole("button", { name: "播放 农村建房预算，别只盯着主体" }),
    );
    expect(fetchViralVideoMedia).toHaveBeenCalledWith(
      "douyin",
      "native-dy-1",
      "video",
    );
    expect(screen.getByText("准备中…")).toBeInTheDocument();
    fireEvent.click(
      screen.getByRole("button", { name: "播放 农村建房预算，别只盯着主体" }),
    );
    expect(fetchViralVideoMedia).toHaveBeenCalledOnce();

    resolveMedia?.({
      kind: "video",
      url: "https://storage.test/viral/douyin/native-dy-1.mp4",
      contentType: "video/mp4",
      cacheHit: false,
      video: null,
    });
    await waitFor(() => {
      expect(view.container.querySelector("video")).toHaveAttribute(
        "src",
        "https://storage.test/viral/douyin/native-dy-1.mp4",
      );
    });
    expect(view.container.querySelector(".viral-card-overlay")).toBeNull();
  });

  it("新卡片开始播放时暂停上一条并保留原播放器", async () => {
    const pause = vi
      .spyOn(HTMLMediaElement.prototype, "pause")
      .mockImplementation(() => {});
    fetchViralVideoMedia.mockImplementation((_platform, videoId) =>
      Promise.resolve({
        kind: "video",
        url: `https://storage.test/${videoId}.mp4`,
        contentType: "video/mp4",
        cacheHit: false,
        video: null,
      }),
    );
    const base = studio();
    const second = {
      ...base.data.videos[0],
      id: "dy-2",
      nativeId: "native-dy-2",
      title: "第二条乡墅参考",
    };
    useStudio.mockReturnValue(
      studio({ data: { ...base.data, videos: [base.data.videos[0], second] } }),
    );
    render(<ViralPage />);

    fireEvent.click(
      screen.getByRole("button", { name: "播放 农村建房预算，别只盯着主体" }),
    );
    const firstPlayer = await screen.findByTitle("农村建房预算，别只盯着主体");
    fireEvent.click(
      screen.getByRole("button", { name: "播放 第二条乡墅参考" }),
    );
    await screen.findByTitle("第二条乡墅参考");

    expect(pause).toHaveBeenCalledWith();
    expect(firstPlayer).toBeInTheDocument();
    pause.mockRestore();
  });

  it("快速切换卡片时旧请求晚返回不会抢占播放", async () => {
    const resolvers = new Map<string, (value: unknown) => void>();
    fetchViralVideoMedia.mockImplementation(
      (_platform, videoId) =>
        new Promise((resolve) => {
          resolvers.set(videoId, resolve);
        }),
    );
    const base = studio();
    const second = {
      ...base.data.videos[0],
      id: "dy-2",
      nativeId: "native-dy-2",
      title: "第二条乡墅参考",
    };
    useStudio.mockReturnValue(
      studio({ data: { ...base.data, videos: [base.data.videos[0], second] } }),
    );
    render(<ViralPage />);

    fireEvent.click(
      screen.getByRole("button", { name: "播放 农村建房预算，别只盯着主体" }),
    );
    fireEvent.click(
      screen.getByRole("button", { name: "播放 第二条乡墅参考" }),
    );
    resolvers.get("native-dy-2")?.({
      kind: "video",
      url: "https://storage.test/native-dy-2.mp4",
      contentType: "video/mp4",
      cacheHit: false,
      video: null,
    });
    expect(await screen.findByTitle("第二条乡墅参考")).toHaveAttribute(
      "src",
      "https://storage.test/native-dy-2.mp4",
    );

    resolvers.get("native-dy-1")?.({
      kind: "video",
      url: "https://storage.test/native-dy-1.mp4",
      contentType: "video/mp4",
      cacheHit: false,
      video: null,
    });
    await waitFor(() => {
      expect(screen.queryByTitle("农村建房预算，别只盯着主体")).toBeNull();
    });
  });

  it("媒体响应携带补采统计时只更新对应爆款视频", async () => {
    const responseVideo = {
      platform: "douyin" as const,
      videoId: "native-dy-1",
      category: "建房预算",
      title: "农村建房预算，别只盯着主体",
      author: "乡墅建房笔记",
      authorAvatar: null,
      verified: true,
      coverUrl: "/studio/demo.jpg",
      durationMs: 88_000,
      likes: 19_001,
      comments: 321,
      shares: 654,
      collects: 987,
      publishedAt: null,
      publishedDisplay: null,
      likeDisplay: "1.9万",
      tags: [],
      hasPlayableAudio: true,
      playUrl: null,
    };
    fetchViralVideoMedia.mockResolvedValue({
      kind: "audio",
      url: "https://storage.test/viral/douyin/native-dy-1.mp3",
      contentType: "audio/mpeg",
      cacheHit: false,
      video: responseVideo,
    });
    const base = studio();
    const value = studio({
      state: {
        ...base.state,
        page: "viral-detail",
        selectedVideoId: "dy-1",
      },
    });
    useStudio.mockReturnValue(value);
    render(<ViralDetailPage />);

    fireEvent.click(screen.getByRole("button", { name: "提取文案" }));
    await waitFor(() => expect(value.updateData).toHaveBeenCalledOnce());
    const update = vi.mocked(value.updateData).mock.calls[0][0];
    const updated = update(value.data);

    expect(updated.videos[0]).toMatchObject({
      likes: 19_001,
      comments: 321,
      shares: 654,
      collections: 987,
      likeDisplay: "1.9万",
    });
    expect(updated.videos[1]).toBe(value.data.videos[1]);
  });

  it("未知爆款统计显示短横线", () => {
    const base = studio();
    useStudio.mockReturnValue(
      studio({
        data: {
          ...base.data,
          videos: [
            {
              ...base.data.videos[0],
              comments: null,
              shares: null,
              collections: null,
            },
          ],
        },
      }),
    );
    render(<ViralPage />);

    expect(screen.getByTitle("评论")).toHaveTextContent("—");
    expect(screen.getByTitle("转发")).toHaveTextContent("—");
    expect(screen.getByTitle("收藏")).toHaveTextContent("—");
  });

  it("非审核列表为当前可见的视频号完整统计查询缓存且每页仅一次", async () => {
    listViralVideos.mockReturnValue(new Promise(() => {}));
    const base = studio();
    const sourceVideo = {
      ...base.data.videos[1],
      comments: 10,
      shares: 20,
      collections: 30,
    };
    fetchViralVideoStatistics.mockResolvedValue({
      items: [
        {
          platform: "wechat_channels",
          videoId: "native-wx-1",
          category: "庭院案例",
          title: sourceVideo.title,
          author: sourceVideo.author,
          authorAvatar: null,
          verified: false,
          coverUrl: sourceVideo.poster,
          durationMs: 72_000,
          likes: 9_800,
          comments: 44,
          shares: 55,
          collects: 66,
          publishedAt: null,
          publishedDisplay: "3天前",
          likeDisplay: "1.2万",
          tags: [],
          hasPlayableAudio: false,
          playUrl: null,
        },
      ],
    });
    const value = studio({
      review: false,
      data: { ...base.data, videos: [sourceVideo] },
    });
    useStudio.mockReturnValue(value);
    const view = render(<ViralPage />);
    fireEvent.click(screen.getByRole("tab", { name: "视频号 1" }));

    await waitFor(() =>
      expect(fetchViralVideoStatistics).toHaveBeenCalledWith(["native-wx-1"]),
    );
    view.rerender(<ViralPage />);
    expect(fetchViralVideoStatistics).toHaveBeenCalledOnce();
    const update = vi.mocked(value.updateData).mock.calls[0][0];
    expect(update(value.data).videos[0]).toMatchObject({
      comments: 44,
      shares: 55,
      collections: 66,
    });
  });

  it("视频号详情直接打开时也查询当前完整统计缓存", async () => {
    const base = studio();
    fetchViralVideoStatistics.mockResolvedValue({ items: [] });
    const value = studio({
      review: false,
      state: {
        ...base.state,
        page: "viral-detail",
        selectedVideoId: "wx-1",
      },
    });
    useStudio.mockReturnValue(value);
    render(<ViralDetailPage />);

    await waitFor(() =>
      expect(fetchViralVideoStatistics).toHaveBeenCalledWith(["native-wx-1"]),
    );
  });

  it("爆款列表默认渲染前十二条并保留平台筛选", () => {
    const base = studio();
    const videos = Array.from({ length: 7 }, (_, index) => ({
      ...base.data.videos[0],
      id: `dy-${index + 1}`,
      title: `乡墅参考 ${index + 1}`,
    }));
    useStudio.mockReturnValue(studio({ data: { ...base.data, videos } }));
    render(<ViralPage />);
    expect(screen.getByText("乡墅参考 7")).toBeInTheDocument();
    expect(screen.getByText("乡墅参考 1")).toBeInTheDocument();
    expect(screen.queryByText("上拉加载更多…")).toBeNull();
  });

  it("爆款超出十二条时先渲染十二条并提供滚动加载占位", () => {
    const base = studio();
    const videos = Array.from({ length: 15 }, (_, index) => ({
      ...base.data.videos[0],
      id: `dy-${index + 1}`,
      title: `乡墅参考 ${index + 1}`,
    }));
    useStudio.mockReturnValue(studio({ data: { ...base.data, videos } }));
    render(<ViralPage />);
    expect(screen.getByText("乡墅参考 12")).toBeInTheDocument();
    expect(screen.queryByText("乡墅参考 13")).toBeNull();
    expect(screen.getByText("上拉加载更多…")).toBeInTheDocument();
  });

  it("爆款视频按热度或发布时间排序", () => {
    const base = studio();
    const videos = Array.from({ length: 7 }, (_, index) => ({
      ...base.data.videos[0],
      id: `dy-${index + 1}`,
      title: `排序参考 ${index + 1}`,
      likes: (index + 1) * 100,
      publishedAt: 1788600000 - index,
    }));
    useStudio.mockReturnValue(studio({ data: { ...base.data, videos } }));
    const view = render(<ViralPage />);

    const titles = () =>
      [...view.container.querySelectorAll(".viral-card-body h3")].map(
        (node) => node.textContent,
      );
    expect(titles()[0]).toBe("排序参考 7");
    fireEvent.change(screen.getByLabelText("排序方式"), {
      target: { value: "最新" },
    });
    expect(titles()[0]).toBe("排序参考 1");
  });

  it("切换最新排序时按当前平台重新读取列表", async () => {
    const value = studio({ review: false });
    useStudio.mockReturnValue(value);
    render(<ViralPage />);

    fireEvent.change(screen.getByLabelText("排序方式"), {
      target: { value: "最新" },
    });

    await waitFor(() =>
      expect(listViralVideos).toHaveBeenCalledWith("douyin", "latest"),
    );
  });

  it("非审核工作区不把示例三十条当作真实采集数据", () => {
    useStudio.mockReturnValue(
      studio({ review: false, data: { ...studio().data, videos: [] } }),
    );
    render(<ViralPage />);
    expect(screen.getByRole("tab", { name: "抖音 0" })).toBeInTheDocument();
    expect(screen.getByText("暂无爆款视频")).toBeInTheDocument();
    expect(
      screen.getByText("数据源尚未配置或最近 7 天暂无内容，配置后自动展示。"),
    ).toBeInTheDocument();
    expect(
      screen.queryByText("采集参数仅在管理后台配置。"),
    ).not.toBeInTheDocument();
  });

  it("素材页用统一选择器，音频仅交给音频口播并带 IP", () => {
    const value = studio();
    useStudio.mockReturnValue(value);
    const view = render(<MaterialsPage />);
    fireEvent.click(
      screen.getByRole("button", { name: "选择素材 张工讲预算.wav" }),
    );
    expect(value.patchState).toHaveBeenCalledWith({
      selectedAssetId: "audio-1",
    });
    const selectedValue = studio({
      state: { ...value.state, selectedAssetId: "audio-1" },
    });
    useStudio.mockReturnValue(selectedValue);
    view.rerender(<MaterialsPage />);
    fireEvent.click(screen.getByRole("button", { name: "用于音频口播" }));
    expect(selectedValue.patchDraft).toHaveBeenCalledWith({
      audioId: "audio-1",
      ipId: "person-1",
      voiceId: undefined,
    });
    expect(selectedValue.navigate).toHaveBeenCalledWith("oral-audio", {
      returnTo: "materials",
    });
  });

  it("素材库六条分页，初始定位已选素材且筛选后保留右侧选择", () => {
    const base = studio();
    const assets = Array.from({ length: 7 }, (_, index) => ({
      id: `image-${index + 1}`,
      name: `乡墅素材 ${index + 1}`,
      kind: "image" as const,
      group: "人物素材",
      source: "人物库",
      saved: true,
      ...(index === 6 ? { composite: true } : {}),
    }));
    useStudio.mockReturnValue(
      studio({
        data: { ...base.data, assets },
        state: { ...base.state, selectedAssetId: "image-7" },
      }),
    );
    render(<MaterialsPage />);

    expect(
      screen.getByRole("button", { name: "选择素材 乡墅素材 7" }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "选择素材 乡墅素材 1" }),
    ).toBeNull();
    fireEvent.click(screen.getByRole("tab", { name: "图片" }));
    expect(
      screen.getByRole("button", { name: "选择素材 乡墅素材 1" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { level: 2, name: "乡墅素材 7" }),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "下一页" }));
    expect(
      screen.getByRole("button", { name: "选择素材 乡墅素材 7" }),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "上一页" }));
    expect(
      screen.getByRole("button", { name: "选择素材 乡墅素材 1" }),
    ).toBeInTheDocument();
  });

  it("非审核素材页读取服务端素材并把真实资产 ID 带入口播草稿", async () => {
    listMaterials.mockResolvedValue({
      items: [
        {
          id: "asset:audio-cloud-1",
          owner_user_id: "employee_1",
          asset_id: "audio-cloud-1",
          generation_task_id: null,
          project_id: null,
          person_id: "person-1",
          title: "云端讲解.mp3",
          group: "口播素材",
          media_type: "audio",
          source: "upload",
          status: "ready",
          delivery: "stored",
          content_type: "audio/mpeg",
          size_bytes: 1024,
          duration_seconds: 18,
          created_at: "2026-09-06 10:00:00",
          hidden: false,
          saved: true,
          allowed_uses: ["oral_audio", "reference"],
          allowed_actions: ["preview", "download", "rename", "hide"],
        },
      ],
      page: 1,
      page_size: 6,
      total: 1,
    });
    const value = studio({
      review: false,
      data: { ...studio().data, assets: [] },
    });
    useStudio.mockReturnValue(value);
    render(<MaterialsPage />);

    fireEvent.click(
      await screen.findByRole("button", { name: "选择素材 云端讲解.mp3" }),
    );
    fireEvent.click(screen.getByRole("button", { name: "用于音频口播" }));

    expect(value.patchDraft).toHaveBeenCalledWith({
      audioId: "audio-cloud-1",
      ipId: "person-1",
      voiceId: undefined,
    });
    expect(value.updateData).toHaveBeenCalled();
    expect(value.navigate).toHaveBeenCalledWith("oral-audio", {
      returnTo: "materials",
    });
  });

  it("素材页完成上传、重命名和移除的服务端闭环", async () => {
    listMaterials.mockResolvedValue({
      items: [],
      page: 1,
      page_size: 6,
      total: 0,
    });
    createMaterialUploadIntent.mockResolvedValue({
      material_id: "asset:image-cloud-1",
      asset_id: "image-cloud-1",
      storage_key: "materials/employee_1/image-cloud-1/original.png",
      method: "PUT",
      url: "https://storage.test/upload",
      headers: { "Content-Type": "image/png" },
      expires_at: "2026-09-06T10:10:00Z",
    });
    uploadMaterial.mockImplementation(
      (_intent, _file, onProgress: (progress: number) => void) => {
        onProgress(100);
        return Promise.resolve();
      },
    );
    const uploaded = {
      id: "asset:image-cloud-1",
      owner_user_id: "employee_1",
      asset_id: "image-cloud-1",
      generation_task_id: null,
      project_id: null,
      person_id: null,
      title: "庭院.png",
      group: "我的上传",
      media_type: "image",
      source: "upload",
      status: "ready",
      delivery: "stored",
      content_type: "image/png",
      size_bytes: 8,
      duration_seconds: null,
      created_at: "2026-09-06 10:00:00",
      hidden: false,
      saved: true,
      allowed_uses: [
        "original_frame",
        "first_frame",
        "tail_frame",
        "reference",
      ],
      allowed_actions: ["preview", "download", "rename", "hide"],
    } as const;
    completeMaterialUpload.mockResolvedValue(uploaded);
    updateMaterial.mockResolvedValue({ ...uploaded, title: "新庭院首帧" });
    hideMaterial.mockResolvedValue(undefined);
    const value = studio({
      review: false,
      data: { ...studio().data, assets: [] },
    });
    useStudio.mockReturnValue(value);
    render(<MaterialsPage />);

    const file = new File([new Uint8Array(8)], "庭院.png", {
      type: "image/png",
    });
    fireEvent.change(screen.getByLabelText("选择上传素材"), {
      target: { files: [file] },
    });
    await waitFor(() =>
      expect(completeMaterialUpload).toHaveBeenCalledWith("image-cloud-1"),
    );
    expect(
      screen.getByRole("heading", { name: "庭院.png" }),
    ).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "用作尾帧" }));
    expect(value.patchDraft).toHaveBeenCalledWith({
      tailFrameId: "image-cloud-1",
    });
    expect(value.navigate).toHaveBeenCalledWith("video", {
      returnTo: "materials",
    });

    fireEvent.change(screen.getByLabelText("素材名称"), {
      target: { value: "新庭院首帧" },
    });
    fireEvent.click(screen.getByRole("button", { name: "保存名称" }));
    await waitFor(() =>
      expect(updateMaterial).toHaveBeenCalledWith("asset:image-cloud-1", {
        title: "新庭院首帧",
      }),
    );
    fireEvent.click(screen.getByRole("button", { name: "从素材库移除" }));
    await waitFor(() =>
      expect(hideMaterial).toHaveBeenCalledWith("asset:image-cloud-1"),
    );
  });

  it("非审核素材页把搜索和来源筛选交给服务端", async () => {
    listMaterials.mockResolvedValue({
      items: [],
      page: 1,
      page_size: 6,
      total: 0,
    });
    useStudio.mockReturnValue(
      studio({ review: false, data: { ...studio().data, assets: [] } }),
    );
    render(<MaterialsPage />);
    await waitFor(() => expect(listMaterials).toHaveBeenCalledTimes(1));

    fireEvent.change(screen.getByLabelText("搜索素材"), {
      target: { value: "庭院" },
    });
    fireEvent.change(screen.getByLabelText("素材来源"), {
      target: { value: "upload" },
    });
    fireEvent.submit(screen.getByRole("form", { name: "素材筛选" }));

    await waitFor(() =>
      expect(listMaterials).toHaveBeenLastCalledWith({
        mediaType: undefined,
        source: "upload",
        query: "庭院",
        page: 1,
        pageSize: 6,
      }),
    );
  });

  it("只并行签名当前素材页并在翻页后打开真实图视频音频详情", async () => {
    const firstPage = [
      material("image-1"),
      material("video-2", {
        title: "video-2.mp4",
        media_type: "video",
        content_type: "video/mp4",
      }),
      material("audio-3", {
        title: "audio-3.mp3",
        media_type: "audio",
        content_type: "audio/mpeg",
      }),
      ...Array.from({ length: 3 }, (_, index) =>
        material(`image-${index + 4}`),
      ),
    ];
    listMaterials.mockImplementation(({ page }: { page: number }) =>
      Promise.resolve({
        items: page === 1 ? firstPage : [material("image-7")],
        page,
        page_size: 6,
        total: 7,
      }),
    );
    const pendingResolvers: Array<() => void> = [];
    getAssetDownloadUrl.mockImplementation(
      (id: string) =>
        new Promise((resolve) => {
          pendingResolvers.push(() =>
            resolve({ url: `https://storage.test/${id}` }),
          );
        }),
    );
    useStudio.mockReturnValue(studio({ review: false }));
    render(<MaterialsPage />);

    await waitFor(() => expect(getAssetDownloadUrl).toHaveBeenCalledTimes(6));
    expect(pendingResolvers).toHaveLength(6);
    pendingResolvers.splice(0).forEach((resolve) => {
      resolve();
    });
    expect(
      await screen.findByRole("img", { name: "image-1.png" }),
    ).toHaveAttribute("src", "https://storage.test/image-1");
    expect(screen.getByLabelText("video-2.mp4")).toHaveAttribute(
      "src",
      "https://storage.test/video-2",
    );
    expect(screen.getByLabelText("audio-3.mp3")).toHaveAttribute(
      "src",
      "https://storage.test/audio-3",
    );

    fireEvent.click(screen.getByRole("button", { name: "下一页" }));
    await waitFor(() => expect(getAssetDownloadUrl).toHaveBeenCalledTimes(7));
    pendingResolvers.splice(0).forEach((resolve) => {
      resolve();
    });
    fireEvent.click(
      await screen.findByRole("button", { name: "选择素材 image-7.png" }),
    );
    expect(
      screen.getByRole("heading", { level: 2, name: "image-7.png" }),
    ).toBeInTheDocument();
    expect(screen.getAllByRole("img", { name: "image-7.png" })).toHaveLength(2);
    expect(getAssetDownloadUrl).toHaveBeenCalledTimes(7);
  });

  it("预览签名失败显示重试且旧页迟到响应不能覆盖当前页", async () => {
    let resolveOld: ((value: { url: string }) => void) | undefined;
    listMaterials.mockImplementation(({ page }: { page: number }) =>
      Promise.resolve({
        items:
          page === 1
            ? [
                material("old-1"),
                ...Array.from({ length: 5 }, (_, index) =>
                  material(`old-${index + 2}`),
                ),
              ]
            : [material("current-7")],
        page,
        page_size: 6,
        total: 7,
      }),
    );
    getAssetDownloadUrl.mockImplementation((id: string) => {
      if (id === "old-1")
        return new Promise((resolve) => {
          resolveOld = resolve;
        });
      if (
        id === "current-7" &&
        getAssetDownloadUrl.mock.calls.filter(([assetId]) => assetId === id)
          .length === 1
      )
        return Promise.reject(new Error("签名服务超时"));
      return Promise.resolve({ url: `https://storage.test/${id}` });
    });
    useStudio.mockReturnValue(studio({ review: false }));
    render(<MaterialsPage />);

    await screen.findByRole("button", { name: "选择素材 old-1.png" });
    fireEvent.click(screen.getByRole("button", { name: "下一页" }));
    expect(
      await screen.findByText("预览加载失败，点击重试"),
    ).toBeInTheDocument();
    resolveOld?.({ url: "https://storage.test/stale-old-1" });
    fireEvent.click(
      screen.getByRole("button", { name: "选择素材 current-7.png" }),
    );
    expect(
      await screen.findAllByRole("img", { name: "current-7.png" }),
    ).toHaveLength(2);
    for (const image of screen.getAllByRole("img", { name: "current-7.png" }))
      expect(image).toHaveAttribute("src", "https://storage.test/current-7");
    expect(screen.queryByText("stale-old-1")).toBeNull();
    expect(getAssetDownloadUrl).toHaveBeenCalledTimes(8);
  });

  it("签名地址过期后显示失败并由用户重试取得新地址", async () => {
    listMaterials.mockResolvedValue({
      items: [material("expiring-image")],
      page: 1,
      page_size: 6,
      total: 1,
    });
    getAssetDownloadUrl
      .mockResolvedValueOnce({ url: "https://storage.test/expired" })
      .mockResolvedValueOnce({ url: "https://storage.test/refreshed" });
    useStudio.mockReturnValue(studio({ review: false }));
    render(<MaterialsPage />);

    const expiredImage = await screen.findByRole("img", {
      name: "expiring-image.png",
    });
    fireEvent.error(expiredImage);
    expect(
      await screen.findByText("预览加载失败，点击重试"),
    ).toBeInTheDocument();
    fireEvent.click(
      screen.getByRole("button", { name: "选择素材 expiring-image.png" }),
    );
    expect(
      await screen.findAllByRole("img", { name: "expiring-image.png" }),
    ).toHaveLength(2);
    for (const image of screen.getAllByRole("img", {
      name: "expiring-image.png",
    }))
      expect(image).toHaveAttribute("src", "https://storage.test/refreshed");
    expect(getAssetDownloadUrl).toHaveBeenCalledTimes(2);
  });

  it("移除末页唯一素材后回到有效页且不形成空白死页", async () => {
    const firstPage = Array.from({ length: 6 }, (_, index) =>
      material(`item-${index + 1}`),
    );
    let total = 7;
    listMaterials.mockImplementation(({ page }: { page: number }) =>
      Promise.resolve({
        items: page === 1 ? firstPage : [material("last-item")],
        page,
        page_size: 6,
        total,
      }),
    );
    hideMaterial.mockImplementation(() => {
      total = 6;
      return Promise.resolve();
    });
    useStudio.mockReturnValue(studio({ review: false }));
    render(<MaterialsPage />);

    await screen.findByRole("button", { name: "选择素材 item-1.png" });
    fireEvent.click(screen.getByRole("button", { name: "下一页" }));
    fireEvent.click(
      await screen.findByRole("button", { name: "选择素材 last-item.png" }),
    );
    fireEvent.click(screen.getByRole("button", { name: "从素材库移除" }));

    await screen.findByRole("button", { name: "选择素材 item-1.png" });
    expect(listMaterials).toHaveBeenLastCalledWith(
      expect.objectContaining({ page: 1 }),
    );
    expect(screen.queryByText("暂无素材")).toBeNull();
  });

  it("已归档素材支持修改分组和直接下载", async () => {
    const material = {
      id: "asset:image-cloud-2",
      owner_user_id: "employee_1",
      asset_id: "image-cloud-2",
      generation_task_id: null,
      project_id: null,
      person_id: null,
      title: "院门.png",
      group: "我的上传",
      media_type: "image",
      source: "upload",
      status: "ready",
      delivery: "stored",
      content_type: "image/png",
      size_bytes: 8,
      duration_seconds: null,
      created_at: "2026-09-06 10:00:00",
      hidden: false,
      saved: true,
      allowed_uses: ["reference"],
      allowed_actions: ["preview", "download", "rename", "hide"],
    } as const;
    listMaterials.mockResolvedValue({
      items: [material],
      page: 1,
      page_size: 6,
      total: 1,
    });
    updateMaterial.mockResolvedValue({ ...material, group: "庭院案例" });
    downloadMaterialAsset.mockResolvedValue(undefined);
    useStudio.mockReturnValue(
      studio({ review: false, data: { ...studio().data, assets: [] } }),
    );
    render(<MaterialsPage />);

    fireEvent.click(
      await screen.findByRole("button", { name: "选择素材 院门.png" }),
    );
    fireEvent.change(screen.getByLabelText("素材分组"), {
      target: { value: "庭院案例" },
    });
    fireEvent.click(screen.getByRole("button", { name: "保存分组" }));
    await waitFor(() =>
      expect(updateMaterial).toHaveBeenCalledWith("asset:image-cloud-2", {
        group: "庭院案例",
      }),
    );

    fireEvent.click(screen.getByRole("button", { name: "下载素材" }));
    await waitFor(() =>
      expect(downloadMaterialAsset).toHaveBeenCalledWith(
        "image-cloud-2",
        "院门.png",
      ),
    );
  });

  it("发布草稿独立保存于当前会话，不覆盖口播脚本", () => {
    const base = studio();
    const value = studio({
      state: {
        ...base.state,
        selectedAssetId: "completed-video",
        draft: {
          ...base.state.draft,
          script: {
            ...base.state.draft.script,
            text: "口播终稿不得被发布表单覆盖",
          },
        },
      },
      data: {
        ...base.data,
        assets: [
          ...base.data.assets,
          {
            id: "other-video",
            name: "不应被随机选作预览的视频.mp4",
            kind: "video",
            poster: "/studio/other.jpg",
            group: "成片",
            source: "任务中心",
            saved: true,
          },
          {
            id: "completed-video",
            name: "张工 · 建房预算确认版.mp4",
            kind: "video",
            poster: "/studio/completed.jpg",
            group: "成片",
            source: "任务中心",
            saved: true,
          },
        ],
        tasks: [
          {
            id: "task-completed",
            title: "张工 · 建房预算确认版",
            type: "数字人口播",
            status: "completed",
            submitted: "今天 09:27",
            resultId: "completed-video",
          },
        ],
      },
    });
    useStudio.mockReturnValue(value);
    const view = render(<PublishPage />);
    expect(
      screen.getByAltText("张工 · 建房预算确认版.mp4 视频预览"),
    ).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("发布标题"), {
      target: { value: "乡墅建房预算避坑清单" },
    });
    fireEvent.change(screen.getByLabelText("添加标签"), {
      target: { value: " 建房避坑 " },
    });
    fireEvent.keyDown(screen.getByLabelText("添加标签"), { key: "Enter" });
    expect(screen.getByText("# 建房避坑")).toBeInTheDocument();
    expect(screen.getByLabelText("添加标签")).toHaveAttribute(
      "placeholder",
      "输入标签后按 Enter",
    );
    fireEvent.change(screen.getByLabelText("添加标签"), {
      target: { value: "建房避坑" },
    });
    fireEvent.keyDown(screen.getByLabelText("添加标签"), { key: "Enter" });
    expect(screen.getAllByText("# 建房避坑")).toHaveLength(1);
    fireEvent.click(screen.getByRole("button", { name: "保存草稿" }));
    expect(value.saveDraft).not.toHaveBeenCalled();
    expect(value.state.draft.script.text).toBe("口播终稿不得被发布表单覆盖");
    expect(
      screen.getByText("已保存到当前会话，未同步到云端。"),
    ).toBeInTheDocument();
    expect(value.patchState).toHaveBeenCalledWith({
      publishDrafts: [
        expect.objectContaining({
          assetId: "completed-video",
          coverId: "completed-video",
          description: "主体之外，门窗、水电、防水和庭院，也要提前规划。",
          platform: "抖音",
          tags: ["农村自建房", "建房预算", "建房避坑"],
          title: "乡墅建房预算避坑清单",
        }),
      ],
    });
    const savedState = (
      value.patchState as unknown as {
        mock: { calls: Array<[Partial<StudioState>]> };
      }
    ).mock.calls.at(-1)?.[0];
    view.unmount();
    useStudio.mockReturnValue(
      studio({
        ...value,
        state: { ...value.state, ...savedState },
      }),
    );
    render(<PublishPage />);
    expect(screen.getByLabelText("发布标题")).toHaveValue(
      "乡墅建房预算避坑清单",
    );
    expect(screen.getByLabelText("发布描述")).toHaveValue(
      "主体之外，门窗、水电、防水和庭院，也要提前规划。",
    );
    expect(screen.getByText("# 农村自建房")).toBeInTheDocument();
    expect(screen.getByText("# 建房避坑")).toBeInTheDocument();
    expect(value.state.draft.script.text).toBe("口播终稿不得被发布表单覆盖");
    expect(
      screen.getByRole("button", { name: "正式发布（接口未接通）" }),
    ).toBeDisabled();
  });

  it("正式模式不伪造已发布数量或已连接账号", () => {
    const base = studio();
    const value = studio({
      review: false,
      state: { ...base.state, selectedAssetId: "completed-video" },
      data: {
        ...base.data,
        assets: [
          ...base.data.assets,
          {
            id: "completed-video",
            name: "乡墅建房预算确认版.mp4",
            kind: "video",
            poster: "/studio/completed.jpg",
            group: "成片",
            source: "任务中心",
            saved: true,
          },
        ],
        tasks: [
          {
            id: "task-completed",
            title: "乡墅建房预算确认版",
            type: "数字人口播",
            status: "completed",
            submitted: "今天 09:27",
            resultId: "completed-video",
          },
        ],
      },
    });
    useStudio.mockReturnValue(value);
    render(<PublishPage />);

    expect(
      screen.getByText("可整理当前会话草稿；平台授权与正式发布暂缓未启用"),
    ).toBeInTheDocument();
    expect(screen.getByText("已发布")).toBeInTheDocument();
    expect(screen.getAllByText("暂缓")).toHaveLength(2);
    expect(screen.queryByText(/待发布\s*0/)).not.toBeInTheDocument();
    expect(screen.queryByText(/已发布\s*0/)).not.toBeInTheDocument();
    expect(screen.getByText("尚未连接发布账号")).toBeInTheDocument();
    fireEvent.click(
      screen.getByRole("button", { name: "前往用户档案管理账号" }),
    );
    expect(value.navigate).toHaveBeenCalledWith("profile");
  });

  it("直接进入发布页但未选择完成视频时不随机回退预览", () => {
    const base = studio();
    useStudio.mockReturnValue(
      studio({
        state: { ...base.state, selectedAssetId: "other-video" },
        data: {
          ...base.data,
          assets: [
            ...base.data.assets,
            {
              id: "other-video",
              name: "仍在生成的视频.mp4",
              kind: "video",
              poster: "/studio/other.jpg",
              group: "成片",
              source: "任务中心",
              saved: true,
            },
            {
              id: "completed-video",
              name: "不可随机回退的完成视频.mp4",
              kind: "video",
              poster: "/studio/completed.jpg",
              group: "成片",
              source: "任务中心",
              saved: true,
            },
          ],
          tasks: [
            {
              id: "task-completed",
              title: "不可随机回退的完成视频",
              type: "数字人口播",
              status: "completed",
              submitted: "今天 09:27",
              resultId: "completed-video",
            },
          ],
        },
      }),
    );
    render(<PublishPage />);

    expect(screen.getByText("暂无可发布成片")).toBeInTheDocument();
    expect(
      screen.queryByAltText("不可随机回退的完成视频.mp4 视频预览"),
    ).toBeNull();
    expect(screen.getByRole("button", { name: "保存草稿" })).toBeDisabled();
  });
});
