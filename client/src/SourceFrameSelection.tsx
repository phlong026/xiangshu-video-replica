import { useCallback, useEffect, useRef, useState } from "react";

import {
  type AnalysisVersion,
  confirmSourceFrame,
  extractSourceFrames,
  getAssetDownloadUrl,
  getLatestProjectSourceFrameSelection,
  getLatestProjectSourceFrames,
  readSourceFrameCandidates,
  type SourceFrameCandidate,
  type SourceFrameCharacterFeatures,
} from "./api";

export function SourceFrameSelection({
  onSelectionChange,
  projectId,
  readOnly = false,
  referenceAssetId,
}: {
  onSelectionChange?: (selection: AnalysisVersion | null) => void;
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
  const [orientation, setOrientation] = useState("");
  const [shotSize, setShotSize] = useState("");
  const [faceVisibility, setFaceVisibility] = useState("");
  const [bodyCompleteness, setBodyCompleteness] = useState("");
  const [timestampsText, setTimestampsText] = useState("0.5, 1.5, 2.5");
  const [status, setStatus] = useState("");
  const [error, setError] = useState("");
  const [isLoading, setIsLoading] = useState(true);
  const [isSubmitting, setIsSubmitting] = useState(false);
  const loadRequestId = useRef(0);

  const resetFeatures = useCallback(() => {
    setOrientation("");
    setShotSize("");
    setFaceVisibility("");
    setBodyCompleteness("");
  }, []);

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
        resetFeatures();
        onSelectionChange?.(null);
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
      const confirmedFeatures = selection.version
        ? readCharacterFeatures(selection.version)
        : null;
      if (typeof confirmedAssetId === "string" && confirmedFeatures) {
        setSelectedAssetId(confirmedAssetId);
        setOrientation(confirmedFeatures.orientation);
        setShotSize(confirmedFeatures.shot_size);
        setFaceVisibility(
          confirmedFeatures.face_visible ? "VISIBLE" : "HIDDEN",
        );
        setBodyCompleteness(confirmedFeatures.body_completeness);
        setStatus("当前候选源画面已确认。");
        onSelectionChange?.(selection.version);
      } else if (typeof confirmedAssetId === "string") {
        setSelectedAssetId(confirmedAssetId);
        resetFeatures();
        setStatus("已保存的源画面缺少人物特征，请重新确认源画面。");
        onSelectionChange?.(null);
      } else if (selection.stale) {
        setSelectedAssetId("");
        resetFeatures();
        setStatus("候选已更新，请重新确认源画面。");
        onSelectionChange?.(null);
      } else {
        setSelectedAssetId("");
        resetFeatures();
        setStatus("");
        onSelectionChange?.(null);
      }
      if (readOnly) {
        return;
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
      onSelectionChange?.(null);
    } finally {
      if (isCurrentRequest()) {
        setIsLoading(false);
      }
    }
  }, [onSelectionChange, projectId, readOnly, resetFeatures]);

  function invalidateConfirmation() {
    setStatus("源画面或人物特征已修改，请重新确认。");
    onSelectionChange?.(null);
  }

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
      resetFeatures();
      onSelectionChange?.(null);
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
    const characterFeatures = currentCharacterFeatures(
      orientation,
      shotSize,
      faceVisibility,
      bodyCompleteness,
    );
    if (
      !selectedAssetId ||
      !previewUrls[selectedAssetId] ||
      !characterFeatures
    ) {
      setError("请先加载并查看候选源画面预览，再进行确认。");
      return;
    }
    setIsSubmitting(true);
    setError("");
    try {
      const selection = await confirmSourceFrame(
        projectId,
        selectedAssetId,
        characterFeatures,
      );
      const selectedIndex = candidates.findIndex(
        (candidate) => candidate.asset_id === selectedAssetId,
      );
      setStatus(`已确认候选源画面 ${selectedIndex + 1}。`);
      onSelectionChange?.(selection);
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
      <fieldset className="source-frame-features">
        <legend>人物替换特征（需人工确认）</legend>
        <label>
          人物朝向
          <select
            aria-label="人物朝向"
            disabled={readOnly || isSubmitting}
            onChange={(event) => {
              setOrientation(event.target.value);
              invalidateConfirmation();
            }}
            value={orientation}
          >
            <option value="">请选择</option>
            <option value="FRONT">正面</option>
            <option value="LEFT_45">左 45°</option>
            <option value="RIGHT_45">右 45°</option>
            <option value="LEFT_SIDE">左侧面</option>
            <option value="RIGHT_SIDE">右侧面</option>
          </select>
        </label>
        <label>
          人物景别
          <select
            aria-label="人物景别"
            disabled={readOnly || isSubmitting}
            onChange={(event) => {
              setShotSize(event.target.value);
              invalidateConfirmation();
            }}
            value={shotSize}
          >
            <option value="">请选择</option>
            <option value="CLOSE_UP">近景</option>
            <option value="HALF_BODY">半身</option>
            <option value="FULL_BODY">全身</option>
          </select>
        </label>
        <label>
          面部可见性
          <select
            aria-label="面部可见性"
            disabled={readOnly || isSubmitting}
            onChange={(event) => {
              setFaceVisibility(event.target.value);
              invalidateConfirmation();
            }}
            value={faceVisibility}
          >
            <option value="">请选择</option>
            <option value="VISIBLE">清晰可见</option>
            <option value="HIDDEN">不可见或遮挡</option>
          </select>
        </label>
        <label>
          身体完整度
          <select
            aria-label="身体完整度"
            disabled={readOnly || isSubmitting}
            onChange={(event) => {
              setBodyCompleteness(event.target.value);
              invalidateConfirmation();
            }}
            value={bodyCompleteness}
          >
            <option value="">请选择</option>
            <option value="FACE_ONLY">仅面部</option>
            <option value="UPPER_BODY">上半身</option>
            <option value="FULL_BODY">全身</option>
            <option value="PARTIAL">局部可见</option>
          </select>
        </label>
      </fieldset>
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
      {readOnly && candidates.length > 0 ? (
        <p className="status-note">只读身份不加载素材预览。</p>
      ) : null}
      <fieldset className="source-frame-options">
        <legend>
          {readOnly
            ? "候选记录（素材预览需要下载权限）"
            : "选择一张用于后续人物置换和首帧生成"}
        </legend>
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
              disabled={
                readOnly || isSubmitting || !previewUrls[candidate.asset_id]
              }
              name="source-frame"
              onChange={() => {
                setSelectedAssetId(candidate.asset_id);
                invalidateConfirmation();
              }}
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
                  : readOnly
                    ? "预览不可用"
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
          disabled={
            isSubmitting ||
            !selectedAssetId ||
            !previewUrls[selectedAssetId] ||
            !currentCharacterFeatures(
              orientation,
              shotSize,
              faceVisibility,
              bodyCompleteness,
            )
          }
          onClick={handleConfirm}
          type="button"
        >
          确认源画面
        </button>
      )}
    </section>
  );
}

function readCharacterFeatures(
  version: AnalysisVersion,
): SourceFrameCharacterFeatures | null {
  const value = version.payload.character_features;
  if (!value || typeof value !== "object") {
    return null;
  }
  const features = value as Record<string, unknown>;
  return currentCharacterFeatures(
    features.orientation,
    features.shot_size,
    features.face_visible === true
      ? "VISIBLE"
      : features.face_visible === false
        ? "HIDDEN"
        : "",
    features.body_completeness,
  );
}

function currentCharacterFeatures(
  orientation: unknown,
  shotSize: unknown,
  faceVisibility: unknown,
  bodyCompleteness: unknown,
): SourceFrameCharacterFeatures | null {
  const orientations = [
    "FRONT",
    "LEFT_45",
    "RIGHT_45",
    "LEFT_SIDE",
    "RIGHT_SIDE",
  ] as const;
  const shotSizes = ["CLOSE_UP", "HALF_BODY", "FULL_BODY"] as const;
  const completeness = [
    "FACE_ONLY",
    "UPPER_BODY",
    "FULL_BODY",
    "PARTIAL",
  ] as const;
  if (
    !orientations.includes(orientation as (typeof orientations)[number]) ||
    !shotSizes.includes(shotSize as (typeof shotSizes)[number]) ||
    (faceVisibility !== "VISIBLE" && faceVisibility !== "HIDDEN") ||
    !completeness.includes(bodyCompleteness as (typeof completeness)[number])
  ) {
    return null;
  }
  return {
    orientation: orientation as SourceFrameCharacterFeatures["orientation"],
    shot_size: shotSize as SourceFrameCharacterFeatures["shot_size"],
    face_visible: faceVisibility === "VISIBLE",
    body_completeness:
      bodyCompleteness as SourceFrameCharacterFeatures["body_completeness"],
  };
}
