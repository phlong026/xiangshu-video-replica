import type { RefObject } from "react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type {
  TrainingMaterialPublicItem,
  TrainingMaterialPublicPage,
  ViralImportPurpose,
  ViralImportTask,
  ViralPlatform,
  ViralVideoItem,
} from "../api";
import {
  clearMaterialCache,
  createGenerationTaskPreviewUrl,
  createMaterialUploadIntent,
  createPublishRecord,
  createViralImportTask,
  downloadMaterialAsset,
  downloadVideoToFile,
  evictMaterialCachedPreview,
  fetchViralVideoMedia,
  fetchViralVideoSource,
  fetchViralVideoStatistics,
  getAssetDownloadUrl,
  getMaterialBatchPreviews,
  getMaterialCachedPreview,
  getMaterialCacheUsage,
  getStudioDraft,
  getViralImportTask,
  listTrainingMaterials,
  listViralFavorites,
  listViralVideos,
  newViralSearchKey,
  openViralVideoDetail,
  putMaterial,
  refreshViralVideoResource,
  refreshViralVideoStatistics,
  removeViralFavorite,
  resolveMaterials,
  saveStudioDraft,
  saveViralFavorite,
  trainingMaterialDownloadUrl,
} from "../api";
import {
  isInsufficientCredits,
  openWalletIfInsufficientCredits,
} from "../insufficientCredits";
import { VideoPreview } from "../VideoPreview";
import { CharacterMaterialViews } from "./CharacterMaterialViews";
import { useStudio } from "./context";
import {
  readAudioDuration,
  studioAssetFromMaterial,
  studioVideoFromViral,
} from "./live";
import {
  type CloudPublishAccount,
  canUseLocalPublishAccounts,
  type LocalPublishAccount,
  listCloudPublishAccounts,
  listLocalPublishAccounts,
  onPublishAccountsChanged,
  openLocalPublishAccount,
} from "./localPublishAccounts";
import { AccountAvatar, PlatformLogo } from "./PlatformLogo";
import { PublishRecordsPanel } from "./PublishRecordsPanel";
import { taskHasDeliverable } from "./taskResults";
import type {
  StudioAsset,
  StudioContextValue,
  StudioPublishDraft,
  StudioVideo,
} from "./types";
import {
  Button,
  Empty,
  Field,
  formatTaskTime,
  Hint,
  Icon,
  Media,
  Panel,
  Tabs,
} from "./ui";
import {
  awaitViralCacheReady,
  CACHE_BADGE_LABELS,
  cacheAvailable,
  cacheProgressRatio,
  clearViralCache,
  enforceViralCacheLimit,
  ensureViralCacheForVideo,
  formatCacheBytes,
  listViralCache,
  openViralCacheFolder,
  reclaimViralCache,
  useViralAutoCache,
  useViralCacheProgress,
  type ViralCachedItem,
  type ViralCacheProgress,
  viralCacheKey,
  viralCacheLocalUrl,
} from "./viralCache";
import {
  clearViralImportIdempotencyKey,
  shouldClearViralImportIdempotencyKey,
  VIRAL_IMPORT_POLLING_TIMEOUT_MESSAGE,
  ViralImportPollingTimeoutError,
  viralImportIdempotencyKey,
} from "./viralImport";
import "./content.css";

const pageSize = 24;
const categoryTabs = ["全部", "建房预算", "户型设计", "施工避坑", "庭院案例"];

/** MATERIAL-PERF-A（P0-1）：分页按钮窗口化——始终含首末页与当前页 ±1，
 * 其余以「…」折叠，页数多时不再渲染整排页码按钮。 */
export function visiblePageButtons(
  page: number,
  pages: number,
): (number | "…")[] {
  if (pages <= 7) {
    return Array.from({ length: pages }, (_, index) => index + 1);
  }
  const wanted = new Set<number>(
    [1, pages, page - 1, page, page + 1].filter(
      (value) => value >= 1 && value <= pages,
    ),
  );
  const ordered = [...wanted].sort((a, b) => a - b);
  const entries: (number | "…")[] = [];
  let previous = 0;
  for (const value of ordered) {
    if (previous && value - previous > 1) entries.push("…");
    entries.push(value);
    previous = value;
  }
  return entries;
}

function formatCount(value: number | null) {
  if (value === null) return "—";
  return value >= 10000
    ? `${(value / 10000).toFixed(1).replace(".0", "")}万`
    : value.toLocaleString("zh-CN");
}

function assetKindLabel(kind: StudioAsset["kind"]) {
  return kind === "image" ? "图片" : kind === "video" ? "视频" : "音频";
}

const viralInitialCount = 12;
const viralPageSize = 12;

function viralIdentity(video: StudioVideo) {
  return video.platformKey && video.nativeId
    ? `${video.platformKey}:${video.nativeId}`
    : video.id;
}

function apiErrorCode(error: unknown) {
  if (!error || typeof error !== "object" || !("code" in error)) return;
  return typeof error.code === "string" ? error.code : undefined;
}

function viralDetailParams():
  | { platform: ViralPlatform; videoId: string }
  | undefined {
  const params = new URLSearchParams(window.location.search);
  const platform = params.get("viralPlatform");
  const videoId = params.get("viralVideoId")?.trim();
  if (
    (platform === "douyin" ||
      platform === "wechat_channels" ||
      platform === "xiaohongshu") &&
    videoId
  ) {
    return { platform, videoId };
  }
  return undefined;
}

function persistViralDetailUrl(video: StudioVideo) {
  if (!video.platformKey || !video.nativeId) return;
  const url = new URL(window.location.href);
  url.searchParams.set("viralPlatform", video.platformKey);
  url.searchParams.set("viralVideoId", video.nativeId);
  window.history.replaceState(null, "", url);
}

function viralLikesLabel(video: StudioVideo) {
  return video.likes == null
    ? "待核验"
    : (video.likeDisplay ?? formatCount(video.likes));
}

function viralMetricLabel(value: number | null | undefined) {
  return value == null ? "待核验" : formatCount(value);
}

function ViralStatisticsNote({ video }: { video: StudioVideo }) {
  const status = video.statisticsStatus;
  const source =
    video.statisticsSource === "platform_search"
      ? "平台搜索"
      : video.statisticsSource === "platform_detail"
        ? "互动核验"
        : "待核验";
  if (!video.statisticsUpdatedAt && !video.statisticsCheckedAt && !status)
    return null;
  return (
    <Hint>
      统计来源：{source} · 统计时间：{video.statisticsUpdatedAt ?? "待核验"}
      {video.statisticsCheckedAt
        ? ` · 最近核验：${video.statisticsCheckedAt}`
        : ""}
      {status === "partial"
        ? " · 本次部分指标未更新，保留已有数据"
        : status === "failed"
          ? " · 本次更新失败，保留已有数据"
          : status === "unknown"
            ? " · 指标待核验"
            : ""}
    </Hint>
  );
}

function viralPublishLabel(video: StudioVideo) {
  if (video.publishedDisplay) return video.publishedDisplay;
  if (video.publishedAt) {
    return new Date(video.publishedAt * 1000).toLocaleDateString("zh-CN", {
      month: "numeric",
      day: "numeric",
    });
  }
  return "";
}

function ViralPoster({
  video,
  className,
}: {
  video: StudioVideo;
  className?: string;
}) {
  return (
    <VideoPreview
      poster={video.poster}
      className={className}
      fallback={<Icon name="video" />}
    />
  );
}

type ViralPlayback =
  | { status: "idle" }
  | { status: "loading" }
  | { status: "playing"; src: string }
  | { status: "error"; message: string };

function updateViralStatistics(
  updateData: StudioContextValue["updateData"],
  items: ViralVideoItem[],
) {
  if (!items.length) return;
  const byId = new Map(items.map((item) => [item.videoId, item]));
  updateData((data) => ({
    ...data,
    videos: data.videos.map((video) => {
      const item = video.nativeId ? byId.get(video.nativeId) : undefined;
      return item && video.platformKey === item.platform
        ? {
            ...video,
            likes: item.likes ?? video.likes,
            comments: item.comments ?? video.comments,
            shares: item.shares ?? video.shares,
            collections: item.collects ?? video.collections,
            likeDisplay:
              item.likes == null ? video.likeDisplay : item.likeDisplay,
            statisticsUpdatedAt:
              item.statisticsUpdatedAt ?? video.statisticsUpdatedAt,
            statisticsCheckedAt: item.statisticsCheckedAt,
            statisticsStatus: item.statisticsStatus,
            statisticsSource: item.statisticsSource,
          }
        : video;
    }),
  }));
}

/** 缺互动统计的视频号条目：评论 / 转发 / 收藏三项补采字段全空（与服务端的完整性判据同口径）。 */
function viralStatisticsMissing(video: StudioVideo) {
  return (
    video.platformKey === "wechat_channels" &&
    Boolean(video.nativeId) &&
    video.comments == null &&
    video.shares == null &&
    video.collections == null
  );
}

type ViralStatisticsHealing = {
  /** 本轮要补全的条数（点「补全统计」那一刻的缺口）。 */
  total: number;
  /** 已处理（回填成功或失败）的条数。 */
  done: number;
};

type ViralStatisticsState = {
  error?: string;
  /** 当前这一页里缺互动统计的视频号条数；为 0 时自愈入口不出现。 */
  missingCount: number;
  /** 补全进行中的进度；未在补全时为 undefined。 */
  healing?: ViralStatisticsHealing;
  /** 用户已忽略当前这批缺口（缺口集合一变就重新出现）。 */
  dismissed: boolean;
  heal: () => void;
  cancel: () => void;
  dismiss: () => void;
};

function useViralStatistics(
  videos: StudioVideo[],
  enabled: boolean,
): ViralStatisticsState {
  const { updateData, openLive } = useStudio();
  const attemptedIds = useRef(new Set<string>());
  const [error, setError] = useState<string>();
  const [healing, setHealing] = useState<ViralStatisticsHealing>();
  // 忽略按「缺哪几条」记账：翻页 / 新条目让缺口集合一变，自愈入口就该重新出现。
  const [dismissedKey, setDismissedKey] = useState("");
  // 补全轮次序号：取消、卸载、重新发起都靠它让上一轮在下一个检查点自行退出。
  const healRunRef = useRef(0);
  const pendingIds = videos
    .filter(
      (video) =>
        video.platformKey === "wechat_channels" &&
        video.nativeId &&
        !attemptedIds.current.has(video.nativeId),
    )
    .map((video) => video.nativeId as string)
    .slice(0, 12);
  const pendingKey = pendingIds.join(",");

  useEffect(() => {
    if (!enabled || !pendingKey) return;
    const ids = pendingKey.split(",");
    ids.forEach((id) => {
      attemptedIds.current.add(id);
    });
    setError(undefined);
    void fetchViralVideoStatistics(ids)
      .then((result) => updateViralStatistics(updateData, result.items))
      .catch(() => setError("部分视频统计暂时无法更新"));
  }, [enabled, pendingKey, updateData]);

  const missingIds = videos
    .filter(viralStatisticsMissing)
    .map((video) => video.nativeId as string);
  const missingKey = missingIds.join(",");
  const missingIdsRef = useRef(missingIds);
  missingIdsRef.current = missingIds;

  useEffect(() => {
    return () => {
      // 卸载后旧轮次可能还在飞：序号作废，它回来时不再写状态。
      healRunRef.current += 1;
    };
  }, []);

  const cancelHeal = () => {
    healRunRef.current += 1;
    setHealing(undefined);
  };

  const heal = () => {
    const ids = missingIdsRef.current;
    if (!ids.length) return;
    const run = ++healRunRef.current;
    setError(undefined);
    setDismissedKey("");
    setHealing({ total: ids.length, done: 0 });
    void (async () => {
      let done = 0;
      for (const id of ids) {
        if (healRunRef.current !== run) return;
        try {
          // 逐条补采：服务端按「实际外呼条数」结算，逐条请求让进度与回填都是增量的
          // （哪条先返回就先填哪条），「取消」也停在真实的计费边界上——已发出的
          // 那一条照常结算，后面没发出去的一条不花钱。
          const result = await refreshViralVideoStatistics(
            [id],
            newViralSearchKey(),
          );
          if (healRunRef.current !== run) return;
          updateViralStatistics(updateData, result.items);
          attemptedIds.current.add(id);
        } catch (cause) {
          if (healRunRef.current !== run) return;
          if (isInsufficientCredits(cause)) {
            setError("余额不足，无法补全互动统计");
            openLive("wallet");
          } else {
            setError("互动统计补全中断，请稍后重试");
          }
          break;
        }
        done += 1;
        setHealing({ total: ids.length, done });
      }
      if (healRunRef.current === run) setHealing(undefined);
    })();
  };

  return {
    error,
    missingCount: missingIds.length,
    healing,
    dismissed: dismissedKey !== "" && dismissedKey === missingKey,
    heal,
    cancel: cancelHeal,
    dismiss: () => setDismissedKey(missingKey),
  };
}

/** 点击播放：真实平台视频先走媒体管线，测试夹具可直接使用 playUrl。 */
function useViralPlayback(
  video?: StudioVideo,
  active = true,
  onActivate?: () => void,
) {
  const [playback, setPlayback] = useState<ViralPlayback>({ status: "idle" });
  const activeRef = useRef(active);
  const requestIdRef = useRef(0);
  const loadingRef = useRef(false);
  const activate = () => {
    activeRef.current = true;
    onActivate?.();
  };

  useEffect(() => {
    activeRef.current = active;
    if (!active) {
      requestIdRef.current += 1;
      loadingRef.current = false;
      setPlayback((current) =>
        current.status === "loading" ? { status: "idle" } : current,
      );
    }
  }, [active]);

  const playFromStorage = async () => {
    if (!video?.platformKey || !video.nativeId || loadingRef.current) return;
    activate();
    const requestId = ++requestIdRef.current;
    loadingRef.current = true;
    setPlayback({ status: "loading" });
    try {
      // §6.3：只播**本地已缓存**的视频，服务器不再参与播放。
      const localUrl = await viralCacheLocalUrl(
        video.platformKey,
        video.nativeId,
      );
      if (requestId !== requestIdRef.current || !activeRef.current) return;
      if (localUrl) {
        setPlayback({ status: "playing", src: localUrl });
        return;
      }
      // 未命中本地缓存：后台照常把缓存踢起来（二次播放/离线更快，数据源是
      // 云端归档不计费），同时直接向服务端要签名地址流式播放——人工推送
      // 模式下已上架的素材必然已归档，播放不再以本地缓存为前提。
      void ensureViralCacheForVideo(video).catch(() => {
        // 失败状态由卡片角标呈现，这里不再重复提示。
      });
      try {
        const media = await fetchViralVideoMedia(
          video.platformKey,
          video.nativeId,
          "video",
        );
        if (requestId !== requestIdRef.current || !activeRef.current) return;
        setPlayback({ status: "playing", src: media.url });
      } catch (cause) {
        if (requestId === requestIdRef.current && activeRef.current) {
          setPlayback({
            status: "error",
            message:
              cause instanceof Error && cause.message
                ? cause.message
                : "视频暂时无法播放，请稍后重试",
          });
        }
      }
    } catch {
      if (requestId === requestIdRef.current && activeRef.current) {
        setPlayback({
          status: "error",
          message: "视频暂时无法播放，请稍后重试",
        });
      }
    } finally {
      if (requestId === requestIdRef.current) loadingRef.current = false;
    }
  };
  const play = async () => {
    if (!video || playback.status !== "idle") return;
    if (video.platformKey && video.nativeId) {
      await playFromStorage();
      return;
    }
    if (video.playUrl) {
      activate();
      setPlayback({ status: "playing", src: video.playUrl });
      return;
    }
    setPlayback({ status: "error", message: "视频暂时无法播放，请稍后重试" });
  };
  const markFailed = () => {
    setPlayback({ status: "error", message: "视频播放失败，请重试" });
  };
  return {
    playback,
    play,
    retry: playFromStorage,
    markFailed,
    activate,
  };
}

/** 封面区（参考 CardCover）：点击原位播放，缓冲态整窗进度条，失败可重试。 */
function ViralCover({
  video,
  playing,
  loading,
  src,
  onPlay,
  onRetry,
  onPlaybackError,
  onNativePlay,
  playerRef,
  error,
  cacheProgress,
}: {
  video: StudioVideo;
  playing: boolean;
  loading: boolean;
  src?: string;
  onPlay: () => void;
  onRetry: () => void;
  onPlaybackError: () => void;
  onNativePlay: () => void;
  playerRef: RefObject<HTMLVideoElement | null>;
  error?: string;
  cacheProgress?: ViralCacheProgress;
}) {
  const publish = viralPublishLabel(video);
  return (
    <div
      className={`viral-card-cover ${playing ? "is-playing" : ""}`}
      title={playing ? undefined : "播放"}
    >
      {playing ? (
        <VideoPreview
          ref={playerRef}
          autoPlay
          controls
          playsInline
          src={src}
          poster={video.poster}
          title={video.title}
          onError={onPlaybackError}
          onPlay={onNativePlay}
        />
      ) : (
        <button
          type="button"
          className="viral-card-cover-trigger"
          onClick={error ? onRetry : onPlay}
          aria-label={`${error ? "重试播放" : "播放"} ${video.title}`}
          disabled={loading}
        >
          <ViralPoster video={video} />
          <span className="viral-card-scrim" aria-hidden="true" />
          <span className="viral-card-playbtn" aria-hidden="true">
            ▶
          </span>
          {loading && <span className="viral-card-loading">准备中…</span>}
          {error && (
            <span className="viral-card-loading" role="status">
              {error}
            </span>
          )}
        </button>
      )}
      <span className="viral-card-platform">
        <PlatformLogo platform={video.platform} size={18} /> {video.platform}
      </span>
      <span className="viral-card-duration">{video.duration}</span>
      {cacheProgress && <ViralCacheBadge progress={cacheProgress} />}
      {!playing && (
        <div className="viral-card-overlay">
          <ViralStatsRow video={video} />
          <div className="viral-card-overlay-author">
            {video.authorAvatar ? (
              <img alt="" loading="lazy" src={video.authorAvatar} />
            ) : (
              <i>{(video.author || "无").slice(0, 1)}</i>
            )}
            <span>{video.author}</span>
            {video.verified && <em title="认证作者">✓</em>}
            {publish && <time>{publish}</time>}
            {video.category && (
              <em className="viral-card-cat">{video.category}</em>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

/** 统计行（参考统计行四字段常显）：点赞 / 评论 / 转发 / 收藏。 */
function ViralStatsRow({ video }: { video: StudioVideo }) {
  const stats: Array<[string, string, string]> = [
    ["heart", "点赞", viralLikesLabel(video)],
    ["comment", "评论", viralMetricLabel(video.comments)],
    ["share", "转发", viralMetricLabel(video.shares)],
    ["star", "收藏", viralMetricLabel(video.collections)],
  ];
  return (
    <div className="viral-card-stats">
      {stats.map(([icon, label, value]) => (
        <span key={icon} title={label}>
          <Icon name={icon} size={13} />
          {value}
        </span>
      ))}
    </div>
  );
}

export function ViralFavoriteButton({
  video,
  savedFromServer,
  onSavedChange,
  compact = false,
}: {
  video: StudioVideo;
  savedFromServer?: boolean;
  onSavedChange?: (saved: boolean) => void;
  compact?: boolean;
}) {
  const { state, review, notify, patchState, user } = useStudio();
  const [saved, setSaved] = useState(
    savedFromServer ?? state.favorites.includes(video.id),
  );
  const [savingOperation, setSavingOperation] = useState<{
    accountId: string;
    operationId: number;
  }>();
  const accountRef = useRef(user.id);
  const operationRef = useRef(0);
  if (accountRef.current !== user.id) {
    accountRef.current = user.id;
    operationRef.current += 1;
  }
  const saving = Boolean(
    savingOperation &&
      savingOperation.accountId === user.id &&
      savingOperation.operationId === operationRef.current,
  );
  useEffect(() => {
    if (savedFromServer !== undefined) setSaved(savedFromServer);
  }, [savedFromServer]);
  useEffect(() => {
    return () => {
      operationRef.current += 1;
    };
  }, []);

  const toggle = async () => {
    if (saving) return;
    const operation = {
      accountId: user.id,
      operationId: ++operationRef.current,
    };
    const isCurrent = () =>
      accountRef.current === operation.accountId &&
      operationRef.current === operation.operationId;
    const nextSaved = !saved;
    if (!isCurrent()) return;
    setSaved(nextSaved);
    onSavedChange?.(nextSaved);
    patchState({
      favorites: nextSaved
        ? [...new Set([...state.favorites, video.id])]
        : state.favorites.filter((id) => id !== video.id),
    });
    if (review || !video.platformKey || !video.nativeId) return;
    setSavingOperation(operation);
    try {
      if (nextSaved) {
        await saveViralFavorite(video.platformKey, video.nativeId);
      } else {
        await removeViralFavorite(video.platformKey, video.nativeId);
      }
    } catch {
      if (!isCurrent()) return;
      setSaved(saved);
      onSavedChange?.(saved);
      patchState({
        favorites: saved
          ? [...new Set([...state.favorites, video.id])]
          : state.favorites.filter((id) => id !== video.id),
      });
      notify("收藏失败，已恢复原状态");
    } finally {
      if (isCurrent()) setSavingOperation(undefined);
    }
  };

  return (
    <Button
      variant="outline"
      className={compact ? "viral-card-favorite" : undefined}
      aria-label={`收藏 ${video.title}`}
      aria-pressed={saved}
      title={saved ? "取消收藏" : "收藏视频"}
      disabled={saving}
      onClick={() => void toggle()}
    >
      {compact ? <Icon name="star" size={18} /> : saved ? "已收藏" : "收藏"}
    </Button>
  );
}

/** 缓存占用与清理入口（决策 #16 下载管理面板的轻量版）。非桌面端不渲染。 */
function ViralCacheUsage({ revision }: { revision: number }) {
  const [items, setItems] = useState<ViralCachedItem[] | null>(null);
  // biome-ignore lint/correctness/useExhaustiveDependencies: revision 是刻意的重载触发器——缓存完成数变化时重新读一次占用，effect 体内不需要用到它。
  useEffect(() => {
    if (!cacheAvailable()) return;
    let cancelled = false;
    void listViralCache()
      .then((next) => {
        if (!cancelled) setItems(next);
      })
      .catch(() => {
        // 读不到就当没有缓存：这只是只读的状态展示，不值得打断浏览。
      });
    return () => {
      cancelled = true;
    };
  }, [revision]);
  if (!cacheAvailable() || items === null) return null;
  const totalBytes = items.reduce((sum, item) => sum + item.bytes, 0);
  return (
    <div className="viral-cache-usage" role="status">
      <span>
        本地已缓存 {items.length} 条 · 占用 {formatCacheBytes(totalBytes)}
      </span>
      <Button variant="quiet" onClick={() => void openViralCacheFolder()}>
        打开缓存目录
      </Button>
      <Button
        variant="quiet"
        disabled={items.length === 0}
        title="只删除本地缓存文件，需要时可以重新缓存"
        onClick={() => {
          void clearViralCache("all").then(() => setItems([]));
        }}
      >
        清空本地缓存
      </Button>
    </div>
  );
}

/** 缓存角标 + 细进度条（决策 #16）。总长度未知时只画角标，不画进度条。 */
function ViralCacheBadge({ progress }: { progress: ViralCacheProgress }) {
  const label = CACHE_BADGE_LABELS[progress.state];
  // 失败/未就绪都带上原因：角标只有四个字，细节放悬浮说明里。
  const hint = progress.error ?? label;
  const ratio = cacheProgressRatio(progress);
  return (
    <>
      <span className={`viral-card-cache is-${progress.state}`} title={hint}>
        {label}
      </span>
      {progress.state === "downloading" && (
        <span aria-hidden="true" className="viral-card-cache-track">
          {ratio !== null && (
            <i
              className="viral-card-cache-fill"
              style={{ width: `${Math.round(ratio * 100)}%` }}
            />
          )}
        </span>
      )}
    </>
  );
}

function ViralCard({
  video,
  active,
  onActivate,
  savedFromServer,
  onFavoriteChange,
  availability = "available",
  cacheProgress,
}: {
  video: StudioVideo;
  active: boolean;
  onActivate: () => void;
  savedFromServer?: boolean;
  onFavoriteChange: (saved: boolean) => void;
  availability?: ViralVideoItem["availability"];
  cacheProgress?: ViralCacheProgress;
}) {
  const { review, navigate, notify, openLive } = useStudio();
  const playerRef = useRef<HTMLVideoElement | null>(null);
  const [downloadBusy, setDownloadBusy] = useState(false);
  // ADM-17 卡片级直达：与详情页同一条 4–30s 门槛；未知/超限不渲染复刻入口，
  // 只保留提取文案方向。
  const canReplicateFromCard =
    typeof video.durationMs === "number" &&
    video.durationMs >= 4_000 &&
    video.durationMs <= 30_000;
  const { playback, play, retry, markFailed, activate } = useViralPlayback(
    video,
    active,
    onActivate,
  );
  useEffect(() => {
    if (!active) playerRef.current?.pause();
  }, [active]);
  const openDetail = () => {
    navigate("viral-detail", {
      selectedVideoId: video.id,
      returnTo: "viral",
    });
    persistViralDetailUrl(video);
  };
  const beginDownload = () => {
    if (availability !== "available") {
      notify("该视频已不可用，无法下载");
      return;
    }
    if (review) {
      notify("审核模式仅演示，不执行下载");
      return;
    }
    if (!video.platformKey || !video.nativeId) {
      notify("该视频缺少可下载的平台标识");
      return;
    }
    if (downloadBusy) return;
    const platformKey = video.platformKey;
    const nativeId = video.nativeId;
    setDownloadBusy(true);
    void (async () => {
      try {
        if (!cacheAvailable()) {
          // Web 端没有本地缓存：抖音走一次计费直链在新窗口打开；视频号
          // 归档后主存储里是解密后的明文 MP4，直接用自有签名链接下载，
          // 未归档（转存中/已回收）才引导桌面端。
          if (platformKey === "douyin") {
            const source = await fetchViralVideoSource(platformKey, nativeId);
            window.open(source.fullUrl, "_blank", "noopener");
            notify("已开始下载，请在新窗口完成保存");
          } else {
            try {
              const media = await fetchViralVideoMedia(
                platformKey,
                nativeId,
                "video",
              );
              window.open(media.url, "_blank", "noopener");
              notify("已开始下载，请在新窗口完成保存");
            } catch {
              notify(
                "视频号素材尚未转存到云端，请使用桌面客户端下载或稍后重试",
              );
            }
          }
          return;
        }
        await ensureViralCacheForVideo({ platformKey, nativeId });
        await awaitViralCacheReady(platformKey, nativeId);
        const assetUrl = await viralCacheLocalUrl(platformKey, nativeId);
        if (!assetUrl) {
          notify("本地缓存尚未就绪，请稍后重试。");
          return;
        }
        const filename = `${(video.title || nativeId).slice(0, 40)}.mp4`;
        const result = await downloadVideoToFile(
          async () => assetUrl,
          filename,
        );
        if (result.status === "saved") notify("视频已保存到所选位置");
        else if (result.status === "started")
          notify("已开始下载，请在新窗口完成保存");
      } catch (cause) {
        if (
          !openWalletIfInsufficientCredits(cause, {
            notify,
            openWallet: () => openLive("wallet"),
          })
        ) {
          notify(cause instanceof Error ? cause.message : "下载失败，请重试。");
        }
      } finally {
        setDownloadBusy(false);
      }
    })();
  };
  return (
    <article className="viral-card">
      <ViralCover
        video={video}
        playing={playback.status === "playing"}
        loading={playback.status === "loading"}
        src={playback.status === "playing" ? playback.src : undefined}
        onPlay={play}
        onRetry={retry}
        onPlaybackError={markFailed}
        onNativePlay={activate}
        playerRef={playerRef}
        error={playback.status === "error" ? playback.message : undefined}
        cacheProgress={cacheProgress}
      />
      <ViralFavoriteButton
        compact
        video={video}
        savedFromServer={savedFromServer}
        onSavedChange={onFavoriteChange}
      />
      <div className="viral-card-body">
        {/* 标题即入口：与「查看详情」同一动作，用户按直觉点标题就能进详情。 */}
        <h3>
          <button
            type="button"
            className="viral-card-title"
            onClick={openDetail}
          >
            {video.title}
          </button>
        </h3>
        <ViralStatisticsNote video={video} />
        <div className="viral-card-tags">
          {video.hasCopy && (
            <span
              className="viral-card-copy"
              title="文案已提取过，可在文案工坊直接复用"
            >
              已有文案
            </span>
          )}
          {video.tags?.slice(0, 6).map((tag) => (
            <span key={tag} title={`#${tag}`}>
              #{tag}
            </span>
          ))}
        </div>
        <div className="content-card-actions">
          {/* 2026-10-31：一键复刻升级为卡片主 CTA——复刻是这个页面的核心转化
              动作，放首位并用 primary 样式；时长门槛与详情页共用 4–30s。
              提取文案是第二方向（文案二创/口播线），同为意图式跳详情执行。 */}
          {canReplicateFromCard ? (
            <Button
              variant="primary"
              aria-label={`一键复刻：${video.title}`}
              onClick={() => {
                navigate("viral-detail", {
                  selectedVideoId: video.id,
                  returnTo: "viral",
                  viralReplicateIntent: true,
                });
                persistViralDetailUrl(video);
              }}
            >
              一键复刻
            </Button>
          ) : null}
          {video.platformKey && video.nativeId ? (
            <Button
              variant="outline"
              aria-label={`提取文案：${video.title}`}
              onClick={() => {
                navigate("viral-detail", {
                  selectedVideoId: video.id,
                  returnTo: "viral",
                  viralExtractIntent: true,
                });
                persistViralDetailUrl(video);
              }}
            >
              提取文案
            </Button>
          ) : null}
          <Button variant="quiet" onClick={openDetail}>
            查看详情
          </Button>
          <Button
            variant="outline"
            aria-label={`下载视频 ${video.title}`}
            disabled={downloadBusy}
            onClick={beginDownload}
          >
            {downloadBusy ? "准备中…" : "下载视频"}
          </Button>
        </div>
        {availability !== "available" && (
          <p className="viral-media-status is-error" role="status">
            该视频已不可用
          </p>
        )}
      </div>
    </article>
  );
}

export function ViralPage() {
  const { data, review, updateData, user } = useStudio();
  const accountId = user.id;
  const [platform, setPlatform] = useState<"抖音" | "视频号">("抖音");
  const [category, setCategory] = useState("全部");
  const [sort, setSort] = useState<"热门优先" | "最新">("热门优先");
  const [scope, setScope] = useState<"all" | "favorites">("all");
  const [visibleCount, setVisibleCount] = useState(viralInitialCount);
  const [activeVideoId, setActiveVideoId] = useState<string>();
  const [listError, setListError] = useState<string>();
  const [refreshStatus, setRefreshStatus] = useState<string>();
  const [listLoading, setListLoading] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  const [nextCursor, setNextCursor] = useState<string>();
  const [hasMore, setHasMore] = useState(false);
  const [listReloadRevision, setListReloadRevision] = useState(0);
  const [homepageNow, setHomepageNow] = useState(Date.now);
  const [homepageClockOffset, setHomepageClockOffset] = useState(0);
  const [nextHomepageChange, setNextHomepageChange] = useState<string | null>(
    null,
  );
  const [favoriteKeys, setFavoriteKeys] = useState(new Set<string>());
  const [availabilityByKey, setAvailabilityByKey] = useState(
    new Map<string, ViralVideoItem["availability"]>(),
  );
  const sentinelRef = useRef<HTMLButtonElement | null>(null);
  const listRequestRef = useRef(0);
  const loadingMoreRef = useRef(false);
  const listReloadRevisionRef = useRef(listReloadRevision);
  listReloadRevisionRef.current = listReloadRevision;
  const platformKey: ViralPlatform =
    platform === "抖音" ? "douyin" : "wechat_channels";
  const sortKey = sort === "最新" ? "latest" : "hot";
  const paginationContextKey = `${accountId}:${scope}:${platformKey}:${sortKey}:${category}`;
  const paginationContextKeyRef = useRef(paginationContextKey);
  const paginationOperationRef = useRef(0);
  const activePaginationContextRef = useRef({
    key: `${paginationContextKey}:${listReloadRevision}`,
    operationId: 0,
  });
  const renderedPaginationContextKey = `${paginationContextKey}:${listReloadRevision}`;
  if (activePaginationContextRef.current.key !== renderedPaginationContextKey) {
    activePaginationContextRef.current = {
      key: renderedPaginationContextKey,
      operationId: ++paginationOperationRef.current,
    };
  }
  const renderedPaginationContext = activePaginationContextRef.current;
  const confirmedPaginationContextRef = useRef<
    typeof renderedPaginationContext | undefined
  >(undefined);
  const paginationSnapshotsRef = useRef(
    new Map<string, { nextCursor?: string; hasMore: boolean }>(),
  );
  paginationContextKeyRef.current = paginationContextKey;

  // 公共列表回执决定客户发布可见性；共享状态还含私有导入，不能直接全部展示。
  // 首次回执前保留已有首页缓存；非首页发布条目凭本页接口回执进入同一网格。
  const curated = useMemo(
    () =>
      data.videos.filter(
        (item) =>
          (item.homepageFeatured === true ||
            availabilityByKey.get(`${item.platformKey}:${item.nativeId}`) ===
              "available") &&
          (!item.homepageStartsAt ||
            Date.parse(item.homepageStartsAt) <= homepageNow) &&
          (!item.homepageEndsAt ||
            Date.parse(item.homepageEndsAt) > homepageNow),
      ),
    [data.videos, homepageNow, availabilityByKey],
  );
  useEffect(() => {
    if (review || scope !== "all") return;
    const now = Math.max(homepageNow, Date.now() - homepageClockOffset);
    const boundaries = [
      nextHomepageChange,
      ...data.videos.flatMap((item) =>
        item.platformKey === platformKey &&
        (item.homepageFeatured ||
          availabilityByKey.get(`${item.platformKey}:${item.nativeId}`) ===
            "available")
          ? [item.homepageStartsAt, item.homepageEndsAt]
          : [],
      ),
    ]
      .filter((value): value is string => Boolean(value))
      .map(Date.parse)
      .filter((value) => Number.isFinite(value) && value > now);
    const refresh = () => {
      // 先移除过期条目；网络失败也不能把已到期内容留在公共列表。
      setHomepageNow(Date.now() - homepageClockOffset);
      setListReloadRevision((revision) => revision + 1);
    };
    const timer = boundaries.length
      ? window.setTimeout(
          refresh,
          Math.min(Math.min(...boundaries) - now, 2_147_000_000),
        )
      : undefined;
    const onVisibility = () => {
      if (document.visibilityState === "visible") refresh();
    };
    window.addEventListener("focus", refresh);
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      window.clearTimeout(timer);
      window.removeEventListener("focus", refresh);
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, [
    data.videos,
    availabilityByKey,
    homepageClockOffset,
    homepageNow,
    nextHomepageChange,
    platformKey,
    review,
    scope,
  ]);
  const shown = useMemo(() => {
    const source = scope === "favorites" ? data.videos : curated;
    return source
      .filter(
        (item) =>
          item.platform === platform &&
          (scope !== "favorites" || favoriteKeys.has(viralIdentity(item))) &&
          (category === "全部" || item.category === category),
      )
      .sort((left, right) => {
        // API已按完整首页顺序分页；再次按点赞排序会把管理员移到首位的内容挤走。
        if (scope === "all" && !review) return 0;
        if (sort === "热门优先") {
          return (
            (scope === "all"
              ? (left.homepageRank ?? 2147483647) -
                (right.homepageRank ?? 2147483647)
              : 0) ||
            (right.likes ?? -1) - (left.likes ?? -1) ||
            left.id.localeCompare(right.id, undefined, { numeric: true })
          );
        }
        return (right.publishedAt ?? 0) - (left.publishedAt ?? 0);
      });
  }, [
    category,
    curated,
    data.videos,
    favoriteKeys,
    platform,
    scope,
    sort,
    review,
  ]);

  // biome-ignore lint/correctness/useExhaustiveDependencies: 平台/分类/排序变化时重置滚动加载计数。
  useEffect(() => {
    setVisibleCount(viralInitialCount);
  }, [platform, category, scope, sort]);

  const current = review ? shown.slice(0, visibleCount) : shown;
  const statistics = useViralStatistics(current, !review);

  // 浏览列表直接展示 current；缓存 hook 喂实际展示的列表（§6.2 翻页即缓存）。
  const gridVideos = current;
  // 归档未就绪的条目由这里补一条合成状态，否则覆盖不到（Rust 侧压根没入队）。
  const cacheFallback = useViralAutoCache(gridVideos);
  // 卡片角标/进度条的数据源；只喂完整的平台+ID 对，避免半条记录产生 ":xxx" 这类废键。
  const cacheProgress = useViralCacheProgress(
    gridVideos.flatMap((video) =>
      video.platformKey && video.nativeId
        ? [{ platform: video.platformKey, videoId: video.nativeId }]
        : [],
    ),
  );
  const cacheProgressFor = (video: StudioVideo) => {
    if (!video.platformKey || !video.nativeId) return undefined;
    const key = viralCacheKey(video.platformKey, video.nativeId);
    // 真实进度优先：同一条视频重试成功后，合成状态自动让位。
    return cacheProgress.get(key) ?? cacheFallback.get(key);
  };

  // 本地缓存回收（D5）的保留集：非首页条目（素材、搜索结果、详情注入）不进爆款
  // 网格，但同样在用本地缓存，回收时必须排除。首页条目以每次下发的结果为准，因此
  // 这里不看它们——被删除的条目下次刷新就不再下发，正好落进回收范围。
  const knownCacheKeys = useMemo(() => {
    const byPlatform = new Map<
      string,
      { platform: string; videoId: string }[]
    >();
    for (const video of data.videos) {
      if (!video.platformKey || !video.nativeId) continue;
      if (video.homepageFeatured === true) continue;
      const keys = byPlatform.get(video.platformKey) ?? [];
      keys.push({ platform: video.platformKey, videoId: video.nativeId });
      byPlatform.set(video.platformKey, keys);
    }
    return byPlatform;
  }, [data.videos]);
  // 列表刷新的回调里要用最新一份，但不能因此把 data 放进 effect 依赖（会连环重拉）。
  const knownCacheKeysRef = useRef(knownCacheKeys);
  knownCacheKeysRef.current = knownCacheKeys;

  // 每完成一次缓存就把缓存压回上限内（§13-1）。以「本页已缓存条数」为触发条件，
  // 避免每条进度事件都去扫一遍缓存目录。
  const cachedOnPage = useMemo(
    () =>
      [...cacheProgress.values()].filter((item) => item.state === "cached")
        .length,
    [cacheProgress],
  );
  useEffect(() => {
    if (!cacheAvailable() || cachedOnPage === 0) return;
    // 收藏条目受保护、不参与淘汰。viralIdentity 形如 "platform:nativeId"，
    // platform 本身不含冒号，故按第一个冒号切分（nativeId 可能含冒号）。
    const protectedKeys = [...favoriteKeys].flatMap((identity) => {
      const separator = identity.indexOf(":");
      return separator > 0
        ? [
            {
              platform: identity.slice(0, separator),
              videoId: identity.slice(separator + 1),
            },
          ]
        : [];
    });
    void enforceViralCacheLimit(protectedKeys).catch(() => {
      // 清理失败不该影响浏览，下一次缓存完成时会再试。
    });
  }, [cachedOnPage, favoriteKeys]);

  useEffect(() => {
    void accountId;
    setFavoriteKeys(new Set());
  }, [accountId]);

  useEffect(() => {
    if (review || scope !== "all") return;
    const requestContext = renderedPaginationContext;
    const requestId = ++listRequestRef.current;
    const requestReloadRevision = listReloadRevision;
    loadingMoreRef.current = false;
    setLoadingMore(false);
    let cancelled = false;
    setListError(undefined);
    setRefreshStatus(undefined);
    setListLoading(true);
    setNextCursor(undefined);
    setHasMore(false);
    void listViralVideos(platformKey, sortKey, {
      limit: viralPageSize,
    })
      .then((result) => {
        if (
          cancelled ||
          requestId !== listRequestRef.current ||
          requestReloadRevision !== listReloadRevisionRef.current ||
          requestContext !== activePaginationContextRef.current ||
          paginationContextKey !== paginationContextKeyRef.current
        )
          return;
        const incoming = result.items.map(studioVideoFromViral);
        const serverTime = result.serverTime
          ? Date.parse(result.serverTime)
          : NaN;
        const offset = Number.isFinite(serverTime)
          ? Date.now() - serverTime
          : 0;
        setHomepageClockOffset(offset);
        setHomepageNow(Date.now() - offset);
        setNextHomepageChange(result.nextChangeAt ?? null);
        const confirmedNextCursor = result.nextCursor ?? undefined;
        const confirmedHasMore = Boolean(result.hasMore && result.nextCursor);
        confirmedPaginationContextRef.current = requestContext;
        paginationSnapshotsRef.current.set(paginationContextKey, {
          nextCursor: confirmedNextCursor,
          hasMore: confirmedHasMore,
        });
        setNextCursor(confirmedNextCursor);
        setHasMore(confirmedHasMore);
        setRefreshStatus(
          result.refreshing
            ? "正在采集爆款视频，当前先展示已缓存内容…"
            : result.refreshError
              ? result.refreshError
              : result.stale
                ? "当前展示缓存内容，等待下次刷新。"
                : undefined,
        );
        setFavoriteKeys(
          new Set(
            result.items
              .filter((item) => item.isFavorite)
              .map((item) => `${item.platform}:${item.videoId}`),
          ),
        );
        setAvailabilityByKey(
          new Map(
            result.items.map((item) => [
              `${item.platform}:${item.videoId}`,
              item.availability,
            ]),
          ),
        );
        updateData((currentData) => ({
          ...currentData,
          videos: [
            ...currentData.videos.filter(
              (video) => video.platformKey !== platformKey,
            ),
            ...incoming,
          ],
        }));
        // 删除与下架只有服务端看得见（条目只是不再出现在下发结果里），所以每次
        // 列表刷新后把本地多余的缓存交给服务端判定再回收（D5）。保留集 = 本次仍
        // 可用的条目 + 收藏 + 已知的非首页条目；下架的条目已经播不了，不保留。
        void reclaimViralCache(platformKey, [
          ...result.items
            .filter(
              (item) => item.availability === "available" || item.isFavorite,
            )
            .map((item) => ({
              platform: item.platform,
              videoId: item.videoId,
            })),
          ...(knownCacheKeysRef.current.get(platformKey) ?? []),
        ]);
      })
      .catch(() => {
        if (
          !cancelled &&
          requestId === listRequestRef.current &&
          requestReloadRevision === listReloadRevisionRef.current &&
          requestContext === activePaginationContextRef.current &&
          paginationContextKey === paginationContextKeyRef.current
        ) {
          setListError("视频列表暂时无法更新，已保留当前内容");
        }
      })
      .finally(() => {
        if (
          !cancelled &&
          requestId === listRequestRef.current &&
          requestReloadRevision === listReloadRevisionRef.current &&
          requestContext === activePaginationContextRef.current &&
          paginationContextKey === paginationContextKeyRef.current
        ) {
          setListLoading(false);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [
    listReloadRevision,
    paginationContextKey,
    platformKey,
    review,
    renderedPaginationContext,
    scope,
    sortKey,
    updateData,
  ]);

  useEffect(() => {
    if (review || scope !== "favorites") return;
    const requestContext = renderedPaginationContext;
    const requestId = ++listRequestRef.current;
    const requestReloadRevision = listReloadRevision;
    loadingMoreRef.current = false;
    setLoadingMore(false);
    let cancelled = false;
    setListError(undefined);
    setListLoading(true);
    setNextCursor(undefined);
    setHasMore(false);
    void listViralFavorites({ platform: platformKey, limit: viralPageSize })
      .then((result) => {
        if (
          cancelled ||
          requestId !== listRequestRef.current ||
          requestReloadRevision !== listReloadRevisionRef.current ||
          requestContext !== activePaginationContextRef.current ||
          paginationContextKey !== paginationContextKeyRef.current
        )
          return;
        const incoming = result.items.map(studioVideoFromViral);
        setFavoriteKeys(
          new Set(
            result.items.map((item) => `${item.platform}:${item.videoId}`),
          ),
        );
        setAvailabilityByKey(
          new Map(
            result.items.map((item) => [
              `${item.platform}:${item.videoId}`,
              item.availability,
            ]),
          ),
        );
        const confirmedNextCursor = result.nextCursor ?? undefined;
        const confirmedHasMore = Boolean(result.hasMore && result.nextCursor);
        confirmedPaginationContextRef.current = requestContext;
        paginationSnapshotsRef.current.set(paginationContextKey, {
          nextCursor: confirmedNextCursor,
          hasMore: confirmedHasMore,
        });
        setNextCursor(confirmedNextCursor);
        setHasMore(confirmedHasMore);
        updateData((currentData) => ({
          ...currentData,
          videos: [
            ...currentData.videos.filter(
              (video) => video.platformKey !== platformKey,
            ),
            ...incoming,
          ],
        }));
        // 删除与下架只有服务端看得见（条目只是不再出现在下发结果里），所以每次
        // 列表刷新后把本地多余的缓存交给服务端判定再回收（D5）。保留集 = 本次仍
        // 可用的条目 + 收藏 + 已知的非首页条目；下架的条目已经播不了，不保留。
        void reclaimViralCache(platformKey, [
          ...result.items
            .filter(
              (item) => item.availability === "available" || item.isFavorite,
            )
            .map((item) => ({
              platform: item.platform,
              videoId: item.videoId,
            })),
          ...(knownCacheKeysRef.current.get(platformKey) ?? []),
        ]);
      })
      .catch(() => {
        if (
          !cancelled &&
          requestId === listRequestRef.current &&
          requestReloadRevision === listReloadRevisionRef.current &&
          requestContext === activePaginationContextRef.current &&
          paginationContextKey === paginationContextKeyRef.current
        ) {
          setListError("收藏列表暂时无法更新");
        }
      })
      .finally(() => {
        if (
          !cancelled &&
          requestId === listRequestRef.current &&
          requestReloadRevision === listReloadRevisionRef.current &&
          requestContext === activePaginationContextRef.current &&
          paginationContextKey === paginationContextKeyRef.current
        ) {
          setListLoading(false);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [
    listReloadRevision,
    paginationContextKey,
    platformKey,
    review,
    renderedPaginationContext,
    scope,
    updateData,
  ]);

  const loadMore = useCallback(async () => {
    if (review) {
      setVisibleCount((count) => Math.min(count + viralPageSize, shown.length));
      return;
    }
    if (
      !hasMore ||
      !nextCursor ||
      loadingMoreRef.current ||
      confirmedPaginationContextRef.current !== renderedPaginationContext
    )
      return;
    const requestId = listRequestRef.current;
    const requestContextKey = paginationContextKey;
    const requestContext = renderedPaginationContext;
    const isCurrent = () =>
      requestId === listRequestRef.current &&
      requestContext === activePaginationContextRef.current &&
      requestContext === confirmedPaginationContextRef.current &&
      requestContextKey === paginationContextKeyRef.current;
    loadingMoreRef.current = true;
    setLoadingMore(true);
    setListError(undefined);
    try {
      const result =
        scope === "favorites"
          ? await listViralFavorites({
              platform: platformKey,
              limit: viralPageSize,
              cursor: nextCursor,
            })
          : await listViralVideos(platformKey, sortKey, {
              limit: viralPageSize,
              cursor: nextCursor,
            });
      if (!isCurrent()) return;
      const incoming = result.items.map(studioVideoFromViral);
      const confirmedNextCursor = result.nextCursor ?? undefined;
      const confirmedHasMore = Boolean(result.hasMore && result.nextCursor);
      paginationSnapshotsRef.current.set(paginationContextKey, {
        nextCursor: confirmedNextCursor,
        hasMore: confirmedHasMore,
      });
      setNextCursor(confirmedNextCursor);
      setHasMore(confirmedHasMore);
      setFavoriteKeys((currentKeys) => {
        const nextKeys = new Set(currentKeys);
        result.items.forEach((item) => {
          if (scope === "favorites" || item.isFavorite) {
            nextKeys.add(`${item.platform}:${item.videoId}`);
          }
        });
        return nextKeys;
      });
      setAvailabilityByKey((currentAvailability) => {
        const nextAvailability = new Map(currentAvailability);
        result.items.forEach((item) => {
          nextAvailability.set(
            `${item.platform}:${item.videoId}`,
            item.availability,
          );
        });
        return nextAvailability;
      });
      updateData((currentData) => {
        const incomingIds = new Set(incoming.map((video) => video.id));
        return {
          ...currentData,
          videos: [
            ...currentData.videos.filter(
              (video) =>
                video.platformKey !== platformKey || !incomingIds.has(video.id),
            ),
            ...incoming,
          ],
        };
      });
    } catch (error) {
      if (isCurrent()) {
        if (apiErrorCode(error) === "VIRAL_CURSOR_INVALID") {
          paginationSnapshotsRef.current.delete(paginationContextKey);
          setNextCursor(undefined);
          setHasMore(false);
          setListError(undefined);
          setListReloadRevision((revision) => revision + 1);
          return;
        }
        setListError("加载更多失败，请重试");
      }
    } finally {
      if (isCurrent()) {
        loadingMoreRef.current = false;
        setLoadingMore(false);
      }
    }
  }, [
    hasMore,
    nextCursor,
    paginationContextKey,
    platformKey,
    renderedPaginationContext,
    review,
    scope,
    shown.length,
    sortKey,
    updateData,
  ]);

  // 浏览列表在非 review 态直接按 hasMore 无限滚动。
  const canLoadMore = review ? visibleCount < shown.length : hasMore;
  useEffect(() => {
    const node = sentinelRef.current;
    if (!node || !canLoadMore) return;
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries.some((entry) => entry.isIntersecting)) void loadMore();
      },
      { rootMargin: "240px 0px" },
    );
    observer.observe(node);
    return () => observer.disconnect();
  }, [canLoadMore, loadMore]);

  const platformTabs = (["抖音", "视频号"] as const).map((item) => ({
    id: item,
    label: `${item} ${
      review
        ? item === "抖音"
          ? 20
          : 30
        : curated.filter((video) => video.platform === item).length
    }`,
  }));
  const categories = review
    ? categoryTabs
    : [
        "全部",
        ...new Set(
          curated
            .filter((video) => video.platform === platform && video.category)
            .map((video) => video.category),
        ),
      ];

  return (
    <section className="content-page content-viral">
      <header className="content-title">
        <div>
          <h1>爆款视频</h1>
          <p>乡墅灵感，持续发现</p>
        </div>
      </header>
      <div className="content-viral-toolbar">
        <div
          className="content-platform-tabs"
          role="tablist"
          aria-label="内容范围"
        >
          <button
            aria-selected={scope === "all"}
            className={scope === "all" ? "is-active" : ""}
            onClick={() => {
              setCategory("全部");
              setScope("all");
            }}
            role="tab"
            type="button"
          >
            全部爆款
          </button>
          <button
            aria-selected={scope === "favorites"}
            className={scope === "favorites" ? "is-active" : ""}
            onClick={() => {
              setCategory("全部");
              setScope("favorites");
            }}
            role="tab"
            type="button"
          >
            我的收藏
          </button>
        </div>
        <div
          className="content-platform-tabs"
          role="tablist"
          aria-label="视频平台"
        >
          {platformTabs.map((item) => (
            <button
              aria-selected={item.id === platform}
              className={item.id === platform ? "is-active" : ""}
              key={item.id}
              onClick={() => {
                setCategory("全部");
                setPlatform(item.id);
              }}
              role="tab"
              type="button"
            >
              <PlatformLogo platform={item.id} /> {item.label}
            </button>
          ))}
        </div>
        <label className="content-sort">
          <span>排序</span>
          <select
            aria-label="排序方式"
            value={sort}
            onChange={(event) =>
              setSort(event.target.value as "热门优先" | "最新")
            }
          >
            <option value="热门优先">热门优先</option>
            <option value="最新">最新</option>
          </select>
        </label>
      </div>
      <nav className="content-filters" aria-label="视频分类">
        {categories.map((item) => (
          <Button
            key={item}
            variant={item === category ? "primary" : "outline"}
            onClick={() => setCategory(item)}
          >
            {item}
          </Button>
        ))}
      </nav>
      {!review && <ViralCacheUsage revision={cachedOnPage} />}
      {(listError || statistics.error) && (
        <p className="viral-media-status is-error" role="status">
          {listError ?? statistics.error}
        </p>
      )}
      {!review &&
      !statistics.healing &&
      statistics.missingCount > 0 &&
      !statistics.dismissed ? (
        <div className="viral-statistics-heal">
          <span className="viral-statistics-heal__icon" aria-hidden="true">
            ↻
          </span>
          <div className="viral-statistics-heal__body" role="status">
            <b>{statistics.missingCount} 条视频号的互动统计还没补全</b>
            <small>
              {
                "视频号的点赞 / 评论 / 转发 / 收藏需要单独补采。补全只对缺失的条目外呼，已取到的数据 24 小时内不再重复外呼，因此不会重复计费。"
              }
            </small>
          </div>
          <div className="viral-statistics-heal__actions">
            <Button variant="quiet" onClick={statistics.dismiss}>
              忽略
            </Button>
            <Button onClick={statistics.heal}>补全统计</Button>
          </div>
        </div>
      ) : null}
      {!review && statistics.healing ? (
        <div className="viral-statistics-heal is-healing">
          <span className="viral-statistics-heal__icon" aria-hidden="true">
            ↻
          </span>
          <span className="viral-statistics-heal__progress" role="status">
            正在补全 {statistics.healing.total} 条视频号的互动统计…（
            {statistics.healing.done} / {statistics.healing.total}）
          </span>
          <span className="viral-statistics-heal__bar" aria-hidden="true">
            <i
              style={{
                width: `${Math.round(
                  (statistics.healing.done / statistics.healing.total) * 100,
                )}%`,
              }}
            />
          </span>
          <Button variant="quiet" onClick={statistics.cancel}>
            取消
          </Button>
        </div>
      ) : null}
      {refreshStatus && (
        <p className="viral-media-status" role="status">
          {refreshStatus}
        </p>
      )}
      {listLoading && (
        <p className="viral-media-status" role="status">
          正在读取爆款视频…
        </p>
      )}
      {gridVideos.length ? (
        <section className="content-video-grid content-video-grid-viral">
          {gridVideos.map((video) => (
            <ViralCard
              key={video.id}
              video={video}
              active={activeVideoId === video.id}
              onActivate={() => setActiveVideoId(video.id)}
              savedFromServer={
                review ? undefined : favoriteKeys.has(viralIdentity(video))
              }
              onFavoriteChange={(saved) =>
                setFavoriteKeys((currentKeys) => {
                  const nextKeys = new Set(currentKeys);
                  const key = viralIdentity(video);
                  if (saved) nextKeys.add(key);
                  else nextKeys.delete(key);
                  return nextKeys;
                })
              }
              availability={availabilityByKey.get(viralIdentity(video))}
              cacheProgress={cacheProgressFor(video)}
            />
          ))}
        </section>
      ) : (
        <Empty
          title={scope === "favorites" ? "暂无收藏视频" : "暂无爆款视频"}
          description={
            scope === "favorites"
              ? "收藏爆款视频后，可以在这里统一查看。"
              : refreshStatus
                ? refreshStatus
                : "暂无已发布且可用的爆款视频，发布后自动展示。"
          }
        />
      )}
      {canLoadMore && (
        <button
          type="button"
          className="viral-reveal-sentinel"
          ref={sentinelRef}
          disabled={loadingMore}
          onClick={() => void loadMore()}
          onKeyDown={(event) => {
            if (event.key === "Enter" || event.key === " ") {
              event.preventDefault();
              void loadMore();
            }
          }}
        >
          {loadingMore
            ? "正在加载…"
            : review
              ? "上拉加载更多…"
              : "加载更多视频"}
        </button>
      )}
    </section>
  );
}

type ViralImportState = {
  status: "idle" | "loading" | "ready" | "error";
  message?: string;
};

function useViralImport(sourceId?: string, accountId = "anonymous") {
  const [state, setState] = useState<ViralImportState>({ status: "idle" });
  const requestRef = useRef(0);
  const sourceRef = useRef(sourceId);
  const accountRef = useRef(accountId);
  const idempotencyKeysRef = useRef(new Map<string, string>());
  useEffect(() => {
    sourceRef.current = sourceId;
    accountRef.current = accountId;
    setState({ status: "idle" });
    return () => {
      requestRef.current += 1;
    };
  }, [sourceId, accountId]);

  const waitForCompletion = async (
    initial: ViralImportTask,
    request: number,
  ) => {
    let task = initial;
    for (let attempt = 0; attempt < 120; attempt += 1) {
      if (request !== requestRef.current) return undefined;
      if (task.status === "SUCCEEDED" || task.status === "FAILED") return task;
      const taskId = task.taskId ?? task.id;
      if (!taskId) throw new Error("导入任务缺少任务 ID");
      await new Promise<void>((resolve) => window.setTimeout(resolve, 1_000));
      if (request !== requestRef.current) return undefined;
      task = await getViralImportTask(taskId);
    }
    if (request !== requestRef.current) return undefined;
    if (task.status === "SUCCEEDED" || task.status === "FAILED") return task;
    throw new ViralImportPollingTimeoutError(
      VIRAL_IMPORT_POLLING_TIMEOUT_MESSAGE,
    );
  };

  return {
    importState: state,
    start: async (
      video: StudioVideo,
      purpose: ViralImportPurpose,
      onReady: (task: ViralImportTask) => void,
    ) => {
      if (video.id !== sourceRef.current || accountId !== accountRef.current)
        return;
      if (!video.platformKey || !video.nativeId) {
        setState({ status: "error", message: "该视频缺少可导入的平台标识" });
        return;
      }
      const request = ++requestRef.current;
      const actionKey = `${video.platformKey}:${video.nativeId}:${purpose}`;
      const memoryKey = `${accountId}:${actionKey}`;
      const idempotencyKey =
        idempotencyKeysRef.current.get(memoryKey) ??
        viralImportIdempotencyKey(accountId, actionKey);
      idempotencyKeysRef.current.set(memoryKey, idempotencyKey);
      const clearKey = () => {
        idempotencyKeysRef.current.delete(memoryKey);
        clearViralImportIdempotencyKey(accountId, actionKey, idempotencyKey);
      };
      setState({ status: "loading", message: "正在导入参考素材…" });
      try {
        const created = await createViralImportTask(
          video.platformKey,
          video.nativeId,
          purpose,
          idempotencyKey,
        );
        const completed = await waitForCompletion(created, request);
        if (!completed || request !== requestRef.current) return;
        if (completed.status === "FAILED") {
          if (completed.retryable === false) clearKey();
          throw new Error(
            completed.errorMessage ||
              completed.error ||
              completed.message ||
              "导入任务执行失败",
          );
        }
        if (!completed.projectId || !completed.sourceAssetId) {
          clearKey();
          throw new Error("导入任务缺少项目或素材结果");
        }
        if (purpose === "copy" && !completed.canTranscribe) {
          clearKey();
          throw new Error("该来源暂不支持提取文案");
        }
        if (purpose === "replica" && !completed.canAnalyze) {
          clearKey();
          throw new Error("该来源暂不支持视频复刻");
        }
        setState({ status: "ready", message: "参考素材已导入" });
        onReady(completed);
      } catch (error) {
        if (request !== requestRef.current) return;
        if (shouldClearViralImportIdempotencyKey(error)) clearKey();
        setState({
          status: "error",
          message: error instanceof Error ? error.message : "导入任务失败",
        });
      }
    },
  };
}

export function ViralDetailPage() {
  const {
    data,
    state,
    user,
    review,
    navigate,
    notify,
    openLive,
    patchDraft,
    patchState,
    updateData,
    extractScriptFromUpload,
    extractViralCopy,
  } = useStudio();
  const accountId = user.id;
  const selectedVideo = state.selectedVideoId
    ? data.videos.find((item) => item.id === state.selectedVideoId)
    : undefined;
  const [remoteVideo, setRemoteVideo] = useState<StudioVideo>();
  const [detailStatus, setDetailStatus] = useState<
    "idle" | "loading" | "error"
  >("idle");
  const [detailAvailability, setDetailAvailability] =
    useState<ViralVideoItem["availability"]>("available");
  const [detailFavorite, setDetailFavorite] = useState<boolean>();
  // 查看详情免费（2026-10-13 拍板）：进入详情读一次服务端权威数据（含互动统计），
  // 不再展示计费回执；失败时只透传原因。
  const [detailError, setDetailError] = useState<string>();
  const detailAccountRef = useRef(accountId);
  const detailRequestRef = useRef(0);
  detailAccountRef.current = accountId;
  const detailParams = useMemo(viralDetailParams, []);
  const video = remoteVideo ?? selectedVideo;
  const { importState, start } = useViralImport(video?.id, accountId);

  useEffect(() => {
    void accountId;
    setRemoteVideo(undefined);
    setDetailFavorite(undefined);
    setDetailAvailability("available");
    setDetailStatus("idle");
    setDetailError(undefined);
  }, [accountId]);

  useEffect(() => {
    void accountId;
    if (remoteVideo || review || !detailParams) return;
    if (
      selectedVideo &&
      (selectedVideo.platformKey !== detailParams.platform ||
        selectedVideo.nativeId !== detailParams.videoId)
    ) {
      return;
    }
    const requestAccountId = accountId;
    const requestId = ++detailRequestRef.current;
    const isCurrent = () =>
      detailAccountRef.current === requestAccountId &&
      detailRequestRef.current === requestId;
    setDetailStatus("loading");
    void openViralVideoDetail(detailParams.platform, detailParams.videoId)
      .then(({ item }) => {
        if (!isCurrent()) return;
        const restored = studioVideoFromViral(item);
        setDetailAvailability(item.availability ?? "available");
        setDetailFavorite(Boolean(item.isFavorite));
        setDetailError(undefined);
        setRemoteVideo(restored);
        setDetailStatus("idle");
        updateData((current) => ({
          ...current,
          videos: [
            ...current.videos.filter(
              (candidate) => candidate.id !== restored.id,
            ),
            restored,
          ],
        }));
      })
      .catch((cause) => {
        if (!isCurrent()) return;
        setDetailStatus("error");
        // 详情免费后这里的失败只剩网络/下架：把原因显式告诉用户，本地已有
        // 条目仍可阅读（这些字段浏览列表本就免费可见）。
        const message =
          cause instanceof Error ? cause.message : "查看详情失败，请重试。";
        setDetailError(message);
      });
    return () => {
      if (detailRequestRef.current === requestId) {
        detailRequestRef.current += 1;
      }
    };
  }, [detailParams, remoteVideo, review, selectedVideo, updateData, accountId]);

  useEffect(() => {
    if (video) persistViralDetailUrl(video);
  }, [video]);

  // 互动统计随「查看详情」同一响应下发（一次计费覆盖详情与统计），
  // 详情页不再单独发起统计请求。
  const { playback, play, retry, markFailed } = useViralPlayback(video);
  const beginReplicateRef = useRef<() => void>(() => {});
  const requestExtractRef = useRef<() => void>(() => {});
  // ADM-17 卡片「去复刻」直达：意图即焚；handler 经 ref 晚绑定，保持 hook
  // 顺序稳定（beginReplicate 的定义在组件 early return 之后）。
  useEffect(() => {
    if (!state.viralReplicateIntent || !video) return;
    patchState({ viralReplicateIntent: false });
    // 卡片一键复刻对超限视频的降级提示：讲清替代方向而不是干巴巴的拒绝。
    // >30s 平台视频暂不能自动走长视频拆解（服务端 canAnalyze 门禁 4–30s），
    // 服务端放开后这里接 handOffLongReplicaSource 路由。
    if (typeof video.durationMs !== "number") {
      notify(
        "该视频时长未知，暂不支持自动复刻；可在本页「提取文案」后由口播数字人翻拍。",
      );
      return;
    }
    if (video.durationMs > 30_000) {
      notify(
        "该视频超过 30 秒，暂不支持自动复刻；可用「提取文案」获取文案后由口播数字人翻拍。",
      );
      return;
    }
    if (video.durationMs < 4_000) {
      notify("该视频不足 4 秒，请走「提取文案」方向");
      return;
    }
    beginReplicateRef.current();
  }, [state.viralReplicateIntent, video, notify, patchState]);
  // 卡片「提取文案」直达：与复刻意图同款即焚消费，复用下方 requestExtract
  // 的双端分流（桌面本地抽音轨 / Web claim-first 回落服务端导入）。
  useEffect(() => {
    if (!state.viralExtractIntent || !video) return;
    patchState({ viralExtractIntent: false });
    if (!video.platformKey || !video.nativeId) {
      notify("该视频缺少平台标识，无法提取文案");
      return;
    }
    requestExtractRef.current();
  }, [state.viralExtractIntent, video, notify, patchState]);

  // hooks 必须在提前 return 之前声明完毕（刷新按钮渲染在下方详情分支里）。
  const [refreshBusy, setRefreshBusy] = useState(false);
  const [refreshNotice, setRefreshNotice] = useState<string>();
  if (!video && detailStatus === "loading") {
    return (
      <section className="content-page">
        <p className="viral-media-status" role="status">
          正在读取视频详情…
        </p>
      </section>
    );
  }
  if (!video)
    return (
      <section className="content-page">
        <Empty
          title={detailStatus === "error" ? "视频暂不可用" : "暂未选择参考视频"}
          description={
            detailStatus === "error"
              ? "该视频可能已下架或暂时无法读取。"
              : "请返回爆款视频列表选择一条内容。"
          }
          action={
            <Button variant="primary" onClick={() => navigate("viral")}>
              返回爆款视频
            </Button>
          }
        />
      </section>
    );
  const published =
    video.publishedDisplay ??
    (video.publishedAt
      ? new Date(video.publishedAt * 1000).toLocaleDateString("zh-CN")
      : "—");
  const metrics: Array<[string, string, string]> = [
    ["heart", "点赞", viralLikesLabel(video)],
    ["comment", "评论", viralMetricLabel(video.comments)],
    ["share", "转发", viralMetricLabel(video.shares)],
    ["star", "收藏", viralMetricLabel(video.collections)],
  ];
  const goExtract = (task: ViralImportTask) => {
    if (!task.canTranscribe || !task.projectId || !task.sourceAssetId) {
      notify("该来源暂不支持提取文案");
      return;
    }
    patchDraft({
      projectId: task.projectId,
      sourceId: task.sourceAssetId,
      sourceAssetId: task.sourceAssetId,
    });
    // Web 降级链路（本地无缓存，素材由服务端导入）：同样带上视频身份，转写完成后
    // 走一次「获取文案」取正文（计费以服务端 claim 回执为准）。
    // 没有平台标识就没法关联文案，只能按普通转写处理（这类条目本就进不了爆款列表）。
    const copyClaim =
      video.platformKey && video.nativeId
        ? { platformKey: video.platformKey, nativeId: video.nativeId }
        : undefined;
    extractScriptFromUpload(
      task.projectId,
      task.sourceAssetId,
      undefined,
      copyClaim,
    );
    navigate("copy", {
      selectedVideoId: video.id,
      returnTo: "viral-detail",
    });
  };
  // 统一 claim-first（2026-11 爆款链路改造 WP3）：本地抽音轨上传已随
  // POST /videos/copy 退役，桌面与 Web 同一动作——先取共享文案，命中即秒回并
  // 跳过整次导入；未命中回落服务端导入 → 转写链路。详情页视频必然来自已发布
  // 的公开列表，claim 不会因可见性 404。
  const requestExtract = () => {
    if (video.platformKey && video.nativeId) {
      // 未命中时留在本页等导入进度，不提前跳转：导入完成后由 goExtract 导航。
      extractViralCopy(video, {
        onCacheMiss: () => void start(video, "copy", goExtract),
      });
      return;
    }
    void start(video, "copy", goExtract);
  };
  // 卡片「提取文案」意图的晚绑定入口（定义在 early return 之后，ref 保序）。
  requestExtractRef.current = requestExtract;
  // 复刻方向只适用于 4–30s 的爆款；与服务端参考视频校验保持一致。
  // 时长未知或超限时隐藏入口，视频仍可走「提取文案→口播数字人」方向。
  const canReplicate =
    typeof video.durationMs === "number" &&
    video.durationMs >= 4_000 &&
    video.durationMs <= 30_000;
  const goReplica = (task: ViralImportTask) => {
    if (!task.canAnalyze || !task.projectId || !task.sourceAssetId) {
      notify("该来源暂不支持视频复刻");
      return;
    }
    patchDraft({
      projectId: task.projectId,
      sourceId: task.sourceAssetId,
      sourceAssetId: task.sourceAssetId,
    });
    navigate("replica", {
      selectedVideoId: video.id,
      returnTo: "viral-detail",
    });
  };
  const beginReplicate = () => {
    if (review) {
      notify("审核模式仅演示，不执行导入");
      return;
    }
    void start(video, "replica", goReplica);
  };
  beginReplicateRef.current = beginReplicate;
  // 刷新资源（仅抖音，按次计费）：封面失效/直链过期时的自救入口。
  const beginRefresh = () => {
    if (review || refreshBusy) return;
    if (video.platformKey !== "douyin" || !video.nativeId) {
      notify("目前仅支持刷新抖音视频的资源");
      return;
    }
    setRefreshBusy(true);
    setRefreshNotice(undefined);
    void refreshViralVideoResource(
      video.platformKey,
      video.nativeId,
      newViralSearchKey(),
    )
      .then(() => {
        setRefreshNotice("资源已刷新，正在更新详情…");
        // 详情effect以 remoteVideo 为门槛：置空即重新拉取最新数据。
        setRemoteVideo(undefined);
      })
      .catch((cause) => {
        if (isInsufficientCredits(cause)) {
          setRefreshNotice("余额不足，无法刷新。");
          openLive("wallet");
          return;
        }
        setRefreshNotice(
          cause instanceof Error ? cause.message : "刷新失败，请重试。",
        );
      })
      .finally(() => setRefreshBusy(false));
  };
  return (
    <section className="content-page content-detail">
      <header className="content-detail-heading">
        <h1>爆款视频 / 视频详情</h1>
        <Button
          variant="quiet"
          onClick={() => navigate(state.returnTo ?? "viral")}
        >
          ‹ 返回列表
        </Button>
      </header>
      <section className="content-detail-grid content-detail-grid-viral">
        <div className="content-player content-player-viral">
          {playback.status === "playing" ? (
            <VideoPreview
              autoPlay
              controls
              playsInline
              src={playback.src}
              poster={video.poster}
              title={video.title}
              onError={markFailed}
            />
          ) : (
            <button
              type="button"
              className="content-player-viral-trigger"
              onClick={playback.status === "error" ? retry : play}
              aria-label={`${playback.status === "error" ? "重试播放" : "播放"} ${video.title}`}
              disabled={playback.status === "loading"}
            >
              <ViralPoster
                video={video}
                className="content-player-viral-poster"
              />
              {playback.status === "loading" ? (
                <span className="viral-card-loading">素材准备中…</span>
              ) : playback.status === "error" ? (
                <span className="viral-card-loading" role="status">
                  {playback.message}
                </span>
              ) : (
                <span className="content-player-viral-play">▶ 播放</span>
              )}
            </button>
          )}
          <span className="viral-card-duration">{video.duration}</span>
        </div>
        <Panel className="content-detail-info">
          <div className="content-detail-author">
            {video.authorAvatar ? (
              <img alt="" src={video.authorAvatar} />
            ) : (
              <i>{(video.author || "无").slice(0, 1)}</i>
            )}
            <div>
              <strong>
                {video.author}
                {video.verified && <em title="认证作者">✓</em>}
              </strong>
              <span>
                <PlatformLogo platform={video.platform} /> {video.platform} ·{" "}
                {video.category || "推荐"}
                {video.hasPlayableAudio ? " · 有原声" : ""}
              </span>
            </div>
          </div>
          <h2>{video.title}</h2>
          {video.tags && video.tags.length > 0 && (
            <div className="viral-card-tags viral-detail-tags">
              {video.tags.slice(0, 6).map((tag) => (
                <span key={tag}>#{tag}</span>
              ))}
            </div>
          )}
          <div className="viral-detail-metrics">
            {metrics.map(([icon, label, value]) => (
              <span key={label} title={label}>
                <Icon name={icon} size={14} />
                {value}
              </span>
            ))}
            <span className="viral-detail-published">发布 {published}</span>
            <span>时长 {video.duration}</span>
          </div>
          <ViralStatisticsNote video={video} />
          {detailError && (
            <p className="viral-media-status is-error" role="status">
              {detailError}
            </p>
          )}
          {refreshNotice && (
            <p className="viral-media-status" role="status">
              {refreshNotice}
            </p>
          )}
          {detailAvailability !== "available" && (
            <p className="viral-media-status is-error" role="status">
              该视频已不可用，暂不能导入创作。
            </p>
          )}
          <Field label="视频摘要（来源描述，非提取文案）">
            <p className="content-source-copy">{video.description}</p>
          </Field>
          {importState.status !== "idle" && (
            <p
              className={`viral-media-status is-${importState.status}`}
              role="status"
            >
              {importState.message}
            </p>
          )}
          <div className="content-detail-actions">
            <div className="content-detail-action">
              <Button
                disabled={
                  importState.status === "loading" ||
                  detailAvailability !== "available"
                }
                variant="outline"
                onClick={requestExtract}
              >
                提取文案
              </Button>
            </div>
            {canReplicate && (
              <div className="content-detail-action">
                <Button
                  disabled={
                    importState.status === "loading" ||
                    detailAvailability !== "available"
                  }
                  variant="outline"
                  onClick={beginReplicate}
                >
                  {importState.status === "loading" ? "导入中…" : "去复刻"}
                </Button>
              </div>
            )}
            {!review && video.platformKey === "douyin" && (
              <div className="content-detail-action">
                <Button
                  variant="quiet"
                  disabled={refreshBusy}
                  onClick={beginRefresh}
                >
                  {refreshBusy ? "刷新中…" : "刷新资源（按次计费）"}
                </Button>
              </div>
            )}
            <div className="content-detail-action">
              <ViralFavoriteButton
                video={video}
                savedFromServer={review ? undefined : detailFavorite}
                onSavedChange={setDetailFavorite}
              />
            </div>
          </div>
        </Panel>
      </section>
    </section>
  );
}

// --- 平台培训素材库（IA 改版第 5 项）-------------------------------------------
// 素材库从「用户自传素材」整体替换为后台统一上传的培训/视频素材（产品拍板
// 彻底替换，2026-11-01）。页面只读：浏览、播放与下载，没有上传/整理入口；
// 用户自传链路（参考视频、人物图、发布封面）不经此页，仍走各自创作页。

const TRAINING_TABS: Array<{ id: string; label: string }> = [
  { id: "", label: "全部" },
  { id: "video", label: "视频" },
  { id: "audio", label: "音频" },
  { id: "image", label: "图片" },
  { id: "doc", label: "文档" },
];
const TRAINING_PAGE_SIZE = 24;
// Office Online 预览入口（用户拍板，2026-11-12）：直链 URL 会经微软服务转码
// 渲染；预览失败/超限时弹层内常驻下载按钮兜底（方案 §5.2）。
function officeViewerUrl(directUrl: string): string {
  return `https://view.officeapps.live.com/op/view.aspx?src=${encodeURIComponent(
    directUrl,
  )}`;
}

function formatMaterialSizeBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1024 * 1024 * 1024)
    return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
  return `${(bytes / 1024 / 1024 / 1024).toFixed(2)} GB`;
}

function isOfficeDoc(item: TrainingMaterialPublicItem): boolean {
  return [
    ".docx",
    ".doc",
    ".xlsx",
    ".xls",
    ".pptx",
    ".ppt",
  ].includes(item.file_suffix);
}

export function MaterialsPage() {
  const { notify } = useStudio();
  const [tab, setTab] = useState("");
  const [tag, setTag] = useState("");
  const [page, setPage] = useState(1);
  const [data, setData] = useState<TrainingMaterialPublicPage | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  // 预览地址按需签发（30 分钟防盗链直链），会话内已签的复用；文档在弹层内
  // 取链，媒体在卡片内取链。直链过期由媒体 error 事件触发重签一次（§5.2）。
  const [previewUrls, setPreviewUrls] = useState<Record<string, string>>({});
  const [busyId, setBusyId] = useState("");
  const [docPreview, setDocPreview] =
    useState<TrainingMaterialPublicItem | null>(null);

  const load = useCallback(
    async (kind: string, tagFilter: string, targetPage: number) => {
      try {
        setLoading(true);
        setError("");
        const result = await listTrainingMaterials({
          kind: kind || undefined,
          tag: tagFilter || undefined,
          page: targetPage,
          pageSize: TRAINING_PAGE_SIZE,
        });
        setData(result);
      } catch (cause) {
        setError(cause instanceof Error ? cause.message : "读取培训素材失败");
      } finally {
        setLoading(false);
      }
    },
    [],
  );

  // 换账号时按 key 重挂载由调用方保证（与旧页同语义）；这里随筛选与页码重拉。
  useEffect(() => {
    void load(tab, tag, page);
  }, [load, tab, tag, page]);

  async function ensurePreviewUrl(
    item: TrainingMaterialPublicItem,
  ): Promise<string | null> {
    const cached = previewUrls[item.id];
    if (cached) return cached;
    try {
      setBusyId(item.id);
      const url = await trainingMaterialDownloadUrl(item.id);
      setPreviewUrls((current) => ({ ...current, [item.id]: url }));
      return url;
    } catch (cause) {
      notify(cause instanceof Error ? cause.message : "获取预览地址失败");
      return null;
    } finally {
      setBusyId("");
    }
  }

  // 媒体加载/播放失败多半是直链过期：清缓存重签一次（有界，不做循环重试）。
  async function resignOnMediaError(item: TrainingMaterialPublicItem) {
    setPreviewUrls((current) => {
      const { [item.id]: _stale, ...rest } = current;
      return rest;
    });
    await ensurePreviewUrl(item);
  }

  async function openDocPreview(item: TrainingMaterialPublicItem) {
    const url = await ensurePreviewUrl(item);
    if (url) setDocPreview(item);
  }

  async function download(item: TrainingMaterialPublicItem) {
    const url = await ensurePreviewUrl(item);
    if (!url) return;
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = `${item.title}${item.file_suffix}`;
    anchor.rel = "noopener";
    document.body.append(anchor);
    anchor.click();
    anchor.remove();
  }

  const items = data?.items ?? [];
  const total = data?.total ?? 0;
  const totalPages = Math.max(1, Math.ceil(total / TRAINING_PAGE_SIZE));
  const tagFacet = data?.tag_facet ?? [];
  const docUrl = docPreview ? previewUrls[docPreview.id] : undefined;

  return (
    <section className="content-page content-materials">
      <header className="content-title content-material-title">
        <div>
          <h1>素材库</h1>
          <p className="le-sub">
            平台统一提供的培训课程、示例视频与资料文档；按分组与标签查找，
            卡片上即可预览或下载。
          </p>
        </div>
        <div
          className="content-view-switch"
          role="tablist"
          aria-label="素材类型"
        >
          {TRAINING_TABS.map((entry) => (
            <button
              key={entry.id || "all"}
              type="button"
              role="tab"
              aria-selected={tab === entry.id}
              className={tab === entry.id ? "is-active" : undefined}
              onClick={() => {
                setTab(entry.id);
                setTag("");
                setPage(1);
              }}
            >
              {entry.label}
            </button>
          ))}
        </div>
      </header>
      {tagFacet.length > 0 ? (
        <div
          className="content-material-tags"
          role="group"
          aria-label="按标签筛选"
        >
          {tagFacet.map((entry) => (
            <button
              key={entry}
              type="button"
              aria-pressed={tag === entry}
              className={tag === entry ? "is-active" : undefined}
              onClick={() => {
                setTag(tag === entry ? "" : entry);
                setPage(1);
              }}
            >
              #{entry}
            </button>
          ))}
        </div>
      ) : null}
      {error ? <p className="le-hint le-hint--error">{error}</p> : null}
      {loading ? (
        <p className="le-hint">正在读取培训素材…</p>
      ) : items.length === 0 ? (
        <p className="le-hint">暂无素材，请等待运营上传。</p>
      ) : (
        <div className="content-asset-grid">
          {items.map((item) => {
            const url = previewUrls[item.id];
            return (
              <article key={item.id} className="content-asset">
                {item.kind === "image" && url ? (
                  <img
                    alt={item.title}
                    loading="lazy"
                    src={url}
                    onError={() => void resignOnMediaError(item)}
                  />
                ) : item.kind === "video" && url ? (
                  // biome-ignore lint/a11y/useMediaCaption: 平台培训视频不提供字幕轨，浏览器原生控件即可。
                  <video
                    controls
                    preload="metadata"
                    src={url}
                    onError={() => void resignOnMediaError(item)}
                  />
                ) : item.kind === "audio" && url ? (
                  // biome-ignore lint/a11y/useMediaCaption: 平台培训音频不提供字幕轨。
                  <audio controls src={url} />
                ) : (
                  <div className="content-asset__quick">
                    <span className="studio-status">{item.kind_label}</span>
                    <strong>{item.title}</strong>
                    {item.description ? (
                      <p className="le-sub">{item.description}</p>
                    ) : null}
                    {item.tags.length > 0 ? (
                      <p className="le-sub">
                        {item.tags.map((entry) => `#${entry}`).join(" ")}
                      </p>
                    ) : null}
                  </div>
                )}
                <footer>
                  <small>
                    {formatTaskTime(item.created_at)} ·{" "}
                    {formatMaterialSizeBytes(item.size_bytes)}
                  </small>
                  <span>
                    {item.kind === "video" || item.kind === "audio" ? (
                      <button
                        type="button"
                        disabled={busyId === item.id}
                        onClick={() => void ensurePreviewUrl(item)}
                      >
                        {url ? "重新加载" : "播放"}
                      </button>
                    ) : item.kind === "image" ? (
                      <button
                        type="button"
                        disabled={busyId === item.id}
                        onClick={() => void ensurePreviewUrl(item)}
                      >
                        {url ? "重新加载" : "查看"}
                      </button>
                    ) : (
                      <button
                        type="button"
                        disabled={busyId === item.id}
                        onClick={() => void openDocPreview(item)}
                      >
                        预览
                      </button>
                    )}{" "}
                    <button
                      type="button"
                      disabled={busyId === item.id}
                      onClick={() => void download(item)}
                    >
                      {busyId === item.id ? "获取链接…" : "下载"}
                    </button>
                  </span>
                </footer>
              </article>
            );
          })}
        </div>
      )}
      {docPreview ? (
        <div
          aria-label="文档预览"
          role="dialog"
          style={{
            position: "fixed",
            inset: 0,
            zIndex: 60,
            background: "rgba(0,0,0,.55)",
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            padding: 24,
          }}
        >
          <div
            style={{
              background: "var(--panel-bg, #fff)",
              borderRadius: 12,
              width: "min(960px, 100%)",
              maxHeight: "90vh",
              display: "flex",
              flexDirection: "column",
              overflow: "hidden",
            }}
          >
            <header
              style={{
                display: "flex",
                gap: 12,
                alignItems: "center",
                padding: "10px 14px",
              }}
            >
              <strong style={{ flex: 1 }}>{docPreview.title}</strong>
              <button
                type="button"
                onClick={() => {
                  if (docPreview) void download(docPreview);
                }}
              >
                下载文件
              </button>
              <button type="button" onClick={() => setDocPreview(null)}>
                关闭
              </button>
            </header>
            {docUrl ? (
              isOfficeDoc(docPreview) ? (
                <iframe
                  src={officeViewerUrl(docUrl)}
                  style={{ flex: 1, border: 0, minHeight: 480 }}
                  title={`${docPreview.title} 预览`}
                />
              ) : (
                <iframe
                  src={docUrl}
                  style={{ flex: 1, border: 0, minHeight: 480 }}
                  title={`${docPreview.title} 预览`}
                />
              )
            ) : (
              <p className="le-hint">正在获取预览地址…</p>
            )}
          </div>
        </div>
      ) : null}
      {totalPages > 1 ? (
        <nav className="content-pagination" aria-label="培训素材分页">
          <button
            type="button"
            disabled={page <= 1 || loading}
            onClick={() => setPage((current) => Math.max(1, current - 1))}
          >
            上一页
          </button>
          <span>
            {page} / {totalPages}
          </span>
          <button
            type="button"
            disabled={page >= totalPages || loading}
            onClick={() => setPage((current) => current + 1)}
          >
            下一页
          </button>
        </nav>
      ) : null}
    </section>
  );
}


function publishPlatformLabel(platform: string) {
  return platform === "douyin"
    ? "抖音"
    : platform === "wechat_channels"
      ? "视频号"
      : "小红书";
}
function formCoverId(
  drafts: StudioPublishDraft[] | undefined,
  assetId: string | undefined,
) {
  return drafts?.find((draft) => draft.assetId === assetId)?.coverId;
}
export function parsePublishDrafts(value: unknown): StudioPublishDraft[] {
  if (!Array.isArray(value) || value.length > 50)
    throw new Error("云端发布草稿格式不正确，请联系支持。");
  return value.map((item) => {
    if (
      !item ||
      typeof item !== "object" ||
      typeof item.id !== "string" ||
      typeof item.assetId !== "string" ||
      typeof item.title !== "string" ||
      typeof item.description !== "string" ||
      typeof item.account !== "string" ||
      !["抖音", "视频号", "小红书"].includes(item.platform) ||
      !Array.isArray(item.tags) ||
      item.tags.some((tag: unknown) => typeof tag !== "string") ||
      (item.coverId !== undefined && typeof item.coverId !== "string") ||
      (item.scheduledAt !== undefined && typeof item.scheduledAt !== "string")
    )
      throw new Error("云端发布草稿内容不完整，请联系支持。");
    return {
      id: item.id,
      assetId: item.assetId,
      coverId: item.coverId,
      platform: item.platform,
      account: item.account,
      title: item.title,
      description: item.description,
      tags: item.tags,
      ...(item.scheduledAt ? { scheduledAt: item.scheduledAt } : {}),
    };
  });
}

/** `<input type="datetime-local">` value (local wall clock) → ISO instant, or null. */
export function isoFromLocalDateTimeInput(value: string): string | null {
  if (!value) return null;
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? null : date.toISOString();
}

/** ISO instant → `<input type="datetime-local">` value in the viewer's zone. */
export function localDateTimeInputFromIso(value: string | undefined): string {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

// 服务端要求至少提前 2 分钟，并以服务端时钟校验；客户端多留 1 分钟余量，避免本机
// 时钟偏慢或提交途中耗时导致刚好卡在边界被服务端 422 拒绝。
const PUBLISH_SCHEDULE_MIN_LEAD_MS = 3 * 60 * 1000;
const PUBLISH_SCHEDULE_MAX_LEAD_MS = 30 * 24 * 60 * 60 * 1000;

export function validatePublishSchedule(
  iso: string | null,
  now: Date = new Date(),
): string | null {
  if (iso === null) return null;
  const at = new Date(iso).getTime();
  if (Number.isNaN(at)) return "请填写有效的定时时间。";
  if (at < now.getTime() + PUBLISH_SCHEDULE_MIN_LEAD_MS)
    return "定时发布至少需要提前 3 分钟。";
  if (at > now.getTime() + PUBLISH_SCHEDULE_MAX_LEAD_MS)
    return "定时发布最多只能提前 30 天。";
  return null;
}

function newPublishDraft(
  asset: StudioAsset | undefined,
  review: boolean,
): StudioPublishDraft {
  return {
    id: `publish-${asset?.id ?? "unsaved"}`,
    assetId: asset?.id ?? "",
    coverId: asset?.id,
    platform: "抖音",
    account: review ? "张工说乡墅" : "",
    title: asset?.name ?? "",
    description: review
      ? "主体之外，门窗、水电、防水和庭院，也要提前规划。"
      : "",
    tags: review ? ["农村自建房", "建房预算"] : [],
  };
}

export function PublishPage() {
  const {
    data,
    navigate,
    notify,
    patchState,
    review,
    state,
    user,
    updateData,
  } = useStudio();
  const notifyRef = useRef(notify);
  notifyRef.current = notify;
  const completedResultIds = new Set(
    data.tasks
      .filter((task) => taskHasDeliverable(task) && task.resultId)
      .map((task) => task.resultId),
  );
  const [cloudRevision, setCloudRevision] = useState<number | null>(
    review ? 0 : null,
  );
  const [cloudError, setCloudError] = useState("");
  const [actionError, setActionError] = useState("");
  const [actionBusy, setActionBusy] = useState(false);
  const [reload, setReload] = useState(0);
  // Delivery accounts live on the server (cloud QR logins + imported desktop logins);
  // local WebView2 profiles only back the manual "前往官方发布" fallback on desktop.
  const [accounts, setAccounts] = useState<CloudPublishAccount[]>([]);
  const [localProfiles, setLocalProfiles] = useState<LocalPublishAccount[]>([]);
  const [recordsToken, setRecordsToken] = useState(0);
  const [scheduleMode, setScheduleMode] = useState<"now" | "later">("now");
  const [scheduleInput, setScheduleInput] = useState("");
  const [cloudVideos, setCloudVideos] = useState<{
    requested: string[];
    available: string[];
  }>({ requested: [], available: [] });
  const operation = useRef(0);
  const mutationPending = useRef(false);
  const coverInput = useRef<HTMLInputElement>(null);
  useEffect(() => {
    void reload;
    void user.id;
    const current = ++operation.current;
    if (review) return;
    setCloudRevision(null);
    setCloudError("");
    setCloudVideos({ requested: [], available: [] });
    void (async () => {
      try {
        const record = await getStudioDraft("publishing");
        const drafts = parsePublishDrafts(record.payload.drafts);
        const ids = [
          ...new Set(
            drafts.flatMap((draft) => [
              draft.assetId,
              ...(draft.coverId ? [draft.coverId] : []),
            ]),
          ),
        ];
        const materials = ids.length
          ? await resolveMaterials(
              ids.map((id) => (id.startsWith("asset:") ? id : `asset:${id}`)),
            )
          : { items: [] };
        if (operation.current !== current) return;
        const previewResults = await Promise.allSettled(
          materials.items.map(async (item) => {
            const asset = studioAssetFromMaterial(item);
            if (!item.asset_id || !item.allowed_actions.includes("preview"))
              return asset;
            return {
              ...asset,
              url: (await getAssetDownloadUrl(item.asset_id)).url,
            };
          }),
        );
        if (operation.current !== current) return;
        const assets = previewResults.map((result, index) =>
          result.status === "fulfilled"
            ? result.value
            : studioAssetFromMaterial(materials.items[index]),
        );
        if (previewResults.some((result) => result.status === "rejected"))
          setActionError(
            "部分草稿素材预览暂时不可用，可重新加载；草稿内容已保留。",
          );
        setCloudVideos({
          requested: drafts.map((draft) => draft.assetId),
          available: materials.items
            .filter(
              (item) =>
                item.media_type === "video" &&
                item.status === "ready" &&
                item.allowed_actions.includes("download"),
            )
            .map((item) => item.asset_id ?? item.id),
        });
        updateData((previous) => ({
          ...previous,
          assets: [
            ...previous.assets.filter(
              (asset) => !assets.some((next) => next.id === asset.id),
            ),
            ...assets,
          ],
        }));
        patchState({ publishDrafts: drafts });
        setCloudRevision(record.revision);
      } catch (cause) {
        if (operation.current !== current) return;
        if (apiErrorCode(cause) === "STUDIO_DRAFT_NOT_FOUND") {
          patchState({ publishDrafts: [] });
          setCloudRevision(0);
        } else
          setCloudError(
            cause instanceof Error
              ? cause.message
              : "读取发布草稿失败，请重试。",
          );
      }
    })();
    return () => {
      operation.current += 1;
    };
  }, [review, user.id, reload, patchState, updateData]);
  useEffect(
    () => onPublishAccountsChanged(() => setReload((value) => value + 1)),
    [],
  );
  useEffect(() => {
    void reload;
    let active = true;
    setAccounts([]);
    setLocalProfiles([]);
    if (review) return;
    void listCloudPublishAccounts()
      .then((value) => {
        if (active) setAccounts(value);
      })
      .catch((cause) => {
        if (active)
          setActionError(
            cause instanceof Error ? cause.message : "读取发布账号失败",
          );
      });
    if (canUseLocalPublishAccounts())
      void listLocalPublishAccounts(user.id)
        .then((value) => {
          if (active) setLocalProfiles(value);
        })
        .catch(() => {
          // Manual fallback only; the server-side list is the delivery source.
        });
    return () => {
      active = false;
    };
  }, [user.id, review, reload]);
  const selectedAsset = data.assets.find(
    (asset) =>
      asset.id === state.selectedAssetId &&
      asset.kind === "video" &&
      (cloudVideos.requested.includes(asset.id)
        ? cloudVideos.available.includes(asset.id)
        : completedResultIds.size === 0 || completedResultIds.has(asset.id)),
  );
  const coverCandidates = [
    ...(selectedAsset ? [selectedAsset] : []),
    ...data.assets.filter(
      (asset) =>
        asset.id !== selectedAsset?.id &&
        asset.kind === "image" &&
        (asset.id === formCoverId(state.publishDrafts, state.selectedAssetId) ||
          Boolean(asset.personId) ||
          asset.group.includes("场景") ||
          asset.group === "发布封面"),
    ),
  ];
  const savedDraftForAsset = state.publishDrafts?.find(
    (draft) => draft.assetId === selectedAsset?.id,
  );
  const visibleDrafts =
    state.publishDrafts ??
    (review && selectedAsset ? [newPublishDraft(selectedAsset, review)] : []);
  const [form, setForm] = useState<StudioPublishDraft>(
    () => savedDraftForAsset ?? newPublishDraft(selectedAsset, review),
  );
  const [tagInput, setTagInput] = useState("");
  const [saveNotice, setSaveNotice] = useState(false);
  const draftCount = visibleDrafts.length;
  const selectedAccount = accounts.find(
    (account) =>
      account.id === form.account &&
      publishPlatformLabel(account.platform) === form.platform,
  );
  const platformAccounts = accounts.filter(
    (account) => publishPlatformLabel(account.platform) === form.platform,
  );
  const selectedLocalProfile = selectedAccount
    ? localProfiles.find(
        (profile) =>
          profile.platform === selectedAccount.platform &&
          profile.platform_user_id === selectedAccount.platform_user_id,
      )
    : undefined;
  const platformDeliverable = form.platform !== "小红书";
  const selectedCover =
    coverCandidates.find((asset) => asset.id === form.coverId) ??
    coverCandidates[0];

  useEffect(() => {
    const next = savedDraftForAsset ?? newPublishDraft(selectedAsset, review);
    setForm(next);
    setScheduleMode(next.scheduledAt ? "later" : "now");
    setScheduleInput(localDateTimeInputFromIso(next.scheduledAt));
    setSaveNotice(false);
  }, [review, savedDraftForAsset, selectedAsset]);

  const updateForm = (patch: Partial<StudioPublishDraft>) => {
    setSaveNotice(false);
    setForm((current) => ({ ...current, ...patch }));
  };

  const addTag = () => {
    const tag = tagInput.trim();
    if (
      !tag ||
      tag.length > 50 ||
      form.tags.length >= 10 ||
      form.tags.includes(tag)
    ) {
      setTagInput("");
      return;
    }
    updateForm({ tags: [...form.tags, tag] });
    setTagInput("");
  };

  const savePublishDraft = async () => {
    if (!selectedAsset || mutationPending.current || cloudRevision === null)
      return;
    const scheduledAt =
      scheduleMode === "later"
        ? (isoFromLocalDateTimeInput(scheduleInput) ?? undefined)
        : undefined;
    const savedDraft = {
      ...form,
      ...(scheduledAt ? { scheduledAt } : {}),
      id: savedDraftForAsset?.id ?? `publish-${selectedAsset.id}`,
      assetId: selectedAsset.id,
      coverId: selectedCover?.id,
    };
    const currentDrafts = state.publishDrafts ?? [];
    const drafts = savedDraftForAsset
      ? currentDrafts.map((draft) =>
          draft.id === savedDraftForAsset.id ? savedDraft : draft,
        )
      : [...currentDrafts, savedDraft];
    if (drafts.length > 50) {
      setActionError("最多保存 50 条发布草稿，请先移除旧草稿。");
      return;
    }
    mutationPending.current = true;
    setActionBusy(true);
    setActionError("");
    const current = operation.current;
    try {
      if (!review) {
        const record = await saveStudioDraft(
          "publishing",
          { drafts },
          false,
          cloudRevision,
        );
        if (current !== operation.current) return;
        setCloudRevision(record.revision);
      }
      patchState({ publishDrafts: drafts });
      setForm(savedDraft);
      setSaveNotice(true);
    } catch (cause) {
      if (current === operation.current)
        setActionError(
          cause instanceof Error ? cause.message : "保存草稿失败，请重试。",
        );
    } finally {
      mutationPending.current = false;
      if (current === operation.current) setActionBusy(false);
    }
  };

  async function submitPublish() {
    if (!selectedAsset || !selectedAccount || mutationPending.current) return;
    const scheduledAt =
      scheduleMode === "later"
        ? isoFromLocalDateTimeInput(scheduleInput)
        : null;
    if (scheduleMode === "later" && scheduledAt === null) {
      setActionError("请填写定时发布时间。");
      return;
    }
    const scheduleError = validatePublishSchedule(scheduledAt);
    if (scheduleError) {
      setActionError(scheduleError);
      return;
    }
    if (!platformDeliverable) {
      setActionError("小红书自动发布正在开发中，请先前往官方页面发布。");
      return;
    }
    await perform(async () => {
      const record = await createPublishRecord({
        account_id: selectedAccount.id,
        video_material_id:
          selectedAsset.materialId ??
          `asset:${selectedAsset.assetId ?? selectedAsset.id}`,
        cover_material_id:
          selectedCover && selectedCover.kind === "image"
            ? (selectedCover.materialId ??
              `asset:${selectedCover.assetId ?? selectedCover.id}`)
            : null,
        title: form.title.trim(),
        description: form.description.trim(),
        tags: form.tags,
        scheduled_at: scheduledAt,
      });
      setRecordsToken((value) => value + 1);
      notifyRef.current(
        record.scheduled_at
          ? `已加入定时发布队列 · ${publishPlatformLabel(record.platform)}`
          : `已提交发布 · ${publishPlatformLabel(record.platform)}`,
      );
    });
  }

  async function uploadCover(file: File) {
    if (!file.type.startsWith("image/") || file.size > 20 * 1024 * 1024) {
      setActionError("封面请选择 20 MB 以内的图片。");
      return;
    }
    if (mutationPending.current) return;
    mutationPending.current = true;
    setActionBusy(true);
    setActionError("");
    const current = operation.current;
    try {
      const intent = await createMaterialUploadIntent(file, {
        title: file.name,
        group: "发布封面",
      });
      const asset = studioAssetFromMaterial(
        await putMaterial(intent, file, () => {}),
      );
      if (current !== operation.current) return;
      updateData((previous) => ({
        ...previous,
        assets: [
          ...previous.assets.filter((item) => item.id !== asset.id),
          asset,
        ],
      }));
      updateForm({ coverId: asset.id });
    } catch (cause) {
      if (current === operation.current)
        setActionError(cause instanceof Error ? cause.message : "上传封面失败");
    } finally {
      mutationPending.current = false;
      if (current === operation.current) setActionBusy(false);
    }
  }
  async function perform(action: () => Promise<void>) {
    if (mutationPending.current) return;
    mutationPending.current = true;
    setActionBusy(true);
    setActionError("");
    try {
      await action();
    } catch (cause) {
      setActionError(
        cause instanceof Error ? cause.message : "操作失败，请重试。",
      );
    } finally {
      mutationPending.current = false;
      setActionBusy(false);
    }
  }

  async function removeCurrentDraft() {
    if (
      !savedDraftForAsset ||
      cloudRevision === null ||
      !window.confirm("移除当前发布草稿？视频与封面素材会保留。")
    )
      return;
    const current = operation.current;
    const drafts = (state.publishDrafts ?? []).filter(
      (draft) => draft.id !== savedDraftForAsset.id,
    );
    await perform(async () => {
      if (!review) {
        const record = await saveStudioDraft(
          "publishing",
          { drafts },
          false,
          cloudRevision,
        );
        if (current !== operation.current) return;
        setCloudRevision(record.revision);
      }
      patchState({ publishDrafts: drafts });
      setForm(newPublishDraft(selectedAsset, review));
      setSaveNotice(false);
    });
  }

  return (
    <section className="content-page content-publish">
      <header className="content-title">
        <div>
          <h1>发布管理</h1>
          <p>选择成片与账号，立即发布或定时发布；发布结果在下方记录中回收</p>
        </div>
      </header>
      <section className="content-publish-layout">
        <Panel className="content-publish-drafts">
          <div className="content-publish-draft-tabs">
            <strong>
              发布草稿 <b>{draftCount}</b>
            </strong>
            <span>草稿保存在云端，可反复编辑</span>
          </div>
          {visibleDrafts.length ? (
            visibleDrafts.map((draft) => {
              const draftAsset = data.assets.find(
                (asset) => asset.id === draft.assetId,
              );
              const isReviewSample = !state.publishDrafts;
              return (
                <button
                  className="content-publish-draft-card"
                  key={draft.id}
                  disabled={actionBusy || cloudRevision === null}
                  onClick={() => {
                    setForm(draft);
                    setSaveNotice(false);
                    patchState({ selectedAssetId: draft.assetId });
                  }}
                  type="button"
                >
                  {draftAsset ? (
                    <Media
                      asset={draftAsset}
                      alt={`${draft.title} 草稿封面`}
                      presentation="video"
                    />
                  ) : (
                    <div className="content-publish-draft-card-empty">
                      <Icon name="video" />
                    </div>
                  )}
                  <span>
                    <strong>{draft.title || "未命名发布草稿"}</strong>
                    <small>来源：任务中心</small>
                    <i>已保存 · 待发布</i>
                    <small>{isReviewSample ? "审核示例" : "云端草稿"}</small>
                  </span>
                </button>
              );
            })
          ) : (
            <Empty
              title="暂无发布草稿"
              description={
                selectedAsset
                  ? "填写右侧信息后保存到云端。"
                  : "从任务中心选择一条已完成的视频后创建草稿。"
              }
            />
          )}
          {!review && (
            <div className="content-publish-records-section">
              <div className="content-publish-draft-tabs">
                <strong>发布记录</strong>
                <span>排队 / 发布中会自动刷新</span>
              </div>
              <PublishRecordsPanel
                refreshToken={recordsToken}
                disabled={actionBusy || user.role === "auditor"}
              />
            </div>
          )}
        </Panel>
        <Panel className="content-publish-editor">
          <header className="content-publish-editor-heading">
            <h2>编辑发布草稿</h2>
            <Hint>发布文案独立于口播终稿；点击发布后由服务端投递到平台。</Hint>
          </header>
          <div className="content-publish-media-grid">
            <section className="content-publish-preview">
              <h3>视频预览</h3>
              {selectedAsset ? (
                <Media
                  asset={selectedAsset}
                  alt={`${selectedAsset.name} 视频预览`}
                />
              ) : (
                <Empty
                  title="暂无可发布成片"
                  description="请从任务中心选择一条已完成的视频。"
                />
              )}
            </section>
            <section className="content-publish-cover">
              <h3>封面选择</h3>
              {coverCandidates.length ? (
                <div className="content-cover-options">
                  {coverCandidates.map((asset, index) => (
                    <button
                      disabled={actionBusy || cloudRevision === null}
                      aria-label={`选择封面 ${asset.name}`}
                      className={
                        selectedCover?.id === asset.id ? "is-selected" : ""
                      }
                      key={asset.id}
                      onClick={() => updateForm({ coverId: asset.id })}
                      type="button"
                    >
                      <Media
                        asset={asset}
                        alt={`封面 ${index + 1}`}
                        presentation="video"
                      />
                    </button>
                  ))}
                </div>
              ) : (
                <div className="content-cover-empty">
                  <Icon name="image" />
                  <span>暂无可选封面</span>
                </div>
              )}
              <input
                hidden
                ref={coverInput}
                type="file"
                accept="image/*"
                aria-label="上传发布封面"
                onChange={(event) => {
                  const file = event.target.files?.[0];
                  event.target.value = "";
                  if (file) void uploadCover(file);
                }}
              />
              <Button
                disabled={review || actionBusy || user.role === "auditor"}
                variant="outline"
                onClick={() => coverInput.current?.click()}
              >
                上传自定义封面
              </Button>
            </section>
          </div>
          <div className="content-publish-fields">
            <Field label="发布平台">
              <div className="content-platform-options">
                {(["抖音", "视频号", "小红书"] as const).map((platform) => (
                  <Button
                    disabled={actionBusy || cloudRevision === null}
                    aria-pressed={form.platform === platform}
                    key={platform}
                    onClick={() => {
                      // 小红书协议发布器尚未开发（自动发布暂缓）：选择时只提示进度，
                      // 不切换平台，避免用户停留在无法提交发布的状态。
                      if (platform === "小红书") {
                        notifyRef.current(
                          "小红书自动发布正在开发中，敬请期待。",
                        );
                        return;
                      }
                      updateForm({ platform, account: "" });
                    }}
                    variant={form.platform === platform ? "primary" : "outline"}
                  >
                    <PlatformLogo platform={platform} /> {platform}
                  </Button>
                ))}
              </div>
            </Field>
            <Field label="发布账号">
              {review ? (
                <div className="content-publish-account">
                  <Button
                    aria-pressed={form.account === "张工说乡墅"}
                    onClick={() => updateForm({ account: "张工说乡墅" })}
                    variant="outline"
                  >
                    张工说乡墅
                  </Button>
                  <span>审核示例账号</span>
                </div>
              ) : platformAccounts.length ? (
                <div className="content-publish-account">
                  {/* A select cannot render pictures, so the chosen account's
                      avatar sits beside it to confirm the target at a glance. */}
                  {selectedAccount && (
                    <AccountAvatar account={selectedAccount} size={28} />
                  )}
                  <select
                    disabled={actionBusy || cloudRevision === null}
                    aria-label="选择发布账号"
                    value={selectedAccount?.id ?? ""}
                    onChange={(event) =>
                      updateForm({ account: event.target.value })
                    }
                  >
                    <option value="">请选择发布账号</option>
                    {platformAccounts.map((account) => (
                      <option key={account.id} value={account.id}>
                        {account.username} · {account.platform_user_id}
                        {account.status === "invalid" ? "（登录态失效）" : ""}
                      </option>
                    ))}
                  </select>
                </div>
              ) : (
                <div className="content-publish-account-empty">
                  <span>尚未连接该平台账号，请先扫码连接</span>
                  <Button onClick={() => navigate("profile")} variant="quiet">
                    前往用户档案管理账号
                  </Button>
                </div>
              )}
              {selectedAccount?.status === "invalid" && (
                <p role="alert">
                  该账号登录态已失效，请在用户档案中重新扫码后再发布。
                </p>
              )}
              {!platformDeliverable && (
                <p role="status">
                  小红书自动发布正在开发中；现在可保存草稿、复制文案后前往官方页面发布。
                </p>
              )}
            </Field>
            <Field label="发布时间">
              <div className="content-publish-schedule">
                <Button
                  disabled={actionBusy || cloudRevision === null}
                  aria-pressed={scheduleMode === "now"}
                  variant={scheduleMode === "now" ? "primary" : "outline"}
                  onClick={() => {
                    setScheduleMode("now");
                    setSaveNotice(false);
                  }}
                >
                  立即
                </Button>
                <Button
                  disabled={actionBusy || cloudRevision === null}
                  aria-pressed={scheduleMode === "later"}
                  variant={scheduleMode === "later" ? "primary" : "outline"}
                  onClick={() => {
                    setScheduleMode("later");
                    setSaveNotice(false);
                  }}
                >
                  定时
                </Button>
                {scheduleMode === "later" && (
                  <input
                    type="datetime-local"
                    aria-label="定时发布时间"
                    disabled={actionBusy || cloudRevision === null}
                    min={localDateTimeInputFromIso(
                      new Date(
                        Date.now() + PUBLISH_SCHEDULE_MIN_LEAD_MS,
                      ).toISOString(),
                    )}
                    value={scheduleInput}
                    onChange={(event) => {
                      setScheduleInput(event.target.value);
                      setSaveNotice(false);
                    }}
                  />
                )}
              </div>
            </Field>
            <Field label="发布标题">
              <input
                disabled={actionBusy || cloudRevision === null}
                onChange={(event) => updateForm({ title: event.target.value })}
                placeholder="填写发布标题"
                value={form.title}
                maxLength={300}
              />
            </Field>
            <Field label="发布描述">
              <textarea
                disabled={actionBusy || cloudRevision === null}
                onChange={(event) =>
                  updateForm({ description: event.target.value })
                }
                placeholder="填写发布说明"
                value={form.description}
                maxLength={5000}
              />
            </Field>
            <Field label="标签">
              <div className="content-publish-tags">
                {form.tags.map((tag) => (
                  <Button
                    key={tag}
                    disabled={actionBusy || cloudRevision === null}
                    aria-label={`移除标签 ${tag}`}
                    onClick={() =>
                      updateForm({
                        tags: form.tags.filter((value) => value !== tag),
                      })
                    }
                  >
                    # {tag} ×
                  </Button>
                ))}
                <input
                  disabled={actionBusy || cloudRevision === null}
                  aria-label="添加标签"
                  onChange={(event) => setTagInput(event.target.value)}
                  onKeyDown={(event) => {
                    if (event.key !== "Enter") return;
                    event.preventDefault();
                    addTag();
                  }}
                  placeholder="输入标签后按 Enter"
                  value={tagInput}
                />
              </div>
            </Field>
          </div>
          {saveNotice && (
            <p className="content-publish-save-notice">
              {review
                ? "已保存审核示例草稿，未同步到云端。"
                : "已保存到云端，可在刷新后继续编辑。"}
            </p>
          )}
          {cloudRevision === null && !cloudError && (
            <p role="status">正在加载云端发布草稿…</p>
          )}
          {cloudError && <p role="alert">{cloudError}</p>}
          {actionError && <p role="alert">{actionError}</p>}
          {!review && (
            <Button
              disabled={actionBusy}
              onClick={() => setReload((value) => value + 1)}
            >
              重新加载云端草稿与本机账号
            </Button>
          )}
          <p>
            点击「立即发布」或「定时发布」后，服务端会用所选账号的登录态自动上传并创建作品；桌面端仍可打开官方页面手动发布。
          </p>
          <div className="content-publish-actions">
            <Button
              disabled={
                !selectedAsset ||
                actionBusy ||
                cloudRevision === null ||
                user.role === "auditor"
              }
              onClick={() => void savePublishDraft()}
              variant="outline"
            >
              保存草稿
            </Button>
            <Button
              disabled={
                !savedDraftForAsset ||
                cloudRevision === null ||
                actionBusy ||
                user.role === "auditor"
              }
              onClick={() => void removeCurrentDraft()}
            >
              移除当前草稿
            </Button>
            <Button
              disabled={!selectedAsset || review || actionBusy}
              onClick={() =>
                selectedAsset &&
                void perform(() =>
                  downloadMaterialAsset(
                    selectedAsset.materialId ??
                      selectedAsset.assetId ??
                      selectedAsset.id,
                    `${form.title || "发布视频"}.mp4`,
                  ),
                )
              }
            >
              下载视频
            </Button>
            <Button
              disabled={selectedCover?.kind !== "image" || review || actionBusy}
              onClick={() =>
                selectedCover &&
                void perform(() =>
                  downloadMaterialAsset(
                    selectedCover.materialId ??
                      selectedCover.assetId ??
                      selectedCover.id,
                    selectedCover.name,
                  ),
                )
              }
            >
              下载封面
            </Button>
            <Button
              disabled={!form.title.trim() || review || actionBusy}
              onClick={() =>
                void perform(() =>
                  navigator.clipboard.writeText(
                    [
                      form.title,
                      form.description,
                      form.tags.map((tag) => `#${tag}`).join(" "),
                    ]
                      .filter(Boolean)
                      .join("\n"),
                  ),
                )
              }
            >
              复制发布文案
            </Button>
            <Button
              disabled={
                !selectedAccount ||
                selectedAccount.status === "invalid" ||
                !selectedAsset ||
                !platformDeliverable ||
                review ||
                actionBusy ||
                cloudRevision === null ||
                user.role === "auditor"
              }
              variant="primary"
              onClick={() => void submitPublish()}
            >
              {scheduleMode === "later" ? "定时发布" : "立即发布"}
            </Button>
            {canUseLocalPublishAccounts() && (
              <Button
                disabled={
                  !selectedLocalProfile ||
                  !selectedAsset ||
                  review ||
                  actionBusy ||
                  user.role === "auditor"
                }
                variant="outline"
                onClick={() =>
                  selectedLocalProfile &&
                  void perform(() =>
                    openLocalPublishAccount(user.id, selectedLocalProfile.id),
                  )
                }
              >
                前往官方发布
              </Button>
            )}
          </div>
        </Panel>
      </section>
    </section>
  );
}
