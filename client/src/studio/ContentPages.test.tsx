import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { useCallback, useLayoutEffect, useState } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type {
  MaterialItem,
  MaterialUsagesResponse,
  ViralDetailViewResponse,
  ViralVideoItem,
} from "../api";
import type {
  StudioContextValue,
  StudioData,
  StudioPublishDraft,
  StudioState,
  StudioVideo,
} from "./types";

const {
  useStudio,
  fetchViralVideoMedia,
  fetchViralVideoStatistics,
  fetchViralVideo,
  openViralVideoDetail,
  getWorkspacePricing,
  createViralImportTask,
  getViralImportTask,
  listViralFavorites,
  listViralVideos,
  newViralSearchKey,
  refreshViralVideoResource,
  refreshViralVideoStatistics,
  downloadVideoToFile,
  fetchViralVideoSource,
  saveViralFavorite,
  removeViralFavorite,
  listMaterials,
  listTrainingMaterials,
  trainingMaterialDownloadUrl,
  listMaterialGroups,
  listMaterialTags,
  bulkUpdateMaterials,
  createMaterialUploadIntent,
  uploadMaterial,
  completeMaterialUpload,
  putMaterial,
  updateMaterial,
  hideMaterial,
  downloadMaterialAsset,
  getAssetDownloadUrl,
  initiateReplicaPipeline,
  fetchReplicaPipeline,
  listReplicaPipelineCharacters,
  getMaterialBatchPreviews,
  getMaterialCachedPreview,
  getMaterialCacheUsage,
  getMaterialUsages,
  clearMaterialCache,
  evictMaterialCachedPreview,
  getStudioDraft,
  saveStudioDraft,
  resolveMaterials,
  createGenerationTaskPreviewUrl,
  createPublishRecord,
  listPublishRecords,
  cancelPublishRecord,
  retryPublishRecord,
  syncPublishRecord,
  deletePublishRecord,
} = vi.hoisted(() => ({
  useStudio: vi.fn(),
  fetchViralVideoMedia: vi.fn(),
  fetchViralVideoStatistics: vi.fn(),
  fetchViralVideo: vi.fn(),
  openViralVideoDetail: vi.fn(),
  initiateReplicaPipeline: vi.fn(),
  fetchReplicaPipeline: vi.fn(),
  listReplicaPipelineCharacters: vi.fn(),
  // 默认空报价表：卡片「查看详情」不显示积分（需要报价的用例自行打桩）。
  getWorkspacePricing: vi.fn(async () => ({
    version: 1,
    configured: true,
    config: null,
    prices: [] as unknown[],
    recharge_rounding: "按支付金额换算，向下取整到整数积分",
  })),
  createViralImportTask: vi.fn(),
  getViralImportTask: vi.fn(),
  listViralFavorites: vi.fn(),
  listViralVideos: vi.fn(),
  newViralSearchKey: vi.fn(() => "viral-search-key"),
  refreshViralVideoResource: vi.fn(),
  refreshViralVideoStatistics: vi.fn(),
  downloadVideoToFile: vi.fn(),
  fetchViralVideoSource: vi.fn(),
  saveViralFavorite: vi.fn(),
  removeViralFavorite: vi.fn(),
  listMaterials: vi.fn(),
  listMaterialGroups: vi.fn(
    async (): Promise<{ items: { name: string; count: number }[] }> => ({
      items: [],
    }),
  ),
  listMaterialTags: vi.fn(
    async (): Promise<{ tag: string; count: number }[]> => [],
  ),
  bulkUpdateMaterials: vi.fn(),
  createMaterialUploadIntent: vi.fn(),
  uploadMaterial: vi.fn(),
  completeMaterialUpload: vi.fn(),
  putMaterial: vi.fn(),
  updateMaterial: vi.fn(),
  hideMaterial: vi.fn(),
  downloadMaterialAsset: vi.fn(),
  getAssetDownloadUrl: vi.fn(),
  getMaterialBatchPreviews: vi.fn(),
  getMaterialCachedPreview: vi.fn(),
  getMaterialUsages: vi.fn(
    async (_materialId: string): Promise<MaterialUsagesResponse> => ({
      producer: { total: 0, items: [] },
      reference: { total: 0, items: [] },
    }),
  ),
  getMaterialCacheUsage: vi.fn(),
  clearMaterialCache: vi.fn(),
  evictMaterialCachedPreview: vi.fn(),
  listTrainingMaterials: vi.fn(async () => ({
    items: [
      {
        id: "tm-1",
        title: "三步完成爆款复刻",
        description: "新手必看的完整流程演示",
        kind: "video",
        kind_label: "视频",
        content_type: "video/mp4",
        file_suffix: ".mp4",
        size_bytes: 12 * 1024 * 1024,
        tags: ["入门教程"],
        created_at: "2026-11-01T02:00:00Z",
      },
      {
        id: "tm-2",
        title: "结算规则讲义",
        description: "",
        kind: "training",
        kind_label: "培训文档",
        content_type: "application/pdf",
        file_suffix: ".pdf",
        size_bytes: 300 * 1024,
        tags: ["结算"],
        created_at: "2026-11-01T01:00:00Z",
      },
    ],
    tag_facet: ["入门教程", "结算"],
    page: 1,
    page_size: 24,
    total: 2,
  })),
  trainingMaterialDownloadUrl: vi.fn(async () => "https://signed/tm-1"),
  getStudioDraft: vi.fn(
    async (): Promise<{
      revision: number;
      payload: { drafts: StudioPublishDraft[] };
    }> => ({ revision: 1, payload: { drafts: [] } }),
  ),
  saveStudioDraft: vi.fn(),
  resolveMaterials: vi.fn(
    async (): Promise<{ items: MaterialItem[] }> => ({ items: [] }),
  ),
  createGenerationTaskPreviewUrl: vi.fn(),
  createPublishRecord: vi.fn(),
  listPublishRecords: vi.fn(async (): Promise<unknown[]> => []),
  cancelPublishRecord: vi.fn(),
  retryPublishRecord: vi.fn(),
  syncPublishRecord: vi.fn(),
  deletePublishRecord: vi.fn(),
}));
vi.mock("./context", () => ({ useStudio }));
vi.mock("../api", () => ({
  fetchViralVideoMedia,
  fetchViralVideoStatistics,
  fetchViralVideo,
  openViralVideoDetail,
  initiateReplicaPipeline,
  fetchReplicaPipeline,
  listReplicaPipelineCharacters,
  confirmReplicaPipeline: vi.fn(),
  cancelReplicaPipeline: vi.fn(),
  quoteReplicaPipeline: vi.fn(),
  replicaPipelineIdempotencyKey: (accountId: string, p: string, n: string) =>
    `replica-pipeline:${accountId}:${p}:${n}`,
  getWorkspacePricing,
  createViralImportTask,
  getViralImportTask,
  listViralFavorites,
  listViralVideos,
  newViralSearchKey,
  refreshViralVideoResource,
  refreshViralVideoStatistics,
  downloadVideoToFile,
  fetchViralVideoSource,
  saveViralFavorite,
  removeViralFavorite,
  listMaterials,
  listTrainingMaterials,
  trainingMaterialDownloadUrl,
  listMaterialGroups,
  listMaterialTags,
  bulkUpdateMaterials,
  createMaterialUploadIntent,
  uploadMaterial,
  completeMaterialUpload,
  putMaterial,
  updateMaterial,
  hideMaterial,
  downloadMaterialAsset,
  getAssetDownloadUrl,
  getMaterialBatchPreviews,
  getMaterialCachedPreview,
  getMaterialCacheUsage,
  getMaterialUsages,
  clearMaterialCache,
  evictMaterialCachedPreview,
  getStudioDraft,
  saveStudioDraft,
  resolveMaterials,
  createGenerationTaskPreviewUrl,
  createPublishRecord,
  listPublishRecords,
  cancelPublishRecord,
  retryPublishRecord,
  syncPublishRecord,
  deletePublishRecord,
}));
const publishAccounts = vi.hoisted(() => ({
  canUseLocalPublishAccounts: vi.fn(() => false),
  listCloudPublishAccounts: vi.fn(async () => [] as unknown[]),
  listLocalPublishAccounts: vi.fn(async () => [] as unknown[]),
  openLocalPublishAccount: vi.fn(),
}));
vi.mock("./localPublishAccounts", async (importOriginal) => ({
  ...(await importOriginal<object>()),
  ...publishAccounts,
}));
// 落实 §6.3 后「只播本地已缓存的视频」，而 jsdom 里 cacheAvailable() 恒为 false、
// viralCacheLocalUrl 恒返回 null —— 也就是单测里没有任何视频能播。因此只覆写
// 这两个碰 Tauri 的函数，其余（比例换算、角标文案、key 等）沿用真实实现：
// 默认 null 表示「未缓存」，需要测播放的用例自行 mockResolvedValue 一个地址。
const viralCacheBridge = vi.hoisted(() => ({
  viralCacheLocalUrl: vi.fn<
    (platform: string, videoId: string) => Promise<string | null>
  >(async () => null),
  ensureViralCacheForVideo: vi.fn(async () => {}),
  awaitViralCacheReady: vi.fn(async () => {}),
  // 本地缓存回收：真实实现要经过 Tauri，这里只验证接线（保留集算得对不对）。
  reclaimViralCache: vi.fn<
    (
      platform: string,
      retain: { platform: string; videoId: string }[],
    ) => Promise<string[]>
  >(async () => []),
  // 桌面端判定：jsdom 里恒为 false（走 Web 的服务端拉取链路），
  // 需要验证桌面链路的用例自行 mockReturnValue(true)。
  cacheAvailable: vi.fn(() => false),
}));
vi.mock("./viralCache", async (importOriginal) => ({
  ...(await importOriginal<object>()),
  ...viralCacheBridge,
}));
// Tauri 边界：只覆写判定与两个桥接口。默认真实行为（jsdom 不在 Tauri 内）一致，
// 需要走真实缓存 hook 的用例再打开 isTauri，否则 hook 内部的 cacheAvailable()
// 会一路短路（模块内部调用不走上面的替身）。
const tauriNative = vi.hoisted(() => ({
  isTauri: vi.fn(() => false),
  invoke: vi.fn(async (command: string) =>
    command === "viral_cache_status" ? [] : undefined,
  ),
  listen: vi.fn(async () => () => {}),
}));
vi.mock("@tauri-apps/api/core", async (importOriginal) => ({
  ...(await importOriginal<object>()),
  isTauri: tauriNative.isTauri,
  invoke: tauriNative.invoke,
  // 缓存文件用 asset 协议播放，路径拼装保持真实实现即可。
}));
vi.mock("@tauri-apps/api/event", async (importOriginal) => ({
  ...(await importOriginal<object>()),
  listen: tauriNative.listen,
}));
// 素材库上传音频要先探测时长（服务端要求音频声明用途与时长）。jsdom 没有
// <audio> 解码与 URL.createObjectURL，因此只覆写探测函数，其余 live 实现保持真实。
const liveMediaProbes = vi.hoisted(() => ({
  readAudioDuration: vi.fn<() => Promise<number>>(),
}));
vi.mock("./live", async (importOriginal) => ({
  ...(await importOriginal<object>()),
  ...liveMediaProbes,
}));
// 组件现在统一走 putMaterial。默认实现沿用旧的「传输 → 完成」两步，
// 这样既有用例针对 uploadMaterial / completeMaterialUpload 打的桩仍然生效；
// 需要覆盖复用路径的用例可以直接给 putMaterial 打桩（beforeEach 会复位到此实现）。
const defaultPutMaterial = async (
  intent: Parameters<typeof putMaterial>[0],
  file: Parameters<typeof putMaterial>[1],
  onProgress: Parameters<typeof putMaterial>[2],
) => {
  await uploadMaterial(intent, file, onProgress);
  return completeMaterialUpload(intent.asset_id);
};
putMaterial.mockImplementation(defaultPutMaterial);

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
  ViralFavoriteButton,
  ViralPage,
  visiblePageButtons,
} from "./ContentPages";
import { readAudioDuration } from "./live";

describe("素材分页窗口化", () => {
  it("页数不超过 7 时完整展示页码", () => {
    expect(visiblePageButtons(1, 3)).toEqual([1, 2, 3]);
    expect(visiblePageButtons(3, 7)).toEqual([1, 2, 3, 4, 5, 6, 7]);
  });

  it("页数较多时折叠为省略号并始终保留首末页与当前页邻域", () => {
    expect(visiblePageButtons(1, 13)).toEqual([1, 2, "…", 13]);
    expect(visiblePageButtons(2, 13)).toEqual([1, 2, 3, "…", 13]);
    expect(visiblePageButtons(7, 13)).toEqual([1, "…", 6, 7, 8, "…", 13]);
    expect(visiblePageButtons(13, 13)).toEqual([1, "…", 12, 13]);
  });
});

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
          homepageFeatured: true,
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
          homepageFeatured: true,
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
    discardSavedDraft: vi.fn(),
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
    tags: [],
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
    composite: false,
    allowed_uses: [],
    allowed_actions: ["preview", "download", "rename", "hide"],
    ...overrides,
  };
}

function StatefulViralPage({ value }: { value: StudioContextValue }) {
  const [data, setData] = useState(value.data);
  const updateData = useCallback(
    (update: (current: StudioData) => StudioData) =>
      setData((current) => update(current)),
    [],
  );
  useStudio.mockReturnValue({
    ...value,
    data,
    updateData,
  });
  return <ViralPage />;
}

function RenderWindowTrigger({ onLayout }: { onLayout?: () => void }) {
  useLayoutEffect(() => onLayout?.(), [onLayout]);
  return <ViralPage />;
}

function DetailRenderWindowTrigger({ onLayout }: { onLayout?: () => void }) {
  useLayoutEffect(() => onLayout?.(), [onLayout]);
  return <ViralDetailPage />;
}

function FavoriteRenderWindowTrigger({
  onLayout,
  onSavedChange,
}: {
  onLayout?: () => void;
  onSavedChange: (saved: boolean) => void;
}) {
  useLayoutEffect(() => onLayout?.(), [onLayout]);
  return (
    <ViralFavoriteButton
      video={studio().data.videos[0]}
      savedFromServer={false}
      onSavedChange={onSavedChange}
    />
  );
}

function viralDetailView(
  item: ViralVideoItem,
  billing: Partial<ViralDetailViewResponse["billing"]> = {},
) {
  return {
    item,
    billing: {
      charged: 0,
      unit: "call",
      deduped: true,
      billable: true,
      ...billing,
    },
  };
}

function viralItem(index: number, overrides: Partial<ViralVideoItem> = {}) {
  return {
    platform: "douyin" as const,
    videoId: `native-dy-${index}`,
    // 列表接口的条目默认带首页展示标记：网格只收策展条目，未策展的另有用例覆盖。
    homepageFeatured: true,
    category: "建房预算",
    title: `乡墅参考 ${index}`,
    author: "乡墅建房笔记",
    authorAvatar: null,
    verified: false,
    coverUrl: null,
    durationMs: 30_000,
    likes: 100 - index,
    comments: 1,
    shares: 2,
    collects: 3,
    publishedAt: 1_788_600_000 - index,
    publishedDisplay: null,
    likeDisplay: String(100 - index),
    tags: [],
    hasPlayableAudio: true,
    playUrl: null,
    isFavorite: false,
    availability: "available" as const,
    ...overrides,
  };
}

/** 视频号缺互动统计的卡片：三个补采字段全空，点赞也退成未知（不显示 0）。 */
function missingWechatVideo(): StudioVideo {
  const base = studio();
  return {
    ...base.data.videos[1],
    likes: null,
    likeDisplay: null,
    comments: null,
    shares: null,
    collections: null,
  };
}

/** 补采接口返回的完整统计（按 nativeId 认领回原卡片）。 */
function wechatStatisticsPayload() {
  return {
    items: [
      {
        platform: "wechat_channels" as const,
        videoId: "native-wx-1",
        category: "庭院案例",
        title: "新中式庭院的三个细节",
        author: "庭院设计老周",
        authorAvatar: null,
        verified: false,
        coverUrl: "/studio/demo-2.jpg",
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
  };
}

describe("V1.4 内容与运营页面", () => {
  it.each([true, false])(
    "发布条目到期先移出，刷新失败也不保留（首页：%s）",
    async (homepageFeatured) => {
      vi.useFakeTimers({ shouldAdvanceTime: true });
      vi.setSystemTime(new Date("2026-10-01T12:00:00Z"));
      try {
        // 客户本机时钟比服务器快1小时，排期仍按服务器窗口。
        listViralVideos
          .mockResolvedValueOnce({
            items: [
              viralItem(1, {
                title: "到期专题",
                homepageFeatured,
                homepageEndsAt: "2026-10-01T11:00:02Z",
              }),
            ],
            serverTime: "2026-10-01T11:00:00Z",
            nextChangeAt: null,
          })
          .mockRejectedValue(new Error("离线"));
        const base = studio();
        render(
          <StatefulViralPage
            value={studio({
              review: false,
              data: { ...base.data, videos: [] },
            })}
          />,
        );
        await screen.findByText("到期专题");
        await act(async () => {
          await vi.advanceTimersByTimeAsync(1_000);
        });
        expect(screen.getByText("到期专题")).toBeInTheDocument();
        await act(async () => {
          await vi.advanceTimersByTimeAsync(1_050);
        });
        expect(screen.queryByText("到期专题")).toBeNull();
        expect(listViralVideos).toHaveBeenCalledTimes(2);
      } finally {
        vi.useRealTimers();
      }
    },
  );

  it.each([true, false])(
    "空页按下一上线边界展示发布条目（首页：%s）",
    async (homepageFeatured) => {
      vi.useFakeTimers({ shouldAdvanceTime: true });
      vi.setSystemTime(new Date("2026-10-01T12:00:00Z"));
      try {
        listViralVideos
          .mockResolvedValueOnce({
            items: [],
            serverTime: "2026-10-01T12:00:00Z",
            nextChangeAt: "2026-10-01T12:00:02Z",
          })
          .mockResolvedValue({
            items: [
              viralItem(2, {
                title: "新上线专题",
                homepageFeatured,
                homepageStartsAt: "2026-10-01T12:00:02Z",
              }),
            ],
          });
        const base = studio();
        render(
          <StatefulViralPage
            value={studio({
              review: false,
              data: { ...base.data, videos: [] },
            })}
          />,
        );
        await act(async () => {
          await vi.advanceTimersByTimeAsync(50);
        });
        expect(screen.queryByText("新上线专题")).toBeNull();
        await act(async () => {
          await vi.advanceTimersByTimeAsync(2_050);
        });
        expect(screen.getByText("新上线专题")).toBeInTheDocument();
        expect(listViralVideos).toHaveBeenCalledTimes(2);
      } finally {
        vi.useRealTimers();
      }
    },
  );

  it("默认首页保留低赞置首与跨页同序结果，不再二次按点赞排序", async () => {
    const base = studio();
    const first = base.data.videos[0];
    listViralVideos.mockReturnValue(new Promise(() => {}));
    const value = studio({
      review: false,
      data: {
        ...base.data,
        videos: [
          {
            ...first,
            id: "rank-low",
            title: "编排首位低赞",
            likes: 1,
            homepageRank: 1,
            homepageFeatured: true,
          },
          {
            ...first,
            id: "rank-high",
            title: "编排次位高赞",
            likes: 999,
            homepageRank: 2,
            homepageFeatured: true,
          },
          {
            ...first,
            id: "rank-tie",
            title: "服务端同序后条",
            likes: 1000,
            homepageRank: 2,
            homepageFeatured: true,
          },
        ],
      },
    });
    render(<StatefulViralPage value={value} />);
    const names = screen
      .getAllByText(/编排首位低赞|编排次位高赞|服务端同序后条/)
      .map((x) => x.textContent);
    expect(names).toEqual(["编排首位低赞", "编排次位高赞", "服务端同序后条"]);
  });

  beforeEach(() => {
    window.sessionStorage.clear();
    useStudio.mockReset();
    fetchViralVideoMedia.mockReset();
    // §6.3 之后只有本地已缓存的视频可播：默认「未缓存」，需要测播放的用例
    // 自行 mockResolvedValue 一个地址。
    viralCacheBridge.viralCacheLocalUrl.mockReset();
    viralCacheBridge.viralCacheLocalUrl.mockResolvedValue(null);
    viralCacheBridge.ensureViralCacheForVideo.mockReset();
    viralCacheBridge.ensureViralCacheForVideo.mockResolvedValue(undefined);
    viralCacheBridge.awaitViralCacheReady.mockReset();
    viralCacheBridge.awaitViralCacheReady.mockResolvedValue(undefined);
    viralCacheBridge.reclaimViralCache.mockReset();
    viralCacheBridge.reclaimViralCache.mockResolvedValue([]);
    viralCacheBridge.cacheAvailable.mockReset();
    viralCacheBridge.cacheAvailable.mockReturnValue(false);
    tauriNative.isTauri.mockReset();
    tauriNative.isTauri.mockReturnValue(false);
    fetchViralVideoSource.mockReset();
    downloadVideoToFile.mockReset();
    refreshViralVideoResource.mockReset();
    refreshViralVideoStatistics.mockReset();
    fetchViralVideoStatistics.mockReset();
    fetchViralVideo.mockReset();
    openViralVideoDetail.mockReset();
    // mockReset 会清掉实现：补回「空报价表」默认值，列表用例不需要逐个打桩。
    getWorkspacePricing.mockReset();
    getWorkspacePricing.mockImplementation(async () => ({
      version: 1,
      configured: true,
      config: null,
      prices: [] as unknown[],
      recharge_rounding: "按支付金额换算，向下取整到整数积分",
    }));
    createViralImportTask.mockReset();
    getViralImportTask.mockReset();
    listViralFavorites.mockReset();
    listViralVideos.mockReset();
    // mockReset 会清掉实现，幂等键生成器要补回默认值。
    newViralSearchKey.mockReset();
    newViralSearchKey.mockImplementation(() => "viral-search-key");
    saveViralFavorite.mockReset();
    removeViralFavorite.mockReset();
    listViralVideos.mockResolvedValue({
      platform: "douyin",
      sort: "hot",
      categories: [],
      items: [],
      fetchedAt: null,
      hasMore: false,
      nextCursor: null,
      total: 0,
    });
    listViralFavorites.mockResolvedValue({ items: [], total: 0 });
    saveViralFavorite.mockResolvedValue({ isFavorite: true });
    removeViralFavorite.mockResolvedValue({ isFavorite: false });
    createViralImportTask.mockResolvedValue({
      taskId: "import-1",
      status: "SUCCEEDED",
      projectId: "project-1",
      sourceAssetId: "asset-1",
      mediaKind: "video",
      canTranscribe: true,
      canAnalyze: true,
    });
    window.history.replaceState(null, "", "/");
    listTrainingMaterials.mockReset();
    // mockReset 清实现：补回培训素材默认页（空态/错误用例自行覆盖）。
    listTrainingMaterials.mockResolvedValue({
      items: [
        {
          id: "tm-1",
          title: "三步完成爆款复刻",
          description: "新手必看的完整流程演示",
          kind: "video",
          kind_label: "视频",
          content_type: "video/mp4",
          file_suffix: ".mp4",
          size_bytes: 12 * 1024 * 1024,
          tags: ["入门教程"],
          created_at: "2026-11-01T02:00:00Z",
        },
        {
          id: "tm-2",
          title: "结算规则讲义",
          description: "",
          kind: "training",
          kind_label: "培训文档",
          content_type: "application/pdf",
          file_suffix: ".pdf",
          size_bytes: 300 * 1024,
          tags: ["结算"],
          created_at: "2026-11-01T01:00:00Z",
        },
      ],
      tag_facet: ["入门教程", "结算"],
      page: 1,
      page_size: 24,
      total: 2,
    });
    trainingMaterialDownloadUrl
      .mockReset()
      .mockResolvedValue("https://signed/tm-1");
    listMaterials.mockReset();
    listMaterialGroups.mockReset().mockResolvedValue({ items: [] });
    listMaterialTags.mockReset().mockResolvedValue([]);
    bulkUpdateMaterials.mockReset();
    createMaterialUploadIntent.mockReset();
    uploadMaterial.mockReset();
    completeMaterialUpload.mockReset();
    putMaterial.mockReset().mockImplementation(defaultPutMaterial);
    updateMaterial.mockReset();
    hideMaterial.mockReset();
    vi.mocked(readAudioDuration).mockReset();
    downloadMaterialAsset.mockReset();
    getAssetDownloadUrl.mockReset();
    getMaterialCachedPreview
      .mockReset()
      .mockImplementation(async (_userId, assetId) => ({
        ...(await getAssetDownloadUrl(assetId)),
        cached: false,
        release: vi.fn(),
      }));
    getMaterialBatchPreviews
      .mockReset()
      .mockImplementation(
        async (
          _userId: string,
          entries: { id: string; populate: boolean }[],
        ) => {
          const previews: Record<
            string,
            { url: string; cached: boolean; release: () => unknown }
          > = {};
          for (const entry of entries) {
            previews[entry.id] = {
              ...(await getAssetDownloadUrl(entry.id)),
              cached: false,
              release: vi.fn(),
            };
          }
          return { previews, thumbnails: {} };
        },
      );
    getMaterialUsages.mockReset().mockResolvedValue({
      producer: { total: 0, items: [] },
      reference: { total: 0, items: [] },
    });
    getMaterialCacheUsage.mockReset().mockResolvedValue({
      bytes: 0,
      limitBytes: 256 * 1024 * 1024,
      available: true,
    });
    clearMaterialCache.mockReset().mockResolvedValue(undefined);
    evictMaterialCachedPreview.mockReset().mockResolvedValue(undefined);
    createGenerationTaskPreviewUrl.mockReset();
    getAssetDownloadUrl.mockResolvedValue({
      url: "https://storage.test/material",
    });
    createGenerationTaskPreviewUrl.mockResolvedValue(
      "https://provider.test/direct-result.mp4",
    );
  });

  it("爆款卡片主操作统一为详情和下载视频并保留收藏", () => {
    const value = studio();
    useStudio.mockReturnValue(value);
    const view = render(<ViralPage />);
    fireEvent.click(screen.getByRole("tab", { name: "视频号 30" }));
    expect(screen.getByText("新中式庭院的三个细节")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("tab", { name: "抖音 20" }));
    fireEvent.click(
      screen.getByRole("button", { name: "收藏 农村建房预算，别只盯着主体" }),
    );
    expect(value.patchState).toHaveBeenCalledWith({ favorites: ["dy-1"] });
    fireEvent.click(
      screen.getByRole("button", {
        name: "下载视频 农村建房预算，别只盯着主体",
      }),
    );
    // 审核示例只演示交互，不执行真实下载。
    expect(value.notify).toHaveBeenCalledWith("审核模式仅演示，不执行下载");
    expect(
      screen.queryByRole("button", { name: /复刻/ }),
    ).not.toBeInTheDocument();
    for (const actions of view.container.querySelectorAll(
      ".content-card-actions",
    )) {
      expect(
        [...actions.querySelectorAll("button")].map(
          (button) => button.textContent,
        ),
      ).toEqual(["提取文案", "查看详情", "下载视频"]);
    }
  });

  it("我的收藏读取当前平台并通过真实素材导入提取文案", async () => {
    const base = studio();
    const value = studio({
      review: false,
      data: { ...base.data, videos: [] },
    });
    listViralFavorites.mockResolvedValue({
      items: [
        {
          platform: "douyin",
          videoId: "favorite-native-1",
          category: "庭院案例",
          title: "已收藏的庭院视频",
          author: "乡墅作者",
          authorAvatar: null,
          verified: false,
          coverUrl: null,
          durationMs: 16_000,
          likes: 123,
          comments: 4,
          shares: 5,
          collects: 6,
          publishedAt: 1_788_600_000,
          publishedDisplay: "1天前",
          likeDisplay: "123",
          tags: ["庭院"],
          hasPlayableAudio: true,
          playUrl: null,
          isFavorite: true,
          availability: "available",
        },
      ],
      total: 1,
      hasMore: false,
      nextCursor: null,
    });
    useStudio.mockReturnValue(value);
    const view = render(<ViralPage />);

    fireEvent.click(screen.getByRole("tab", { name: "我的收藏" }));

    await waitFor(() =>
      expect(listViralFavorites).toHaveBeenCalledWith({
        platform: "douyin",
        limit: 12,
      }),
    );
    const update = vi.mocked(value.updateData).mock.calls.at(-1)?.[0];
    expect(update?.(value.data).videos).toEqual([
      expect.objectContaining({
        nativeId: "favorite-native-1",
        title: "已收藏的庭院视频",
      }),
    ]);
    useStudio.mockReturnValue({
      ...value,
      data: update?.(value.data) ?? value.data,
    });
    view.rerender(<ViralPage />);
    // 16 秒在 4–30s 复刻门槛内：卡片主 CTA（一键复刻）随列表数据渲染；
    // 提取文案仍经详情页意图发起。
    expect(
      screen.getByRole("button", { name: "一键复刻：已收藏的庭院视频" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "收藏 已收藏的庭院视频" }),
    ).toHaveAttribute("aria-pressed", "true");
    // 提取文案入口在详情页：从收藏卡片进入详情后再发起。
    fireEvent.click(screen.getByRole("button", { name: "查看详情" }));
    expect(value.navigate).toHaveBeenCalledWith(
      "viral-detail",
      expect.objectContaining({ selectedVideoId: "douyin-favorite-native-1" }),
    );
  });

  it("收藏视频在详情页发起提取文案时走服务端导入链路", async () => {
    const base = studio();
    const value = studio({
      review: false,
      state: {
        ...base.state,
        selectedVideoId: "douyin-favorite-native-1",
      },
      data: {
        ...base.data,
        videos: [
          {
            id: "douyin-favorite-native-1",
            title: "已收藏的庭院视频",
            author: "乡墅作者",
            platform: "抖音",
            category: "庭院案例",
            poster: "",
            duration: "00:16",
            likes: 123,
            collections: 6,
            shares: 5,
            description: "",
            platformKey: "douyin",
            nativeId: "favorite-native-1",
          },
        ],
      },
    });
    openViralVideoDetail.mockResolvedValue(
      viralDetailView({
        platform: "douyin",
        videoId: "favorite-native-1",
        category: "庭院案例",
        title: "已收藏的庭院视频",
        author: "乡墅作者",
        authorAvatar: null,
        verified: false,
        coverUrl: null,
        durationMs: 16_000,
        likes: 123,
        comments: 4,
        shares: 5,
        collects: 6,
        publishedAt: 1_788_600_000,
        publishedDisplay: "1天前",
        likeDisplay: "123",
        tags: ["庭院"],
        hasPlayableAudio: true,
        playUrl: null,
        isFavorite: true,
        availability: "available",
      }),
    );
    useStudio.mockReturnValue(value);
    render(<ViralDetailPage />);

    fireEvent.click(await screen.findByRole("button", { name: "提取文案" }));

    await waitFor(() =>
      expect(value.extractScriptFromUpload).toHaveBeenCalledWith(
        "project-1",
        "asset-1",
      ),
    );
    expect(createViralImportTask).toHaveBeenCalledWith(
      "douyin",
      "favorite-native-1",
      "copy",
      expect.any(String),
    );
    expect(value.navigate).toHaveBeenCalledWith(
      "copy",
      expect.objectContaining({ returnTo: "viral-detail" }),
    );
  });

  it("爆款详情导入来源项目后把真实资产交给文案工坊", async () => {
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
    expect(createViralImportTask).toHaveBeenCalledWith(
      "douyin",
      "native-dy-1",
      "copy",
      expect.any(String),
    );
    await waitFor(() => {
      expect(value.patchDraft).toHaveBeenCalledWith({
        projectId: "project-1",
        sourceId: "asset-1",
        sourceAssetId: "asset-1",
      });
      expect(value.extractScriptFromUpload).toHaveBeenCalledWith(
        "project-1",
        "asset-1",
      );
    });
    expect(value.navigate).toHaveBeenCalledWith("copy", {
      selectedVideoId: "dy-1",
      returnTo: "viral-detail",
    });
    expect(screen.getByText("参考素材已导入")).toBeInTheDocument();
  });

  it("同一爆款重挂载后复用幂等键，避免重复创建项目和任务", async () => {
    const value = studio({
      state: {
        ...studio().state,
        page: "viral-detail",
        selectedVideoId: "dy-1",
      },
    });
    useStudio.mockReturnValue(value);
    const firstView = render(<ViralDetailPage />);

    fireEvent.click(screen.getByRole("button", { name: "提取文案" }));
    await waitFor(() => expect(createViralImportTask).toHaveBeenCalledOnce());
    const firstKey = createViralImportTask.mock.calls[0][3];
    firstView.unmount();
    useStudio.mockReturnValue(value);
    render(<ViralDetailPage />);

    fireEvent.click(screen.getByRole("button", { name: "提取文案" }));
    await waitFor(() => expect(createViralImportTask).toHaveBeenCalledTimes(2));

    expect(createViralImportTask.mock.calls[1][3]).toBe(firstKey);
  });

  it("请求未完成时切换爆款来源会丢弃旧来源迟到结果", async () => {
    let resolveFirst: ((value: unknown) => void) | undefined;
    createViralImportTask.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          resolveFirst = resolve;
        }),
    );
    const base = studio();
    let value = studio({
      state: { ...base.state, page: "viral-detail", selectedVideoId: "dy-1" },
    });
    useStudio.mockImplementation(() => value);
    const view = render(<ViralDetailPage />);
    fireEvent.click(screen.getByRole("button", { name: "提取文案" }));

    value = studio({
      state: { ...base.state, page: "viral-detail", selectedVideoId: "wx-1" },
    });
    view.rerender(<ViralDetailPage />);
    expect(screen.getByText("新中式庭院的三个细节")).toBeInTheDocument();

    await act(async () => {
      resolveFirst?.({
        taskId: "late-first",
        status: "SUCCEEDED",
        projectId: "project-first",
        sourceAssetId: "asset-first",
        mediaKind: "video",
        canTranscribe: true,
        canAnalyze: true,
      });
    });

    expect(value.patchDraft).not.toHaveBeenCalled();
    expect(value.navigate).not.toHaveBeenCalled();
    expect(value.extractScriptFromUpload).not.toHaveBeenCalled();
  });

  it("切换账户会隔离操作键并丢弃上一账户的迟到结果", async () => {
    let resolveFirst: ((value: unknown) => void) | undefined;
    createViralImportTask.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          resolveFirst = resolve;
        }),
    );
    const base = studio();
    const accountA = {
      id: "customer-a",
      username: "customer-a",
      display_name: "客户A",
      role: "customer" as const,
    };
    const accountB = {
      id: "customer-b",
      username: "customer-b",
      display_name: "客户B",
      role: "customer" as const,
    };
    let value = studio({
      user: accountA,
      state: { ...base.state, page: "viral-detail", selectedVideoId: "dy-1" },
    });
    useStudio.mockImplementation(() => value);
    const view = render(<ViralDetailPage />);
    fireEvent.click(screen.getByRole("button", { name: "提取文案" }));
    const accountAKey = createViralImportTask.mock.calls[0][3];

    value = studio({
      user: accountB,
      state: { ...base.state, page: "viral-detail", selectedVideoId: "dy-1" },
    });
    view.rerender(<ViralDetailPage />);
    await act(async () => {
      resolveFirst?.({
        id: "late-account-a",
        status: "SUCCEEDED",
        projectId: "project-a",
        sourceAssetId: "asset-a",
        canTranscribe: true,
        canAnalyze: true,
      });
    });
    expect(value.patchDraft).not.toHaveBeenCalled();

    createViralImportTask.mockResolvedValueOnce({
      id: "account-b-import",
      status: "SUCCEEDED",
      projectId: "project-b",
      sourceAssetId: "asset-b",
      canTranscribe: true,
      canAnalyze: true,
    });
    fireEvent.click(screen.getByRole("button", { name: "提取文案" }));
    await waitFor(() => expect(createViralImportTask).toHaveBeenCalledTimes(2));
    expect(createViralImportTask.mock.calls[1][3]).not.toBe(accountAKey);

    value = studio({
      user: accountA,
      state: { ...base.state, page: "viral-detail", selectedVideoId: "dy-1" },
    });
    view.rerender(<ViralDetailPage />);
    createViralImportTask.mockResolvedValueOnce({
      id: "account-a-replay",
      status: "SUCCEEDED",
      projectId: "project-a",
      sourceAssetId: "asset-a",
      canTranscribe: true,
      canAnalyze: true,
    });
    fireEvent.click(screen.getByRole("button", { name: "提取文案" }));
    await waitFor(() => expect(createViralImportTask).toHaveBeenCalledTimes(3));
    expect(createViralImportTask.mock.calls[2][3]).toBe(accountAKey);
  });

  it("视频号爆款使用平台与原生视频 ID 建立提取任务", async () => {
    const base = studio();
    const value = studio({
      state: { ...base.state, page: "viral-detail", selectedVideoId: "wx-1" },
    });
    useStudio.mockReturnValue(value);
    render(<ViralDetailPage />);

    fireEvent.click(screen.getByRole("button", { name: "提取文案" }));

    await waitFor(() =>
      expect(createViralImportTask).toHaveBeenCalledWith(
        "wechat_channels",
        "native-wx-1",
        "copy",
        expect.any(String),
      ),
    );
  });

  it("时长未知的爆款详情不提供去复刻，仅音频来源可交给文案提取", async () => {
    createViralImportTask.mockResolvedValue({
      taskId: "audio-import",
      status: "SUCCEEDED",
      projectId: "audio-project",
      sourceAssetId: "audio-asset",
      mediaKind: "audio",
      canTranscribe: true,
      canAnalyze: false,
    });
    const value = studio({
      state: {
        ...studio().state,
        page: "viral-detail",
        selectedVideoId: "dy-1",
      },
    });
    useStudio.mockReturnValue(value);
    render(<ViralDetailPage />);

    expect(
      screen.queryByRole("button", { name: /复刻/ }),
    ).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "提取文案" }));
    await waitFor(() =>
      expect(value.extractScriptFromUpload).toHaveBeenCalledWith(
        "audio-project",
        "audio-asset",
      ),
    );
    expect(createViralImportTask).toHaveBeenCalledWith(
      "douyin",
      "native-dy-1",
      "copy",
      expect.any(String),
    );
    expect(value.navigate).toHaveBeenCalledWith(
      "copy",
      expect.objectContaining({ returnTo: "viral-detail" }),
    );
  });

  it("超过15秒的爆款详情不提供去复刻入口", async () => {
    const base = studio();
    const value = studio({
      data: {
        ...base.data,
        videos: [
          { ...base.data.videos[0], durationMs: 88_000 },
          ...base.data.videos.slice(1),
        ],
      },
      state: {
        ...base.state,
        page: "viral-detail",
        selectedVideoId: "dy-1",
      },
    });
    useStudio.mockReturnValue(value);
    render(<ViralDetailPage />);

    expect(
      screen.queryByRole("button", { name: /复刻/ }),
    ).not.toBeInTheDocument();
  });

  it("低于4秒的爆款详情不提供去复刻入口", () => {
    const base = studio();
    const value = studio({
      data: {
        ...base.data,
        videos: [
          { ...base.data.videos[0], durationMs: 3_000 },
          ...base.data.videos.slice(1),
        ],
      },
      state: {
        ...base.state,
        page: "viral-detail",
        selectedVideoId: "dy-1",
      },
    });
    useStudio.mockReturnValue(value);
    render(<ViralDetailPage />);

    expect(
      screen.queryByRole("button", { name: /复刻/ }),
    ).not.toBeInTheDocument();
  });

  it("4–15秒的爆款详情提供去复刻并带入参考素材", async () => {
    createViralImportTask.mockResolvedValue({
      taskId: "replica-import",
      status: "SUCCEEDED",
      projectId: "replica-project",
      sourceAssetId: "replica-asset",
      mediaKind: "video",
      canTranscribe: true,
      canAnalyze: true,
    });
    const base = studio();
    const value = studio({
      review: false,
      data: {
        ...base.data,
        videos: [
          { ...base.data.videos[0], durationMs: 4_000 },
          ...base.data.videos.slice(1),
        ],
      },
      state: {
        ...base.state,
        page: "viral-detail",
        selectedVideoId: "dy-1",
      },
    });
    useStudio.mockReturnValue(value);
    render(<ViralDetailPage />);

    fireEvent.click(screen.getByRole("button", { name: "去复刻" }));
    await waitFor(() =>
      expect(createViralImportTask).toHaveBeenCalledWith(
        "douyin",
        "native-dy-1",
        "replica",
        expect.any(String),
      ),
    );
    await waitFor(() =>
      expect(value.patchDraft).toHaveBeenCalledWith({
        projectId: "replica-project",
        sourceId: "replica-asset",
        sourceAssetId: "replica-asset",
      }),
    );
    expect(value.navigate).toHaveBeenCalledWith(
      "replica",
      expect.objectContaining({ returnTo: "viral-detail" }),
    );
  });

  it("不可转写的畸形成功不会写入文案草稿且再次点击使用新键", async () => {
    createViralImportTask.mockResolvedValue({
      taskId: "malformed-copy",
      status: "SUCCEEDED",
      projectId: "copy-project",
      sourceAssetId: "copy-asset",
      mediaKind: "video",
      canTranscribe: false,
      canAnalyze: true,
    });
    const value = studio({
      state: {
        ...studio().state,
        page: "viral-detail",
        selectedVideoId: "dy-1",
      },
    });
    useStudio.mockReturnValue(value);
    render(<ViralDetailPage />);

    fireEvent.click(screen.getByRole("button", { name: "提取文案" }));
    expect(
      await screen.findByText("该来源暂不支持提取文案"),
    ).toBeInTheDocument();
    const malformedKey = createViralImportTask.mock.calls[0][3];
    fireEvent.click(screen.getByRole("button", { name: "提取文案" }));
    await waitFor(() => expect(createViralImportTask).toHaveBeenCalledTimes(2));
    expect(createViralImportTask.mock.calls[1][3]).not.toBe(malformedKey);
    expect(value.patchDraft).not.toHaveBeenCalled();
    expect(value.extractScriptFromUpload).not.toHaveBeenCalled();
    expect(value.navigate).not.toHaveBeenCalled();
  });

  it("桌面端卡片下载视频复用本地缓存并走保存对话框", async () => {
    viralCacheBridge.cacheAvailable.mockReturnValue(true);
    viralCacheBridge.viralCacheLocalUrl.mockResolvedValue(
      "http://asset.localhost/viral-cache/douyin/native-dy-1.mp4",
    );
    downloadVideoToFile.mockResolvedValue({
      status: "saved",
      downloadId: "download-1",
      path: "C:/Downloads/native-dy-1.mp4",
    });
    const value = studio({ review: false });
    useStudio.mockReturnValue(value);
    render(<ViralPage />);

    fireEvent.click(
      screen.getByRole("button", {
        name: "下载视频 农村建房预算，别只盯着主体",
      }),
    );

    // 先确保本地缓存（已缓存则直接复用），再经保存对话框落盘。
    expect(viralCacheBridge.ensureViralCacheForVideo).toHaveBeenCalledWith(
      expect.objectContaining({
        platformKey: "douyin",
        nativeId: "native-dy-1",
      }),
    );
    await waitFor(() =>
      expect(value.notify).toHaveBeenCalledWith("视频已保存到所选位置"),
    );
    expect(downloadVideoToFile).toHaveBeenCalledWith(
      expect.any(Function),
      expect.stringContaining("农村建房预算"),
    );
    // 下载不产生服务端导入任务，也不进入文案工坊。
    expect(createViralImportTask).not.toHaveBeenCalled();
    expect(value.navigate).not.toHaveBeenCalled();
  });

  it("下载遇余额不足时打开钱包侧栏并透传服务端文案", async () => {
    viralCacheBridge.cacheAvailable.mockReturnValue(true);
    viralCacheBridge.ensureViralCacheForVideo.mockRejectedValueOnce(
      Object.assign(new Error("积分不足，本次需要 6 积分。"), {
        code: "INSUFFICIENT_CREDITS",
      }),
    );
    const value = studio({ review: false });
    useStudio.mockReturnValue(value);
    render(<ViralPage />);

    fireEvent.click(
      screen.getByRole("button", {
        name: "下载视频 农村建房预算，别只盯着主体",
      }),
    );

    await waitFor(() => expect(value.openLive).toHaveBeenCalledWith("wallet"));
    expect(value.notify).toHaveBeenCalledWith("积分不足，本次需要 6 积分。");
  });

  it("爆款详情提取文案直连服务端导入：桌面与 Web 同一动作", () => {
    viralCacheBridge.cacheAvailable.mockReturnValue(true);
    const value = studio({
      review: false,
      state: {
        ...studio().state,
        page: "viral-detail",
        selectedVideoId: "dy-1",
      },
    });
    useStudio.mockReturnValue(value);
    render(<ViralDetailPage />);

    fireEvent.click(screen.getByRole("button", { name: "提取文案" }));

    // 共享文案缓存退役：不再前置查缓存，直接走服务端导入 → 转写。
    expect(createViralImportTask).toHaveBeenCalledWith(
      "douyin",
      "native-dy-1",
      "copy",
      expect.any(String),
    );
  });

  it("查看详情免费：卡片不再报价，也不再有已查看角标（2026-10-06 拍板）", async () => {
    listViralVideos.mockResolvedValue({
      platform: "douyin",
      sort: "hot",
      categories: [],
      items: [
        viralItem(1, { title: "未看过的视频" }),
        viralItem(2, { title: "历史已购的视频", detailCharged: true }),
      ],
      fetchedAt: "2026-09-07T12:00:00Z",
      hasMore: false,
      nextCursor: null,
      total: 2,
    });
    const base = studio();
    const value = studio({
      review: false,
      data: { ...base.data, videos: [] },
    });
    render(<StatefulViralPage value={value} />);

    // 两张卡片各一个不带价签的「查看详情」按钮，且不再渲染已查看角标。
    expect(
      await screen.findAllByRole("button", { name: "查看详情" }),
    ).toHaveLength(2);
    expect(screen.queryByText("已查看")).not.toBeInTheDocument();
    expect(screen.queryByText(/积分/)).not.toBeInTheDocument();
  });

  it("详情页刷新资源仅抖音展示并按次调用计费接口", async () => {
    const value = studio({
      review: false,
      state: {
        ...studio().state,
        page: "viral-detail",
        selectedVideoId: "dy-1",
      },
    });
    useStudio.mockReturnValue(value);
    refreshViralVideoResource.mockResolvedValue({
      video: {
        platform: "douyin",
        videoId: "native-dy-1",
        title: "农村建房预算，别只盯着主体",
        author: "乡墅老张",
        coverUrl: null,
      },
      billing: { charged: 1, unit: "call" },
    });
    render(<ViralDetailPage />);

    fireEvent.click(
      await screen.findByRole("button", { name: "刷新资源（按次计费）" }),
    );

    expect(refreshViralVideoResource).toHaveBeenCalledWith(
      "douyin",
      "native-dy-1",
      "viral-search-key",
    );
    await waitFor(() =>
      expect(screen.getByText("资源已刷新，正在更新详情…")).toBeInTheDocument(),
    );
  });

  it("正式爆款卡片缺少平台原生 ID 时明确报错且不进入伪项目", () => {
    const base = studio();
    const malformed = {
      ...base.data.videos[0],
      platformKey: undefined,
      nativeId: undefined,
    };
    const value = studio({
      review: false,
      data: { ...base.data, videos: [malformed] },
    });
    useStudio.mockReturnValue(value);
    render(<ViralPage />);

    fireEvent.click(
      screen.getByRole("button", { name: `下载视频 ${malformed.title}` }),
    );

    expect(value.notify).toHaveBeenCalledWith("该视频缺少可下载的平台标识");
    expect(value.patchDraft).not.toHaveBeenCalled();
    expect(value.navigate).not.toHaveBeenCalled();
  });

  it("导入排队时始终使用服务端返回的同一任务 ID 轮询", async () => {
    vi.useFakeTimers();
    try {
      createViralImportTask.mockResolvedValue({
        id: "server-import-1",
        status: "PENDING",
        projectId: "project-1",
        sourceAssetId: null,
        canTranscribe: false,
        canAnalyze: false,
      });
      getViralImportTask.mockResolvedValue({
        id: "server-import-1",
        status: "SUCCEEDED",
        projectId: "project-1",
        sourceAssetId: "asset-1",
        canTranscribe: true,
        canAnalyze: true,
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
      await act(async () => {
        await vi.advanceTimersByTimeAsync(1_000);
      });

      expect(getViralImportTask).toHaveBeenCalledWith("server-import-1");
      expect(value.patchDraft).toHaveBeenCalledWith({
        projectId: "project-1",
        sourceId: "asset-1",
        sourceAssetId: "asset-1",
      });
    } finally {
      vi.useRealTimers();
    }
  });

  it("第120次轮询成功时只写入一次草稿并保留操作键", async () => {
    vi.useFakeTimers();
    try {
      let polls = 0;
      createViralImportTask.mockResolvedValue({
        id: "boundary-success",
        status: "PENDING",
        projectId: "boundary-project",
        sourceAssetId: null,
      });
      getViralImportTask.mockImplementation(async () => {
        polls += 1;
        return polls >= 120
          ? {
              id: "boundary-success",
              status: "SUCCEEDED",
              projectId: "boundary-project",
              sourceAssetId: "boundary-asset",
              canTranscribe: true,
              canAnalyze: true,
            }
          : {
              id: "boundary-success",
              status: "PENDING",
              projectId: "boundary-project",
              sourceAssetId: null,
            };
      });
      const value = studio({
        state: {
          ...studio().state,
          page: "viral-detail",
          selectedVideoId: "dy-1",
        },
      });
      useStudio.mockReturnValue(value);
      render(<ViralDetailPage />);

      fireEvent.click(screen.getByRole("button", { name: "提取文案" }));
      const firstKey = createViralImportTask.mock.calls[0][3];
      await act(async () => {
        await vi.advanceTimersByTimeAsync(120_000);
      });

      expect(getViralImportTask).toHaveBeenCalledTimes(120);
      expect(value.patchDraft).toHaveBeenCalledOnce();
      expect(value.patchDraft).toHaveBeenCalledWith({
        projectId: "boundary-project",
        sourceId: "boundary-asset",
        sourceAssetId: "boundary-asset",
      });
      fireEvent.click(screen.getByRole("button", { name: "提取文案" }));
      expect(createViralImportTask.mock.calls[1][3]).toBe(firstKey);
    } finally {
      vi.useRealTimers();
    }
  });

  it("第120次轮询永久失败时展示失败并清除操作键", async () => {
    vi.useFakeTimers();
    try {
      let polls = 0;
      createViralImportTask.mockResolvedValue({
        id: "boundary-failure",
        status: "PENDING",
        projectId: "boundary-project",
        sourceAssetId: null,
      });
      getViralImportTask.mockImplementation(async () => {
        polls += 1;
        return polls >= 120
          ? {
              id: "boundary-failure",
              status: "FAILED",
              projectId: "boundary-project",
              sourceAssetId: null,
              retryable: false,
              errorMessage: "来源归属已变化",
            }
          : {
              id: "boundary-failure",
              status: "PENDING",
              projectId: "boundary-project",
              sourceAssetId: null,
            };
      });
      const value = studio({
        state: {
          ...studio().state,
          page: "viral-detail",
          selectedVideoId: "dy-1",
        },
      });
      useStudio.mockReturnValue(value);
      render(<ViralDetailPage />);

      fireEvent.click(screen.getByRole("button", { name: "提取文案" }));
      const firstKey = createViralImportTask.mock.calls[0][3];
      await act(async () => {
        await vi.advanceTimersByTimeAsync(120_000);
      });

      expect(getViralImportTask).toHaveBeenCalledTimes(120);
      expect(screen.getByText("来源归属已变化")).toBeInTheDocument();
      expect(value.patchDraft).not.toHaveBeenCalled();
      fireEvent.click(screen.getByRole("button", { name: "提取文案" }));
      expect(createViralImportTask.mock.calls[1][3]).not.toBe(firstKey);
    } finally {
      vi.useRealTimers();
    }
  });

  it("离开爆款详情后，迟到的来源导入不会替换当前草稿或跳转", async () => {
    let resolve: ((value: unknown) => void) | undefined;
    createViralImportTask.mockImplementation(
      () =>
        new Promise((done) => {
          resolve = done;
        }),
    );
    const base = studio();
    const value = studio({
      state: { ...base.state, page: "viral-detail", selectedVideoId: "dy-1" },
    });
    useStudio.mockReturnValue(value);
    const view = render(<ViralDetailPage />);
    fireEvent.click(screen.getByRole("button", { name: "提取文案" }));
    const firstKey = createViralImportTask.mock.calls[0][3];
    view.unmount();

    await act(async () => {
      resolve?.({
        taskId: "late-import",
        status: "SUCCEEDED",
        projectId: "late-project",
        sourceAssetId: "late-asset",
        mediaKind: "video",
        canTranscribe: true,
        canAnalyze: true,
      });
    });

    expect(value.patchDraft).not.toHaveBeenCalled();
    expect(value.navigate).not.toHaveBeenCalled();
    expect(value.updateData).not.toHaveBeenCalled();

    createViralImportTask.mockResolvedValueOnce({
      taskId: "replayed-import",
      status: "SUCCEEDED",
      projectId: "replayed-project",
      sourceAssetId: "replayed-asset",
      mediaKind: "video",
      canTranscribe: true,
      canAnalyze: true,
    });
    useStudio.mockReturnValue(value);
    render(<ViralDetailPage />);
    fireEvent.click(screen.getByRole("button", { name: "提取文案" }));
    await waitFor(() => expect(createViralImportTask).toHaveBeenCalledTimes(2));
    expect(createViralImportTask.mock.calls[1][3]).toBe(firstKey);
  });

  it("导入轮询超时后保留旧键，再点接回同一导入任务而不新建项目（FIX-09）", async () => {
    vi.useFakeTimers();
    try {
      createViralImportTask.mockResolvedValue({
        id: "stalled-import",
        status: "PENDING",
        projectId: "stalled-project",
        sourceAssetId: null,
        retryable: true,
      });
      getViralImportTask.mockResolvedValue({
        id: "stalled-import",
        status: "RUNNING",
        projectId: "stalled-project",
        sourceAssetId: null,
        retryable: true,
      });
      const base = studio();
      const value = studio({
        user: {
          id: "customer-timeout",
          username: "customer-timeout",
          display_name: "超时客户",
          role: "customer",
        },
        state: {
          ...base.state,
          page: "viral-detail",
          selectedVideoId: "dy-1",
        },
      });
      useStudio.mockReturnValue(value);
      render(<ViralDetailPage />);

      fireEvent.click(screen.getByRole("button", { name: "提取文案" }));
      const firstKey = createViralImportTask.mock.calls[0][3];
      await act(async () => {
        await vi.advanceTimersByTimeAsync(120_000);
      });
      expect(screen.getByText(/导入仍在后台进行/)).toBeInTheDocument();

      createViralImportTask.mockResolvedValueOnce({
        id: "stalled-import",
        status: "SUCCEEDED",
        projectId: "stalled-project",
        sourceAssetId: "stalled-asset",
        canTranscribe: true,
        canAnalyze: true,
      });
      fireEvent.click(screen.getByRole("button", { name: "提取文案" }));
      await act(async () => {});

      expect(createViralImportTask).toHaveBeenCalledTimes(2);
      // 同一幂等键：服务端回放原任务，不会再建一个导入任务和空壳项目。
      expect(createViralImportTask.mock.calls[1][3]).toBe(firstKey);
    } finally {
      vi.useRealTimers();
    }
  });

  it("爆款导入失败时保持详情页并展示错误", async () => {
    createViralImportTask.mockRejectedValue(new Error("素材暂时无法获取"));
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
    fireEvent.click(screen.getByRole("button", { name: "提取文案" }));
    await waitFor(() => {
      expect(screen.getByText(/素材暂时无法获取/)).toBeInTheDocument();
    });
    const firstKey = createViralImportTask.mock.calls[0][3];
    fireEvent.click(screen.getByRole("button", { name: "提取文案" }));
    await waitFor(() => expect(createViralImportTask).toHaveBeenCalledTimes(2));
    expect(createViralImportTask.mock.calls[1][3]).toBe(firstKey);
    expect(value.navigate).not.toHaveBeenCalled();
  });

  it("永久失败后清除旧幂等键，再次点击创建新操作", async () => {
    createViralImportTask
      .mockResolvedValueOnce({
        id: "permanent-failure",
        status: "FAILED",
        retryable: false,
        errorMessage: "来源已过期",
      })
      .mockResolvedValueOnce({
        id: "replacement",
        status: "SUCCEEDED",
        projectId: "project-new",
        sourceAssetId: "asset-new",
        canTranscribe: true,
        canAnalyze: true,
      });
    const value = studio({
      user: {
        id: "customer-a",
        username: "customer-a",
        display_name: "客户A",
        role: "customer",
      },
      state: {
        ...studio().state,
        page: "viral-detail",
        selectedVideoId: "dy-1",
      },
    });
    useStudio.mockReturnValue(value);
    render(<ViralDetailPage />);

    fireEvent.click(screen.getByRole("button", { name: "提取文案" }));
    await screen.findByText("来源已过期");
    const failedKey = createViralImportTask.mock.calls[0][3];
    fireEvent.click(screen.getByRole("button", { name: "提取文案" }));
    await waitFor(() => expect(createViralImportTask).toHaveBeenCalledTimes(2));

    expect(createViralImportTask.mock.calls[1][3]).not.toBe(failedKey);
  });

  it("结果未知的网络异常保留幂等键供安全重放", async () => {
    createViralImportTask
      .mockRejectedValueOnce(new Error("network uncertain"))
      .mockResolvedValueOnce({
        id: "replayed",
        status: "SUCCEEDED",
        projectId: "project-replayed",
        sourceAssetId: "asset-replayed",
        canTranscribe: true,
        canAnalyze: true,
      });
    const value = studio({
      user: {
        id: "customer-a",
        username: "customer-a",
        display_name: "客户A",
        role: "customer",
      },
      state: {
        ...studio().state,
        page: "viral-detail",
        selectedVideoId: "dy-1",
      },
    });
    useStudio.mockReturnValue(value);
    render(<ViralDetailPage />);

    fireEvent.click(screen.getByRole("button", { name: "提取文案" }));
    await screen.findByText("network uncertain");
    const uncertainKey = createViralImportTask.mock.calls[0][3];
    fireEvent.click(screen.getByRole("button", { name: "提取文案" }));
    await waitFor(() => expect(createViralImportTask).toHaveBeenCalledTimes(2));

    expect(createViralImportTask.mock.calls[1][3]).toBe(uncertainKey);
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

    const brokenPoster = view.container.querySelector(
      ".viral-card-cover .video-preview__foreground",
    );
    expect(brokenPoster).not.toBeNull();
    if (!brokenPoster) return;
    fireEvent.error(brokenPoster);
    expect(
      view.container.querySelector(
        ".viral-card-cover .video-preview__foreground",
      ),
    ).toBeNull();

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
      view.container.querySelector(
        ".viral-card-cover .video-preview__foreground",
      ),
    ).toHaveAttribute("src", "/studio/fresh.jpg");
  });

  it("未命中本地缓存的视频点击播放时向服务端取签名地址直接播放，并触发按需缓存", async () => {
    fetchViralVideoMedia.mockResolvedValue({
      kind: "video",
      url: "https://media.test/signed/native-dy-1.mp4",
      contentType: "video/mp4",
      cacheHit: true,
    });
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

    // 人工推送模式下已上架素材必然已归档：未命中本地缓存时直接流式播放
    // 服务端下发的签名地址（COS 预签名/本地签名路由），不再要求先缓存。
    await waitFor(() =>
      expect(
        view.container.querySelector(
          ".viral-card-cover .video-preview__foreground",
        ),
      ).toHaveAttribute("src", "https://media.test/signed/native-dy-1.mp4"),
    );
    expect(fetchViralVideoMedia).toHaveBeenCalledWith(
      "douyin",
      "native-dy-1",
      "video",
    );
    // 播放不走会过期的「源站直链」（那条路按次计费，只留给 Web 端下载）。
    expect(fetchViralVideoSource).not.toHaveBeenCalled();
    // 缓存同时踢起来：二次播放/离线场景直接命中本地。
    await waitFor(() =>
      expect(viralCacheBridge.ensureViralCacheForVideo).toHaveBeenCalledWith(
        expect.objectContaining({
          platformKey: "douyin",
          nativeId: "native-dy-1",
        }),
      ),
    );
  });

  it("链接导入等未策展条目不出现在爆款网格里", () => {
    const base = studio();
    const imported = {
      ...base.data.videos[0],
      id: "douyin-imported-1",
      nativeId: "imported-native-1",
      title: "刚从链接导入的参考视频",
      category: "链接导入",
      // 链接导入/搜索结果注入的条目没有首页展示标记。
      homepageFeatured: false,
      playUrl: "https://source.test/imported.mp4",
    };
    useStudio.mockReturnValue(
      studio({
        review: false,
        data: { ...base.data, videos: [...base.data.videos, imported] },
      }),
    );
    render(<ViralPage />);

    expect(screen.getByText("农村建房预算，别只盯着主体")).toBeInTheDocument();
    expect(screen.queryByText("刚从链接导入的参考视频")).toBeNull();
    // 平台计数与分类同样只算策展集合：否则会出现点进去什么都没有的分类。
    expect(screen.getByRole("tab", { name: "抖音 1" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "链接导入" })).toBeNull();
  });

  it("每次列表刷新后把本地多余的缓存交给服务端回收（删除与下架连带清缓存）", async () => {
    const base = studio();
    // 链接导入的素材不进爆款网格，但同样在用本地缓存：回收时必须保留。
    const imported = {
      ...base.data.videos[0],
      id: "douyin-imported-1",
      nativeId: "imported-native-1",
      title: "刚从链接导入的参考视频",
      category: "链接导入",
      homepageFeatured: false,
    };
    useStudio.mockReturnValue(
      studio({ review: false, data: { ...base.data, videos: [imported] } }),
    );
    const stillServed = { ...viralItem(1), isFavorite: true };
    const hidden = { ...viralItem(2), availability: "unavailable" as const };
    listViralVideos.mockResolvedValue({
      platform: "douyin",
      sort: "hot",
      categories: [],
      items: [stillServed, hidden],
      fetchedAt: null,
      hasMore: false,
      nextCursor: null,
      total: 2,
    });
    render(<ViralPage />);

    await waitFor(() =>
      expect(viralCacheBridge.reclaimViralCache).toHaveBeenCalled(),
    );
    const [platform, retain] = viralCacheBridge.reclaimViralCache.mock.calls[0];
    expect(platform).toBe("douyin");
    const retained = new Set(retain.map((key) => key.videoId));
    // 仍在下发的条目与收藏由客户端先挡掉（服务端还会再挡一次）；素材一并保留。
    expect(retained.has("native-dy-1")).toBe(true);
    expect(retained.has("imported-native-1")).toBe(true);
    // 已下架的条目已经播不了，缓存留着没意义，交给服务端判定回收。
    expect(retained.has("native-dy-2")).toBe(false);
  });

  it("归档未就绪的视频标「素材未就绪」，不谎报缓存失败", async () => {
    // 真实缓存 hook 的早退条件是模块内的 cacheAvailable()，只能从 Tauri 判定这一层打开。
    tauriNative.isTauri.mockReturnValue(true);
    fetchViralVideoMedia.mockRejectedValue({
      code: "VIRAL_MEDIA_PREPARATION_BUSY",
    });
    const base = studio();
    useStudio.mockReturnValue(
      studio({
        review: false,
        data: { ...base.data, videos: [base.data.videos[0]] },
      }),
    );
    render(<ViralPage />);

    expect(await screen.findByText("素材未就绪")).toBeInTheDocument();
    expect(screen.queryByText("缓存失败（可重试）")).toBeNull();
    expect(fetchViralVideoMedia).toHaveBeenCalledWith(
      "douyin",
      "native-dy-1",
      "video",
    );
  });

  it("新卡片开始播放时暂停上一条并保留原播放器", async () => {
    const pause = vi
      .spyOn(HTMLMediaElement.prototype, "pause")
      .mockImplementation(() => {});
    // 两个视频都已缓存：§6.3 之后只有本地文件可播。
    viralCacheBridge.viralCacheLocalUrl.mockImplementation(
      async (_platform, videoId) => `asset://localhost/${videoId}.mp4`,
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
    const resolvers = new Map<string, (value: string | null) => void>();
    viralCacheBridge.viralCacheLocalUrl.mockImplementation(
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
    // 两次点击都会去查本地缓存；先让后点击的那条返回并开始播放。
    await waitFor(() => expect(resolvers.has("native-dy-1")).toBe(true));
    await waitFor(() => expect(resolvers.has("native-dy-2")).toBe(true));
    resolvers.get("native-dy-2")?.("asset://localhost/native-dy-2.mp4");
    expect(await screen.findByTitle("第二条乡墅参考")).toHaveAttribute(
      "src",
      "asset://localhost/native-dy-2.mp4",
    );

    // 先点击的那条晚返回，不得抢走播放。
    resolvers.get("native-dy-1")?.("asset://localhost/native-dy-1.mp4");
    await waitFor(() => {
      expect(screen.queryByTitle("农村建房预算，别只盯着主体")).toBeNull();
    });
  });

  it("未知爆款统计逐项显示待核验", () => {
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

    expect(screen.getByTitle("评论")).toHaveTextContent("待核验");
    expect(screen.getByTitle("转发")).toHaveTextContent("待核验");
    expect(screen.getByTitle("收藏")).toHaveTextContent("待核验");
  });

  it.each([0, 1])(
    "两平台卡片与详情逐字段区分未知、真零和已有统计：%s",
    (platformIndex) => {
      const base = studio();
      const video = {
        ...base.data.videos[platformIndex],
        likes: 1200,
        likeDisplay: null,
        comments: null,
        shares: 0,
        collections: null,
        statisticsUpdatedAt: "2026-10-12T08:00:00Z",
        statisticsCheckedAt: "2026-10-12T09:00:00Z",
        statisticsStatus: "partial" as const,
        statisticsSource:
          platformIndex === 0 ? "platform_search" : "platform_detail",
      };
      const value = studio({ data: { ...base.data, videos: [video] } });
      useStudio.mockReturnValue(value);
      const view = render(<ViralPage />);
      if (platformIndex === 1)
        fireEvent.click(screen.getByRole("tab", { name: /视频号/ }));
      expect(screen.getByText(/统计来源：/)).toHaveTextContent(
        platformIndex === 0 ? "平台搜索" : "互动核验",
      );
      expect(screen.getByTitle("点赞")).toHaveTextContent("1,200");
      expect(screen.getByTitle("评论")).toHaveTextContent("待核验");
      expect(screen.getByTitle("转发")).toHaveTextContent(/^0$/);
      expect(screen.getByTitle("收藏")).toHaveTextContent("待核验");
      expect(
        screen.getByText(/统计时间：2026-10-12T08:00:00Z/),
      ).toHaveTextContent("本次部分指标未更新");
      view.unmount();
      useStudio.mockReturnValue({
        ...value,
        state: {
          ...value.state,
          page: "viral-detail",
          selectedVideoId: video.id,
        },
      });
      render(<ViralDetailPage />);
      expect(screen.getByTitle("点赞")).toHaveTextContent("1,200");
      expect(screen.getByTitle("评论")).toHaveTextContent("待核验");
      expect(screen.getByTitle("转发")).toHaveTextContent(/^0$/);
      expect(
        screen.getByText(/最近核验：2026-10-12T09:00:00Z/),
      ).toBeInTheDocument();
    },
  );

  it("缺字段刷新保留1200赞并提示本次未完整更新", async () => {
    listViralVideos.mockReturnValue(new Promise(() => {}));
    const base = studio();
    const video = {
      ...missingWechatVideo(),
      likes: 1200,
      statisticsUpdatedAt: "2026-10-12T08:00:00Z",
    };
    fetchViralVideoStatistics.mockResolvedValue({
      items: [
        viralItem(1, {
          platform: "wechat_channels",
          videoId: video.nativeId,
          likes: null,
          comments: null,
          shares: null,
          collects: null,
          likeDisplay: null,
          statisticsUpdatedAt: null,
          statisticsCheckedAt: "2026-10-12T09:00:00Z",
          statisticsStatus: "partial",
        }),
      ],
    });
    render(
      <StatefulViralPage
        value={studio({
          review: false,
          data: { ...base.data, videos: [video] },
        })}
      />,
    );
    fireEvent.click(screen.getByRole("tab", { name: /视频号/ }));
    await waitFor(() =>
      expect(screen.getByText(/本次部分指标未更新/)).toBeInTheDocument(),
    );
    expect(screen.getByTitle("点赞")).toHaveTextContent("1,200");
    expect(screen.getByTitle("评论")).toHaveTextContent("待核验");
    expect(
      screen.getByText(/统计时间：2026-10-12T08:00:00Z/),
    ).toBeInTheDocument();
  });

  it("非首页发布条目在客户列表首屏和下一页可见，私有导入不混入", async () => {
    const base = studio();
    const first = viralItem(71, {
      homepageFeatured: false,
      title: "普通发布首屏",
    });
    const second = viralItem(72, {
      homepageFeatured: false,
      title: "普通发布次页",
    });
    const privateVideo = {
      ...base.data.videos[0],
      homepageFeatured: false,
      id: "private",
      nativeId: "private",
      title: "私有链接导入",
    };
    listViralVideos
      .mockResolvedValueOnce({
        platform: "douyin",
        sort: "hot",
        categories: [],
        items: [first],
        fetchedAt: null,
        hasMore: true,
        nextCursor: "published-page-2",
        total: 2,
      })
      .mockResolvedValueOnce({
        platform: "douyin",
        sort: "hot",
        categories: [],
        items: [second],
        fetchedAt: null,
        hasMore: false,
        nextCursor: null,
        total: 2,
      });
    render(
      <StatefulViralPage
        value={studio({
          review: false,
          data: { ...base.data, videos: [privateVideo] },
        })}
      />,
    );
    expect(await screen.findByText("普通发布首屏")).toBeInTheDocument();
    expect(screen.queryByText("私有链接导入")).not.toBeInTheDocument();
    fireEvent.click(
      await screen.findByRole("button", { name: "加载更多视频" }),
    );
    expect(await screen.findByText("普通发布次页")).toBeInTheDocument();
    expect(screen.getByText("普通发布首屏")).toBeInTheDocument();
    expect(listViralVideos).toHaveBeenNthCalledWith(1, "douyin", "hot", {
      limit: 12,
    });
    expect(listViralVideos).toHaveBeenNthCalledWith(2, "douyin", "hot", {
      limit: 12,
      cursor: "published-page-2",
    });
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

  it("视频号缺互动统计时给出自愈入口，补全后逐条回填并自动收起", async () => {
    listViralVideos.mockReturnValue(new Promise(() => {}));
    fetchViralVideoStatistics.mockResolvedValue({ items: [] });
    let release: ((payload: { items: ViralVideoItem[] }) => void) | undefined;
    refreshViralVideoStatistics.mockImplementation(
      () =>
        new Promise((resolve) => {
          release = resolve;
        }),
    );
    const base = studio();
    const value = studio({
      review: false,
      data: { ...base.data, videos: [missingWechatVideo()] },
    });
    render(<StatefulViralPage value={value} />);
    fireEvent.click(screen.getByRole("tab", { name: /视频号/ }));

    // 缺统计的卡片四项都是「—」：点赞也不显示 0（0 会被读成已到手的真实数据）。
    expect(screen.getByTitle("点赞")).toHaveTextContent("待核验");
    expect(
      await screen.findByText("1 条视频号的互动统计还没补全"),
    ).toBeInTheDocument();
    expect(screen.getByText(/补全只对缺失的条目外呼/)).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "补全统计" }));
    // 逐条补采：入口换成进度，报价口径由服务端按实际外呼条数结算。
    expect(
      await screen.findByText(/正在补全 1 条视频号的互动统计…（0 \/ 1）/),
    ).toBeInTheDocument();
    await waitFor(() =>
      expect(refreshViralVideoStatistics).toHaveBeenCalledWith(
        ["native-wx-1"],
        "viral-search-key",
      ),
    );

    await act(async () => {
      release?.(wechatStatisticsPayload());
    });
    await waitFor(() =>
      expect(screen.getByTitle("评论")).toHaveTextContent("44"),
    );
    expect(screen.getByTitle("点赞")).toHaveTextContent("1.2万");
    // 补齐后自愈入口自动消失。
    expect(screen.queryByText(/还没补全/)).toBeNull();
    expect(screen.queryByText(/正在补全/)).toBeNull();
  });

  it("取消补全后丢弃迟到的回填，缺口重新出现在入口上", async () => {
    listViralVideos.mockReturnValue(new Promise(() => {}));
    fetchViralVideoStatistics.mockResolvedValue({ items: [] });
    let release: ((payload: { items: ViralVideoItem[] }) => void) | undefined;
    refreshViralVideoStatistics.mockImplementation(
      () =>
        new Promise((resolve) => {
          release = resolve;
        }),
    );
    const base = studio();
    const value = studio({
      review: false,
      data: { ...base.data, videos: [missingWechatVideo()] },
    });
    render(<StatefulViralPage value={value} />);
    fireEvent.click(screen.getByRole("tab", { name: /视频号/ }));

    fireEvent.click(await screen.findByRole("button", { name: "补全统计" }));
    expect(await screen.findByText(/正在补全/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "取消" }));
    expect(screen.queryByText(/正在补全/)).toBeNull();
    expect(
      screen.getByText("1 条视频号的互动统计还没补全"),
    ).toBeInTheDocument();

    // 取消后那一条仍会返回：迟到的结果不得再写进列表（否则会白扣一次还改了界面）。
    await act(async () => {
      release?.(wechatStatisticsPayload());
    });
    expect(screen.getByTitle("评论")).toHaveTextContent("待核验");
    expect(
      screen.getByText("1 条视频号的互动统计还没补全"),
    ).toBeInTheDocument();
  });

  it("忽略后收起自愈入口", async () => {
    listViralVideos.mockReturnValue(new Promise(() => {}));
    fetchViralVideoStatistics.mockResolvedValue({ items: [] });
    const base = studio();
    const value = studio({
      review: false,
      data: { ...base.data, videos: [missingWechatVideo()] },
    });
    render(<StatefulViralPage value={value} />);
    fireEvent.click(screen.getByRole("tab", { name: /视频号/ }));

    fireEvent.click(await screen.findByRole("button", { name: "忽略" }));
    expect(screen.queryByText(/还没补全/)).toBeNull();
  });

  it("补全失败时保留入口并给出重试提示", async () => {
    listViralVideos.mockReturnValue(new Promise(() => {}));
    fetchViralVideoStatistics.mockResolvedValue({ items: [] });
    refreshViralVideoStatistics.mockRejectedValue(new Error("upstream down"));
    const base = studio();
    const value = studio({
      review: false,
      data: { ...base.data, videos: [missingWechatVideo()] },
    });
    render(<StatefulViralPage value={value} />);
    fireEvent.click(screen.getByRole("tab", { name: /视频号/ }));

    fireEvent.click(await screen.findByRole("button", { name: "补全统计" }));
    expect(
      await screen.findByText("互动统计补全中断，请稍后重试"),
    ).toBeInTheDocument();
    expect(
      screen.getByText("1 条视频号的互动统计还没补全"),
    ).toBeInTheDocument();
  });

  it("补全遇到余额不足时引导充值并保留入口", async () => {
    listViralVideos.mockReturnValue(new Promise(() => {}));
    fetchViralVideoStatistics.mockResolvedValue({ items: [] });
    refreshViralVideoStatistics.mockRejectedValue(
      Object.assign(new Error("积分不足"), { code: "INSUFFICIENT_CREDITS" }),
    );
    const base = studio();
    const value = studio({
      review: false,
      data: { ...base.data, videos: [missingWechatVideo()] },
    });
    render(<StatefulViralPage value={value} />);
    fireEvent.click(screen.getByRole("tab", { name: /视频号/ }));

    fireEvent.click(await screen.findByRole("button", { name: "补全统计" }));
    expect(
      await screen.findByText("余额不足，无法补全互动统计"),
    ).toBeInTheDocument();
    expect(value.openLive).toHaveBeenCalledWith("wallet");
  });

  it("审核演示不出现自愈入口，也不发起补采", async () => {
    listViralVideos.mockReturnValue(new Promise(() => {}));
    const base = studio();
    useStudio.mockReturnValue(
      studio({ data: { ...base.data, videos: [missingWechatVideo()] } }),
    );
    render(<ViralPage />);
    fireEvent.click(screen.getByRole("tab", { name: /视频号/ }));

    expect(screen.getByTitle("点赞")).toHaveTextContent("待核验");
    expect(screen.queryByText(/还没补全/)).toBeNull();
    expect(fetchViralVideoStatistics).not.toHaveBeenCalled();
    expect(refreshViralVideoStatistics).not.toHaveBeenCalled();
  });

  it("视频号详情不再单独请求统计（统计随「查看详情」同批返回）", async () => {
    const base = studio();
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

    expect(
      await screen.findByRole("heading", { name: "新中式庭院的三个细节" }),
    ).toBeInTheDocument();
    // 统计字段与详情在同一个响应里返回：详情页只调一次计费接口，
    // 不再单独打统计缓存端点（一次计费覆盖详情与统计）。
    expect(fetchViralVideoStatistics).not.toHaveBeenCalled();
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
      expect(listViralVideos).toHaveBeenCalledWith("douyin", "latest", {
        limit: 12,
      }),
    );
  });

  it("非审核列表使用服务端游标分页，并提供可键盘触发的加载入口", async () => {
    const base = studio();
    const item = {
      platform: "douyin" as const,
      videoId: "native-dy-1",
      category: "建房预算",
      title: "农村建房预算，别只盯着主体",
      author: "乡墅建房笔记",
      authorAvatar: null,
      verified: true,
      coverUrl: "/studio/demo.jpg",
      durationMs: 88_000,
      likes: 18_000,
      comments: 10,
      shares: 20,
      collects: 30,
      publishedAt: 1_788_600_000,
      publishedDisplay: null,
      likeDisplay: "1.8万",
      tags: [],
      hasPlayableAudio: true,
      playUrl: null,
      isFavorite: false,
      availability: "available" as const,
    };
    listViralVideos
      .mockResolvedValueOnce({
        platform: "douyin",
        sort: "hot",
        categories: ["建房预算"],
        items: [item],
        fetchedAt: "2026-09-07T12:00:00Z",
        hasMore: true,
        nextCursor: "cursor-2",
        total: 13,
      })
      .mockResolvedValueOnce({
        platform: "douyin",
        sort: "hot",
        categories: ["建房预算"],
        items: [{ ...item, videoId: "native-dy-2", title: "第二页视频" }],
        fetchedAt: "2026-09-07T12:00:00Z",
        hasMore: false,
        nextCursor: null,
        total: 13,
      });
    useStudio.mockReturnValue(
      studio({ review: false, data: { ...base.data, videos: [] } }),
    );
    render(<ViralPage />);

    await waitFor(() =>
      expect(listViralVideos).toHaveBeenCalledWith("douyin", "hot", {
        limit: 12,
      }),
    );
    const loadMore = await screen.findByRole("button", {
      name: "加载更多视频",
    });
    fireEvent.keyDown(loadMore, { key: "Enter" });
    await waitFor(() =>
      expect(listViralVideos).toHaveBeenCalledWith("douyin", "hot", {
        limit: 12,
        cursor: "cursor-2",
      }),
    );
  });

  it("连续加载至少三十条并去除跨页重复项", async () => {
    const base = studio({ review: false });
    listViralVideos
      .mockResolvedValueOnce({
        platform: "douyin",
        sort: "hot",
        categories: ["建房预算"],
        items: Array.from({ length: 12 }, (_, index) => viralItem(index + 1)),
        fetchedAt: "2026-09-07T12:00:00Z",
        hasMore: true,
        nextCursor: "cursor-2",
        total: 31,
      })
      .mockResolvedValueOnce({
        platform: "douyin",
        sort: "hot",
        categories: ["建房预算"],
        items: [
          viralItem(12),
          ...Array.from({ length: 11 }, (_, index) => viralItem(index + 13)),
        ],
        fetchedAt: "2026-09-07T12:00:00Z",
        hasMore: true,
        nextCursor: "cursor-3",
        total: 31,
      })
      .mockResolvedValueOnce({
        platform: "douyin",
        sort: "hot",
        categories: ["建房预算"],
        items: Array.from({ length: 8 }, (_, index) => viralItem(index + 24)),
        fetchedAt: "2026-09-07T12:00:00Z",
        hasMore: false,
        nextCursor: null,
        total: 31,
      });

    render(
      <StatefulViralPage
        value={{ ...base, data: { ...base.data, videos: [] } }}
      />,
    );

    fireEvent.click(
      await screen.findByRole("button", { name: "加载更多视频" }),
    );
    expect(await screen.findByText("乡墅参考 23")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "加载更多视频" }));

    expect(await screen.findByText("乡墅参考 31")).toBeInTheDocument();
    expect(screen.getAllByText("乡墅参考 12")).toHaveLength(1);
    expect(screen.queryByRole("button", { name: "加载更多视频" })).toBeNull();
  });

  it("分页请求在途时忽略连续触发，失败后保留同一游标供重试", async () => {
    const base = studio({ review: false });
    let resolvePage:
      | ((value: Awaited<ReturnType<typeof listViralVideos>>) => void)
      | undefined;
    listViralVideos
      .mockResolvedValueOnce({
        platform: "douyin",
        sort: "hot",
        categories: ["建房预算"],
        items: Array.from({ length: 12 }, (_, index) => viralItem(index + 1)),
        fetchedAt: "2026-09-07T12:00:00Z",
        hasMore: true,
        nextCursor: "cursor-2",
        total: 13,
      })
      .mockReturnValueOnce(
        new Promise((resolve) => {
          resolvePage = resolve;
        }),
      )
      .mockRejectedValueOnce(new Error("temporary failure"))
      .mockResolvedValueOnce({
        platform: "douyin",
        sort: "hot",
        categories: ["建房预算"],
        items: [viralItem(13)],
        fetchedAt: "2026-09-07T12:00:00Z",
        hasMore: false,
        nextCursor: null,
        total: 13,
      });
    render(
      <StatefulViralPage
        value={{ ...base, data: { ...base.data, videos: [] } }}
      />,
    );

    const loadMore = await screen.findByRole("button", {
      name: "加载更多视频",
    });
    fireEvent.click(loadMore);
    fireEvent.keyDown(loadMore, { key: "Enter" });
    expect(listViralVideos).toHaveBeenCalledTimes(2);

    resolvePage?.({
      platform: "douyin",
      sort: "hot",
      categories: ["建房预算"],
      items: [],
      fetchedAt: "2026-09-07T12:00:00Z",
      hasMore: true,
      nextCursor: "cursor-2",
      total: 13,
    });
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "加载更多视频" }),
      ).toBeEnabled(),
    );
    fireEvent.click(screen.getByRole("button", { name: "加载更多视频" }));
    expect(await screen.findByText("加载更多失败，请重试")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "加载更多视频" }));

    expect(await screen.findByText("乡墅参考 13")).toBeInTheDocument();
    expect(listViralVideos).toHaveBeenLastCalledWith("douyin", "hot", {
      limit: 12,
      cursor: "cursor-2",
    });
  });

  it("游标失效时清除当前上下文快照并自动恢复首屏", async () => {
    const base = studio({ review: false });
    listViralVideos
      .mockResolvedValueOnce({
        platform: "douyin",
        sort: "hot",
        categories: ["建房预算"],
        items: Array.from({ length: 12 }, (_, index) => viralItem(index + 1)),
        fetchedAt: "2026-09-07T12:00:00Z",
        hasMore: true,
        nextCursor: "expired-cursor",
        total: 13,
      })
      .mockRejectedValueOnce(
        Object.assign(new Error("分页游标无效，请刷新列表"), {
          code: "VIRAL_CURSOR_INVALID",
        }),
      )
      .mockResolvedValueOnce({
        platform: "douyin",
        sort: "hot",
        categories: ["建房预算"],
        items: [viralItem(31, { title: "恢复后的首屏" })],
        fetchedAt: "2026-09-07T12:01:00Z",
        hasMore: false,
        nextCursor: null,
        total: 1,
      });
    render(
      <StatefulViralPage
        value={{ ...base, data: { ...base.data, videos: [] } }}
      />,
    );

    fireEvent.click(
      await screen.findByRole("button", { name: "加载更多视频" }),
    );

    expect(await screen.findByText("恢复后的首屏")).toBeInTheDocument();
    expect(listViralVideos).toHaveBeenNthCalledWith(2, "douyin", "hot", {
      limit: 12,
      cursor: "expired-cursor",
    });
    expect(listViralVideos).toHaveBeenNthCalledWith(3, "douyin", "hot", {
      limit: 12,
    });
    expect(listViralVideos).toHaveBeenCalledTimes(3);
  });

  it("切换分类会从首屏重置分页并丢弃旧分类的迟到结果", async () => {
    const base = studio({ review: false });
    let resolveOldPage:
      | ((value: Awaited<ReturnType<typeof listViralVideos>>) => void)
      | undefined;
    listViralVideos
      .mockResolvedValueOnce({
        platform: "douyin",
        sort: "hot",
        categories: ["建房预算", "庭院案例"],
        items: [
          viralItem(1, { category: "庭院案例" }),
          ...Array.from({ length: 11 }, (_, index) => viralItem(index + 2)),
        ],
        fetchedAt: "2026-09-07T12:00:00Z",
        hasMore: true,
        nextCursor: "cursor-2",
        total: 13,
      })
      .mockReturnValueOnce(
        new Promise((resolve) => {
          resolveOldPage = resolve;
        }),
      )
      .mockResolvedValueOnce({
        platform: "douyin",
        sort: "hot",
        categories: ["建房预算", "庭院案例"],
        items: [viralItem(21, { category: "庭院案例" })],
        fetchedAt: "2026-09-07T12:00:00Z",
        hasMore: false,
        nextCursor: null,
        total: 13,
      });
    render(
      <StatefulViralPage
        value={{ ...base, data: { ...base.data, videos: [] } }}
      />,
    );

    fireEvent.click(
      await screen.findByRole("button", { name: "加载更多视频" }),
    );
    fireEvent.click(screen.getByRole("button", { name: "庭院案例" }));

    await waitFor(() =>
      expect(listViralVideos).toHaveBeenLastCalledWith("douyin", "hot", {
        limit: 12,
      }),
    );
    resolveOldPage?.({
      platform: "douyin",
      sort: "hot",
      categories: ["建房预算", "庭院案例"],
      items: [viralItem(99, { category: "庭院案例" })],
      fetchedAt: "2026-09-07T12:00:00Z",
      hasMore: false,
      nextCursor: null,
      total: 13,
    });

    expect(await screen.findByText("乡墅参考 21")).toBeInTheDocument();
    expect(screen.queryByText("乡墅参考 99")).toBeNull();
  });

  it("切换平台后丢弃上一平台迟到的分页结果", async () => {
    const base = studio();
    const updateData = vi.fn();
    const item = {
      platform: "douyin" as const,
      videoId: "native-dy-1",
      category: "建房预算",
      title: "抖音首页视频",
      author: "作者",
      authorAvatar: null,
      verified: false,
      coverUrl: "/studio/demo.jpg",
      durationMs: 30_000,
      likes: 100,
      comments: 1,
      shares: 2,
      collects: 3,
      publishedAt: 1_788_600_000,
      publishedDisplay: null,
      likeDisplay: "100",
      tags: [],
      hasPlayableAudio: true,
      playUrl: null,
      isFavorite: false,
      availability: "available" as const,
    };
    let resolveOldPage:
      | ((value: {
          platform: "douyin";
          sort: "hot";
          categories: string[];
          items: Array<typeof item>;
          fetchedAt: string;
          hasMore: boolean;
          nextCursor: null;
          total: number;
        }) => void)
      | undefined;
    listViralVideos
      .mockResolvedValueOnce({
        platform: "douyin",
        sort: "hot",
        categories: ["建房预算"],
        items: [item],
        fetchedAt: "2026-09-07T12:00:00Z",
        hasMore: true,
        nextCursor: "cursor-2",
        total: 2,
      })
      .mockReturnValueOnce(
        new Promise((resolve) => {
          resolveOldPage = resolve;
        }),
      )
      .mockResolvedValueOnce({
        platform: "wechat_channels",
        sort: "hot",
        categories: [],
        items: [],
        fetchedAt: "2026-09-07T12:00:01Z",
        hasMore: false,
        nextCursor: null,
        total: 0,
      });
    useStudio.mockReturnValue(
      studio({
        review: false,
        data: { ...base.data, videos: [] },
        updateData,
      }),
    );
    render(<ViralPage />);

    const loadMore = await screen.findByRole("button", {
      name: "加载更多视频",
    });
    updateData.mockClear();
    fireEvent.click(loadMore);
    fireEvent.click(screen.getByRole("tab", { name: /视频号/ }));
    await waitFor(() =>
      expect(listViralVideos).toHaveBeenCalledWith("wechat_channels", "hot", {
        limit: 12,
      }),
    );

    resolveOldPage?.({
      platform: "douyin",
      sort: "hot",
      categories: ["建房预算"],
      items: [{ ...item, videoId: "native-dy-late", title: "迟到的抖音视频" }],
      fetchedAt: "2026-09-07T12:00:00Z",
      hasMore: false,
      nextCursor: null,
      total: 2,
    });
    await Promise.resolve();

    expect(updateData).toHaveBeenCalledTimes(1);
  });

  it("冷库刷新时保持页面可用并显示采集状态", async () => {
    const base = studio();
    listViralVideos.mockResolvedValue({
      platform: "douyin",
      sort: "hot",
      categories: [],
      items: [],
      fetchedAt: null,
      stale: true,
      refreshing: true,
      refreshError: null,
      hasMore: false,
      nextCursor: null,
      total: 0,
    });
    useStudio.mockReturnValue(
      studio({ review: false, data: { ...base.data, videos: [] } }),
    );

    render(<ViralPage />);

    expect(
      await screen.findAllByText("正在采集爆款视频，当前先展示已缓存内容…"),
    ).not.toHaveLength(0);
    expect(screen.getByText("暂无爆款视频")).toBeInTheDocument();
  });

  it("切换账号后立即清除旧收藏并忽略上一账号的迟到响应", async () => {
    let current = studio({
      review: false,
      user: { id: "user-a" } as StudioContextValue["user"],
    });
    useStudio.mockImplementation(() => current);
    let resolveUserA!: (value: {
      platform: "douyin";
      sort: "hot";
      categories: string[];
      items: ViralVideoItem[];
      fetchedAt: null;
      hasMore: false;
      nextCursor: null;
      total: number;
    }) => void;
    listViralVideos.mockReturnValueOnce(
      new Promise((resolve) => {
        resolveUserA = resolve;
      }),
    );
    const view = render(<ViralPage />);

    current = {
      ...current,
      user: { id: "user-b" } as StudioContextValue["user"],
    };
    listViralVideos.mockResolvedValueOnce({
      platform: "douyin",
      sort: "hot",
      categories: [],
      items: [viralItem(1, { isFavorite: false })],
      fetchedAt: null,
      hasMore: false,
      nextCursor: null,
      total: 1,
    });
    view.rerender(<ViralPage />);

    await waitFor(() => expect(listViralVideos).toHaveBeenCalledTimes(2));
    resolveUserA({
      platform: "douyin",
      sort: "hot",
      categories: [],
      items: [viralItem(1, { isFavorite: true })],
      fetchedAt: null,
      hasMore: false,
      nextCursor: null,
      total: 1,
    });
    await act(async () => Promise.resolve());

    expect(
      screen.getByRole("button", {
        name: "收藏 农村建房预算，别只盯着主体",
      }),
    ).toHaveAttribute("aria-pressed", "false");
  });

  it.each([
    ["全部", "成功"],
    ["全部", "失败"],
    ["收藏", "成功"],
    ["收藏", "失败"],
  ] as const)("%s深页在切换账号后忽略迟到%s", async (scope, outcome) => {
    let resolvePage!: (
      value: Awaited<ReturnType<typeof listViralVideos>>,
    ) => void;
    let rejectPage!: (reason: Error) => void;
    const deferred = new Promise<Awaited<ReturnType<typeof listViralVideos>>>(
      (resolve, reject) => {
        resolvePage = resolve;
        rejectPage = reject;
      },
    );
    const firstPage = {
      platform: "douyin" as const,
      sort: "hot" as const,
      categories: [],
      items: [viralItem(1, { isFavorite: scope === "收藏" })],
      fetchedAt: null,
      hasMore: true,
      nextCursor: "cursor-2",
      total: 2,
    };
    if (scope === "全部") {
      listViralVideos
        .mockResolvedValueOnce(firstPage)
        .mockReturnValueOnce(deferred)
        .mockReturnValueOnce(new Promise(() => undefined));
    } else {
      listViralFavorites
        .mockResolvedValueOnce(firstPage)
        .mockReturnValueOnce(deferred)
        .mockReturnValueOnce(new Promise(() => undefined));
    }
    const updateData = vi.fn();
    let current = studio({
      review: false,
      user: { id: "user-a" } as StudioContextValue["user"],
      updateData,
    });
    useStudio.mockImplementation(() => current);
    const view = render(<RenderWindowTrigger />);
    if (scope === "收藏") {
      fireEvent.click(screen.getByRole("tab", { name: "我的收藏" }));
    }
    fireEvent.click(
      await screen.findByRole("button", { name: "加载更多视频" }),
    );
    updateData.mockClear();

    current = {
      ...current,
      user: { id: "user-b" } as StudioContextValue["user"],
    };
    const settle = () => {
      if (outcome === "成功") {
        resolvePage({
          ...firstPage,
          items: [viralItem(2, { title: "旧账号迟到深页" })],
          hasMore: false,
          nextCursor: null,
        });
      } else {
        rejectPage(new Error("旧账号深页失败"));
      }
    };
    view.rerender(<RenderWindowTrigger onLayout={settle} />);
    await act(async () => Promise.resolve());

    expect(updateData).not.toHaveBeenCalled();
    expect(screen.queryByText("旧账号迟到深页")).toBeNull();
    expect(screen.queryByText("加载更多失败，请重试")).toBeNull();
  });

  it("新账号首屏尚未启动时不能沿用旧账号游标加载深页", async () => {
    listViralVideos
      .mockResolvedValueOnce({
        platform: "douyin",
        sort: "hot",
        categories: [],
        items: [viralItem(1)],
        fetchedAt: null,
        hasMore: true,
        nextCursor: "user-a-cursor",
        total: 2,
      })
      .mockReturnValueOnce(new Promise(() => undefined));
    let current = studio({
      review: false,
      user: { id: "user-a" } as StudioContextValue["user"],
    });
    useStudio.mockImplementation(() => current);
    const view = render(<RenderWindowTrigger />);
    await screen.findByRole("button", { name: "加载更多视频" });

    current = {
      ...current,
      user: { id: "user-b" } as StudioContextValue["user"],
    };
    view.rerender(
      <RenderWindowTrigger
        onLayout={() =>
          screen.getByRole("button", { name: "加载更多视频" }).click()
        }
      />,
    );

    expect(
      listViralVideos.mock.calls.filter(([, , options]) => options?.cursor),
    ).toHaveLength(0);
  });

  it("收藏使用持久化接口并在保存失败时回滚乐观状态", async () => {
    saveViralFavorite.mockRejectedValueOnce(new Error("收藏失败"));
    const value = studio({ review: false });
    useStudio.mockReturnValue(value);
    render(<ViralPage />);

    const button = screen.getByRole("button", {
      name: "收藏 农村建房预算，别只盯着主体",
    });
    fireEvent.click(button);
    expect(button).toHaveAttribute("aria-pressed", "true");
    await waitFor(() =>
      expect(button).toHaveAttribute("aria-pressed", "false"),
    );
    expect(saveViralFavorite).toHaveBeenCalledWith("douyin", "native-dy-1");
    expect(value.notify).toHaveBeenCalledWith("收藏失败，已恢复原状态");
  });

  it("取消收藏失败时恢复收藏状态且请求未完成期间不重复提交", async () => {
    let rejectRemoval!: (reason: Error) => void;
    removeViralFavorite.mockReturnValueOnce(
      new Promise((_resolve, reject) => {
        rejectRemoval = reject;
      }),
    );
    listViralVideos.mockResolvedValueOnce({
      platform: "douyin",
      sort: "hot",
      categories: [],
      items: [viralItem(1, { isFavorite: true })],
      fetchedAt: null,
      hasMore: false,
      nextCursor: null,
      total: 1,
    });
    const value = studio({ review: false });
    useStudio.mockReturnValue(value);
    render(<ViralPage />);

    const button = screen.getByRole("button", {
      name: "收藏 农村建房预算，别只盯着主体",
    });
    await waitFor(() => expect(button).toHaveAttribute("aria-pressed", "true"));
    fireEvent.click(button);
    fireEvent.click(button);
    expect(removeViralFavorite).toHaveBeenCalledTimes(1);
    expect(button).toHaveAttribute("aria-pressed", "false");

    rejectRemoval(new Error("取消失败"));
    await waitFor(() => expect(button).toHaveAttribute("aria-pressed", "true"));
    expect(value.notify).toHaveBeenCalledWith("收藏失败，已恢复原状态");
  });

  it("账号渲染完成但被动 effect 尚未执行时仍回滚新账号收藏失败", async () => {
    saveViralFavorite.mockImplementationOnce(() => {
      throw new Error("新账号收藏失败");
    });
    let current = studio({
      review: false,
      user: { id: "user-a" } as StudioContextValue["user"],
    });
    useStudio.mockImplementation(() => current);
    const view = render(<RenderWindowTrigger />);

    vi.mocked(current.patchState).mockClear();
    vi.mocked(current.notify).mockClear();
    current = {
      ...current,
      user: { id: "user-b" } as StudioContextValue["user"],
    };
    view.rerender(
      <RenderWindowTrigger
        onLayout={() =>
          screen
            .getByRole("button", {
              name: "收藏 农村建房预算，别只盯着主体",
            })
            .click()
        }
      />,
    );
    await act(async () => Promise.resolve());

    expect(
      screen.getByRole("button", {
        name: "收藏 农村建房预算，别只盯着主体",
      }),
    ).toHaveAttribute("aria-pressed", "false");
    expect(current.notify).toHaveBeenCalledWith("收藏失败，已恢复原状态");
  });

  it("A到B再回A时旧收藏结算保持静默且不会永久禁用", async () => {
    let rejectUserA!: (reason: Error) => void;
    saveViralFavorite.mockReturnValueOnce(
      new Promise((_resolve, reject) => {
        rejectUserA = reject;
      }),
    );
    const userA = studio({
      review: false,
      user: { id: "user-a" } as StudioContextValue["user"],
    });
    let current = userA;
    useStudio.mockImplementation(() => current);
    const onSavedChange = vi.fn();
    const view = render(
      <FavoriteRenderWindowTrigger onSavedChange={onSavedChange} />,
    );
    fireEvent.click(screen.getByRole("button", { name: /收藏/ }));
    expect(saveViralFavorite).toHaveBeenCalledOnce();

    onSavedChange.mockClear();
    vi.mocked(userA.patchState).mockClear();
    vi.mocked(userA.notify).mockClear();
    current = {
      ...userA,
      user: { id: "user-b" } as StudioContextValue["user"],
    };
    view.rerender(
      <FavoriteRenderWindowTrigger onSavedChange={onSavedChange} />,
    );
    current = userA;
    view.rerender(
      <FavoriteRenderWindowTrigger
        onSavedChange={onSavedChange}
        onLayout={() => rejectUserA(new Error("用户 A 的旧请求失败"))}
      />,
    );
    await act(async () => Promise.resolve());

    expect(screen.getByRole("button", { name: /收藏/ })).toBeEnabled();
    expect(onSavedChange).not.toHaveBeenCalled();
    expect(userA.patchState).not.toHaveBeenCalled();
    expect(userA.notify).not.toHaveBeenCalled();
  });

  it("收藏按钮卸载后旧请求结算不再回滚或通知", async () => {
    let rejectRequest!: (reason: Error) => void;
    saveViralFavorite.mockReturnValueOnce(
      new Promise((_resolve, reject) => {
        rejectRequest = reject;
      }),
    );
    const value = studio({ review: false });
    useStudio.mockReturnValue(value);
    const onSavedChange = vi.fn();
    const view = render(
      <FavoriteRenderWindowTrigger onSavedChange={onSavedChange} />,
    );
    fireEvent.click(screen.getByRole("button", { name: /收藏/ }));
    onSavedChange.mockClear();
    vi.mocked(value.patchState).mockClear();
    vi.mocked(value.notify).mockClear();

    view.unmount();
    rejectRequest(new Error("卸载后的旧请求失败"));
    await act(async () => Promise.resolve());

    expect(onSavedChange).not.toHaveBeenCalled();
    expect(value.patchState).not.toHaveBeenCalled();
    expect(value.notify).not.toHaveBeenCalled();
  });

  it("详情地址保留平台和视频 ID，刷新后可从单条接口恢复", async () => {
    const base = studio();
    const item = {
      platform: "douyin" as const,
      videoId: "native-dy-1",
      category: "建房预算",
      title: "刷新恢复的视频",
      author: "乡墅建房笔记",
      authorAvatar: null,
      verified: true,
      coverUrl: "/studio/demo.jpg",
      durationMs: 88_000,
      likes: 18_000,
      comments: 10,
      shares: 20,
      collects: 30,
      publishedAt: 1_788_600_000,
      publishedDisplay: null,
      likeDisplay: "1.8万",
      tags: [],
      hasPlayableAudio: true,
      playUrl: null,
      isFavorite: false,
      availability: "available" as const,
    };
    openViralVideoDetail.mockResolvedValue(
      viralDetailView(item, { charged: 2, deduped: false }),
    );
    window.history.replaceState(
      null,
      "",
      "/?viralPlatform=douyin&viralVideoId=native-dy-1#studio/viral-detail",
    );
    useStudio.mockReturnValue(
      studio({
        review: false,
        data: { ...base.data, videos: [] },
        state: { ...base.state, page: "viral-detail" },
      }),
    );
    render(<ViralDetailPage />);

    expect(screen.getByText("正在读取视频详情…")).toBeInTheDocument();
    expect(
      await screen.findByRole("heading", { name: "刷新恢复的视频" }),
    ).toBeInTheDocument();
    expect(openViralVideoDetail).toHaveBeenCalledWith("douyin", "native-dy-1");
    // 查看详情免费：即便旧服务端回执里带了扣费数字，页面也不再展示计费回执。
    expect(screen.queryByText(/查看详情扣除/)).not.toBeInTheDocument();
    expect(screen.queryByText(/积分/)).not.toBeInTheDocument();
  });

  it("已下架视频仍可查看详情，页面不出现任何计费回执", async () => {
    window.history.replaceState(
      null,
      "",
      "/?viralPlatform=douyin&viralVideoId=native-dy-1#studio/viral-detail",
    );
    const base = studio();
    openViralVideoDetail.mockResolvedValue(
      viralDetailView(
        viralItem(1, { title: "已下架的视频", availability: "hidden" }),
        {
          charged: 0,
          deduped: false,
          billable: false,
        },
      ),
    );
    useStudio.mockReturnValue(
      studio({
        review: false,
        data: { ...base.data, videos: [] },
        state: { ...base.state, page: "viral-detail" },
      }),
    );
    render(<ViralDetailPage />);

    expect(
      await screen.findByRole("heading", { name: "已下架的视频" }),
    ).toBeInTheDocument();
    expect(screen.queryByText(/未计费|扣积分|扣除/)).not.toBeInTheDocument();
  });

  it("查看详情失败时不展示计费回执，只提示失败原因", async () => {
    window.history.replaceState(
      null,
      "",
      "/?viralPlatform=douyin&viralVideoId=native-dy-1#studio/viral-detail",
    );
    const base = studio();
    // 失败即未完成计费：服务端同事务回滚，客户端不能留下「已扣费」的错觉。
    openViralVideoDetail.mockRejectedValue(new Error("视频详情暂不可用"));
    useStudio.mockReturnValue(
      studio({
        review: false,
        data: base.data,
        // 列表里已有条目：失败后仍可阅读免费字段，只是不给计费回执。
        state: { ...base.state, page: "viral-detail", selectedVideoId: "dy-1" },
      }),
    );
    render(<ViralDetailPage />);

    expect(await screen.findByText("视频详情暂不可用")).toBeInTheDocument();
    expect(screen.queryByText(/本次查看详情扣除/)).not.toBeInTheDocument();
    expect(screen.queryByText(/本次查看不再扣积分/)).not.toBeInTheDocument();
  });

  it("查看详情免费后不再有积分不足分支：失败只透传原因，不打开钱包", async () => {
    window.history.replaceState(
      null,
      "",
      "/?viralPlatform=douyin&viralVideoId=native-dy-1#studio/viral-detail",
    );
    const base = studio();
    const insufficient = Object.assign(new Error("积分不足"), {
      code: "INSUFFICIENT_CREDITS",
    });
    openViralVideoDetail.mockRejectedValue(insufficient);
    const value = studio({
      review: false,
      data: base.data,
      state: { ...base.state, page: "viral-detail", selectedVideoId: "dy-1" },
    });
    useStudio.mockReturnValue(value);
    render(<ViralDetailPage />);

    // 2026-10-06 拍板查看免费：这个分支理论上已不可达；若仍收到该错误，
    // 与普通失败同一处理——展示原因，不打开钱包。
    expect(await screen.findByText("积分不足")).toBeInTheDocument();
    expect(value.openLive).not.toHaveBeenCalled();
  });

  it("详情页切换账号后重新读取当前账号的收藏状态", async () => {
    window.history.replaceState(
      null,
      "",
      "/?viralPlatform=douyin&viralVideoId=native-dy-1#studio/viral-detail",
    );
    const initial = studio();
    let current = studio({
      review: false,
      user: { id: "user-a" } as StudioContextValue["user"],
      state: {
        ...initial.state,
        page: "viral-detail",
        selectedVideoId: "dy-1",
      },
    });
    useStudio.mockImplementation(() => current);
    openViralVideoDetail
      .mockResolvedValueOnce(
        viralDetailView(
          viralItem(1, {
            title: "农村建房预算，别只盯着主体",
            isFavorite: true,
          }),
        ),
      )
      .mockResolvedValueOnce(
        viralDetailView(
          viralItem(1, {
            title: "农村建房预算，别只盯着主体",
            isFavorite: false,
          }),
        ),
      );
    const view = render(<ViralDetailPage />);

    const button = screen.getByRole("button", {
      name: "收藏 农村建房预算，别只盯着主体",
    });
    await waitFor(() => expect(button).toHaveAttribute("aria-pressed", "true"));
    current = {
      ...current,
      user: { id: "user-b" } as StudioContextValue["user"],
    };
    view.rerender(<ViralDetailPage />);

    await waitFor(() => expect(openViralVideoDetail).toHaveBeenCalledTimes(2));
    await waitFor(() =>
      expect(button).toHaveAttribute("aria-pressed", "false"),
    );
  });

  it("详情页账号已渲染时同步拒绝旧账号迟到成功", async () => {
    window.history.replaceState(
      null,
      "",
      "/?viralPlatform=douyin&viralVideoId=native-dy-1#studio/viral-detail",
    );
    let resolveUserA!: (value: ViralDetailViewResponse) => void;
    openViralVideoDetail
      .mockReturnValueOnce({
        // biome-ignore lint/suspicious/noThenProperty: 同步 thenable 用于复现 render 到 passive effect 之间的响应窗口。
        then(resolve: (value: ViralDetailViewResponse) => void) {
          resolveUserA = resolve;
          return { catch: () => undefined };
        },
      })
      .mockReturnValueOnce(new Promise(() => undefined));
    const initial = studio();
    let current = studio({
      review: false,
      user: { id: "user-a" } as StudioContextValue["user"],
      data: { ...initial.data, videos: [] },
      state: { ...initial.state, page: "viral-detail" },
    });
    useStudio.mockImplementation(() => current);
    const view = render(<DetailRenderWindowTrigger />);
    await waitFor(() => expect(openViralVideoDetail).toHaveBeenCalledTimes(1));

    vi.mocked(current.updateData).mockClear();
    current = {
      ...current,
      user: { id: "user-b" } as StudioContextValue["user"],
    };
    view.rerender(
      <DetailRenderWindowTrigger
        onLayout={() =>
          resolveUserA(
            viralDetailView(viralItem(1, { title: "用户 A 的迟到详情" })),
          )
        }
      />,
    );

    expect(current.updateData).not.toHaveBeenCalled();
    expect(screen.queryByText("用户 A 的迟到详情")).toBeNull();
  });

  it("非审核工作区不把示例三十条当作真实采集数据", () => {
    useStudio.mockReturnValue(
      studio({ review: false, data: { ...studio().data, videos: [] } }),
    );
    render(<ViralPage />);
    expect(screen.getByRole("tab", { name: "抖音 0" })).toBeInTheDocument();
    expect(screen.getByText("暂无爆款视频")).toBeInTheDocument();
    expect(
      screen.getByText("暂无已发布且可用的爆款视频，发布后自动展示。"),
    ).toBeInTheDocument();
    expect(
      screen.queryByText("采集参数仅在管理后台配置。"),
    ).not.toBeInTheDocument();
  });

  it("审核示例发布草稿独立保存，不覆盖口播脚本", () => {
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
    expect(
      screen.getByRole("button", { name: "移除标签 建房避坑" }),
    ).toBeInTheDocument();
    expect(screen.getByLabelText("添加标签")).toHaveAttribute(
      "placeholder",
      "输入标签后按 Enter",
    );
    fireEvent.change(screen.getByLabelText("添加标签"), {
      target: { value: "建房避坑" },
    });
    fireEvent.keyDown(screen.getByLabelText("添加标签"), { key: "Enter" });
    expect(
      screen.getAllByRole("button", { name: "移除标签 建房避坑" }),
    ).toHaveLength(1);
    fireEvent.click(screen.getByRole("button", { name: "保存草稿" }));
    expect(value.saveDraft).not.toHaveBeenCalled();
    expect(value.state.draft.script.text).toBe("口播终稿不得被发布表单覆盖");
    expect(
      screen.getByText("已保存审核示例草稿，未同步到云端。"),
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
    expect(
      screen.getByRole("button", { name: "移除标签 农村自建房" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "移除标签 建房避坑" }),
    ).toBeInTheDocument();
    expect(value.state.draft.script.text).toBe("口播终稿不得被发布表单覆盖");
    // Review mode never submits a real delivery.
    expect(screen.getByRole("button", { name: "立即发布" })).toBeDisabled();
  });

  it("发布草稿使用云端版本保存，失败保留表单且不提示成功", async () => {
    const base = studio();
    const value = studio({
      review: false,
      state: { ...base.state, selectedAssetId: "ready-video" },
      data: {
        ...base.data,
        tasks: [],
        assets: [
          {
            id: "ready-video",
            assetId: "ready-video",
            name: "成片",
            kind: "video",
            group: "成片",
            source: "任务中心",
            saved: true,
          },
        ],
      },
    });
    getStudioDraft.mockResolvedValueOnce({
      revision: 7,
      payload: { drafts: [] },
    });
    saveStudioDraft.mockRejectedValueOnce(
      new Error("云端草稿已在其他窗口更新"),
    );
    useStudio.mockReturnValue(value);
    render(<PublishPage />);
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "保存草稿" })).toBeEnabled(),
    );
    fireEvent.change(screen.getByLabelText("发布标题"), {
      target: { value: "新标题" },
    });
    fireEvent.click(screen.getByRole("button", { name: "保存草稿" }));
    await screen.findByText("云端草稿已在其他窗口更新");
    expect(saveStudioDraft).toHaveBeenCalledWith(
      "publishing",
      {
        drafts: [
          expect.objectContaining({ title: "新标题", assetId: "ready-video" }),
        ],
      },
      false,
      7,
    );
    expect(screen.getByLabelText("发布标题")).toHaveValue("新标题");
    expect(screen.queryByText("已保存到云端，可在刷新后继续编辑。")).toBeNull();
    saveStudioDraft.mockResolvedValueOnce({ revision: 8 });
    fireEvent.click(screen.getByRole("button", { name: "保存草稿" }));
    await screen.findByText("已保存到云端，可在刷新后继续编辑。");
    expect(value.patchState).toHaveBeenLastCalledWith({
      publishDrafts: [expect.objectContaining({ title: "新标题" })],
    });
  });

  it.each([
    [true, true],
    [false, true],
    [true, false],
    [false, false],
  ])(
    "历史发布成片按素材接口确认权限（可访问：%s，已有任务：%s）",
    async (available, hasRecentTask) => {
      const base = studio();
      const value = studio({
        review: false,
        state: { ...base.state, selectedAssetId: "historical-video" },
        data: {
          ...base.data,
          tasks: hasRecentTask
            ? [
                {
                  id: "new-task",
                  title: "近期成片",
                  type: "数字人口播",
                  status: "completed",
                  submitted: "今天",
                  resultId: "new-video",
                },
              ]
            : [],
          assets: [
            {
              id: "historical-video",
              name: "历史成片",
              kind: "video",
              group: "成片",
              source: "任务中心",
              saved: true,
              allowedActions: ["download"],
            },
          ],
        },
      });
      getStudioDraft.mockResolvedValueOnce({
        revision: 3,
        payload: {
          drafts: [
            {
              id: "old-draft",
              assetId: "historical-video",
              coverId: "asset:historical-cover",
              title: "旧发布草稿",
              description: "正文",
              account: "",
              platform: "抖音",
              tags: [],
            },
          ],
        },
      });
      resolveMaterials.mockResolvedValueOnce({
        items: available
          ? [
              material("historical-video", {
                media_type: "video",
                content_type: "video/mp4",
              }),
            ]
          : [],
      });
      useStudio.mockReturnValue(value);
      render(<PublishPage />);
      await waitFor(() =>
        expect(value.patchState).toHaveBeenCalledWith({
          publishDrafts: [expect.objectContaining({ id: "old-draft" })],
        }),
      );
      expect(resolveMaterials).toHaveBeenCalledWith([
        "asset:historical-video",
        "asset:historical-cover",
      ]);
      if (available) {
        const update = vi.mocked(value.updateData).mock.calls.at(-1)?.[0];
        const next = (update as (data: StudioData) => StudioData)(value.data);
        expect(
          next.assets.find((asset) => asset.id === "historical-video")?.url,
        ).toBe("https://storage.test/material");
      }
      const save = screen.getByRole("button", { name: "保存草稿" });
      if (available) expect(save).toBeEnabled();
      else expect(save).toBeDisabled();
    },
  );

  it("正式模式不伪造已发布数量或已连接账号", async () => {
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
      screen.getByText(
        "选择成片与账号，立即发布或定时发布；发布结果在下方记录中回收",
      ),
    ).toBeInTheDocument();
    expect(screen.getByText("草稿保存在云端，可反复编辑")).toBeInTheDocument();
    expect(screen.queryByText(/待发布\s*0/)).not.toBeInTheDocument();
    expect(screen.queryByText(/已发布\s*0/)).not.toBeInTheDocument();
    await screen.findByText("尚未连接该平台账号，请先扫码连接");
    await screen.findByText("暂无发布记录");
    expect(screen.getByRole("button", { name: "立即发布" })).toBeDisabled();
    fireEvent.click(
      screen.getByRole("button", { name: "前往用户档案管理账号" }),
    );
    expect(value.navigate).toHaveBeenCalledWith("profile");
  });

  it("选择服务端账号后可立即发布或按时区换算的定时发布", async () => {
    createPublishRecord.mockReset();
    const base = studio();
    const value = studio({
      review: false,
      state: { ...base.state, selectedAssetId: "ready-video" },
      data: {
        ...base.data,
        tasks: [],
        assets: [
          {
            id: "ready-video",
            assetId: "ready-video",
            materialId: "asset:ready-video",
            name: "成片",
            kind: "video",
            group: "成片",
            source: "任务中心",
            saved: true,
          },
        ],
      },
    });
    publishAccounts.listCloudPublishAccounts.mockResolvedValue([
      {
        id: "cloud-douyin",
        platform: "douyin",
        platform_user_id: "uid-1",
        username: "张工说乡墅",
        verified_at: 1,
        status: "connected",
        error_message: null,
        source: "cloud",
      },
      {
        id: "cloud-dead",
        platform: "douyin",
        platform_user_id: "uid-2",
        username: "失效账号",
        verified_at: 1,
        status: "invalid",
        error_message: "登录过期",
        source: "desktop",
      },
    ]);
    getStudioDraft.mockResolvedValueOnce({
      revision: 3,
      payload: { drafts: [] },
    });
    createPublishRecord.mockResolvedValue({
      id: "rec-1",
      platform: "douyin",
      scheduled_at: null,
      status: "queued",
    });
    useStudio.mockReturnValue(value);
    render(<PublishPage />);
    const select = await screen.findByLabelText("选择发布账号");
    fireEvent.change(select, { target: { value: "cloud-dead" } });
    expect(
      screen.getByText("该账号登录态已失效，请在用户档案中重新扫码后再发布。"),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "立即发布" })).toBeDisabled();
    fireEvent.change(select, { target: { value: "cloud-douyin" } });
    fireEvent.change(screen.getByLabelText("发布标题"), {
      target: { value: "立即发的标题" },
    });
    fireEvent.click(screen.getByRole("button", { name: "立即发布" }));
    await waitFor(() =>
      expect(createPublishRecord).toHaveBeenCalledWith({
        account_id: "cloud-douyin",
        video_material_id: "asset:ready-video",
        cover_material_id: null,
        title: "立即发的标题",
        description: "",
        tags: [],
        scheduled_at: null,
      }),
    );
    expect(value.notify).toHaveBeenCalledWith("已提交发布 · 抖音");
    expect(listPublishRecords).toHaveBeenCalled();

    // Scheduled: the datetime-local value is local wall-clock time → ISO instant.
    fireEvent.click(screen.getByRole("button", { name: "定时" }));
    const timeInput = screen.getByLabelText("定时发布时间");
    fireEvent.change(timeInput, { target: { value: "2020-01-01T10:00" } });
    fireEvent.click(screen.getByRole("button", { name: "定时发布" }));
    await screen.findByText("定时发布至少需要提前 3 分钟。");
    expect(createPublishRecord).toHaveBeenCalledTimes(1);
    const future = new Date(Date.now() + 3 * 60 * 60 * 1000);
    future.setSeconds(0, 0);
    const pad = (n: number) => String(n).padStart(2, "0");
    const local = `${future.getFullYear()}-${pad(future.getMonth() + 1)}-${pad(future.getDate())}T${pad(future.getHours())}:${pad(future.getMinutes())}`;
    fireEvent.change(timeInput, { target: { value: local } });
    createPublishRecord.mockResolvedValueOnce({
      id: "rec-2",
      platform: "douyin",
      scheduled_at: future.toISOString(),
      status: "queued",
    });
    fireEvent.click(screen.getByRole("button", { name: "定时发布" }));
    await waitFor(() => expect(createPublishRecord).toHaveBeenCalledTimes(2));
    expect(createPublishRecord.mock.calls[1][0].scheduled_at).toBe(
      future.toISOString(),
    );
    expect(value.notify).toHaveBeenCalledWith("已加入定时发布队列 · 抖音");
  });

  it("小红书自动发布暂缓：选择时弹出正在开发中且不切换平台", async () => {
    createPublishRecord.mockReset();
    const base = studio();
    const value = studio({
      review: false,
      state: { ...base.state, selectedAssetId: "ready-video" },
      data: {
        ...base.data,
        tasks: [],
        assets: [
          {
            id: "ready-video",
            assetId: "ready-video",
            name: "成片",
            kind: "video",
            group: "成片",
            source: "任务中心",
            saved: true,
          },
        ],
      },
    });
    publishAccounts.listCloudPublishAccounts.mockResolvedValue([
      {
        id: "cloud-xhs",
        platform: "xiaohongshu",
        platform_user_id: "uid-x",
        username: "小红书号",
        verified_at: 1,
        status: "connected",
        error_message: null,
        source: "cloud",
      },
    ]);
    getStudioDraft.mockResolvedValueOnce({
      revision: 1,
      payload: { drafts: [] },
    });
    useStudio.mockReturnValue(value);
    render(<PublishPage />);
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "保存草稿" })).toBeEnabled(),
    );
    const xhsButton = screen.getByRole("button", { name: "小红书" });
    fireEvent.click(xhsButton);
    expect(value.notify).toHaveBeenCalledWith(
      "小红书自动发布正在开发中，敬请期待。",
    );
    // 平台停留在抖音：小红书按钮未激活，也不渲染小红书账号，不发起提交。
    expect(xhsButton).toHaveAttribute("aria-pressed", "false");
    expect(screen.getByRole("button", { name: "抖音" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    expect(screen.queryByText("小红书号")).toBeNull();
    expect(createPublishRecord).not.toHaveBeenCalled();
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

describe("全自动复刻入口（M8 v1）", () => {
  it("详情页渲染入口按钮，点击后校验人物并调用发起接口", async () => {
    const nativeId = "replica-native-1";
    const base = studio();
    const value = studio({
      review: false,
      state: { ...base.state, selectedVideoId: `douyin-${nativeId}` },
      data: {
        ...base.data,
        videos: [
          {
            id: `douyin-${nativeId}`,
            title: "全自动复刻样例",
            author: "乡墅作者",
            platform: "抖音",
            category: "庭院案例",
            poster: "",
            duration: "00:08",
            durationMs: 8_000,
            likes: 123,
            comments: 4,
            shares: 5,
            collections: 6,
            description: "",
            platformKey: "douyin",
            nativeId,
          },
        ],
      },
    });
    openViralVideoDetail.mockResolvedValue(
      viralDetailView({
        platform: "douyin",
        videoId: nativeId,
        category: "庭院案例",
        title: "全自动复刻样例",
        author: "乡墅作者",
        authorAvatar: null,
        verified: false,
        coverUrl: null,
        durationMs: 8_000,
        likes: 123,
        comments: 4,
        shares: 5,
        collects: 6,
        publishedAt: 1_788_600_000,
        publishedDisplay: "1天前",
        likeDisplay: "123",
        tags: ["庭院"],
        hasPlayableAudio: true,
        playUrl: null,
        isFavorite: false,
        availability: "available",
      }),
    );
    vi.mocked(listReplicaPipelineCharacters).mockResolvedValue({
      characters: [
        { characterVersionId: "cv-1", name: "主角", versionNumber: 1 },
      ],
    });
    vi.mocked(initiateReplicaPipeline).mockResolvedValue({
      id: "rplc-1",
      projectId: "p-1",
      status: "ENQUEUED",
      replay: false,
    });
    // 预处理费用披露确认（评审 M8-r1#1）：用户点「确定」后才发起。
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(true);
    useStudio.mockReturnValue(value);
    const view = render(<ViralDetailPage />);
    const entry = await screen.findByRole("button", { name: "全自动复刻" });
    fireEvent.click(entry);
    expect(confirmSpy).toHaveBeenCalledOnce();
    confirmSpy.mockRestore();
    await waitFor(() => expect(initiateReplicaPipeline).toHaveBeenCalledOnce());
    expect(vi.mocked(initiateReplicaPipeline).mock.calls[0]?.[0]).toMatchObject(
      {
        platform: "douyin",
        videoId: nativeId,
        characterVersionId: "cv-1",
      },
    );
    view.unmount();
  });
});
