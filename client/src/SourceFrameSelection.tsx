import { useCallback, useEffect, useRef, useState } from "react";

import {
  confirmSourceFrame,
  extractSourceFrames,
  getAssetDownloadUrl,
  getLatestProjectSourceFrameSelection,
  getLatestProjectSourceFrames,
  readSourceFrameCandidates,
  type SourceFrameCandidate,
} from "./api";

export function SourceFrameSelection({
  projectId,
  readOnly = false,
  referenceAssetId,
}: {
  projectId: string;
  readOnly?: boolean;
  referenceAssetId: string | null;
}) {
  const [candidates, setCandidates] = useState<SourceFrameCandidate[]>([]);
  const [previewUrls, setPreviewUrls] = useState<Record<string, string>>({});
  const [failedPreviewAssetIds, setFailedPreviewAssetIds] = useState<string[]>(
    [],
  );
  const [selectedAssetId, setSelectedAssetId] = useState("");
  const [timestampsText, setTimestampsText] = useState("0.5, 1.5, 2.5");
  const [status, setStatus] = useState("");
  const [error, setError] = useState("");
  const [isLoading, setIsLoading] = useState(true);
  const [isSubmitting, setIsSubmitting] = useState(false);
  const loadRequestId = useRef(0);

  const loadCandidates = useCallback(async () => {
    const requestId = loadRequestId.current + 1;
    loadRequestId.current = requestId;
    const isCurrentRequest = () => requestId === loadRequestId.current;
    setIsLoading(true);
    setError("");
    try {
      const [version, selection] = await Promise.all([
        getLatestProjectSourceFrames(projectId),
        getLatestProjectSourceFrameSelection(projectId),
      ]);
      if (!isCurrentRequest()) {
        return;
      }
      if (!version) {
        setCandidates([]);
        setPreviewUrls({});
        setFailedPreviewAssetIds([]);
        setSelectedAssetId("");
        setStatus(selection.stale ? "候选已更新，请重新确认源画面。" : "");
        return;
      }
      const payload = readSourceFrameCandidates(version);
      if (!payload) {
        setError("候选源画面数据格式无效，请重新提取。");
        return;
      }
      setCandidates(payload.candidates);
      setPreviewUrls({});
      setFailedPreviewAssetIds([]);
      setTimestampsText(payload.requested_timestamps_seconds.join(", "));
      const confirmedAssetId = selection.version?.payload.source_frame_asset_id;
      if (typeof confirmedAssetId === "string") {
        setSelectedAssetId(confirmedAssetId);
        setStatus("当前候选源画面已确认。");
      } else if (selection.stale) {
        setSelectedAssetId("");
        setStatus("候选已更新，请重新确认源画面。");
      } else {
        setSelectedAssetId("");
        setStatus("");
      }
      const previewResults = await Promise.allSettled(
        payload.candidates.map(async (candidate) => {
          const download = await getAssetDownloadUrl(candidate.asset_id);
          return [candidate.asset_id, download.url] as const;
        }),
      );
      const previewEntries = previewResults.flatMap((result) =>
        result.status === "fulfilled" ? [result.value] : [],
      );
      if (!isCurrentRequest()) {
        return;
      }
      setPreviewUrls(Object.fromEntries(previewEntries));
      setFailedPreviewAssetIds(
        previewResults.flatMap((result, index) =>
          result.status === "rejected"
            ? [payload.candidates[index].asset_id]
            : [],
        ),
      );
    } catch (requestError) {
      if (!isCurrentRequest()) {
        return;
      }
      setError(
        requestError instanceof Error
          ? requestError.message
          : "读取候选源画面失败。",
      );
    } finally {
      if (isCurrentRequest()) {
        setIsLoading(false);
      }
    }
  }, [projectId]);

  useEffect(() => {
    void loadCandidates();
    return () => {
      loadRequestId.current += 1;
    };
  }, [loadCandidates]);

  function parseTimestamps(): number[] | null {
    const values = timestampsText.split(",").map((value) => value.trim());
    if (values.some((value) => value === "")) {
      return null;
    }
    const timestamps = values.map(Number);
    if (
      timestamps.length < 1 ||
      timestamps.length > 3 ||
      timestamps.some(
        (timestamp) =>
          !Number.isFinite(timestamp) || timestamp < 0 || timestamp > 3,
      ) ||
      new Set(timestamps).size !== timestamps.length
    ) {
      return null;
    }
    return timestamps;
  }

  async function handleExtract() {
    if (readOnly) {
      return;
    }
    if (!referenceAssetId) {
      setError("参考视频尚未就绪，不能提取源画面。");
      return;
    }
    const timestamps = parseTimestamps();
    if (!timestamps) {
      setError("请输入 1–3 个首 3 秒内且不重复的时间点，例如 0.5, 1.5, 2.5。");
      return;
    }
    setIsSubmitting(true);
    loadRequestId.current += 1;
    setError("");
    setStatus("");
    try {
      await extractSourceFrames(projectId, referenceAssetId, timestamps);
      setSelectedAssetId("");
      setStatus("候选源画面已更新，请选择并确认一张。");
      await loadCandidates();
    } catch (requestError) {
      setError(
        requestError instanceof Error
          ? requestError.message
          : "提取候选源画面失败。",
      );
    } finally {
      setIsSubmitting(false);
    }
  }

  async function handleConfirm() {
    if (readOnly) {
      return;
    }
    if (!selectedAssetId || !previewUrls[selectedAssetId]) {
      setError("请先加载并查看候选源画面预览，再进行确认。");
      return;
    }
    setIsSubmitting(true);
    setError("");
    try {
      await confirmSourceFrame(projectId, selectedAssetId);
      const selectedIndex = candidates.findIndex(
        (candidate) => candidate.asset_id === selectedAssetId,
      );
      setStatus(`已确认候选源画面 ${selectedIndex + 1}。`);
    } catch (requestError) {
      setError(
        requestError instanceof Error
          ? requestError.message
          : "确认源画面失败。",
      );
    } finally {
      setIsSubmitting(false);
    }
  }

  return (
    <section
      className="source-frame-selection"
      aria-labelledby="source-frame-title"
    >
      <div>
        <span className="eyebrow">SOURCE FRAME</span>
        <h3 id="source-frame-title">候选源画面</h3>
        <p>
          技术画质参考只用于排序；请以人物可见性和替换效果为准，手动确认一张。
        </p>
      </div>
      <div className="source-frame-toolbar">
        <label className="source-frame-timestamps">
          重新取帧时间点（秒）
          <input
            aria-describedby="source-frame-time-hint"
            aria-label="重新取帧时间点（秒）"
            disabled={readOnly || isSubmitting}
            onChange={(event) => setTimestampsText(event.target.value)}
            value={timestampsText}
          />
          <small id="source-frame-time-hint">
            支持 1–3 个首 3 秒内的时间点，以逗号分隔。
          </small>
        </label>
        <button
          className="secondary-button"
          disabled={readOnly || isSubmitting || !referenceAssetId}
          onClick={handleExtract}
          type="button"
        >
          {isSubmitting ? "正在处理" : "重新提取候选"}
        </button>
      </div>
      {isLoading ? <p className="status-note">正在读取候选源画面</p> : null}
      {error ? <p className="settings-error">{error}</p> : null}
      {status ? <p className="setup-success">{status}</p> : null}
      {!isLoading && !error && candidates.length === 0 ? (
        <p className="file-note">尚未提取候选源画面。</p>
      ) : null}
      <fieldset className="source-frame-options">
        <legend>选择一张用于后续人物置换和首帧生成</legend>
        {candidates.map((candidate, index) => (
          <label
            className={
              selectedAssetId === candidate.asset_id
                ? "source-frame-option source-frame-option--selected"
                : "source-frame-option"
            }
            key={candidate.asset_id}
          >
            <input
              checked={selectedAssetId === candidate.asset_id}
              disabled={readOnly || !previewUrls[candidate.asset_id]}
              name="source-frame"
              onChange={() => setSelectedAssetId(candidate.asset_id)}
              type="radio"
              value={candidate.asset_id}
            />
            {previewUrls[candidate.asset_id] ? (
              <img
                alt={`候选源画面 ${index + 1}`}
                src={previewUrls[candidate.asset_id]}
              />
            ) : (
              <span className="source-frame-placeholder">
                {failedPreviewAssetIds.includes(candidate.asset_id)
                  ? "预览加载失败，请重新提取"
                  : "预览加载中"}
              </span>
            )}
            <span>
              <strong>候选 {index + 1}</strong>
              <small>{candidate.timestamp_seconds.toFixed(1)} 秒</small>
              <small>
                技术画质参考{" "}
                {candidate.score === null ? "暂无" : candidate.score.toFixed(2)}
              </small>
            </span>
          </label>
        ))}
      </fieldset>
      {readOnly ? (
        <p className="status-note">只读身份不能重新提取或确认源画面。</p>
      ) : (
        <button
          className="source-frame-confirm"
          disabled={isSubmitting || !selectedAssetId}
          onClick={handleConfirm}
          type="button"
        >
          确认源画面
        </button>
      )}
    </section>
  );
}
