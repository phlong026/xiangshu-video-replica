import { useEffect, useState } from "react";
import {
  getCopyExtractionQuote,
  getLatestScriptFromAudioTask,
  getLatestViralScriptFromAudioTask,
  type ScriptFromAudioTask,
  type ViralPlatform,
} from "../api";
import type { CopyExtractionSource, StudioVideo } from "./types";
import { Button, StudioDialog } from "./ui";

export function viralCopySource(
  video: StudioVideo,
  returnTo: "viral" | "workbench",
): CopyExtractionSource {
  return {
    title: video.title.trim() || "未命名视频",
    viral:
      video.platformKey && video.nativeId
        ? {
            platform: video.platformKey,
            label: video.platform,
            videoId: video.nativeId,
            studioId: video.id,
            returnTo,
          }
        : undefined,
  };
}

export function persistViralDetailUrl(
  video: Pick<StudioVideo, "platformKey" | "nativeId">,
) {
  if (!video.platformKey || !video.nativeId) return;
  const url = new URL(window.location.href);
  url.searchParams.set("viralPlatform", video.platformKey);
  url.searchParams.set("viralVideoId", video.nativeId);
  window.history.replaceState(null, "", url);
}

export function useCopyExtractionTask(
  accountId: string,
  source?:
    | { projectId: string; assetId: string }
    | { platform: ViralPlatform; videoId: string },
) {
  const projectId = source && "projectId" in source ? source.projectId : "";
  const assetId = source && "assetId" in source ? source.assetId : "";
  const platform = source && "platform" in source ? source.platform : undefined;
  const videoId = source && "videoId" in source ? source.videoId : "";
  const key = JSON.stringify([
    accountId,
    projectId,
    assetId,
    platform,
    videoId,
  ]);
  const [result, setResult] = useState<{
    key: string;
    task: ScriptFromAudioTask | null;
  }>();
  useEffect(() => {
    if (!(platform && videoId) && !(projectId && assetId)) return;
    let current = true;
    const request =
      platform && videoId
        ? getLatestViralScriptFromAudioTask(platform, videoId)
        : getLatestScriptFromAudioTask(projectId, assetId);
    void request.then(
      (task) => {
        if (current) setResult({ key, task });
      },
      () => {
        // 提交时服务端仍会复用已有任务，历史查询失败也不会导致用户重复付费。
        if (current) setResult({ key, task: null });
      },
    );
    return () => {
      current = false;
    };
  }, [key, projectId, assetId, platform, videoId]);
  return result?.key === key ? result.task : undefined;
}

export function useCopyQuote(
  accountId: string,
  service: "asr" | "link_resolution",
  units?: number,
) {
  const key = JSON.stringify([accountId, service, units]);
  const [result, setResult] = useState<{
    key: string;
    credits?: number;
    unitCredits?: string;
  }>();
  useEffect(() => {
    if (units === undefined) return;
    let current = true;
    void getCopyExtractionQuote(service, units).then(
      (quote) => {
        if (current)
          setResult({
            key,
            credits: quote.credits,
            unitCredits: quote.unit_credits,
          });
      },
      () => {
        if (current) setResult({ key });
      },
    );
    return () => {
      current = false;
    };
  }, [key, service, units]);
  return result?.key === key ? result : undefined;
}

export function copyQuoteText(credits?: number) {
  return credits === undefined
    ? "转写按实际音频时长计费"
    : `预计 ${credits} 积分，按实际音频时长结算`;
}

export function CopyExtractionAction({
  disabled,
  restartDisabled = disabled,
  task,
  quote,
  onSelect,
  actionLabel = "提取文案",
}: {
  disabled: boolean;
  restartDisabled?: boolean;
  task?: ScriptFromAudioTask | null;
  quote?: number;
  onSelect: (restart: boolean, task?: ScriptFromAudioTask) => void;
  actionLabel?: string;
}) {
  const [confirm, setConfirm] = useState(false);
  const completed = task?.status === "SUCCEEDED" && Boolean(task.result?.text);
  const pending = task?.status === "PENDING" || task?.status === "RUNNING";
  const uncertain = task?.status === "SUBMISSION_UNCERTAIN";
  const existing = completed || pending || uncertain;
  return (
    <>
      <Button
        variant="outline"
        disabled={disabled}
        onClick={() => onSelect(false, existing ? task : undefined)}
      >
        {completed
          ? "查看已提取文案"
          : pending
            ? "查看转写进度"
            : uncertain
              ? "查看转写状态"
              : actionLabel}
      </Button>
      {completed && (
        <Button
          variant="quiet"
          disabled={restartDisabled}
          onClick={() => setConfirm(true)}
        >
          重新转写
        </Button>
      )}
      {confirm && completed && (
        <StudioDialog title="重新转写文案" onClose={() => setConfirm(false)}>
          <p>{copyQuoteText(quote)}。本次会创建新的转写任务。</p>
          <Button
            variant="primary"
            disabled={restartDisabled}
            onClick={() => {
              setConfirm(false);
              onSelect(true);
            }}
          >
            确认重新转写
          </Button>
        </StudioDialog>
      )}
    </>
  );
}
