import type { RefObject } from "react";
import { useEffect, useMemo, useRef, useState } from "react";
import type { MaterialItem, MaterialPage, ViralVideoItem } from "../api";
import {
  completeMaterialUpload,
  createGenerationTaskPreviewUrl,
  createMaterialUploadIntent,
  downloadMaterialAsset,
  fetchViralVideoMedia,
  fetchViralVideoStatistics,
  getAssetDownloadUrl,
  hideMaterial,
  listMaterials,
  listViralVideos,
  updateMaterial,
  uploadMaterial,
} from "../api";
import { useStudio } from "./context";
import { studioAssetFromMaterial, studioVideoFromViral } from "./live";
import type {
  StudioAsset,
  StudioContextValue,
  StudioPublishDraft,
  StudioVideo,
} from "./types";
import { Button, Empty, Field, Hint, Icon, Media, Panel, Tabs } from "./ui";
import "./content.css";

const pageSize = 6;
const categoryTabs = ["全部", "建房预算", "户型设计", "施工避坑", "庭院案例"];

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
const viralRevealStep = 8;

function viralLikesLabel(video: StudioVideo) {
  return video.likeDisplay ?? formatCount(video.likes);
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
  const [brokenPoster, setBrokenPoster] = useState<string>();
  if (!video.poster || brokenPoster === video.poster) {
    return (
      <span
        className={
          className
            ? `viral-card-cover-empty ${className}`
            : "viral-card-cover-empty"
        }
      >
        <Icon name="video" />
      </span>
    );
  }
  return (
    <img
      alt=""
      className={
        className ? `viral-card-cover-img ${className}` : "viral-card-cover-img"
      }
      loading="lazy"
      referrerPolicy="no-referrer"
      onError={() => setBrokenPoster(video.poster)}
      src={video.poster}
    />
  );
}

type ViralPlayback =
  | { status: "idle" }
  | { status: "loading" }
  | { status: "playing"; src: string }
  | { status: "error"; message: string };

function updateViralStats(
  updateData: StudioContextValue["updateData"],
  item?: ViralVideoItem | null,
) {
  updateViralStatistics(updateData, item ? [item] : []);
}

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
            likes: item.likes,
            comments: item.comments,
            shares: item.shares,
            collections: item.collects,
            likeDisplay: item.likeDisplay,
          }
        : video;
    }),
  }));
}

function useViralStatistics(videos: StudioVideo[], enabled: boolean) {
  const { updateData } = useStudio();
  const attemptedIds = useRef(new Set<string>());
  const [error, setError] = useState<string>();
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

  return error;
}

/** 点击播放：真实平台视频先走媒体管线，测试夹具可直接使用 playUrl。 */
function useViralPlayback(
  video?: StudioVideo,
  active = true,
  onActivate?: () => void,
) {
  const { updateData } = useStudio();
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
      const media = await fetchViralVideoMedia(
        video.platformKey,
        video.nativeId,
        "video",
      );
      updateViralStats(updateData, media.video);
      if (requestId === requestIdRef.current && activeRef.current) {
        setPlayback({ status: "playing", src: media.url });
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
}) {
  const publish = viralPublishLabel(video);
  return (
    <div
      className={`viral-card-cover ${playing ? "is-playing" : ""}`}
      title={playing ? undefined : "播放"}
    >
      {playing ? (
        // biome-ignore lint/a11y/useMediaCaption: 源平台视频无字幕轨可挂载
        <video
          ref={playerRef}
          autoPlay
          controls
          playsInline
          src={src}
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
      <span className="viral-card-platform">{video.platform}</span>
      <span className="viral-card-duration">{video.duration}</span>
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
    ["comment", "评论", formatCount(video.comments ?? null)],
    ["share", "转发", formatCount(video.shares)],
    ["star", "收藏", formatCount(video.collections)],
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

function ViralCard({
  video,
  active,
  onActivate,
}: {
  video: StudioVideo;
  active: boolean;
  onActivate: () => void;
}) {
  const { state, navigate, patchDraft, patchState } = useStudio();
  const playerRef = useRef<HTMLVideoElement | null>(null);
  const { playback, play, retry, markFailed, activate } = useViralPlayback(
    video,
    active,
    onActivate,
  );
  useEffect(() => {
    if (!active) playerRef.current?.pause();
  }, [active]);
  const saved = state.favorites.includes(video.id);
  const toggleFavorite = () =>
    patchState({
      favorites: saved
        ? state.favorites.filter((id) => id !== video.id)
        : [...state.favorites, video.id],
    });
  const openDetail = () =>
    navigate("viral-detail", {
      selectedVideoId: video.id,
      returnTo: "viral",
    });
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
      />
      <div className="viral-card-body">
        <h3>{video.title}</h3>
        {video.tags && video.tags.length > 0 && (
          <div className="viral-card-tags">
            {video.tags.slice(0, 6).map((tag) => (
              <span key={tag}>#{tag}</span>
            ))}
          </div>
        )}
        <div className="content-card-actions">
          <Button variant="quiet" onClick={openDetail}>
            查看详情
          </Button>
          <Button
            variant="outline"
            aria-label={`收藏 ${video.title}`}
            onClick={toggleFavorite}
          >
            {saved ? "已收藏" : "收藏"}
          </Button>
          <Button
            variant="outline"
            aria-label={`复刻 ${video.title}`}
            onClick={() => {
              patchDraft({ sourceId: video.id });
              navigate("replica", {
                selectedVideoId: video.id,
                returnTo: "viral",
              });
            }}
          >
            复刻
          </Button>
        </div>
      </div>
    </article>
  );
}

export function ViralPage() {
  const { data, review, updateData } = useStudio();
  const [platform, setPlatform] = useState<"抖音" | "视频号">("抖音");
  const [category, setCategory] = useState("全部");
  const [query, setQuery] = useState("");
  const [sort, setSort] = useState<"热门优先" | "最新">("热门优先");
  const [visibleCount, setVisibleCount] = useState(viralInitialCount);
  const [activeVideoId, setActiveVideoId] = useState<string>();
  const [listError, setListError] = useState<string>();
  const sentinelRef = useRef<HTMLDivElement | null>(null);

  const shown = useMemo(
    () =>
      data.videos
        .filter(
          (item) =>
            item.platform === platform &&
            (category === "全部" || item.category === category) &&
            `${item.title}${item.author}`.includes(query),
        )
        .sort((left, right) => {
          if (sort === "热门优先") {
            return (
              right.likes - left.likes ||
              left.id.localeCompare(right.id, undefined, { numeric: true })
            );
          }
          return (right.publishedAt ?? 0) - (left.publishedAt ?? 0);
        }),
    [category, data.videos, platform, query, sort],
  );

  // biome-ignore lint/correctness/useExhaustiveDependencies: 平台/分类/搜索词/排序变化时重置滚动加载计数。
  useEffect(() => {
    setVisibleCount(viralInitialCount);
  }, [platform, category, query, sort]);

  useEffect(() => {
    const node = sentinelRef.current;
    if (!node) return;
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries.some((entry) => entry.isIntersecting)) {
          setVisibleCount((count) =>
            Math.min(count + viralRevealStep, shown.length),
          );
        }
      },
      { rootMargin: "240px 0px" },
    );
    observer.observe(node);
    return () => observer.disconnect();
  }, [shown.length]);

  const current = shown.slice(0, visibleCount);
  const statisticsError = useViralStatistics(current, !review);

  useEffect(() => {
    if (review) return;
    const platformKey = platform === "抖音" ? "douyin" : "wechat_channels";
    const sortKey = sort === "最新" ? "latest" : "hot";
    let cancelled = false;
    setListError(undefined);
    void listViralVideos(platformKey, sortKey)
      .then((result) => {
        if (cancelled) return;
        const incoming = result.items.map(studioVideoFromViral);
        updateData((currentData) => ({
          ...currentData,
          videos: [
            ...currentData.videos.filter(
              (video) => video.platformKey !== platformKey,
            ),
            ...incoming,
          ],
        }));
      })
      .catch(() => {
        if (!cancelled) setListError("视频列表暂时无法更新，已保留当前内容");
      });
    return () => {
      cancelled = true;
    };
  }, [platform, review, sort, updateData]);
  const platformTabs = (["抖音", "视频号"] as const).map((item) => ({
    id: item,
    label: `${item} ${
      review
        ? item === "抖音"
          ? 20
          : 30
        : data.videos.filter((video) => video.platform === item).length
    }`,
  }));

  return (
    <section className="content-page content-viral">
      <header className="content-title">
        <div>
          <h1>爆款视频</h1>
          <p>乡墅灵感，持续发现 · 最近 7 天爆款</p>
        </div>
        <div className="content-search">
          <Icon name="search" />
          <input
            aria-label="搜索视频标题"
            value={query}
            onChange={(event) => {
              setQuery(event.target.value);
            }}
            placeholder="在已加载的爆款中搜索"
          />
        </div>
      </header>
      <div className="content-viral-toolbar">
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
              {item.label}
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
        {categoryTabs.map((item) => (
          <Button
            key={item}
            variant={item === category ? "primary" : "outline"}
            onClick={() => setCategory(item)}
          >
            {item}
          </Button>
        ))}
      </nav>
      {(listError || statisticsError) && (
        <p className="viral-media-status is-error" role="status">
          {listError ?? statisticsError}
        </p>
      )}
      {current.length ? (
        <>
          <section className="content-video-grid content-video-grid-viral">
            {current.map((video) => (
              <ViralCard
                key={video.id}
                video={video}
                active={activeVideoId === video.id}
                onActivate={() => setActiveVideoId(video.id)}
              />
            ))}
          </section>
          {visibleCount < shown.length && (
            <div className="viral-reveal-sentinel" aria-hidden="true">
              <span>上拉加载更多…</span>
            </div>
          )}
        </>
      ) : (
        <Empty
          title="暂无爆款视频"
          description="数据源尚未配置或最近 7 天暂无内容，配置后自动展示。"
        />
      )}
    </section>
  );
}

type ViralMediaState = {
  status: "idle" | "loading" | "ready" | "error";
  message?: string;
};

function useViralMedia() {
  const { updateData } = useStudio();
  const [media, setMedia] = useState<ViralMediaState>({ status: "idle" });
  return {
    media,
    prepare: async (
      video: StudioVideo,
      onReady: (kind: "audio" | "video") => void,
    ) => {
      if (!video.platformKey || !video.nativeId) {
        onReady("video");
        return;
      }
      setMedia({ status: "loading" });
      try {
        const result = await fetchViralVideoMedia(
          video.platformKey,
          video.nativeId,
        );
        updateViralStats(updateData, result.video);
        setMedia({
          status: "ready",
          message:
            result.kind === "audio" ? "原声音频已就绪" : "低清视频已就绪",
        });
        onReady(result.kind);
      } catch (error) {
        setMedia({
          status: "error",
          message: error instanceof Error ? error.message : "素材准备失败",
        });
      }
    },
  };
}

export function ViralDetailPage() {
  const { data, state, review, navigate, patchDraft, patchState } = useStudio();
  const { media, prepare } = useViralMedia();
  const video = state.selectedVideoId
    ? data.videos.find((item) => item.id === state.selectedVideoId)
    : undefined;
  const statisticsError = useViralStatistics(video ? [video] : [], !review);
  const { playback, play, retry, markFailed } = useViralPlayback(video);
  if (!video)
    return (
      <section className="content-page">
        <Empty
          title="暂未选择参考视频"
          description="请返回爆款视频列表选择一条内容。"
          action={
            <Button variant="primary" onClick={() => navigate("viral")}>
              返回爆款视频
            </Button>
          }
        />
      </section>
    );
  const saved = state.favorites.includes(video.id);
  const published =
    video.publishedDisplay ??
    (video.publishedAt
      ? new Date(video.publishedAt * 1000).toLocaleDateString("zh-CN")
      : "—");
  const metrics: Array<[string, string, string]> = [
    ["heart", "点赞", viralLikesLabel(video)],
    ["comment", "评论", formatCount(video.comments ?? null)],
    ["share", "转发", formatCount(video.shares)],
    ["star", "收藏", formatCount(video.collections)],
  ];
  const goExtract = () => {
    patchDraft({ sourceId: video.id });
    navigate("copy", {
      selectedVideoId: video.id,
      returnTo: "viral-detail",
    });
  };
  const goReplica = () => {
    patchDraft({ sourceId: video.id });
    navigate("replica", {
      selectedVideoId: video.id,
      returnTo: "viral-detail",
    });
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
            // biome-ignore lint/a11y/useMediaCaption: 源平台视频无字幕轨可挂载
            <video
              autoPlay
              controls
              playsInline
              src={playback.src}
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
                {video.platform} · {video.category || "推荐"}
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
          {statisticsError && (
            <p className="viral-media-status is-error" role="status">
              {statisticsError}
            </p>
          )}
          <Field label="视频摘要（来源作品原文）">
            <p className="content-source-copy">{video.description}</p>
          </Field>
          {media.status !== "idle" && (
            <p
              className={`viral-media-status is-${media.status}`}
              role="status"
            >
              {media.status === "loading" && "素材准备中，可能需要几十秒…"}
              {media.status === "ready" && media.message}
              {media.status === "error" &&
                `素材准备失败：${media.message ?? ""}`}
            </p>
          )}
          <div className="content-detail-actions">
            <div className="content-detail-action">
              <Button
                disabled={media.status === "loading"}
                variant="outline"
                onClick={() => prepare(video, goExtract)}
              >
                提取文案
              </Button>
            </div>
            <div className="content-detail-action">
              <Button
                disabled={media.status === "loading"}
                variant="outline"
                onClick={() => prepare(video, goReplica)}
              >
                视频复刻
              </Button>
            </div>
            <div className="content-detail-action">
              <Button
                variant="outline"
                onClick={() =>
                  patchState({
                    favorites: saved
                      ? state.favorites.filter((id) => id !== video.id)
                      : [...state.favorites, video.id],
                  })
                }
              >
                {saved ? "已收藏" : "收藏"}
              </Button>
            </div>
          </div>
        </Panel>
      </section>
    </section>
  );
}

function AssetCard({
  asset,
  selected,
  onSelect,
}: {
  asset: StudioAsset;
  selected: boolean;
  onSelect: () => void;
}) {
  return (
    <button
      type="button"
      className={`content-asset ${selected ? "is-selected" : ""} ${asset.composite ? "content-asset--composite" : ""}`}
      onClick={onSelect}
      aria-label={`选择素材 ${asset.name}`}
    >
      <Media asset={asset} alt={asset.name} />
      <strong>{asset.name}</strong>
      <span>
        {asset.group} · {assetKindLabel(asset.kind)}
      </span>
      <i>
        {asset.delivery === "direct"
          ? "供应商直出"
          : asset.saved
            ? "永久保存"
            : "处理中"}
      </i>
    </button>
  );
}

export function MaterialsPage() {
  const {
    data,
    state,
    review,
    patchState,
    patchDraft,
    updateData,
    navigate,
    notify,
  } = useStudio();
  const [kind, setKind] = useState<"全部" | StudioAsset["kind"]>("全部");
  const [source, setSource] = useState<"" | MaterialItem["source"]>("");
  const [queryInput, setQueryInput] = useState("");
  const [query, setQuery] = useState("");
  const [remotePage, setRemotePage] = useState<MaterialPage | null>(null);
  const [remoteError, setRemoteError] = useState<string>();
  const [remoteLoading, setRemoteLoading] = useState(false);
  const [selectedAsset, setSelectedAsset] = useState<StudioAsset>();
  const [uploadProgress, setUploadProgress] = useState<number>();
  const [busyAction, setBusyAction] = useState<string>();
  const [renameValue, setRenameValue] = useState("");
  const [groupValue, setGroupValue] = useState("");
  const uploadInputRef = useRef<HTMLInputElement | null>(null);
  const reviewAssets = data.assets.filter(
    (asset) => kind === "全部" || asset.kind === kind,
  );
  const remoteAssets = useMemo(
    () => remotePage?.items.map(studioAssetFromMaterial) ?? [],
    [remotePage],
  );
  const assets = review ? reviewAssets : remoteAssets;
  const visibleSelected = assets.find(
    (asset) => asset.id === state.selectedAssetId,
  );
  const selected =
    selectedAsset &&
    (state.selectedAssetId === undefined ||
      selectedAsset.id === state.selectedAssetId)
      ? selectedAsset
      : (visibleSelected ??
        data.assets.find((asset) => asset.id === state.selectedAssetId));
  const selectedIndex = reviewAssets.findIndex(
    (asset) => asset.id === state.selectedAssetId,
  );
  const [page, setPage] = useState(() =>
    selectedIndex >= 0 ? Math.floor(selectedIndex / pageSize) + 1 : 1,
  );
  const total = review ? reviewAssets.length : (remotePage?.total ?? 0);
  const pages = Math.max(1, Math.ceil(total / pageSize));
  const selectedAssetIdRef = useRef(state.selectedAssetId);

  useEffect(() => {
    if (review) return;
    let current = true;
    setRemoteLoading(true);
    setRemoteError(undefined);
    void listMaterials({
      mediaType: kind === "全部" ? undefined : kind,
      source: source || undefined,
      query: query || undefined,
      page,
      pageSize,
    })
      .then((result) => {
        if (current) setRemotePage(result);
      })
      .catch((error: unknown) => {
        if (current) {
          setRemoteError(
            error instanceof Error ? error.message : "读取素材库失败",
          );
        }
      })
      .finally(() => {
        if (current) setRemoteLoading(false);
      });
    return () => {
      current = false;
    };
  }, [kind, page, query, review, source]);

  useEffect(() => {
    if (selectedAssetIdRef.current === state.selectedAssetId) return;
    selectedAssetIdRef.current = state.selectedAssetId;
    if (review && selectedIndex >= 0) {
      setPage(Math.floor(selectedIndex / pageSize) + 1);
    }
  }, [review, selectedIndex, state.selectedAssetId]);

  useEffect(() => {
    setRenameValue(selected?.name ?? "");
    setGroupValue(selected?.group ?? "");
  }, [selected?.group, selected?.name]);

  useEffect(() => {
    if (review || !selected?.materialId || selected.url) return;
    let current = true;
    const preview = selected.assetId
      ? getAssetDownloadUrl(selected.assetId).then((result) => result.url)
      : selected.generationTaskId
        ? createGenerationTaskPreviewUrl(selected.generationTaskId)
        : Promise.resolve(null);
    void preview
      .then((url) => {
        if (current && url) {
          setSelectedAsset((asset) =>
            asset?.id === selected.id ? { ...asset, url } : asset,
          );
        }
      })
      .catch(() => {
        if (current) notify("素材预览暂不可用，请稍后重试");
      });
    return () => {
      current = false;
    };
  }, [notify, review, selected]);

  const currentAssets = review
    ? assets.slice((page - 1) * pageSize, page * pageSize)
    : assets;

  const retainForDraft = (asset: StudioAsset) => {
    updateData((current) =>
      current.assets.some((item) => item.id === asset.id)
        ? current
        : { ...current, assets: [asset, ...current.assets] },
    );
  };

  const handleUpload = async (file: File) => {
    setBusyAction("upload");
    setUploadProgress(0);
    try {
      const intent = await createMaterialUploadIntent(file, {
        title: file.name,
        group: "我的上传",
      });
      await uploadMaterial(intent, file, setUploadProgress);
      const completed = await completeMaterialUpload(intent.asset_id);
      const asset = studioAssetFromMaterial(completed);
      retainForDraft(asset);
      setSelectedAsset(asset);
      patchState({ selectedAssetId: asset.id });
      setKind(completed.media_type);
      setPage(1);
      setRemotePage((current) => ({
        items: [completed, ...(current?.items ?? [])]
          .filter(
            (item, index, items) =>
              items.findIndex((candidate) => candidate.id === item.id) ===
              index,
          )
          .slice(0, pageSize),
        page: 1,
        page_size: pageSize,
        total: (current?.total ?? 0) + 1,
      }));
      notify(`素材“${completed.title}”已上传并永久保存`);
    } catch (error) {
      notify(error instanceof Error ? error.message : "上传素材失败");
    } finally {
      setBusyAction(undefined);
      setUploadProgress(undefined);
      if (uploadInputRef.current) uploadInputRef.current.value = "";
    }
  };

  const saveName = async () => {
    if (!selected?.materialId || !renameValue.trim()) return;
    setBusyAction("rename");
    try {
      const updated = studioAssetFromMaterial(
        await updateMaterial(selected.materialId, {
          title: renameValue.trim(),
        }),
      );
      setSelectedAsset({ ...updated, url: selected.url });
      setRemotePage((current) =>
        current
          ? {
              ...current,
              items: current.items.map((item) =>
                item.id === updated.materialId
                  ? { ...item, title: updated.name }
                  : item,
              ),
            }
          : current,
      );
      notify("素材名称已保存");
    } catch (error) {
      notify(error instanceof Error ? error.message : "更新素材失败");
    } finally {
      setBusyAction(undefined);
    }
  };

  const removeSelected = async () => {
    if (!selected?.materialId) return;
    setBusyAction("hide");
    try {
      await hideMaterial(selected.materialId);
      setSelectedAsset(undefined);
      patchState({ selectedAssetId: undefined });
      setRemotePage((current) =>
        current
          ? {
              ...current,
              items: current.items.filter(
                (item) => item.id !== selected.materialId,
              ),
              total: Math.max(0, current.total - 1),
            }
          : current,
      );
      notify("素材已从素材库移除，原业务记录仍保留");
    } catch (error) {
      notify(error instanceof Error ? error.message : "移除素材失败");
    } finally {
      setBusyAction(undefined);
    }
  };

  const saveGroup = async () => {
    if (!selected?.materialId || !groupValue.trim()) return;
    setBusyAction("group");
    try {
      const updated = studioAssetFromMaterial(
        await updateMaterial(selected.materialId, {
          group: groupValue.trim(),
        }),
      );
      setSelectedAsset({ ...updated, url: selected.url });
      setRemotePage((current) =>
        current
          ? {
              ...current,
              items: current.items.map((item) =>
                item.id === updated.materialId
                  ? { ...item, group: updated.group }
                  : item,
              ),
            }
          : current,
      );
      notify("素材分组已保存");
    } catch (error) {
      notify(error instanceof Error ? error.message : "更新素材分组失败");
    } finally {
      setBusyAction(undefined);
    }
  };

  const downloadSelected = async () => {
    if (!selected?.assetId) return;
    setBusyAction("download");
    try {
      await downloadMaterialAsset(selected.assetId, selected.name);
      notify("素材下载已开始");
    } catch (error) {
      notify(error instanceof Error ? error.message : "下载素材失败");
    } finally {
      setBusyAction(undefined);
    }
  };

  const applyAsReference = (asset: StudioAsset) => {
    retainForDraft(asset);
    patchDraft({
      referenceIds: [...new Set([...state.draft.referenceIds, asset.id])],
    });
    navigate("reference", { returnTo: "materials" });
  };

  return (
    <section className="content-page content-materials">
      <header className="content-title">
        <div>
          <h1>素材库</h1>
          <p>统一管理和复用乡墅创作素材</p>
        </div>
        <input
          accept=".jpg,.jpeg,.png,.mp3,.mp4,.mov"
          aria-label="选择上传素材"
          hidden
          onChange={(event) => {
            const file = event.target.files?.[0];
            if (file) void handleUpload(file);
          }}
          ref={uploadInputRef}
          type="file"
        />
        <Button
          className="content-title-action"
          disabled={busyAction === "upload"}
          variant="primary"
          onClick={() =>
            review
              ? notify("审核模式保留示例素材，不执行真实上传")
              : uploadInputRef.current?.click()
          }
        >
          {uploadProgress === undefined
            ? "上传素材"
            : `上传中 ${uploadProgress}%`}
        </Button>
      </header>
      <Tabs
        items={[
          { id: "全部", label: "全部" },
          { id: "video", label: "视频" },
          { id: "image", label: "图片" },
          { id: "audio", label: "音频" },
        ]}
        value={kind}
        onChange={(value) => {
          setKind(value as typeof kind);
          setPage(1);
        }}
      />
      {!review ? (
        <form
          aria-label="素材筛选"
          className="content-material-filters"
          onSubmit={(event) => {
            event.preventDefault();
            setPage(1);
            setQuery(queryInput.trim());
          }}
        >
          <input
            aria-label="搜索素材"
            maxLength={120}
            placeholder="搜索素材名称"
            value={queryInput}
            onChange={(event) => setQueryInput(event.target.value)}
          />
          <select
            aria-label="素材来源"
            value={source}
            onChange={(event) => {
              setSource(event.target.value as typeof source);
              setPage(1);
            }}
          >
            <option value="">全部来源</option>
            <option value="upload">我的上传</option>
            <option value="project">项目素材</option>
            <option value="character">人物素材</option>
            <option value="oral">口播成片</option>
            <option value="generation">视频成片</option>
          </select>
          <Button type="submit" variant="outline">
            搜索
          </Button>
        </form>
      ) : null}
      <section className="content-material-layout">
        <div className="content-material-list">
          <div className="content-asset-grid">
            {currentAssets.map((asset) => (
              <AssetCard
                key={asset.id}
                asset={asset}
                selected={selected?.id === asset.id}
                onSelect={() => {
                  setSelectedAsset(asset);
                  patchState({ selectedAssetId: asset.id });
                }}
              />
            ))}
          </div>
          {remoteLoading ? <Hint>正在读取云端素材…</Hint> : null}
          {remoteError ? <Hint>{remoteError}</Hint> : null}
          {!remoteLoading && !remoteError && currentAssets.length === 0 ? (
            <Empty title="暂无素材" description="上传后即可跨项目复用。" />
          ) : null}
          {total > pageSize ? (
            <nav className="content-pagination" aria-label="素材分页">
              <span>
                共 {total} 条 · 每页 {pageSize} 条
              </span>
              <Button
                aria-label="上一页"
                disabled={page === 1}
                variant="outline"
                onClick={() => setPage((currentPage) => currentPage - 1)}
              >
                ‹
              </Button>
              {Array.from({ length: pages }, (_, index) => index + 1).map(
                (pageNumber) => (
                  <Button
                    key={pageNumber}
                    variant={page === pageNumber ? "primary" : "quiet"}
                    onClick={() => setPage(pageNumber)}
                  >
                    {pageNumber}
                  </Button>
                ),
              )}
              <Button
                aria-label="下一页"
                disabled={page === pages}
                variant="outline"
                onClick={() => setPage((currentPage) => currentPage + 1)}
              >
                ›
              </Button>
            </nav>
          ) : null}
        </div>
        <Panel className="content-inspector">
          {selected ? (
            <>
              <h2>{selected.name}</h2>
              <Media asset={selected} alt={selected.name} />
              <dl>
                <dt>类型</dt>
                <dd>{assetKindLabel(selected.kind)}</dd>
                <dt>来源</dt>
                <dd>{selected.source}</dd>
                <dt>归属</dt>
                <dd>{selected.personId ? "人物库" : selected.group}</dd>
                <dt>状态</dt>
                <dd>
                  {selected.delivery === "direct"
                    ? "供应商直出，尚未归档"
                    : selected.saved
                      ? "云端永久保存"
                      : "处理中"}
                </dd>
              </dl>
              {selected.kind === "audio" &&
              (review || selected.allowedUses?.includes("oral_audio")) ? (
                <Button
                  variant="primary"
                  onClick={() => {
                    retainForDraft(selected);
                    patchDraft({
                      audioId: selected.id,
                      ipId: selected.personId,
                      voiceId: undefined,
                    });
                    navigate("oral-audio", { returnTo: "materials" });
                  }}
                >
                  用于音频口播
                </Button>
              ) : null}
              {selected.kind === "image" &&
              selected.allowedUses?.includes("original_frame") ? (
                <Button
                  variant="outline"
                  onClick={() => {
                    retainForDraft(selected);
                    patchDraft({
                      originalImageId: selected.id,
                      frameConfirmed: false,
                    });
                    navigate("replica", { returnTo: "materials" });
                  }}
                >
                  用作原画面
                </Button>
              ) : null}
              {selected.kind === "image" &&
              selected.allowedUses?.includes("first_frame") ? (
                <Button
                  variant="outline"
                  onClick={() => {
                    retainForDraft(selected);
                    patchDraft({ firstFrameId: selected.id });
                    navigate("video", { returnTo: "materials" });
                  }}
                >
                  用作首帧
                </Button>
              ) : null}
              {selected.kind === "image" &&
              selected.allowedUses?.includes("tail_frame") ? (
                <Button
                  variant="outline"
                  onClick={() => {
                    retainForDraft(selected);
                    patchDraft({ tailFrameId: selected.id });
                    navigate("video", { returnTo: "materials" });
                  }}
                >
                  用作尾帧
                </Button>
              ) : null}
              {selected.allowedUses?.includes("reference") ? (
                <Button
                  variant="outline"
                  onClick={() => applyAsReference(selected)}
                >
                  用于参考生视频
                </Button>
              ) : null}
              {selected.materialId &&
              selected.allowedActions?.includes("rename") ? (
                <>
                  <div className="content-material-manage">
                    <Field label="素材名称">
                      <input
                        aria-label="素材名称"
                        maxLength={120}
                        onChange={(event) => setRenameValue(event.target.value)}
                        value={renameValue}
                      />
                    </Field>
                    <Button
                      disabled={busyAction === "rename" || !renameValue.trim()}
                      onClick={() => void saveName()}
                      variant="outline"
                    >
                      保存名称
                    </Button>
                  </div>
                  <div className="content-material-manage">
                    <Field label="素材分组">
                      <input
                        aria-label="素材分组"
                        maxLength={80}
                        onChange={(event) => setGroupValue(event.target.value)}
                        value={groupValue}
                      />
                    </Field>
                    <Button
                      disabled={busyAction === "group" || !groupValue.trim()}
                      onClick={() => void saveGroup()}
                      variant="outline"
                    >
                      保存分组
                    </Button>
                  </div>
                </>
              ) : null}
              {selected.assetId &&
              selected.allowedActions?.includes("download") ? (
                <Button
                  disabled={busyAction === "download"}
                  onClick={() => void downloadSelected()}
                  variant="outline"
                >
                  下载素材
                </Button>
              ) : null}
              {selected.materialId &&
              selected.allowedActions?.includes("hide") ? (
                <Button
                  disabled={busyAction === "hide"}
                  onClick={() => void removeSelected()}
                  variant="quiet"
                >
                  从素材库移除
                </Button>
              ) : null}
              {selected.delivery === "direct" ? (
                <Hint>
                  该结果仍由供应商托管，可预览；归档后才可作为云端素材复用。
                </Hint>
              ) : null}
            </>
          ) : (
            <Empty
              title="选择一个素材"
              description="统一选择后再进入相应创作流程。"
            />
          )}
        </Panel>
      </section>
    </section>
  );
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
  const { data, navigate, patchState, review, state } = useStudio();
  const completedResultIds = new Set(
    data.tasks
      .filter((task) => task.status === "completed" && task.resultId)
      .map((task) => task.resultId),
  );
  const selectedAsset = data.assets.find(
    (asset) =>
      asset.id === state.selectedAssetId &&
      asset.kind === "video" &&
      (completedResultIds.size === 0 || completedResultIds.has(asset.id)),
  );
  const coverCandidates = [
    ...(selectedAsset ? [selectedAsset] : []),
    ...data.assets.filter(
      (asset) =>
        asset.id !== selectedAsset?.id &&
        asset.kind === "image" &&
        (Boolean(asset.personId) || asset.group.includes("场景")),
    ),
  ].slice(0, 3);
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
  const selectedCover =
    coverCandidates.find((asset) => asset.id === form.coverId) ??
    coverCandidates[0];

  useEffect(() => {
    setForm(savedDraftForAsset ?? newPublishDraft(selectedAsset, review));
    setSaveNotice(false);
  }, [review, savedDraftForAsset, selectedAsset]);

  const updateForm = (patch: Partial<StudioPublishDraft>) => {
    setSaveNotice(false);
    setForm((current) => ({ ...current, ...patch }));
  };

  const addTag = () => {
    const tag = tagInput.trim();
    if (!tag || form.tags.includes(tag)) {
      setTagInput("");
      return;
    }
    updateForm({ tags: [...form.tags, tag] });
    setTagInput("");
  };

  const savePublishDraft = () => {
    if (!selectedAsset) return;
    const savedDraft = {
      ...form,
      id: savedDraftForAsset?.id ?? `publish-${selectedAsset.id}`,
      assetId: selectedAsset.id,
      coverId: selectedCover?.id,
    };
    const currentDrafts = state.publishDrafts ?? [];
    patchState({
      publishDrafts: savedDraftForAsset
        ? currentDrafts.map((draft) =>
            draft.id === savedDraftForAsset.id ? savedDraft : draft,
          )
        : [...currentDrafts, savedDraft],
    });
    setForm(savedDraft);
    setSaveNotice(true);
  };

  return (
    <section className="content-page content-publish">
      <header className="content-title">
        <div>
          <h1>发布管理</h1>
          <p>编辑发布草稿，确认后再提交至平台发布</p>
        </div>
      </header>
      <section className="content-publish-layout">
        <Panel className="content-publish-drafts">
          <div className="content-publish-draft-tabs">
            <strong>
              发布草稿 <b>{draftCount}</b>
            </strong>
            <span>
              待发布 <b>0</b>
            </span>
            <span>
              已发布 <b>0</b>
            </span>
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
                  onClick={() => {
                    setForm(draft);
                    setSaveNotice(false);
                    patchState({ selectedAssetId: draft.assetId });
                  }}
                  type="button"
                >
                  {draftAsset ? (
                    <Media asset={draftAsset} alt={`${draft.title} 草稿封面`} />
                  ) : (
                    <div className="content-publish-draft-card-empty">
                      <Icon name="video" />
                    </div>
                  )}
                  <span>
                    <strong>{draft.title || "未命名发布草稿"}</strong>
                    <small>来源：任务中心</small>
                    <i>已保存 · 待发布</i>
                    <small>
                      {isReviewSample ? "审核示例" : "当前会话草稿"}
                    </small>
                  </span>
                </button>
              );
            })
          ) : (
            <Empty
              title="暂无发布草稿"
              description={
                selectedAsset
                  ? "填写右侧信息后保存到当前会话。"
                  : "从任务中心选择一条已完成的视频后创建草稿。"
              }
            />
          )}
        </Panel>
        <Panel className="content-publish-editor">
          <header className="content-publish-editor-heading">
            <h2>编辑发布草稿</h2>
            <Hint>发布文案独立于口播终稿；确认后才提交发布。</Hint>
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
                      aria-label={`选择封面 ${asset.name}`}
                      className={
                        selectedCover?.id === asset.id ? "is-selected" : ""
                      }
                      key={asset.id}
                      onClick={() => updateForm({ coverId: asset.id })}
                      type="button"
                    >
                      <Media asset={asset} alt={`封面 ${index + 1}`} />
                    </button>
                  ))}
                </div>
              ) : (
                <div className="content-cover-empty">
                  <Icon name="image" />
                  <span>暂无可选封面</span>
                </div>
              )}
              <Button disabled variant="outline">
                上传自定义封面（接口待接通）
              </Button>
            </section>
          </div>
          <div className="content-publish-fields">
            <Field label="发布平台">
              <div className="content-platform-options">
                {(["抖音", "视频号"] as const).map((platform) => (
                  <Button
                    aria-pressed={form.platform === platform}
                    key={platform}
                    onClick={() => updateForm({ platform })}
                    variant={form.platform === platform ? "primary" : "outline"}
                  >
                    {platform}
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
              ) : (
                <div className="content-publish-account-empty">
                  <span>尚未连接发布账号</span>
                  <Button onClick={() => navigate("profile")} variant="quiet">
                    前往用户档案管理账号
                  </Button>
                </div>
              )}
            </Field>
            <Field label="发布标题">
              <input
                onChange={(event) => updateForm({ title: event.target.value })}
                placeholder="填写发布标题"
                value={form.title}
              />
            </Field>
            <Field label="发布描述">
              <textarea
                onChange={(event) =>
                  updateForm({ description: event.target.value })
                }
                placeholder="填写发布说明"
                value={form.description}
              />
            </Field>
            <Field label="标签">
              <div className="content-publish-tags">
                {form.tags.map((tag) => (
                  <span key={tag}># {tag}</span>
                ))}
                <input
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
              已保存到当前会话，未同步到云端。
            </p>
          )}
          <div className="content-publish-actions">
            <Button
              disabled={!selectedAsset}
              onClick={savePublishDraft}
              variant="outline"
            >
              保存草稿
            </Button>
            <Button
              aria-label="正式发布（接口未接通）"
              disabled
              variant="primary"
            >
              正式发布（接口未接通）
            </Button>
          </div>
        </Panel>
      </section>
    </section>
  );
}
