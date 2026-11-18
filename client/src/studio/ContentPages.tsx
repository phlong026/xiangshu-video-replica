import { invoke, isTauri } from "@tauri-apps/api/core";
import type { RefObject } from "react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type {
  TrainingMaterialPublicItem,
  ViralImportPurpose,
  ViralImportTask,
  ViralPlatform,
  ViralVideoItem,
} from "../api";
import {
  cancelReplicaPipeline,
  confirmReplicaPipeline,
  createMaterialUploadIntent,
  createPublishRecord,
  createViralImportTask,
  downloadMaterialAsset,
  downloadVideoToFile,
  fetchCoverStyles,
  fetchReplicaPipeline,
  fetchViralVideoMedia,
  findActiveReplicaPipeline,
  generateCoverFromTemplate,
  getAssetDownloadUrl,
  getStudioDraft,
  getViralImportTask,
  initiateReplicaPipeline,
  listReplicaPipelineCharacters,
  listTrainingMaterials,
  listViralFavorites,
  listViralVideos,
  openViralVideoDetail,
  putMaterial,
  quoteReplicaPipeline,
  type ReplicaPipelineQuote,
  type ReplicaPipelineStatus,
  removeViralFavorite,
  replicaPipelineIdempotencyKey,
  resolveMaterials,
  saveStudioDraft,
  saveViralFavorite,
  trainingMaterialDownloadUrl,
} from "../api";
import { openWalletIfInsufficientCredits } from "../insufficientCredits";
import { VideoPreview } from "../VideoPreview";
import { useStudio } from "./context";
import { studioAssetFromMaterial, studioVideoFromViral } from "./live";
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
import type { StudioAsset, StudioPublishDraft, StudioVideo } from "./types";
import {
  Button,
  Empty,
  Field,
  formatTaskTime,
  Hint,
  Icon,
  Media,
  Panel,
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
import { isPublishedViralVideo } from "./viralVisibility";
import "./content.css";

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
    ? "—"
    : (video.likeDisplay ?? formatCount(video.likes));
}

function viralMetricLabel(value: number | null | undefined) {
  return value == null ? "—" : formatCount(value);
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
  onOpenDetail,
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
  onOpenDetail: () => void;
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
          <div className="viral-card-overlay-text">
            <h3>
              <button
                type="button"
                className="viral-card-title"
                onClick={onOpenDetail}
              >
                {video.title}
              </button>
            </h3>
            {video.tags?.length ? (
              <div className="viral-card-tags">
                {video.tags.slice(0, 6).map((tag) => (
                  <span key={tag} title={`#${tag}`}>
                    #{tag}
                  </span>
                ))}
              </div>
            ) : null}
          </div>
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

export const REPLICA_STAGE_LABELS: Record<string, string> = {
  ENQUEUED: "排队中",
  IMPORTING: "导入参考素材",
  ANALYZING: "拆解分镜与文案",
  SCRIPT_VERSIONING: "保存文案",
  FIRST_FRAMING: "准备人物首帧",
  COMPOSING: "合成最终提示词",
  AWAIT_CONFIRM: "待确认费用",
  GENERATING: "生成视频中",
  NEEDS_REVIEW: "待核对",
  NEEDS_REVIEW_SETTLED: "待核对（已收口）",
  DRAINING: "收尾中",
  DELIVERED: "已交付",
  DELIVERED_PARTIAL: "已交付（部分）",
  FAILED: "失败",
  CANCELLED: "已放弃",
  EXPIRED: "已过期",
};

/**
 * 全自动复刻入口与状态控制（M8 v1）.
 *
 * 一键发起：导入→拆解→首帧→合成提示词全程自动；到达确认点显示预估积分，
 * 用户确认后才建批次扣费（资金闸门在前端只做展示与确认，校验在服务端）。
 * v1 简化（M8b 迭代）：人物候选自动取第一个（多人物选择弹层待做）、
 * 300s 报价倒计时未做（过期确认会被服务端拒绝并要求重核价）。
 */
function ReplicaPipelineControl({ video }: { video: StudioVideo }) {
  const { notify, user } = useStudio();
  const [pipelineId, setPipelineId] = useState<string | null>(null);
  const [status, setStatus] = useState<ReplicaPipelineStatus | null>(null);
  const [quote, setQuote] = useState<ReplicaPipelineQuote | null>(null);
  const [quoteError, setQuoteError] = useState<string | null>(null);
  const [quoteAgeMs, setQuoteAgeMs] = useState(0);
  const [busy, setBusy] = useState(false);
  // 幂等键按「尝试」生成（评审 M8-r1#6）：同一 busy 周期内网络重试复用，
  // 放弃/终态后清空，下一次点击是新尝试（不会重放原终态）。
  const attemptKeyRef = useRef<string | null>(null);
  const [recovering, setRecovering] = useState(true);
  const [recoveryError, setRecoveryError] = useState(false);
  const [recoveryAttempt, setRecoveryAttempt] = useState(0);

  // biome-ignore lint/correctness/useExhaustiveDependencies: 用户点击重试时重新执行恢复查询。
  useEffect(() => {
    let cancelled = false;
    setRecovering(true);
    setRecoveryError(false);
    if (!video.platformKey || !video.nativeId) {
      setRecovering(false);
      return;
    }
    void findActiveReplicaPipeline(video.platformKey, video.nativeId)
      .then((result) => {
        if (!cancelled) setPipelineId(result.pipelineId);
      })
      .catch(() => {
        if (!cancelled) setRecoveryError(true);
      })
      .finally(() => {
        if (!cancelled) setRecovering(false);
      });
    return () => {
      cancelled = true;
    };
  }, [video.platformKey, video.nativeId, recoveryAttempt]);

  useEffect(() => {
    if (!pipelineId) return;
    let cancelled = false;
    const tick = async () => {
      try {
        const next = await fetchReplicaPipeline(pipelineId);
        if (cancelled) return;
        setStatus(next);
        // 报价不随轮询自动续发（评审 M8-r1#3）：进入待确认时取一次，
        // 用户核对的是这一份；过期后由「重新核价」显式刷新。
        if (next.status === "AWAIT_CONFIRM" && !quote) {
          try {
            const fresh = await quoteReplicaPipeline(pipelineId);
            if (!cancelled) {
              setQuote(fresh);
              setQuoteError(null);
              setQuoteAgeMs(0);
            }
          } catch (cause) {
            // 报价失败不能静默（评审 M8-r3#4）：价格变化等错误要显式展示，
            // 且「放弃」操作不依赖报价成功。
            if (!cancelled) {
              setQuoteError(
                cause instanceof Error ? cause.message : "报价获取失败",
              );
            }
          }
        }
      } catch {
        // 轮询失败静默重试（页面离开由 cleanup 停止）。
      }
    };
    void tick();
    const timer = window.setInterval(tick, 5000);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [pipelineId, quote]);

  // 报价 300 秒有效期：倒计时用于置灰确认。
  useEffect(() => {
    if (!quote) return;
    const timer = window.setInterval(
      () => setQuoteAgeMs((age) => age + 1000),
      1000,
    );
    return () => window.clearInterval(timer);
  }, [quote]);

  const begin = async () => {
    if (!video.platformKey || !video.nativeId) {
      notify("该视频缺少平台标识，无法发起全自动复刻");
      return;
    }
    // 预处理费用披露（评审 M8-r1#1）：拆解与首帧在确认点之前就会产生费用。
    const acknowledged = window.confirm(
      [
        "全自动复刻说明：",
        "1. 系统将自动完成：导入参考素材 → 拆解分镜与文案 → 生成 3 张首帧候选并选定 → 合成最终提示词。",
        "2. 拆解与首帧生成会按现有计费口径产生费用（具体以账单为准），每个环节失败自动重试至多 2 次。",
        "3. 提示词就绪后**不会自动扣费生成**：会先展示费用明细，需要你确认一次才开始生成视频。",
        "是否继续？",
      ].join("\n"),
    );
    if (!acknowledged) return;
    setBusy(true);
    try {
      const { characters } = await listReplicaPipelineCharacters();
      const character = characters[0];
      if (!character) {
        notify("请先在人物库创建一个可用角色后再使用全自动复刻");
        return;
      }
      attemptKeyRef.current ??= `${replicaPipelineIdempotencyKey(user.id, video.platformKey, video.nativeId)}:${Date.now()}`;
      const created = await initiateReplicaPipeline({
        platform: video.platformKey,
        videoId: video.nativeId,
        characterVersionId: character.characterVersionId,
        idempotencyKey: attemptKeyRef.current,
      });
      setPipelineId(created.id);
      notify("全自动复刻已启动：完成后会请你确认费用再生成");
    } catch (cause) {
      notify(cause instanceof Error ? cause.message : "全自动复刻发起失败");
    } finally {
      setBusy(false);
    }
  };

  const confirmGeneration = async () => {
    if (!pipelineId || !quote) return;
    if (quoteAgeMs > 300_000) {
      notify("报价已过期，请点击「重新核价」");
      return;
    }
    setBusy(true);
    try {
      await confirmReplicaPipeline(pipelineId, quote);
      notify("已确认，开始生成视频");
      setQuote(null);
    } catch (cause) {
      notify(cause instanceof Error ? cause.message : "确认失败，请刷新重试");
    } finally {
      setBusy(false);
    }
  };

  const requote = async () => {
    if (!pipelineId) return;
    setBusy(true);
    try {
      const fresh = await quoteReplicaPipeline(pipelineId);
      setQuote(fresh);
      setQuoteError(null);
      setQuoteAgeMs(0);
    } catch (cause) {
      notify(cause instanceof Error ? cause.message : "报价刷新失败");
    } finally {
      setBusy(false);
    }
  };

  const abandon = async () => {
    if (!pipelineId) return;
    setBusy(true);
    try {
      await cancelReplicaPipeline(pipelineId);
      notify("已放弃全自动复刻（已产出内容保留在项目中）");
      attemptKeyRef.current = null;
      setPipelineId(null);
      setStatus(null);
      setQuote(null);
      setQuoteError(null);
    } catch (cause) {
      notify(cause instanceof Error ? cause.message : "放弃失败");
    } finally {
      setBusy(false);
    }
  };

  if (!pipelineId) {
    if (recoveryError) {
      return (
        <div role="alert">
          已有复刻任务查询失败，请重试。
          <Button
            variant="outline"
            onClick={() => setRecoveryAttempt((value) => value + 1)}
          >
            重新检查任务
          </Button>
        </div>
      );
    }
    return (
      <Button disabled={busy || recovering} variant="outline" onClick={begin}>
        {busy ? "准备中…" : "全自动复刻"}
      </Button>
    );
  }
  const label = status
    ? (REPLICA_STAGE_LABELS[status.status] ?? status.status)
    : "启动中…";
  const quoteStale = quoteAgeMs > 300_000;
  const fees = status?.feeSummary;
  return (
    <div className="replica-pipeline-panel">
      <p role="status">全自动复刻：{label}</p>
      {status?.status === "AWAIT_CONFIRM" ? (
        <div className="replica-pipeline-review">
          {status.promptText ? (
            <details>
              <summary>最终提示词（核对后确认）</summary>
              <p>{status.promptText}</p>
            </details>
          ) : null}
          {status.scriptText ? (
            <details>
              <summary>口播文案终稿</summary>
              <p>{status.scriptText}</p>
            </details>
          ) : null}
          {status.firstFrameAssetId ? (
            <p className="admin-hint">首帧已选定（可在复刻页预览）</p>
          ) : null}
          {quote ? (
            <p className="admin-hint">
              费用：已结算 {fees?.settledCredits ?? 0} 积分 · 仍预占{" "}
              {fees?.reservedCredits ?? 0} 积分 · 本次新增{" "}
              {quote.estimatedCredits} 积分 （{quote.priceTier}，
              {quote.durationSeconds} 秒）
            </p>
          ) : null}
          {quoteError ? (
            <p className="admin-hint" role="alert">
              {quoteError}
            </p>
          ) : null}
          <div className="replica-pipeline-actions">
            <Button
              disabled={busy || quoteStale || !quote}
              variant="primary"
              onClick={confirmGeneration}
            >
              {!quote
                ? "正在获取报价…"
                : quoteStale
                  ? "报价已过期"
                  : `确认并生成（预估 ${quote.estimatedCredits} 积分）`}
            </Button>
            <Button disabled={busy} variant="outline" onClick={requote}>
              重新核价
            </Button>
            <Button disabled={busy} variant="quiet" onClick={abandon}>
              放弃
            </Button>
          </div>
        </div>
      ) : null}
      {status && ["FAILED", "CANCELLED", "EXPIRED"].includes(status.status) ? (
        <div className="replica-pipeline-actions">
          <p className="admin-hint">任务已结束，可在项目里转手动继续处理</p>
          <Button
            disabled={busy}
            variant="outline"
            onClick={() => {
              // 重新发起 = 新尝试（评审 M8-r2#6/M8-r3#5）：换幂等键并清空
              // 状态与报价，回到入口按钮；网络结果不确定时用户可先留在
              // 本页重试原键（放弃路径亦会清键）。
              attemptKeyRef.current = null;
              setPipelineId(null);
              setStatus(null);
              setQuote(null);
              setQuoteError(null);
            }}
          >
            重新发起
          </Button>
        </div>
      ) : null}
      {status && ["DELIVERED", "DELIVERED_PARTIAL"].includes(status.status) ? (
        <p className="admin-hint">成片已生成，可在任务中心查看与下载</p>
      ) : null}
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

export function ViralCard({
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
  onFavoriteChange?: (saved: boolean) => void;
  availability?: ViralVideoItem["availability"];
  cacheProgress?: ViralCacheProgress;
}) {
  const { review, navigate, notify, openLive } = useStudio();
  const playerRef = useRef<HTMLVideoElement | null>(null);
  const [downloadBusy, setDownloadBusy] = useState(false);
  // 一键复刻爆款按钮常驻（用户拍板 2026-11-15）：按时长在详情页意图里分流
  // ——≤30s 走 AI 视频复刻，>30s 走数字人口播路线，不再按门槛隐藏入口。
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
          const media = await fetchViralVideoMedia(
            platformKey,
            nativeId,
            "video",
          );
          const result = await downloadVideoToFile(
            async () => media.url,
            `${(video.title || nativeId).slice(0, 40)}.mp4`,
          );
          if (result.status === "saved") notify("视频已保存到所选位置");
          else if (result.status === "started")
            notify("已开始下载，请在新窗口完成保存");
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
        onOpenDetail={openDetail}
      />
      <ViralFavoriteButton
        compact
        video={video}
        savedFromServer={savedFromServer}
        onSavedChange={onFavoriteChange}
      />
      <div className="viral-card-body">
        {/* 布局重设计（用户拍板 2026-11-15）：标题/统计/标签全部浮层压在封面
            上（见 ViralCover 的 overlay-text），卡片高度由封面 9:16 统一决定，
            不再被下方文字撑出参差；统计来源元数据行整体取消。body 只留按钮
            区，等宽两列。 */}
        <div className="content-card-actions">
          {/* 一键复刻爆款=卡片主 CTA，常驻按时长分流：≤30s 直接 AI 视频复刻，
              >30s 直接数字人口播路线（详见详情页意图消费）。 */}
          <Button
            variant="primary"
            aria-label={`一键复刻爆款：${video.title}`}
            onClick={() => {
              navigate("viral-detail", {
                selectedVideoId: video.id,
                returnTo: "viral",
                viralReplicateIntent: true,
              });
              persistViralDetailUrl(video);
            }}
          >
            一键复刻爆款
          </Button>
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
  const [platform, setPlatform] = useState<"抖音" | "视频号" | "小红书">(
    "抖音",
  );
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
  const homepageClockOffsetRef = useRef(homepageClockOffset);
  homepageClockOffsetRef.current = homepageClockOffset;
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
    platform === "抖音"
      ? "douyin"
      : platform === "小红书"
        ? "xiaohongshu"
        : "wechat_channels";
  const sortKey = sort === "最新" ? "latest" : "hot";
  const paginationContextKey = `${accountId}:${scope}:${platformKey}:${sortKey}`;
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

  // 首页缓存与公共列表共用推送标记；私有导入和历史抓取不进入客户目录。
  const curated = useMemo(
    () =>
      data.videos.filter(
        (item) =>
          isPublishedViralVideo(item, homepageNow) &&
          (availabilityByKey.get(viralIdentity(item)) ?? "available") ===
            "available",
      ),
    [data.videos, homepageNow, availabilityByKey],
  );
  useEffect(() => {
    if (review) return;
    const now = Math.max(homepageNow, Date.now() - homepageClockOffset);
    const boundaries = [
      nextHomepageChange,
      ...data.videos.flatMap((item) =>
        item.platformKey === platformKey && item.pushedAt
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
    homepageClockOffset,
    homepageNow,
    nextHomepageChange,
    platformKey,
    review,
  ]);
  const shown = useMemo(() => {
    const source = curated;
    return source
      .filter(
        (item) =>
          item.platform === platform &&
          (scope !== "favorites" || favoriteKeys.has(viralIdentity(item))),
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
  }, [curated, favoriteKeys, platform, scope, sort, review]);

  // biome-ignore lint/correctness/useExhaustiveDependencies: 平台/排序变化时重置滚动加载计数。
  useEffect(() => {
    setVisibleCount(viralInitialCount);
  }, [platform, scope, sort]);

  const current = review ? shown.slice(0, visibleCount) : shown;

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
        setHomepageNow(Date.now() - homepageClockOffsetRef.current);
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
      setHomepageNow(Date.now() - homepageClockOffsetRef.current);
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

  const platformTabs = (["抖音", "视频号", "小红书"] as const).map((item) => ({
    id: item,
    label: `${item} ${
      review
        ? item === "抖音"
          ? 20
          : 30
        : curated.filter((video) => video.platform === item).length
    }`,
  }));

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
            onClick={() => setScope("all")}
            role="tab"
            type="button"
          >
            全部爆款
          </button>
          <button
            aria-selected={scope === "favorites"}
            className={scope === "favorites" ? "is-active" : ""}
            onClick={() => setScope("favorites")}
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
              onClick={() => setPlatform(item.id)}
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
      {!review && <ViralCacheUsage revision={cachedOnPage} />}
      {listError && (
        <p className="viral-media-status is-error" role="status">
          {listError}
        </p>
      )}
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
    patchDraft,
    patchState,
    updateData,
    extractScriptFromUpload,
  } = useStudio();
  const accountId = user.id;
  const selectedVideo = state.selectedVideoId
    ? data.videos.find((item) => item.id === state.selectedVideoId)
    : undefined;
  const [remoteVideo, setRemoteVideo] = useState<StudioVideo>();
  const [detailStatus, setDetailStatus] = useState<
    "idle" | "loading" | "error"
  >("idle");
  const [detailAvailability, setDetailAvailability] = useState<
    ViralVideoItem["availability"]
  >(review ? "available" : "unavailable");
  const [detailFavorite, setDetailFavorite] = useState<boolean>();
  // 查看详情免费（2026-10-13 拍板）：进入详情读一次服务端权威数据（含互动统计），
  // 不再展示计费回执；失败时只透传原因。
  const [detailError, setDetailError] = useState<string>();
  const detailAccountRef = useRef(accountId);
  const detailRequestRef = useRef(0);
  detailAccountRef.current = accountId;
  const detailParams = useMemo(viralDetailParams, []);
  const targetPlatform = selectedVideo?.platformKey ?? detailParams?.platform;
  const targetVideoId = selectedVideo?.nativeId ?? detailParams?.videoId;
  const video = remoteVideo ?? selectedVideo;
  const { importState, start } = useViralImport(video?.id, accountId);

  useEffect(() => {
    setRemoteVideo(undefined);
    setDetailFavorite(undefined);
    setDetailAvailability(review ? "available" : "unavailable");
    setDetailError(undefined);
    if (review || !targetPlatform || !targetVideoId) {
      setDetailStatus("idle");
      return;
    }
    const requestAccountId = accountId;
    const requestId = ++detailRequestRef.current;
    const isCurrent = () =>
      detailAccountRef.current === requestAccountId &&
      detailRequestRef.current === requestId;
    setDetailStatus("loading");
    void openViralVideoDetail(targetPlatform, targetVideoId)
      .then(({ item }) => {
        if (!isCurrent()) return;
        const restored = studioVideoFromViral(item);
        setDetailAvailability(item.availability ?? "available");
        setDetailFavorite(Boolean(item.isFavorite));
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
        setDetailAvailability("unavailable");
        setDetailError(
          cause instanceof Error ? cause.message : "查看详情失败，请重试。",
        );
      });
    return () => {
      if (detailRequestRef.current === requestId) detailRequestRef.current += 1;
    };
  }, [targetPlatform, targetVideoId, review, updateData, accountId]);

  useEffect(() => {
    if (video) persistViralDetailUrl(video);
  }, [video]);

  // 互动数据随免费详情响应下发，客户页面不触发源站补采。
  const { playback, play, retry, markFailed } = useViralPlayback(video);
  const beginReplicateRef = useRef<() => void>(() => {});
  const requestExtractRef = useRef<() => void>(() => {});
  // ADM-17 卡片「去复刻」直达：意图即焚；handler 经 ref 晚绑定，保持 hook
  // 顺序稳定（beginReplicate 的定义在组件 early return 之后）。
  useEffect(() => {
    if (
      !state.viralReplicateIntent ||
      !video ||
      detailStatus !== "idle" ||
      detailAvailability !== "available"
    )
      return;
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
      // 一键复刻爆款按时长分流（用户拍板 2026-11-15）：>30s 直接走数字人口
      // 播路线——导入转写后落文案页确认，确认终稿即可口播；不再弹「暂不支持」。
      if (!video.platformKey || !video.nativeId) {
        notify("该视频缺少平台标识，无法复刻");
        return;
      }
      requestExtractRef.current();
      return;
    }
    if (video.durationMs < 4_000) {
      notify("该视频不足 4 秒，请走「提取文案」方向");
      return;
    }
    beginReplicateRef.current();
  }, [
    state.viralReplicateIntent,
    video,
    notify,
    patchState,
    detailStatus,
    detailAvailability,
  ]);
  // 卡片「提取文案」直达：与复刻意图同款即焚消费，复用下方 requestExtract
  // 的服务端导入 → 转写链路。
  useEffect(() => {
    if (
      !state.viralExtractIntent ||
      !video ||
      detailStatus !== "idle" ||
      detailAvailability !== "available"
    )
      return;
    patchState({ viralExtractIntent: false });
    if (!video.platformKey || !video.nativeId) {
      notify("该视频缺少平台标识，无法提取文案");
      return;
    }
    requestExtractRef.current();
  }, [
    state.viralExtractIntent,
    video,
    notify,
    patchState,
    detailStatus,
    detailAvailability,
  ]);

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
    // 服务端导入链路：正文随本人转写任务直出（共享文案缓存已退役），
    // 不再需要携带视频身份回「获取文案」取正文。
    extractScriptFromUpload(task.projectId, task.sourceAssetId);
    navigate("copy", {
      selectedVideoId: video.id,
      returnTo: "viral-detail",
    });
  };
  // 提取文案统一走服务端导入 → 转写：每个用户各自转写各自计费，
  // 不再先查共享文案缓存分流。
  const requestExtract = () => {
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
  // 全自动复刻单独门禁 4–15 秒（评审 M8-r2#2）：自动生成批次上限 15 秒，
  // 16–30 秒走手动复刻分段，避免先收费拆解/首帧再必然失败。
  const canReplicaAuto = canReplicate && (video.durationMs ?? 0) <= 15_000;
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
          {detailError && (
            <p className="viral-media-status is-error" role="status">
              {detailError}
            </p>
          )}
          {detailAvailability !== "available" && (
            <p className="viral-media-status is-error" role="status">
              该视频已不可用，暂不能导入创作。
            </p>
          )}
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
            {canReplicaAuto &&
              !review &&
              detailStatus === "idle" &&
              detailAvailability === "available" && (
                <div className="content-detail-action">
                  <ReplicaPipelineControl
                    key={`${user.id}:${video.platformKey}:${video.nativeId}`}
                    video={video}
                  />
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
// 骨架屏占位卡数量：静态键名数组，避免数组索引 key（biome noArrayIndexKey）。
const SKELETON_KEYS = ["s1", "s2", "s3", "s4", "s5", "s6"];
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
  return [".docx", ".doc", ".xlsx", ".xls", ".pptx", ".ppt"].includes(
    item.file_suffix,
  );
}

export function MaterialsPage() {
  const { notify } = useStudio();
  const [tab, setTab] = useState("");
  const [tags, setTags] = useState<string[]>([]);
  const [search, setSearch] = useState("");
  const [page, setPage] = useState(1);
  // 全量素材元数据（当前标签筛选维度）：素材库是运营上传的小体量只读列表，
  // 全量在手才能做类型计数、站内搜索与纯前端分页，不必为计数加服务端 facet。
  const [library, setLibrary] = useState<TrainingMaterialPublicItem[]>([]);
  const [tagFacet, setTagFacet] = useState<string[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  // 预览地址按需签发（30 分钟防盗链直链），会话内已签的复用；弹层内取链，
  // 封面同理。直链过期由媒体 error 事件触发重签一次（§5.2）。
  const [previewUrls, setPreviewUrls] = useState<Record<string, string>>({});
  const [busyId, setBusyId] = useState("");
  // 封面/弹层媒体加载失败的记录：只有失败的卡才显示「重试」，正常卡不暴露
  // 「重新加载」这类实现细节词汇（MATERIAL-UX P0，2026-11-15）。
  const [failedIds, setFailedIds] = useState<string[]>([]);
  const renewedMedia = useRef(new Set<string>());
  const [previewItem, setPreviewItem] =
    useState<TrainingMaterialPublicItem | null>(null);

  const load = useCallback(async (tagFilter: string[]) => {
    try {
      setLoading(true);
      setError("");
      // 服务端 pageSize 上限 100：循环翻页拿全量（上限 10 页 = 1000 条，
      // 培训素材实际量远低于此；触顶时按已取得的条目呈现）。
      const first = await listTrainingMaterials({
        tags: tagFilter.length ? tagFilter : undefined,
        page: 1,
        pageSize: 100,
      });
      const collected = [...first.items];
      const pages = Math.min(Math.ceil(first.total / 100), 10);
      for (let next = 2; next <= pages; next += 1) {
        const part = await listTrainingMaterials({
          tags: tagFilter.length ? tagFilter : undefined,
          page: next,
          pageSize: 100,
        });
        collected.push(...part.items);
      }
      setLibrary(collected);
      setTagFacet(first.tag_facet);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "读取培训素材失败");
    } finally {
      setLoading(false);
    }
  }, []);

  // 换账号时按 key 重挂载由调用方保证（与旧页同语义）；标签变化时重拉全量。
  useEffect(() => {
    void load(tags);
    setPage(1);
  }, [load, tags]);

  async function ensurePreviewUrl(
    item: TrainingMaterialPublicItem,
    forceRefresh = false,
  ): Promise<string | null> {
    const cached = previewUrls[item.id];
    if (cached && !forceRefresh) return cached;
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
    if (renewedMedia.current.has(item.id)) return;
    renewedMedia.current.add(item.id);
    const url = await ensurePreviewUrl(item, true);
    if (!url) setFailedIds((current) => [...current, item.id]);
  }

  async function retryPreview(item: TrainingMaterialPublicItem) {
    // 用户主动重试开启新一轮，自动重签每轮仍只允许一次。
    renewedMedia.current.delete(item.id);
    setFailedIds((current) => current.filter((id) => id !== item.id));
    await ensurePreviewUrl(item, true);
  }

  async function openPreview(item: TrainingMaterialPublicItem) {
    const url = await ensurePreviewUrl(item);
    if (url) setPreviewItem(item);
  }

  async function download(item: TrainingMaterialPublicItem) {
    const url = await ensurePreviewUrl(item, true);
    if (!url) return;
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = `${item.title}${item.file_suffix}`;
    // 跨域直链可能按预览打开，新页兜底避免替换工作台；下载时始终重新签发。
    anchor.target = "_blank";
    anchor.rel = "noopener noreferrer";
    document.body.append(anchor);
    anchor.click();
    anchor.remove();
    notify("文件将在新窗口打开；若浏览器显示预览，请使用另存为保存。");
  }

  // 类型页签 + 站内搜索：先按类型（空 tab=全部）再按关键词命中
  // （标题 / 描述 / 标签，大小写不敏感），全量内过滤。
  const keyword = search.trim().toLowerCase();
  const filtered = useMemo(() => {
    const byKind = tab ? library.filter((item) => item.kind === tab) : library;
    if (!keyword) return byKind;
    return byKind.filter(
      (item) =>
        item.title.toLowerCase().includes(keyword) ||
        item.description.toLowerCase().includes(keyword) ||
        item.tags.some((tag) => `#${tag}`.toLowerCase().includes(keyword)),
    );
  }, [library, keyword, tab]);
  const items = filtered.slice(
    (page - 1) * TRAINING_PAGE_SIZE,
    page * TRAINING_PAGE_SIZE,
  );
  const total = filtered.length;
  const totalPages = Math.max(1, Math.ceil(total / TRAINING_PAGE_SIZE));
  const kindCounts = useMemo(() => {
    const counts: Record<string, number> = {};
    for (const item of library) {
      counts[item.kind] = (counts[item.kind] ?? 0) + 1;
    }
    return counts;
  }, [library]);
  const previewUrl = previewItem ? previewUrls[previewItem.id] : undefined;

  return (
    <section className="content-page content-materials">
      <header className="content-title content-material-title">
        <div>
          <h1>素材库</h1>
          <p className="le-sub">平台统一提供的培训课程、示例视频与资料文档。</p>
        </div>
      </header>
      {/* 筛选区收敛为一行下划线页签 + 搜索框（对齐同行素材库的紧凑工具栏，
          不让筛选占据版面）；标签降为小巧胶囊，仅在有标签时出现。 */}
      <div className="content-material-toolbar">
        <div
          className="content-view-switch"
          role="tablist"
          aria-label="素材类型"
        >
          {TRAINING_TABS.map((entry) => {
            // 计数随标签筛选联动（标签变了全量重拉，计数天然联动）；
            // 「全部」= 库内总数。
            const count = entry.id
              ? (kindCounts[entry.id] ?? 0)
              : library.length;
            return (
              <button
                key={entry.id || "all"}
                type="button"
                role="tab"
                aria-selected={tab === entry.id}
                className={tab === entry.id ? "is-active" : undefined}
                onClick={() => {
                  setTab(entry.id);
                  setPage(1);
                }}
              >
                {entry.label}
                <small>{count}</small>
              </button>
            );
          })}
        </div>
        <input
          aria-label="搜索素材"
          className="content-material-search"
          onChange={(event) => {
            setSearch(event.target.value);
            setPage(1);
          }}
          placeholder="搜索标题、描述、标签"
          type="search"
          value={search}
        />
        {!loading ? (
          <span className="content-material-total">共 {total} 个素材</span>
        ) : null}
      </div>
      {tagFacet.length > 0 ? (
        <fieldset
          className="content-material-tags"
          aria-label="按标签筛选"
          style={{ border: 0, padding: 0, margin: 0 }}
        >
          {tagFacet.map((entry) => {
            const active = tags.includes(entry);
            return (
              <button
                key={entry}
                type="button"
                aria-pressed={active}
                className={active ? "is-active" : undefined}
                onClick={() => {
                  setTags((current) =>
                    current.includes(entry)
                      ? current.filter((t) => t !== entry)
                      : [...current, entry],
                  );
                  setPage(1);
                }}
              >
                #{entry}
              </button>
            );
          })}
          {tags.length > 0 ? (
            <button
              type="button"
              aria-label="清除全部标签筛选"
              onClick={() => {
                setTags([]);
                setPage(1);
              }}
            >
              清除标签
            </button>
          ) : null}
        </fieldset>
      ) : null}
      {error ? <p className="le-hint le-hint--error">{error}</p> : null}
      {loading ? (
        // 骨架屏占位（MATERIAL-UX P0）：加载形态与结果一致，不再跳变。
        <div aria-hidden="true" className="content-material-grid">
          {SKELETON_KEYS.map((key) => (
            <div
              className="content-material-card content-material-card--skeleton"
              key={key}
            />
          ))}
        </div>
      ) : items.length === 0 ? (
        keyword ? (
          <Empty
            description="换个关键词，或清除标签筛选后再试。"
            title="没有找到匹配的素材"
          />
        ) : (
          <Empty
            action={
              <Button variant="outline" onClick={() => void load(tags)}>
                刷新
              </Button>
            }
            description="运营上传的培训素材会集中显示在这里。"
            title="暂无素材"
          />
        )
      ) : (
        <div className="content-material-grid">
          {items.map((item) => {
            const url = previewUrls[item.id];
            const failed = failedIds.includes(item.id);
            return (
              <article
                className="content-material-card"
                data-kind={item.kind}
                key={item.id}
              >
                <button
                  aria-label={`预览${item.title}`}
                  className="content-material-card__cover"
                  disabled={busyId === item.id}
                  onClick={() => void openPreview(item)}
                  type="button"
                >
                  {item.kind === "image" && url ? (
                    <img
                      alt={item.title}
                      loading="lazy"
                      onError={() => void resignOnMediaError(item)}
                      src={url}
                    />
                  ) : item.kind === "video" && url ? (
                    <video
                      muted
                      preload="metadata"
                      src={url}
                      onError={() => void resignOnMediaError(item)}
                    />
                  ) : (
                    <span
                      className="content-material-card__placeholder"
                      data-kind={item.kind}
                    >
                      <Icon
                        aria-hidden="true"
                        name={
                          item.kind === "video"
                            ? "video"
                            : item.kind === "audio"
                              ? "audio"
                              : item.kind === "doc"
                                ? "copy"
                                : "image"
                        }
                        size={30}
                      />
                      <small>{item.kind_label}</small>
                    </span>
                  )}
                  {item.kind === "video" && url ? (
                    <span
                      aria-hidden="true"
                      className="content-material-card__play"
                    >
                      <Icon name="play" size={16} />
                    </span>
                  ) : null}
                </button>
                <div className="content-material-card__body">
                  <strong>{item.title}</strong>
                  {item.description ? <p>{item.description}</p> : null}
                  <span className="content-material-card__meta">
                    <em data-kind={item.kind}>{item.kind_label}</em>
                    {formatMaterialSizeBytes(item.size_bytes)} ·{" "}
                    {formatTaskTime(item.created_at)}
                  </span>
                </div>
                <footer className="content-material-card__actions">
                  <button
                    disabled={busyId === item.id}
                    onClick={() => void openPreview(item)}
                    type="button"
                  >
                    预览
                  </button>
                  <button
                    disabled={busyId === item.id}
                    onClick={() => void download(item)}
                    type="button"
                  >
                    {busyId === item.id ? "获取链接…" : "下载"}
                  </button>
                  {failed ? (
                    // 直链过期且自动重签失败：仅此时给「重试」，正常卡不暴露。
                    <button
                      onClick={() => void retryPreview(item)}
                      type="button"
                    >
                      重试
                    </button>
                  ) : null}
                </footer>
              </article>
            );
          })}
        </div>
      )}
      {previewItem ? (
        <div
          aria-label="素材预览"
          className="content-material-dialog"
          role="dialog"
        >
          <div className="content-material-dialog__panel">
            <header className="content-material-dialog__head">
              <strong>{previewItem.title}</strong>
              <button
                type="button"
                onClick={() => {
                  if (previewItem) void download(previewItem);
                }}
              >
                下载文件
              </button>
              <button type="button" onClick={() => setPreviewItem(null)}>
                关闭
              </button>
            </header>
            <div className="content-material-dialog__body">
              {previewUrl ? (
                previewItem.kind === "video" ? (
                  // biome-ignore lint/a11y/useMediaCaption: 平台培训视频不提供字幕轨。
                  <video
                    autoPlay
                    controls
                    src={previewUrl}
                    onError={() => void resignOnMediaError(previewItem)}
                  />
                ) : previewItem.kind === "audio" ? (
                  // biome-ignore lint/a11y/useMediaCaption: 平台培训音频不提供字幕轨。
                  <audio
                    autoPlay
                    controls
                    src={previewUrl}
                    onError={() => void resignOnMediaError(previewItem)}
                  />
                ) : previewItem.kind === "image" ? (
                  <img
                    alt={`${previewItem.title} 预览`}
                    src={previewUrl}
                    onError={() => void resignOnMediaError(previewItem)}
                  />
                ) : isOfficeDoc(previewItem) ? (
                  <iframe
                    src={officeViewerUrl(previewUrl)}
                    title={`${previewItem.title} 预览`}
                  />
                ) : (
                  <iframe
                    src={previewUrl}
                    title={`${previewItem.title} 预览`}
                  />
                )
              ) : (
                <p className="le-hint">正在获取预览地址…</p>
              )}
              {failedIds.includes(previewItem.id) ? (
                <p className="le-hint le-hint--error">
                  预览地址已失效。
                  <button
                    type="button"
                    onClick={() => void retryPreview(previewItem)}
                  >
                    重试
                  </button>
                  或直接使用「下载文件」。
                </p>
              ) : null}
            </div>
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
  // 模板封面（飞影，用户拍板 2026-11-15）：模板列表懒加载 + 文案自动填充。
  const [coverSource, setCoverSource] = useState<"frames" | "templates">(
    "frames",
  );
  const [coverStyles, setCoverStyles] = useState<
    Array<{ id: string; title: string; url: string }>
  >([]);
  const [coverStylesLoading, setCoverStylesLoading] = useState(false);
  const [coverStylesError, setCoverStylesError] = useState("");
  const [selectedTemplate, setSelectedTemplate] = useState("");
  const [coverContent, setCoverContent] = useState("");
  const [generatingCover, setGeneratingCover] = useState(false);
  const [generatedCoverUrl, setGeneratedCoverUrl] = useState("");
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

  const downloadSelectedVideo = () =>
    selectedAsset &&
    perform(() =>
      downloadMaterialAsset(
        selectedAsset.materialId ?? selectedAsset.assetId ?? selectedAsset.id,
        `${form.title || "发布视频"}.mp4`,
      ),
    );
  const copyPublishText = () =>
    perform(() =>
      navigator.clipboard.writeText(
        [
          form.title,
          form.description,
          form.tags.map((tag) => `#${tag}`).join(" "),
        ]
          .filter(Boolean)
          .join("\n"),
      ),
    );

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
          <p>选择成片与账号发布，或前往快手创作者中心手动发布</p>
        </div>
      </header>
      <section
        aria-labelledby="kuaishou-manual-publish-title"
        className="studio-panel content-publish-manual"
      >
        <div>
          <h2 id="kuaishou-manual-publish-title">快手手动发布</h2>
          <p>
            下载所选成片、复制发布草稿的文案，再到快手创作者中心登录、手动上传并确认发布，结果以快手官方页面为准。
          </p>
          <span>
            {selectedAsset ? `已选择：${selectedAsset.name}` : "尚未选择成片"}
          </span>
        </div>
        <div className="content-publish-manual-actions">
          <Button onClick={() => navigate("tasks")}>
            {selectedAsset ? "更换发布成片" : "选择发布成片"}
          </Button>
          <Button
            disabled={!selectedAsset || review || actionBusy}
            onClick={() => void downloadSelectedVideo()}
          >
            下载所选成片
          </Button>
          <Button
            disabled={!form.title.trim() || review || actionBusy}
            onClick={() => void copyPublishText()}
          >
            复制快手发布文案
          </Button>
          <a
            className="studio-button studio-button--primary"
            href="https://cp.kuaishou.com/article/publish/video"
            rel="noopener noreferrer"
            target="_blank"
            onClick={(event) => {
              if (!isTauri()) return;
              event.preventDefault();
              setActionError("");
              void invoke<void>("open_kuaishou_creator").catch((cause) => {
                setActionError(
                  cause instanceof Error
                    ? cause.message
                    : typeof cause === "string"
                      ? cause
                      : "无法打开快手创作者中心，请重试。",
                );
              });
            }}
          >
            前往快手手动发布
          </a>
        </div>
      </section>
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
                  action={
                    <Button variant="primary" onClick={() => navigate("tasks")}>
                      去任务中心选择成片
                    </Button>
                  }
                  title="暂无可发布成片"
                  description="选择一条已完成的视频即可开始编辑发布。"
                />
              )}
            </section>
            <section className="content-publish-cover">
              <h3>封面选择</h3>
              <Hint>
                从你的成片中抽取的候选帧；也可以上传自定义图片或用飞影模板生成。
              </Hint>
              <div
                aria-label="封面来源"
                className="content-cover-source"
                role="tablist"
              >
                <button
                  aria-selected={coverSource === "frames"}
                  role="tab"
                  type="button"
                  onClick={() => setCoverSource("frames")}
                >
                  成片帧
                </button>
                <button
                  aria-selected={coverSource === "templates"}
                  role="tab"
                  type="button"
                  onClick={() => {
                    setCoverSource("templates");
                    if (coverStyles.length === 0 && !coverStylesLoading) {
                      setCoverStylesLoading(true);
                      setCoverStylesError("");
                      void fetchCoverStyles()
                        .then((items) => {
                          setCoverStyles(items);
                          if (items.length === 0)
                            setCoverStylesError("暂无可用模板。");
                        })
                        .catch((cause: unknown) => {
                          setCoverStylesError(
                            cause instanceof Error
                              ? cause.message
                              : "读取封面模板失败",
                          );
                        })
                        .finally(() => setCoverStylesLoading(false));
                    }
                  }}
                >
                  模板封面
                </button>
              </div>
              {coverSource === "templates" ? (
                <div className="content-cover-templates">
                  {coverStylesLoading ? (
                    <p className="admin-hint">正在读取模板…</p>
                  ) : null}
                  {coverStylesError ? (
                    <p className="admin-hint">{coverStylesError}</p>
                  ) : null}
                  <div className="content-cover-options">
                    {coverStyles.map((style) => (
                      <button
                        aria-label={`选择模板 ${style.title}`}
                        className={
                          selectedTemplate === style.id ? "is-selected" : ""
                        }
                        key={style.id}
                        onClick={() => setSelectedTemplate(style.id)}
                        type="button"
                      >
                        <img alt={style.title} loading="lazy" src={style.url} />
                        <small>{style.title}</small>
                      </button>
                    ))}
                  </div>
                  {selectedTemplate ? (
                    <div className="content-cover-generate">
                      <textarea
                        aria-label="封面文案"
                        maxLength={500}
                        onChange={(event) =>
                          setCoverContent(event.target.value)
                        }
                        placeholder="封面文案（留空则用草稿标题生成）"
                        rows={2}
                        value={coverContent}
                      />
                      <Button
                        disabled={generatingCover}
                        variant="primary"
                        onClick={() => {
                          const content =
                            coverContent.trim() ||
                            (form.title ? form.title : "") ||
                            "乡村自建房 · 建房指南";
                          if (!selectedTemplate) return;
                          setGeneratingCover(true);
                          void (async () => {
                            try {
                              const generated = await generateCoverFromTemplate(
                                {
                                  canvasPreset: "9:16",
                                  content,
                                  template: selectedTemplate,
                                },
                              );
                              const response = await fetch(
                                `data:image/${
                                  generated.coverId ? "jpeg" : "jpeg"
                                };base64,${generated.imageBase64}`,
                              );
                              const blob = await response.blob();
                              const file = new File(
                                [blob],
                                `封面-${Date.now()}.jpg`,
                                { type: "image/jpeg" },
                              );
                              setGeneratedCoverUrl(
                                `data:image/jpeg;base64,${generated.imageBase64}`,
                              );
                              await uploadCover(file);
                              notify("封面已生成并应用。");
                            } catch (cause) {
                              notify(
                                cause instanceof Error
                                  ? cause.message
                                  : "生成封面失败，请稍后重试。",
                              );
                            } finally {
                              setGeneratingCover(false);
                            }
                          })();
                        }}
                      >
                        {generatingCover ? "生成中…约需 1 分钟" : "生成封面"}
                      </Button>
                      {generatingCover ? (
                        <Hint>生成需要约 1 分钟，请勿关闭页面。</Hint>
                      ) : null}
                      {generatedCoverUrl && !generatingCover ? (
                        <img
                          alt="生成的封面预览"
                          className="content-cover-generated"
                          src={generatedCoverUrl}
                        />
                      ) : null}
                    </div>
                  ) : null}
                </div>
              ) : null}
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
              onClick={() => void downloadSelectedVideo()}
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
              onClick={() => void copyPublishText()}
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
