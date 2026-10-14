import { useCallback, useEffect, useRef, useState } from "react";
import {
  adminActivationErrorMessage,
  archiveCollectedViralVideo,
  estimateViralOperation,
  getViralHomepage,
  reorderViralHomepage,
  scheduleViralHomepage,
  type ViralHomepage,
  type ViralHomepageVideo,
  type ViralOperationEstimate,
} from "../api.admin";
import { ConfirmDialog } from "./ui/ConfirmDialog";
import { PageBanner } from "./ui/PageBanner";
import { formatDateTime } from "./ui/vocabulary";
import { ViralOperationCostSummary } from "./ViralOperationCostSummary";

function dateInput(value: string | null) {
  if (!value) return "";
  const date = new Date(value);
  return new Date(date.getTime() + 8 * 3600000).toISOString().slice(0, 16);
}

/** 一条首页内容当前的问题（封面缺失 / 下架 / 素材未就绪 / 展示过久）。 */
function homepageProblems(video: ViralHomepageVideo) {
  return [
    !video.cover_key ? "封面缺失" : "",
    video.availability !== "AVAILABLE" ? "已下架" : "",
    video.media_status !== "SUCCEEDED" || !video.storage_uri
      ? "素材未就绪"
      : "",
    video.homepage_featured_at &&
    Date.now() - Date.parse(video.homepage_featured_at) > 30 * 86400000
      ? "已展示超过30天，建议替换"
      : "",
  ].filter(Boolean);
}

/** 预览封面兜底：无封面或源站链接过期都退回占位，不出现裂图。 */
function PreviewCover({ src }: { src: string | null }) {
  const [failed, setFailed] = useState(false);
  if (!src || failed) {
    return <div className="admin-homepage-cover-empty">暂无封面</div>;
  }
  return <img src={src} alt="" onError={() => setFailed(true)} />;
}

export function ViralHomepagePage({
  readOnly = false,
}: {
  readOnly?: boolean;
}) {
  const [preparation, setPreparation] = useState<{
    video: ViralHomepageVideo;
    estimate: ViralOperationEstimate;
    key: string;
  } | null>(null);
  const [platform, setPlatform] = useState<ViralHomepage["platform"]>("douyin");
  const [data, setData] = useState<ViralHomepage | null>(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [saving, setSaving] = useState(false);
  const [editing, setEditing] = useState<ViralHomepageVideo | null>(null);
  const [starts, setStarts] = useState("");
  const [ends, setEnds] = useState("");
  const [positions, setPositions] = useState<Record<string, string>>({});
  // 顺序草稿（T36）：上移 / 下移 / 拖拽 / 移到第 N 位都只改本地草稿，点「保存顺序」
  // 才提交一次；null 表示没有未保存的调整。
  const [draftIds, setDraftIds] = useState<string[] | null>(null);
  const [dragIndex, setDragIndex] = useState<number | null>(null);
  const generation = useRef(0);
  const retry = useRef<{ fingerprint: string; key: string } | null>(null);
  const load = useCallback(async () => {
    const current = ++generation.current;
    try {
      const result = await getViralHomepage(platform);
      if (current === generation.current) setData(result);
    } catch (cause) {
      if (current === generation.current)
        setError(adminActivationErrorMessage(cause, "读取首页失败"));
    }
  }, [platform]);
  useEffect(() => {
    setData(null);
    setDraftIds(null);
    setEditing(null);
    setPreparation(null);
    setError("");
    setNotice("");
    void load();
    return () => {
      generation.current += 1;
    };
  }, [load]);
  function keyFor(fingerprint: string) {
    if (retry.current?.fingerprint !== fingerprint)
      retry.current = { fingerprint, key: crypto.randomUUID() };
    return retry.current.key;
  }
  const orderedItems = (() => {
    if (!data) return [];
    if (!draftIds) return data.items;
    const byId = new Map(data.items.map((video) => [video.video_id, video]));
    return draftIds.flatMap((id) => {
      const video = byId.get(id);
      return video ? [video] : [];
    });
  })();
  function move(index: number, to: number) {
    if (
      !data ||
      saving ||
      readOnly ||
      !Number.isInteger(to) ||
      to < 0 ||
      to >= orderedItems.length ||
      index === to
    )
      return;
    const after = orderedItems.map((video) => video.video_id);
    after.splice(to, 0, after.splice(index, 1)[0]);
    const original = data.items.map((video) => video.video_id);
    // 调回原顺序等于没有调整，不留「未保存」状态。
    setDraftIds(after.every((id, i) => id === original[i]) ? null : after);
    setNotice("");
  }
  async function saveOrder() {
    if (!data || !draftIds || saving || readOnly) return;
    const before = data.items.map((video) => video.video_id);
    setSaving(true);
    setError("");
    try {
      // 整体提交完整顺序与加载时的旧快照：多处调整只产生一次写入与一条审计。
      await reorderViralHomepage(
        platform,
        draftIds,
        before,
        keyFor(JSON.stringify({ platform, after: draftIds, before })),
      );
      retry.current = null;
      setDraftIds(null);
      await load();
      setNotice("首页顺序已保存，客户首页按此顺序展示可用内容。");
    } catch (cause) {
      setError(adminActivationErrorMessage(cause, "保存顺序失败"));
    } finally {
      setSaving(false);
    }
  }
  async function requestPreparation(video: ViralHomepageVideo) {
    if (readOnly || saving) return;
    setSaving(true);
    setError("");
    try {
      const estimate = await estimateViralOperation({
        action: "archive",
        platform,
        items: [video],
      });
      setPreparation({ video, estimate, key: crypto.randomUUID() });
    } catch (cause) {
      setError(
        adminActivationErrorMessage(cause, "费用预估失败，未准备素材。"),
      );
    } finally {
      setSaving(false);
    }
  }
  async function confirmPreparation(reason: string) {
    if (!preparation || readOnly || saving) return;
    setSaving(true);
    setError("");
    try {
      await archiveCollectedViralVideo(
        preparation.video,
        reason,
        preparation.key,
        preparation.estimate.snapshot,
      );
      setPreparation(null);
      await load();
      setNotice("已提交素材准备，排期与实际就绪状态共同决定首页可见性。");
    } catch (cause) {
      setError(adminActivationErrorMessage(cause, "准备素材失败"));
    } finally {
      setSaving(false);
    }
  }
  async function saveSchedule() {
    if (!editing || saving) return;
    const from = starts ? new Date(`${starts}:00+08:00`).toISOString() : null;
    const to = ends ? new Date(`${ends}:00+08:00`).toISOString() : null;
    if (from && to && to <= from) {
      setError("下线时间必须晚于上线时间。");
      return;
    }
    setSaving(true);
    setError("");
    try {
      await scheduleViralHomepage(
        editing,
        from,
        to,
        keyFor(
          JSON.stringify({
            platform,
            id: editing.video_id,
            from,
            to,
            expected_starts_at: editing.homepage_starts_at,
            expected_ends_at: editing.homepage_ends_at,
          }),
        ),
      );
      retry.current = null;
      setEditing(null);
      await load();
      setNotice("排期已保存，到点自动改变客户首页可见性。");
    } catch (cause) {
      setError(adminActivationErrorMessage(cause, "保存排期失败"));
    } finally {
      setSaving(false);
    }
  }
  return (
    <section className="admin-panel admin-homepage-page" aria-label="首页编排">
      <header>
        <p className="admin-hint">
          此顺序对应客户的默认“首页推荐”；客户主动选“最新”时按发布时间排列。排期采用北京时间。
        </p>
      </header>
      <div className="admin-viral-seg">
        {(["douyin", "wechat_channels"] as const).map((p) => (
          <button
            type="button"
            key={p}
            className={platform === p ? "is-active" : ""}
            aria-pressed={platform === p}
            disabled={saving}
            onClick={() => setPlatform(p)}
          >
            {p === "douyin" ? "抖音" : "视频号"}
          </button>
        ))}
      </div>
      {error && <PageBanner tone="error">{error}</PageBanner>}
      {notice && <PageBanner tone="notice">{notice}</PageBanner>}
      {!data && !error && <p role="status">正在读取首页内容…</p>}
      {data && (
        <>
          <p className="admin-hint">
            {data.counting_rule} 容量：
            {data.capacity == null ? "尚未配置容量约束" : data.capacity}。
          </p>
          {data.capacity != null && data.items.length > data.capacity ? (
            <PageBanner tone="error">
              当前已上首页 {data.items.length} 条，超过容量上限 {data.capacity}{" "}
              条；超出部分客户端仍会展示，请下移或隐藏部分内容，
              或到「采集设置」调整容量。
            </PageBanner>
          ) : null}
          {(() => {
            const flagged = orderedItems.filter(
              (video) => homepageProblems(video).length > 0,
            );
            return flagged.length > 0 ? (
              <section
                aria-label="需要处理的内容"
                className="admin-homepage-problems"
              >
                <h3>需要处理（{flagged.length}）</h3>
                <ul>
                  {flagged.map((video) => (
                    <li key={video.video_id}>
                      <button
                        type="button"
                        onClick={() =>
                          document
                            .getElementById(`homepage-item-${video.video_id}`)
                            ?.scrollIntoView({ block: "center" })
                        }
                      >
                        {video.title || "未命名视频"}
                      </button>
                      ：{homepageProblems(video).join(" · ")}
                    </li>
                  ))}
                </ul>
              </section>
            ) : null;
          })()}
          {draftIds ? (
            <div
              className="admin-homepage-savebar"
              role="status"
              aria-label="顺序调整"
            >
              <strong>顺序已调整，尚未保存</strong>
              <button
                type="button"
                disabled={saving}
                onClick={() => void saveOrder()}
              >
                {saving ? "保存中…" : "保存顺序"}
              </button>
              <button
                type="button"
                disabled={saving}
                onClick={() => setDraftIds(null)}
              >
                撤销调整
              </button>
            </div>
          ) : null}
          <ol className="admin-homepage-order">
            {orderedItems.map((video, index) => {
              const problems = homepageProblems(video);
              return (
                <li
                  id={`homepage-item-${video.video_id}`}
                  key={video.video_id}
                  className={problems.length > 0 ? "has-problem" : undefined}
                  draggable={!readOnly && !saving}
                  onDragStart={() => setDragIndex(index)}
                  onDragOver={(event) => event.preventDefault()}
                  onDrop={() => {
                    if (dragIndex !== null) move(dragIndex, index);
                    setDragIndex(null);
                  }}
                  onDragEnd={() => setDragIndex(null)}
                >
                  <span className="admin-homepage-position">{index + 1}</span>
                  <div className="admin-homepage-thumb">
                    <PreviewCover src={video.cover_url ?? null} />
                  </div>
                  <div className="admin-homepage-content">
                    <strong>{video.title || "未命名视频"}</strong>
                    <p className="admin-hint">
                      {video.homepage_live ? "当前展示" : "排期未生效或已结束"}{" "}
                      · {video.author || "未知作者"}
                    </p>
                    <p
                      className={
                        video.opens_after_homepage === 0 ? "admin-hint" : ""
                      }
                    >
                      上首页后详情读取：
                      {video.opens_after_homepage ?? "历史无法还原"}
                    </p>
                    {problems.length > 0 && (
                      <p className="admin-homepage-warning">
                        {problems.join(" · ")}
                      </p>
                    )}
                    <p className="admin-hint">
                      上线{" "}
                      {video.homepage_starts_at
                        ? formatDateTime(video.homepage_starts_at)
                        : "立即"}{" "}
                      · 下线{" "}
                      {video.homepage_ends_at
                        ? formatDateTime(video.homepage_ends_at)
                        : "不限制"}
                    </p>
                  </div>
                  {!readOnly && (
                    <div className="admin-homepage-actions">
                      <button
                        type="button"
                        disabled={saving || index === 0}
                        onClick={() => move(index, index - 1)}
                      >
                        上移
                      </button>
                      <button
                        type="button"
                        disabled={saving || index === orderedItems.length - 1}
                        onClick={() => move(index, index + 1)}
                      >
                        下移
                      </button>
                      <label>
                        移到第
                        <input
                          type="number"
                          min={1}
                          max={orderedItems.length}
                          aria-label={`「${video.title}」目标位置`}
                          value={positions[video.video_id] ?? String(index + 1)}
                          onChange={(e) =>
                            setPositions({
                              ...positions,
                              [video.video_id]: e.target.value,
                            })
                          }
                        />
                      </label>
                      <button
                        type="button"
                        disabled={saving}
                        onClick={() =>
                          move(
                            index,
                            Number(positions[video.video_id] ?? index + 1) - 1,
                          )
                        }
                      >
                        移动
                      </button>
                      <button
                        type="button"
                        disabled={saving}
                        onClick={() => {
                          setEditing(video);
                          setStarts(dateInput(video.homepage_starts_at));
                          setEnds(dateInput(video.homepage_ends_at));
                        }}
                      >
                        设置排期
                      </button>
                    </div>
                  )}
                </li>
              );
            })}
          </ol>
          {(data.preparing?.length ?? 0) > 0 && (
            <section aria-label="准备后上首页">
              <h3>准备完成后上首页</h3>
              <p className="admin-hint">
                这些素材仍在准备，可先设置排期；过了下线时间的任务不会发布。取消发布请在视频库操作。
              </p>
              {data.preparing?.map((video) => (
                <div className="admin-homepage-actions" key={video.video_id}>
                  <strong>{video.title}</strong>
                  <span>待准备 · 尚未展示</span>
                  {!readOnly && (
                    <div>
                      <button
                        type="button"
                        disabled={saving || video.availability !== "AVAILABLE"}
                        onClick={() => void requestPreparation(video)}
                      >
                        准备 / 重试素材
                      </button>
                      <button
                        type="button"
                        disabled={saving}
                        onClick={() => {
                          setEditing(video);
                          setStarts(dateInput(video.homepage_starts_at));
                          setEnds(dateInput(video.homepage_ends_at));
                        }}
                      >
                        设置排期
                      </button>
                    </div>
                  )}
                </div>
              ))}
            </section>
          )}
          {data.measurement_started_at && (
            <p className="admin-hint">
              详情读取从 {formatDateTime(data.measurement_started_at)}{" "}
              起计，包含已授权客户的再次成功读取；更早的点击历史无法还原。
            </p>
          )}
          {orderedItems.length === 0 && (
            <p>该平台暂无首页内容，请到视频库选择并上首页。</p>
          )}
          <h3>客户首页预览 · 前12条</h3>
          <div className="admin-homepage-preview">
            {data.preview.map((v) => (
              <article key={v.video_id}>
                <PreviewCover src={v.cover_url ?? null} />
                <strong>{v.title || "未命名视频"}</strong>
                <p>{v.author}</p>
              </article>
            ))}
          </div>
        </>
      )}
      <ConfirmDialog
        open={preparation !== null}
        title="确认首页素材准备费用"
        confirmLabel="确认准备"
        description="只准备这条素材，不重新搜索；缓存可复用，下载、存储和流量总费用未知。"
        busy={saving}
        error={error}
        level="reason"
        onClose={() => !saving && setPreparation(null)}
        onConfirm={(reason) => void confirmPreparation(reason)}
      >
        {preparation ? (
          <ViralOperationCostSummary estimate={preparation.estimate} />
        ) : null}
      </ConfirmDialog>
      {editing && (
        <form
          className="admin-homepage-schedule"
          onSubmit={(e) => {
            e.preventDefault();
            void saveSchedule();
          }}
        >
          <h3>排期 · {editing.title}</h3>
          <label>
            上线时间（北京时间）
            <input
              type="datetime-local"
              value={starts}
              onChange={(e) => setStarts(e.target.value)}
            />
          </label>
          <label>
            下线时间（北京时间）
            <input
              type="datetime-local"
              value={ends}
              onChange={(e) => setEnds(e.target.value)}
            />
          </label>
          <p className="admin-hint">留空表示立即上线或不限制下线时间。</p>
          <button type="submit" disabled={saving}>
            保存排期
          </button>
          <button
            type="button"
            disabled={saving}
            onClick={() => setEditing(null)}
          >
            取消
          </button>
        </form>
      )}
    </section>
  );
}
