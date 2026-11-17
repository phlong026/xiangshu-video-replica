import { useCallback, useEffect, useRef, useState } from "react";
import { ViralCard } from "./ContentPages";
import { useStudio } from "./context";
import { loadViralVideos } from "./live";
import type { StudioVideo, StudioViralTiming } from "./types";
import { Button, Empty, Panel } from "./ui";
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
  const [activeVideoId, setActiveVideoId] = useState<string>();
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

  const videos = (source ?? [])
    .filter(
      (video) =>
        video.homepageFeatured &&
        isPublishedViralVideo(
          video,
          now - (review ? 0 : clockOffset(video.platformKey, homepage?.timing)),
        ),
    )
    .sort(
      (left, right) =>
        (left.homepageRank ?? 2147483647) -
          (right.homepageRank ?? 2147483647) ||
        Date.parse(right.pushedAt ?? "") - Date.parse(left.pushedAt ?? ""),
    )
    .slice(0, 4);

  return (
    <Panel className="studio-home-recommendations">
      <div className="studio-home-recommendations-heading">
        <h2>爆款视频精选</h2>
        <Button variant="quiet" onClick={() => navigate("viral")}>
          查看全部爆款
        </Button>
      </div>
      {videos.length ? (
        <div className="content-video-grid content-video-grid-viral">
          {videos.map((video) => (
            <ViralCard
              key={video.id}
              video={video}
              active={activeVideoId === video.id}
              onActivate={() => setActiveVideoId(video.id)}
              savedFromServer={
                review ? undefined : (favorites[video.id] ?? video.isFavorite)
              }
              onFavoriteChange={(saved) =>
                setFavorites((current) => ({ ...current, [video.id]: saved }))
              }
              availability={video.availability}
            />
          ))}
        </div>
      ) : (
        <Empty
          title={
            loading || (review && data.loading)
              ? "正在读取推荐视频"
              : "暂无推荐视频"
          }
        />
      )}
    </Panel>
  );
}
