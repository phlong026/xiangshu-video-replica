/**
 * 爆款视频获取 —— 内容组精简后的唯一爆款内容采集入口.
 *
 * 交互与客户工作台同构（样式约定见 viral-acquire.css 文件头）：
 * - 一条输入条合并链接采集与文件上传：粘贴抖音 / 小红书 / 视频号分享链接
 *   直接采集；点左侧上传图标选文件（MP4/MOV ≤200MB）弹「补充信息」确认窗
 *   ——自动识别项（时长/分辨率/封面预览）在浏览器本地读取直接显示，采集
 *   不到的（平台/标题/作者/发布时间/互动数据/标签）由上传人补填。
 * - 采集/上传成功即自动上架（服务端置 pushed_at + 首页推荐位），下方卡片
 *   网格 = 最近入库（最新在前），与客户爆款卡同款视觉、不显示来源分类角标。
 * - 已上架卡片提供「下架」（复用 curation hide：客户列表与首页同时摘除）。
 */
import { useCallback, useEffect, useRef, useState } from "react";
import {
  adminActivationErrorMessage,
  adminImportViralLink,
  adminUploadViralVideo,
  type CollectedViralVideo,
  curateViralVideosBatch,
  listCollectedViralVideos,
} from "../api.admin";
import { PageBanner } from "./ui/PageBanner";
import "./viral-acquire.css";

const PLATFORM_LABELS: Record<CollectedViralVideo["platform"], string> = {
  douyin: "抖音",
  wechat_channels: "视频号",
  xiaohongshu: "小红书",
};

const MAX_UPLOAD_BYTES = 200 * 1024 * 1024;

function formatDuration(durationMs: number): string {
  const totalSeconds = Math.max(0, Math.round(durationMs / 1000));
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  return `${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;
}

function formatCount(value: number | null): string {
  if (value == null) return "—";
  if (value >= 10000) {
    return `${(value / 10000).toFixed(1).replace(/\.0$/, "")}w`;
  }
  return String(value);
}

function formatAcquiredAt(iso: string): string {
  const at = new Date(iso);
  if (Number.isNaN(at.getTime())) return iso;
  const pad = (value: number) => String(value).padStart(2, "0");
  return `${pad(at.getMonth() + 1)}-${pad(at.getDate())} ${pad(at.getHours())}:${pad(at.getMinutes())}`;
}

/** 卡片状态章：转存/上架/下架三态，视觉沿用客户端缓存角标语义。 */
function cardStatus(video: CollectedViralVideo): {
  label: string;
  tone: "cached" | "unready" | "failed";
} {
  if (
    video.availability === "HIDDEN" ||
    video.availability === "UNAVAILABLE" ||
    video.content_state === "removed" ||
    video.content_state === "blocked"
  ) {
    return { label: "已下架", tone: "failed" };
  }
  if (video.content_state === "prepare_failed") {
    return { label: "转存失败", tone: "failed" };
  }
  if (video.content_state === "pending_prepare") {
    return { label: "转存中", tone: "unready" };
  }
  return { label: "已就绪", tone: "cached" };
}

function statIcon(name: "heart" | "comment" | "share" | "star") {
  const paths: Record<string, string> = {
    heart:
      "M12 21s-7.5-4.9-10-9.2C.3 8.6 2 4.5 5.8 4.1c2.2-.2 4.2 1 6.2 3.2 2-2.2 4-3.4 6.2-3.2C21.9 4.5 23.7 8.6 22 11.8 19.5 16.1 12 21 12 21z",
    comment: "M3 5h18v11H8l-5 4V5z",
    share: "M13 5l7 6-7 6v-4C7 13 4 15 2 19c0-6 4-10 11-11V5z",
    star: "M12 2l2.9 6.2 6.6.8-4.9 4.6 1.3 6.6L12 16.9 6.1 20.2l1.3-6.6L2.5 9l6.6-.8z",
  };
  return (
    <svg
      aria-hidden="true"
      fill="currentColor"
      height="12"
      viewBox="0 0 24 24"
      width="12"
    >
      <path d={paths[name]} />
    </svg>
  );
}

function AcquireVideoCard({
  video,
  readOnly,
  curateBusy,
  onTakedown,
  onReshow,
}: {
  video: CollectedViralVideo;
  readOnly: boolean;
  curateBusy: boolean;
  onTakedown: (video: CollectedViralVideo) => void;
  onReshow: (video: CollectedViralVideo) => void;
}) {
  const status = cardStatus(video);
  const cover = video.cover_url ?? "";
  const stats: Array<
    ["heart" | "comment" | "share" | "star", string, number | null]
  > = [
    ["heart", "点赞", video.likes],
    ["comment", "评论", video.comments],
    ["share", "转发", video.shares],
    ["star", "收藏", video.collects],
  ];
  return (
    <article className="viral-card">
      <div className="viral-card-cover">
        {cover ? (
          <img
            alt=""
            className="viral-card-cover-img"
            loading="lazy"
            src={cover}
          />
        ) : (
          <div className="viral-card-cover-empty">暂无封面</div>
        )}
        <span aria-hidden="true" className="viral-card-scrim" />
        <span className="viral-card-platform">
          {PLATFORM_LABELS[video.platform] ?? video.platform}
        </span>
        <span className="viral-card-duration">
          {formatDuration(video.duration_ms)}
        </span>
        <span className={`viral-card-cache is-${status.tone}`}>
          {status.label}
        </span>
        <div className="viral-card-overlay">
          <div className="viral-card-stats">
            {stats.map(([icon, label, value]) => (
              <span key={label} title={label}>
                {statIcon(icon)}
                {formatCount(value)}
              </span>
            ))}
          </div>
          <div className="viral-card-overlay-author">
            {video.author_avatar ? (
              <img alt="" loading="lazy" src={video.author_avatar} />
            ) : (
              <i>{(video.author || "无").slice(0, 1)}</i>
            )}
            <span>{video.author || "佚名"}</span>
            {video.verified ? <em title="认证作者">✓</em> : null}
            {video.published_display ? (
              <time>{video.published_display}</time>
            ) : null}
          </div>
        </div>
      </div>
      <div className="viral-card-body">
        <h3>
          <span className="viral-card-title">
            {video.title || video.video_id}
          </span>
        </h3>
        {video.tags && video.tags.length > 0 ? (
          <div className="viral-card-tags">
            {video.tags.slice(0, 6).map((tag) => (
              <span key={tag} title={`#${tag}`}>
                #{tag}
              </span>
            ))}
          </div>
        ) : null}
        <div className="viral-card-caption">
          <span>入库于 {formatAcquiredAt(video.created_at)}</span>
          {!readOnly && status.tone === "cached" ? (
            <button
              className="viral-acquire-takedown"
              disabled={curateBusy}
              type="button"
              onClick={() => onTakedown(video)}
            >
              下架
            </button>
          ) : null}
          {!readOnly && video.availability === "HIDDEN" ? (
            <button
              className="viral-acquire-takedown"
              disabled={curateBusy}
              type="button"
              onClick={() => onReshow(video)}
            >
              重新上架
            </button>
          ) : null}
        </div>
      </div>
    </article>
  );
}

/** 选文件后本地预读：时长 / 分辨率 / 封面预览（失败不阻塞，服务端兜底）。 */
async function probeVideoFile(file: File): Promise<{
  durationMs: number;
  width: number;
  height: number;
  posterUrl: string;
}> {
  const url = URL.createObjectURL(file);
  try {
    const video = document.createElement("video");
    video.muted = true;
    video.preload = "metadata";
    video.src = url;
    // 无超时守卫时（jsdom / 解码器缺失 / 损坏文件）loadedmetadata 永不到来，
    // 弹窗会一直停在"正在读取…"——4 秒后按预读失败处理，不阻塞补填。
    await new Promise<void>((resolve, reject) => {
      const timer = window.setTimeout(
        () => reject(new Error("metadata timeout")),
        4000,
      );
      video.onloadedmetadata = () => {
        window.clearTimeout(timer);
        resolve();
      };
      video.onerror = () => {
        window.clearTimeout(timer);
        reject(new Error("browser cannot decode the video"));
      };
    });
    const durationMs = Number.isFinite(video.duration)
      ? Math.round(video.duration * 1000)
      : 0;
    const width = video.videoWidth;
    const height = video.videoHeight;
    let posterUrl = "";
    try {
      await new Promise<void>((resolve) => {
        video.onseeked = () => resolve();
        video.currentTime = Math.min(
          1,
          Math.max(0.1, (video.duration || 1) / 3),
        );
        window.setTimeout(resolve, 2000);
      });
      const canvas = document.createElement("canvas");
      canvas.width = width;
      canvas.height = height;
      canvas.getContext("2d")?.drawImage(video, 0, 0, width, height);
      posterUrl = canvas.toDataURL("image/jpeg", 0.7);
    } catch {
      // 封面预览失败不阻塞补填，入库封面由服务端 ffprobe 抽帧兜底。
    }
    return { durationMs, width, height, posterUrl };
  } finally {
    URL.revokeObjectURL(url);
  }
}

export function ViralAcquirePage({ readOnly }: { readOnly: boolean }) {
  const [linkUrl, setLinkUrl] = useState("");
  const [linkBusy, setLinkBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  const [items, setItems] = useState<CollectedViralVideo[] | null>(null);
  const [listLoading, setListLoading] = useState(false);
  const [curateTarget, setCurateTarget] = useState<CollectedViralVideo | null>(
    null,
  );
  const [modalOpen, setModalOpen] = useState(false);
  const [modalFile, setModalFile] = useState<File | null>(null);
  const [probe, setProbe] = useState<{
    durationMs: number;
    width: number;
    height: number;
    posterUrl: string;
  } | null>(null);
  const [probeFailed, setProbeFailed] = useState(false);
  const [uploadPlatform, setUploadPlatform] =
    useState<CollectedViralVideo["platform"]>("wechat_channels");
  const [uploadTitle, setUploadTitle] = useState("");
  const [uploadAuthor, setUploadAuthor] = useState("");
  const [uploadVerified, setUploadVerified] = useState(false);
  const [uploadPublished, setUploadPublished] = useState("");
  const [uploadLikes, setUploadLikes] = useState("");
  const [uploadComments, setUploadComments] = useState("");
  const [uploadShares, setUploadShares] = useState("");
  const [uploadCollects, setUploadCollects] = useState("");
  const [uploadTags, setUploadTags] = useState("");
  const [uploadBusy, setUploadBusy] = useState(false);
  const linkKeyRef = useRef(crypto.randomUUID());
  const uploadKeyRef = useRef(crypto.randomUUID());
  const fileInputRef = useRef<HTMLInputElement | null>(null);

  const reload = useCallback(async () => {
    setListLoading(true);
    try {
      const result = await listCollectedViralVideos({ offset: 0 });
      setItems(result.items);
    } catch (cause) {
      setError(adminActivationErrorMessage(cause, "读取最近获取失败"));
    } finally {
      setListLoading(false);
    }
  }, []);

  useEffect(() => {
    void reload();
  }, [reload]);

  const closeUploadModal = useCallback(() => {
    setModalOpen(false);
    setModalFile(null);
    setProbe(null);
    setProbeFailed(false);
    setUploadPlatform("wechat_channels");
    setUploadTitle("");
    setUploadAuthor("");
    setUploadVerified(false);
    setUploadPublished("");
    setUploadLikes("");
    setUploadComments("");
    setUploadShares("");
    setUploadCollects("");
    setUploadTags("");
  }, []);

  const submitLink = async () => {
    if (linkBusy) return;
    const url = linkUrl.trim();
    if (!url) return;
    setLinkBusy(true);
    setError("");
    setNotice("");
    try {
      const result = await adminImportViralLink(
        url,
        "粘贴链接采集",
        linkKeyRef.current,
      );
      linkKeyRef.current = crypto.randomUUID();
      setLinkUrl("");
      const label =
        PLATFORM_LABELS[result.platform as CollectedViralVideo["platform"]] ??
        result.platform;
      setNotice(
        result.archiveQueued
          ? `已入库（${label} · ${result.videoId}），媒体自动转存中；转存完成后自动上架。`
          : "已入库；平台转存槽位被占用，本次未排队转存，请稍后重新提交同一链接。",
      );
      await reload();
    } catch (cause) {
      setError(adminActivationErrorMessage(cause, "链接采集失败"));
      linkKeyRef.current = crypto.randomUUID();
    } finally {
      setLinkBusy(false);
    }
  };

  const onFileChosen = (file: File | undefined) => {
    if (!file) return;
    const looksVideo =
      /\.(mp4|mov)$/i.test(file.name) ||
      file.type === "video/mp4" ||
      file.type === "video/quicktime";
    if (!looksVideo) {
      setError("请选择 MP4 / MOV 视频文件。");
      return;
    }
    if (file.size <= 0 || file.size > MAX_UPLOAD_BYTES) {
      setError("请选择不超过 200 MB 的视频文件。");
      return;
    }
    setError("");
    setModalFile(file);
    setProbe(null);
    setProbeFailed(false);
    setModalOpen(true);
    void probeVideoFile(file)
      .then(setProbe)
      .catch(() => setProbeFailed(true));
  };

  const confirmUpload = async () => {
    if (uploadBusy || !modalFile) return;
    if (!uploadTitle.trim()) {
      setError("请填写视频标题。");
      return;
    }
    setUploadBusy(true);
    setError("");
    setNotice("");
    try {
      const data = new FormData();
      data.set("file", modalFile);
      data.set("platform", uploadPlatform);
      data.set("title", uploadTitle.trim());
      if (uploadAuthor.trim()) data.set("author", uploadAuthor.trim());
      data.set("verified", uploadVerified ? "true" : "false");
      if (uploadPublished) data.set("published_display", uploadPublished);
      const metric = (raw: string) => {
        const value = Number(raw);
        return raw.trim() && Number.isFinite(value) && value >= 0
          ? String(Math.round(value))
          : "";
      };
      const metricFields = [
        ["likes", uploadLikes],
        ["comments", uploadComments],
        ["shares", uploadShares],
        ["collects", uploadCollects],
      ] as const;
      for (const [field, raw] of metricFields) {
        const value = metric(raw);
        if (value) data.set(field, value);
      }
      if (uploadTags.trim()) data.set("tags", uploadTags.trim());
      await adminUploadViralVideo(data, uploadKeyRef.current);
      uploadKeyRef.current = crypto.randomUUID();
      closeUploadModal();
      setNotice("视频已入库并自动上架，客户即可播放与一键复刻。");
      await reload();
    } catch (cause) {
      setError(adminActivationErrorMessage(cause, "视频上传失败"));
      uploadKeyRef.current = crypto.randomUUID();
    } finally {
      setUploadBusy(false);
    }
  };

  const curate = async (
    video: CollectedViralVideo,
    action: "hide" | "push",
    fallbackError: string,
  ) => {
    if (readOnly || curateTarget) return;
    setCurateTarget(video);
    setError("");
    try {
      await curateViralVideosBatch(
        [video],
        action,
        action === "hide" ? "后台下架爆款视频" : "后台重新上架爆款视频",
        crypto.randomUUID(),
      );
      setNotice(
        action === "hide"
          ? "已下架，客户列表与首页已同步移除。"
          : "已重新上架，客户即可播放与一键复刻。",
      );
      await reload();
    } catch (cause) {
      setError(adminActivationErrorMessage(cause, fallbackError));
    } finally {
      setCurateTarget(null);
    }
  };

  const formatFileSize = (size: number) =>
    size >= 1024 * 1024
      ? `${(size / (1024 * 1024)).toFixed(1)} MB`
      : `${Math.max(1, Math.round(size / 1024))} KB`;

  return (
    <section aria-label="爆款视频获取">
      <div className="viral-acquire-bar">
        <input
          accept=".mp4,.mov,video/mp4,video/quicktime"
          aria-label="选择视频文件"
          hidden
          onChange={(event) => {
            onFileChosen(event.target.files?.[0]);
            event.target.value = "";
          }}
          ref={fileInputRef}
          type="file"
        />
        <button
          aria-label="上传视频"
          className="viral-acquire-upload"
          disabled={readOnly}
          title="上传视频文件"
          type="button"
          onClick={() => fileInputRef.current?.click()}
        >
          <svg
            aria-hidden="true"
            fill="none"
            height="20"
            stroke="currentColor"
            strokeLinecap="round"
            strokeLinejoin="round"
            strokeWidth="2"
            viewBox="0 0 24 24"
            width="20"
          >
            <path d="M12 16V4" />
            <path d="M6 10l6-6 6 6" />
            <path d="M4 20h16" />
          </svg>
        </button>
        <input
          aria-label="视频链接"
          disabled={linkBusy || readOnly}
          placeholder="粘贴抖音 / 小红书 / 视频号分享链接"
          value={linkUrl}
          onChange={(event) => setLinkUrl(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter") {
              event.preventDefault();
              void submitLink();
            }
          }}
        />
        <button
          className="viral-acquire-go"
          disabled={linkBusy || readOnly || !linkUrl.trim()}
          type="button"
          onClick={() => void submitLink()}
        >
          {linkBusy ? "采集中…" : "采集入库"}
        </button>
      </div>
      <p className="viral-acquire-helper">
        支持抖音、小红书、视频号的分享链接；也可上传视频文件（MP4 / MOV，≤ 200
        MB）
      </p>
      {error ? <PageBanner tone="error">{error}</PageBanner> : null}
      {notice ? <PageBanner tone="notice">{notice}</PageBanner> : null}

      <div className="viral-acquire-results">
        获取结果 · 最近入库
        <button
          className="viral-acquire-refresh"
          disabled={listLoading}
          type="button"
          onClick={() => void reload()}
        >
          {listLoading ? "刷新中…" : "↻ 刷新"}
        </button>
      </div>
      {items === null ? (
        <p role="status">正在读取最近获取…</p>
      ) : items.length === 0 ? (
        <p>还没有爆款视频，粘贴链接或上传文件获取第一条。</p>
      ) : (
        <div className="viral-acquire-grid">
          {items.map((video) => (
            <AcquireVideoCard
              key={`${video.platform}-${video.video_id}`}
              curateBusy={curateTarget !== null}
              onReshow={(target) => void curate(target, "push", "重新上架失败")}
              onTakedown={(target) => void curate(target, "hide", "下架失败")}
              readOnly={readOnly}
              video={video}
            />
          ))}
        </div>
      )}

      {/* biome-ignore lint/a11y/noStaticElementInteractions: 遮罩是 aria-modal dialog 模式的标准视觉层，键盘经 Esc 与对话框内控件交互 */}
      <div
        className="viral-acquire-modal-backdrop"
        hidden={!modalOpen}
        onKeyDown={(event) => {
          if (event.key === "Escape" && !uploadBusy) closeUploadModal();
        }}
        onClick={(event) => {
          if (event.target === event.currentTarget && !uploadBusy) {
            closeUploadModal();
          }
        }}
        role="presentation"
      >
        <div
          aria-label="上传视频 · 补充信息"
          aria-modal="true"
          className="viral-acquire-modal"
          role="dialog"
        >
          <div className="viral-acquire-modal-head">
            <h3>上传视频 · 补充信息</h3>
            <button
              aria-label="关闭"
              className="viral-acquire-modal-close"
              disabled={uploadBusy}
              type="button"
              onClick={closeUploadModal}
            >
              ×
            </button>
          </div>
          {modalFile ? (
            <div className="viral-acquire-file">
              <span>{modalFile.name}</span>
              <span className="viral-acquire-file-size">
                {formatFileSize(modalFile.size)}
              </span>
            </div>
          ) : null}
          <div className="viral-acquire-section">自动识别</div>
          <div className="viral-acquire-auto">
            <div className="viral-acquire-thumb">
              {probe?.posterUrl ? (
                <img alt="" src={probe.posterUrl} />
              ) : (
                <span aria-hidden="true">▶</span>
              )}
            </div>
            <div>
              <div className="viral-acquire-facts">
                {probe
                  ? `${formatDuration(probe.durationMs)} · ${probe.width}×${probe.height}`
                  : probeFailed
                    ? "本地预读失败，时长与封面上传后由服务端生成"
                    : "正在读取…"}
              </div>
              <div className="viral-acquire-facts-sub">
                时长 / 分辨率 / 封面预览来自本地读取；入库封面由服务端最终生成
              </div>
            </div>
          </div>
          <div className="viral-acquire-section">补充信息</div>
          <div className="viral-acquire-fields">
            <label>
              平台
              <select
                disabled={uploadBusy}
                value={uploadPlatform}
                onChange={(event) =>
                  setUploadPlatform(
                    event.target.value as CollectedViralVideo["platform"],
                  )
                }
              >
                <option value="wechat_channels">视频号</option>
                <option value="douyin">抖音</option>
                <option value="xiaohongshu">小红书</option>
              </select>
            </label>
            <label className="viral-acquire-grow">
              视频标题
              <input
                disabled={uploadBusy}
                maxLength={200}
                placeholder="必填，最长 200 字"
                value={uploadTitle}
                onChange={(event) => setUploadTitle(event.target.value)}
              />
            </label>
            <label>
              作者
              <input
                disabled={uploadBusy}
                maxLength={120}
                placeholder="选填"
                value={uploadAuthor}
                onChange={(event) => setUploadAuthor(event.target.value)}
              />
            </label>
            <label className="viral-acquire-check">
              <input
                checked={uploadVerified}
                disabled={uploadBusy}
                type="checkbox"
                onChange={(event) => setUploadVerified(event.target.checked)}
              />
              认证作者
            </label>
            <label>
              发布时间
              <input
                disabled={uploadBusy}
                type="date"
                value={uploadPublished}
                onChange={(event) => setUploadPublished(event.target.value)}
              />
            </label>
          </div>
          <div className="viral-acquire-fields">
            <label>
              点赞
              <input
                disabled={uploadBusy}
                inputMode="numeric"
                min={0}
                placeholder="选填"
                type="number"
                value={uploadLikes}
                onChange={(event) => setUploadLikes(event.target.value)}
              />
            </label>
            <label>
              评论
              <input
                disabled={uploadBusy}
                inputMode="numeric"
                min={0}
                placeholder="选填"
                type="number"
                value={uploadComments}
                onChange={(event) => setUploadComments(event.target.value)}
              />
            </label>
            <label>
              转发
              <input
                disabled={uploadBusy}
                inputMode="numeric"
                min={0}
                placeholder="选填"
                type="number"
                value={uploadShares}
                onChange={(event) => setUploadShares(event.target.value)}
              />
            </label>
            <label>
              收藏
              <input
                disabled={uploadBusy}
                inputMode="numeric"
                min={0}
                placeholder="选填"
                type="number"
                value={uploadCollects}
                onChange={(event) => setUploadCollects(event.target.value)}
              />
            </label>
            <label className="viral-acquire-grow">
              标签
              <input
                disabled={uploadBusy}
                maxLength={300}
                placeholder="选填，如 #探店 #vlog"
                value={uploadTags}
                onChange={(event) => setUploadTags(event.target.value)}
              />
            </label>
          </div>
          <div className="viral-acquire-modal-actions">
            <button
              className="viral-acquire-cancel"
              disabled={uploadBusy}
              type="button"
              onClick={closeUploadModal}
            >
              取消
            </button>
            <button
              className="viral-acquire-confirm"
              disabled={uploadBusy}
              type="button"
              onClick={() => void confirmUpload()}
            >
              {uploadBusy ? "上传处理中…" : "确认上传入库"}
            </button>
          </div>
        </div>
      </div>
    </section>
  );
}
