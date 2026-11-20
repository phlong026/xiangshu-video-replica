import {
  type ReactNode,
  useCallback,
  useEffect,
  useRef,
  useState,
} from "react";
import {
  createGenerationResultPreviewUrl,
  createGenerationTaskPreviewUrl,
  DIRECT_RESULT_RETENTION_HINT,
  downloadGenerationResult,
  downloadGenerationTaskResult,
  type GenerationTask,
  getGenerationBatch,
  isResultUrlExpired,
  RESULT_URL_EXPIRED_MESSAGE,
} from "../api";
import { VideoPreview } from "../VideoPreview";
import { Button, Hint, Icon } from "./ui";

/**
 * 成片就地消费面板：在创作页里直接播放刚生成的视频并下载，不再把用户
 * 推去任务中心。数据链路与任务中心完全一致——批次详情取可交付任务，
 * 再由服务端按需签发播放地址（直链交付走任务级接口，归档交付走资产级
 * 接口），下载同样复用那两条既有通道，不新增后端契约。
 */

function deliverableTask(tasks: GenerationTask[]): GenerationTask | undefined {
  return tasks.find(
    (task) =>
      task.status === "SUCCEEDED" &&
      (task.direct_result_available || Boolean(task.result_asset_id)),
  );
}

async function taskPreviewUrl(task: GenerationTask): Promise<string> {
  if (task.direct_result_available) {
    return createGenerationTaskPreviewUrl(task.id);
  }
  if (!task.result_asset_id) {
    throw new Error("成片还在归档，请稍后重试。");
  }
  return createGenerationResultPreviewUrl(task.result_asset_id);
}

export type StudioResultPreviewProps = {
  batchId: string;
  /** 首帧缩略图：播放器就绪前与播放失败时的占位画面。 */
  poster?: ReactNode;
  onOpenTasks: () => void;
};

export function StudioResultPreview({
  batchId,
  poster,
  onOpenTasks,
}: StudioResultPreviewProps) {
  const [task, setTask] = useState<GenerationTask>();
  const [url, setUrl] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [downloading, setDownloading] = useState(false);
  const [downloadNote, setDownloadNote] = useState("");
  const request = useRef(0);

  const load = useCallback(async () => {
    request.current += 1;
    const ticket = request.current;
    setLoading(true);
    setError("");
    try {
      const batch = await getGenerationBatch(batchId);
      const next = deliverableTask(batch.tasks);
      if (!next) throw new Error("成片还在归档，请稍后重试。");
      if (ticket !== request.current) return;
      setTask(next);
      const src = await taskPreviewUrl(next);
      if (ticket !== request.current) return;
      setUrl(src);
    } catch (cause) {
      if (ticket !== request.current) return;
      setError(
        isResultUrlExpired(cause)
          ? RESULT_URL_EXPIRED_MESSAGE
          : "成片暂时打不开，请点「重新获取」再试。",
      );
    } finally {
      if (ticket === request.current) setLoading(false);
    }
  }, [batchId]);

  useEffect(() => {
    void load();
    return () => {
      // 批次切换或卸载后到达的结果一律丢弃，避免旧地址覆盖新播放器。
      request.current += 1;
    };
  }, [load]);

  const download = async () => {
    if (!task || downloading) return;
    setDownloading(true);
    setDownloadNote("");
    try {
      const filename = `${task.id}.mp4`;
      const result = task.direct_result_available
        ? await downloadGenerationTaskResult(task.id, filename)
        : await downloadGenerationResult(task.result_asset_id ?? "", filename);
      setDownloadNote(
        result.status === "saved"
          ? "已保存到本地。"
          : result.status === "cancelled"
            ? "已取消保存。"
            : "已开始下载，可在浏览器下载列表中查看。",
      );
    } catch (cause) {
      setDownloadNote(
        isResultUrlExpired(cause)
          ? RESULT_URL_EXPIRED_MESSAGE
          : "下载失败，请检查网络后重试。",
      );
    } finally {
      setDownloading(false);
    }
  };

  return (
    <div className="creation-result">
      {url ? (
        <VideoPreview
          alt="成片预览"
          aria-label="成片预览"
          className="creation-result-player"
          controls
          frameRatio="source"
          playsInline
          preload="metadata"
          src={url}
          videoClassName="creation-result-video"
        />
      ) : (
        <div className="creation-result-poster">
          {poster}
          <div className="creation-result-overlay">
            {error ? (
              <>
                <p>{error}</p>
                <Button variant="outline" onClick={() => void load()}>
                  重新获取
                </Button>
              </>
            ) : (
              <>
                <span className="creation-result-spinner" aria-hidden />
                <p>{loading ? "正在获取成片…" : "成片准备中"}</p>
              </>
            )}
          </div>
        </div>
      )}
      <div className="creation-result-actions">
        <Button
          disabled={!task || downloading}
          onClick={() => void download()}
          variant="primary"
        >
          <Icon name="download" size={16} />
          {downloading ? "下载中…" : "下载成片"}
        </Button>
        <Button onClick={onOpenTasks} variant="quiet">
          任务中心 <Icon name="arrow" />
        </Button>
      </div>
      {downloadNote ? <Hint>{downloadNote}</Hint> : null}
      <Hint>{DIRECT_RESULT_RETENTION_HINT}</Hint>
    </div>
  );
}
