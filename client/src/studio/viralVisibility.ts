import type { StudioVideo } from "./types";

export function isPublishedViralVideo(video: StudioVideo, now = Date.now()) {
  return (
    Boolean(video.pushedAt) &&
    (video.availability ?? "available") === "available" &&
    (!video.homepageStartsAt || Date.parse(video.homepageStartsAt) <= now) &&
    (!video.homepageEndsAt || Date.parse(video.homepageEndsAt) > now)
  );
}
