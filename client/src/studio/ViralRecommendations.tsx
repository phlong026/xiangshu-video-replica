import { useCallback, useEffect, useRef, useState } from "react";
import { ViralCard } from "./ContentPages";
import { useStudio } from "./context";
import { loadViralVideos } from "./live";
import type { StudioVideo, StudioViralTiming } from "./types";
import { Button, Panel } from "./ui";
import { isPublishedViralVideo } from "./viralVisibility";

type HomepageResult = Awaited<ReturnType<typeof loadViralVideos>>;

function clockOffset(
  platformKey: StudioVideo["platformKey"],
  timing?: StudioViralTiming[],
) {
  const response = timing?.find((item) => item.platformKey === platformKey);
  const serverTime = response?.serverTime
    ? Date.parse(response.serverTime)
    : NaN;
  return response &&
    Number.isFinite(response.receivedAt) &&
    Number.isFinite(serverTime)
    ? response.receivedAt - serverTime
    : 0;
}

// 工作台左列按 3 列铺排，两整行共 6 张；更多精选通过「查看全部爆款」进列表页。
const HOMEPAGE_CARD_LIMIT = 6;

function selectHomepageVideos(videos: StudioVideo[]) {
  // 各平台的置顶序号独立分配；队首按推送时间合并，避免新平台上传被旧负序号挤走。
  const groups = new Map<string, StudioVideo[]>();
  const ranked = [...videos].sort(
    (left, right) =>
      (left.homepageRank ?? 2147483647) - (right.homepageRank ?? 2147483647) ||
      Date.parse(right.pushedAt ?? "") - Date.parse(left.pushedAt ?? ""),
  );
  for (const video of ranked) {
    const platform = video.platformKey ?? video.platform;
    const queue = groups.get(platform) ?? [];
    queue.push(video);
    groups.set(platform, queue);
  }
  const pushedTime = (video?: StudioVideo) =>
    Date.parse(video?.pushedAt ?? "") || 0;
  const selected: StudioVideo[] = [];
  while (selected.length < HOMEPAGE_CARD_LIMIT) {
    const queues = [...groups.values()]
      .filter((queue) => queue.length > 0)
      .sort((left, right) => pushedTime(right[0]) - pushedTime(left[0]));
    const next = queues[0]?.shift();
    if (!next) break;
    selected.push(next);
  }
  return selected;
}

export function ViralRecommendations() {
  const { review, user, requireLogin } = useStudio();
  if (requireLogin) return null;
  // 换账号立即丢弃已确认集合与收藏覆盖，旧请求只能触及已卸载的组件。
  return <HomepageRecommendations key={`${review}:${user.id}`} />;
}

function HomepageRecommendations() {
  const { data, navigate, review } = useStudio();
  const [homepage, setHomepage] = useState<HomepageResult>();
  const [loading, setLoading] = useState(!review);
  const [now, setNow] = useState(Date.now);
  const [favorites, setFavorites] = useState<Record<string, boolean>>({});
  const [requestVersion, setRequestVersion] = useState(0);
  const requestVersionRef = useRef(0);
  // 真实模式每次进入都先读当前精选，不用启动缓存闪回已下架内容。
  const source = review ? data.homepageVideos : homepage?.videos;

  const refresh = useCallback(() => {
    setNow(Date.now());
    if (review) return;
    const version = ++requestVersionRef.current;
    setRequestVersion(version);
  }, [review]);

  useEffect(() => {
    if (review) return;
    let cancelled = false;
    const isCurrent = () =>
      !cancelled && requestVersion === requestVersionRef.current;
    setLoading(true);
    void loadViralVideos()
      .then((result) => {
        if (!isCurrent()) return;
        setHomepage(result);
        setNow(Date.now());
      })
      .catch(() => {
        // 查询失败保留最近一次确认的集合，不退回共享缓存复活已隐藏内容。
      })
      .finally(() => {
        if (isCurrent()) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [requestVersion, review]);

  useEffect(() => {
    const onVisibility = () => {
      if (document.visibilityState === "visible") refresh();
    };
    window.addEventListener("focus", refresh);
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      window.removeEventListener("focus", refresh);
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, [refresh]);

  useEffect(() => {
    if (review || !homepage) return;
    const clientNow = Date.now();
    const boundaries = homepage.videos
      .flatMap((video) =>
        [video.homepageStartsAt, video.homepageEndsAt].map(
          (value) =>
            Date.parse(value ?? "") +
            clockOffset(video.platformKey, homepage.timing),
        ),
      )
      .filter((value) => Number.isFinite(value) && value > clientNow);
    for (const timing of homepage.timing ?? []) {
      const boundary =
        Date.parse(timing.nextChangeAt ?? "") +
        clockOffset(timing.platformKey, homepage.timing);
      // 较早返回的平台可能已跨过边界：此时立即补查，不能因聚合耗时丢掉排期。
      if (Number.isFinite(boundary)) boundaries.push(boundary);
    }
    if (!boundaries.length) return;
    const timer = window.setTimeout(
      refresh,
      Math.max(
        0,
        Math.min(Math.min(...boundaries) - Date.now(), 2_147_000_000),
      ),
    );
    return () => window.clearTimeout(timer);
  }, [homepage, refresh, review]);

  useEffect(() => {
    if (!review) return;
    const clientNow = Date.now();
    const boundaries = (data.homepageVideos ?? [])
      .flatMap((video) => [video.homepageStartsAt, video.homepageEndsAt])
      .map((value) => Date.parse(value ?? ""))
      .filter(
        (value) => Number.isFinite(value) && value > now && value > clientNow,
      );
    if (!boundaries.length) return;
    const timer = window.setTimeout(
      refresh,
      Math.max(
        0,
        Math.min(Math.min(...boundaries) - Date.now(), 2_147_000_000),
      ),
    );
    return () => window.clearTimeout(timer);
  }, [data.homepageVideos, now, refresh, review]);

  const videos = selectHomepageVideos(
    (source ?? []).filter(
      (video) =>
        video.homepageFeatured &&
        isPublishedViralVideo(
          video,
          now - (review ? 0 : clockOffset(video.platformKey, homepage?.timing)),
        ),
    ),
  );

  return (
    <Panel className="studio-home-recommendations">
      <div className="studio-home-section-heading">
        <h2>爆款视频精选</h2>
        <Button variant="quiet" onClick={() => navigate("viral")}>
          查看全部爆款
        </Button>
      </div>
      {videos.length ? (
        <div className="studio-home-viral-grid">
          {videos.map((video) => (
            <ViralCard
              key={video.id}
              video={video}
              active={false}
              onActivate={() => {}}
              detailReturnTo="workbench"
              savedFromServer={
                review ? undefined : (favorites[video.id] ?? video.isFavorite)
              }
              onFavoriteChange={(saved) =>
                setFavorites((current) => ({ ...current, [video.id]: saved }))
              }
            />
          ))}
        </div>
      ) : (
        <div className="studio-home-empty-card" role="status">
          <strong>
            {loading || (review && data.loading)
              ? "正在读取推荐视频"
              : "暂无推荐视频"}
          </strong>
          {!loading && !(review && data.loading) ? (
            <Button variant="outline" onClick={() => navigate("viral")}>
              去爆款库逛逛
            </Button>
          ) : null}
        </div>
      )}
    </Panel>
  );
}
