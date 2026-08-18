import { useCallback, useEffect, useRef, useState } from "react";

import {
  type AnalysisVersion,
  type CharacterReferenceSelection,
  confirmFirstFrame,
  type FirstFrameCandidate,
  type FirstFrameModel,
  generateFirstFrames,
  getAssetDownloadUrl,
  getLatestProjectFirstFrameSelection,
  getLatestProjectFirstFrames,
  getProjectFirstFrameHistory,
  readFirstFrameCandidates,
  readFirstFrameSelectionPayload,
} from "./api";

const DEFAULT_PROMPT =
  "保留原图的镜头位置、人物姿态、动作、场景、构图、道具、光线与色调，只将原人物身份替换为角色库人物；保持自然皮肤、正确肢体和真实透视；不得增加或删除主体。";

export function FirstFrameSelection({
  legacyCharacterSelected = false,
  onBusyChange,
  onSelectionChange,
  projectId,
  readOnly = false,
  referenceSelection,
  simplified = false,
  sourceFrameSelectionId,
}: {
  legacyCharacterSelected?: boolean;
  onBusyChange?: (isBusy: boolean) => void;
  onSelectionChange?: (selection: AnalysisVersion | null) => void;
  projectId: string;
  readOnly?: boolean;
  referenceSelection: CharacterReferenceSelection | null;
  // 详情页简化模式：模型固定 gpt-image-2（Nano 仅保留为后端备选）、
  // 隐藏编辑提示词，生成参数全部走内置默认值。
  simplified?: boolean;
  sourceFrameSelectionId: string | null;
}) {
  const [version, setVersion] = useState<AnalysisVersion | null>(null);
  const [latestVersionId, setLatestVersionId] = useState("");
  const [history, setHistory] = useState<AnalysisVersion[]>([]);
  const [previewUrls, setPreviewUrls] = useState<Record<string, string>>({});
  const [selectedAssetId, setSelectedAssetId] = useState("");
  const [model, setModel] = useState<FirstFrameModel>("gpt-image-2");
  const [prompt, setPrompt] = useState(DEFAULT_PROMPT);
  const [quantity, setQuantity] = useState(1);
  const [status, setStatus] = useState("");
  const [error, setError] = useState("");
  const [isLoading, setIsLoading] = useState(true);
  const [isSubmitting, setIsSubmitting] = useState(false);
  const loadRequestId = useRef(0);
  const referenceSelectionId = referenceSelection?.id ?? "";
  const canGenerate =
    Boolean(sourceFrameSelectionId) &&
    Boolean(referenceSelection || legacyCharacterSelected);

  const load = useCallback(
    async (
      preferredVersion?: AnalysisVersion,
      // P0-03-04：仅生成完成后的重载自动预选第一张候选（确认压缩为一次
      // 点击）；进入页面/切历史版本仍保持人工选择，stale 语义不变。
      autoSelectFirstCandidate = false,
    ) => {
      const requestId = loadRequestId.current + 1;
      loadRequestId.current = requestId;
      const isCurrentRequest = () => requestId === loadRequestId.current;
      setIsLoading(true);
      setIsSubmitting(false);
      setError("");
      try {
        const [latestState, selection, versions] = await Promise.all([
          getLatestProjectFirstFrames(projectId),
          getLatestProjectFirstFrameSelection(projectId),
          getProjectFirstFrameHistory(projectId),
        ]);
        if (!isCurrentRequest()) {
          return;
        }
        const latest = latestState.version;
        const displayVersion = preferredVersion ?? latest;
        const latestPayload = latest ? readFirstFrameCandidates(latest) : null;
        const confirmedSelection = selection.version
          ? readFirstFrameSelectionPayload(selection.version)
          : null;
        const confirmedAssetId = confirmedSelection?.first_frame_asset_id;
        const currentSelection =
          !latestState.stale &&
          !selection.stale &&
          latest &&
          latestPayload &&
          confirmedSelection?.first_frame_candidates_version_id === latest.id &&
          typeof confirmedAssetId === "string" &&
          latestPayload.candidates.some(
            (candidate) => candidate.asset_id === confirmedAssetId,
          )
            ? selection.version
            : null;
        onSelectionChange?.(currentSelection);
        setLatestVersionId(latest?.id ?? "");
        setHistory(versions);
        setVersion(displayVersion);
        setSelectedAssetId("");
        setPreviewUrls({});
        if (!displayVersion) {
          setStatus(
            latestState.stale || selection.stale
              ? "上游输入已更新，请重新生成人物置换首帧。"
              : !sourceFrameSelectionId
                ? "请先确认当前源画面；已有首帧历史仍可查看。"
                : referenceSelectionId
                  ? "人物参考图已确认，可以生成人物置换首帧。"
                  : legacyCharacterSelected
                    ? "历史兼容人物已恢复，可以继续生成首帧。"
                    : "请先确认人物参考图；已有首帧历史仍可查看。",
          );
          return;
        }
        const payload = readFirstFrameCandidates(displayVersion);
        if (!payload) {
          setError("首帧候选数据格式无效，请重新生成。");
          return;
        }
        setModel(payload.model);
        if (!simplified) {
          setPrompt(payload.prompt);
        }
        // P0-03-04：预选仅是建议，确认仍为人工动作；候选生成的付费语义
        // 不变（仍由用户显式点击触发）。
        const canAutoSelect =
          autoSelectFirstCandidate &&
          !(latestState.stale || selection.stale) &&
          displayVersion.id === latest?.id &&
          !currentSelection;
        if (canAutoSelect) {
          setSelectedAssetId(payload.candidates[0]?.asset_id ?? "");
        }
        if (latestState.stale || selection.stale) {
          setStatus("上游输入已更新，请重新生成人物置换首帧。");
        } else if (displayVersion.id !== latest?.id) {
          setStatus("正在查看历史版本；仅最新候选可确认用于 H3。");
        } else if (currentSelection && typeof confirmedAssetId === "string") {
          setSelectedAssetId(confirmedAssetId);
          setStatus("当前候选首帧已确认，将作为后续 H3 提示词的唯一首帧输入。");
        } else if (selection.version) {
          setStatus("已确认首帧与当前候选不一致，请重新确认最新候选。");
        } else if (canAutoSelect) {
          setStatus("已自动预选第一张候选，请查看后单击确认。");
        } else {
          setStatus("");
        }
        if (readOnly) {
          return;
        }
        const previews = await Promise.allSettled(
          payload.candidates.map(async (candidate) => {
            const download = await getAssetDownloadUrl(candidate.asset_id);
            return [candidate.asset_id, download.url] as const;
          }),
        );
        if (!isCurrentRequest()) {
          return;
        }
        setPreviewUrls(
          Object.fromEntries(
            previews.flatMap((result) =>
              result.status === "fulfilled" ? [result.value] : [],
            ),
          ),
        );
      } catch (requestError) {
        if (isCurrentRequest()) {
          onSelectionChange?.(null);
          setError(
            requestError instanceof Error
              ? requestError.message
              : "读取人物置换首帧失败。",
          );
        }
      } finally {
        if (isCurrentRequest()) {
          setIsLoading(false);
        }
      }
    },
    [
      legacyCharacterSelected,
      onSelectionChange,
      projectId,
      readOnly,
      referenceSelectionId,
      sourceFrameSelectionId,
    ],
  );

  useEffect(() => {
    void load();
    return () => {
      loadRequestId.current += 1;
    };
  }, [load]);

  const payload = version ? readFirstFrameCandidates(version) : null;
  const isHistoryVersion = Boolean(version && version.id !== latestVersionId);
  const selectedPreview = previewUrls[selectedAssetId];

  async function handleGenerate() {
    if (readOnly) {
      return;
    }
    if (!canGenerate) {
      setError("请先确认当前源画面和人物参考图，再生成新的置换首帧。");
      return;
    }
    if (!Number.isInteger(quantity) || quantity < 1 || quantity > 3) {
      setError("候选数量必须是 1–3 的整数。");
      return;
    }
    const requestId = loadRequestId.current;
    onBusyChange?.(true);
    setIsSubmitting(true);
    setError("");
    setStatus("");
    try {
      const binding = referenceSelection
        ? {
            character_version_id: referenceSelection.character_version_id,
            character_reference_selection_id: referenceSelection.id,
          }
        : {};
      const generated = await generateFirstFrames(projectId, {
        model: simplified ? "gpt-image-2" : model,
        prompt: simplified ? DEFAULT_PROMPT : prompt,
        quantity,
        ...binding,
      });
      if (requestId !== loadRequestId.current) {
        return;
      }
      // 过程性中性文案：成功路径由 load 内 canAutoSelect 分支覆盖为预选
      // 提示；若候选数据无效早退，不会残留与事实矛盾的预选文案（评审 Minor）。
      setStatus("候选首帧已更新，正在读取候选…");
      await load(generated, true);
    } catch (requestError) {
      if (requestId !== loadRequestId.current) {
        return;
      }
      setError(
        requestError instanceof Error
          ? requestError.message
          : "生成人物置换首帧失败。",
      );
    } finally {
      if (requestId === loadRequestId.current) {
        setIsSubmitting(false);
      }
      onBusyChange?.(false);
    }
  }

  async function handleConfirm() {
    if (readOnly) {
      return;
    }
    if (!selectedAssetId || !selectedPreview || isHistoryVersion) {
      setError("请先加载并查看最新候选首帧预览，再进行确认。");
      return;
    }
    const requestId = loadRequestId.current;
    onBusyChange?.(true);
    setIsSubmitting(true);
    setError("");
    try {
      const selection = await confirmFirstFrame(projectId, selectedAssetId);
      if (requestId !== loadRequestId.current) {
        return;
      }
      const selectedIndex = payload?.candidates.findIndex(
        (candidate) => candidate.asset_id === selectedAssetId,
      );
      setStatus(
        `已确认首帧候选 ${(selectedIndex ?? 0) + 1}。保存镜头卡片并锁定 H3 提示词后，才能创建视频批次。`,
      );
      onSelectionChange?.(selection);
    } catch (requestError) {
      if (requestId !== loadRequestId.current) {
        return;
      }
      setError(
        requestError instanceof Error ? requestError.message : "确认首帧失败。",
      );
    } finally {
      if (requestId === loadRequestId.current) {
        setIsSubmitting(false);
      }
      onBusyChange?.(false);
    }
  }

  return (
    <section
      className="first-frame-selection"
      aria-labelledby="first-frame-title"
    >
      <div>
        <h3 id="first-frame-title">人物置换首帧</h3>
        {!simplified ? (
          <p>
            {legacyCharacterSelected
              ? "历史兼容人物 · 沿用冻结的人物快照。"
              : "由已确认的源画面与角色参考生成。"}
          </p>
        ) : null}
      </div>
      {!simplified ? (
        <div className="first-frame-controls">
          <label>
            首帧模型
            <select
              aria-label="首帧模型"
              disabled={readOnly || isSubmitting || !canGenerate}
              onChange={(event) =>
                setModel(event.target.value as FirstFrameModel)
              }
              value={model}
            >
              <option value="gpt-image-2">GPT Image 2（默认）</option>
              <option value="nano-banana-pro-2k">Nano Banana Pro 2K</option>
            </select>
          </label>
          <label>
            候选数量
            <input
              aria-label="候选数量"
              disabled={readOnly || isSubmitting || !canGenerate}
              max="3"
              min="1"
              onChange={(event) => setQuantity(Number(event.target.value))}
              type="number"
              value={quantity}
            />
          </label>
        </div>
      ) : null}
      {!simplified ? (
        <label className="first-frame-prompt">
          首帧编辑提示词
          <textarea
            aria-label="首帧编辑提示词"
            disabled={readOnly || isSubmitting || !canGenerate}
            onChange={(event) => setPrompt(event.target.value)}
            rows={5}
            value={prompt}
          />
        </label>
      ) : null}
      <div className="source-frame-actions">
        <button
          disabled={readOnly || isSubmitting || !canGenerate}
          onClick={handleGenerate}
          type="button"
        >
          {isSubmitting ? "正在生成" : "重新生成候选首帧"}
        </button>
        <button
          className="secondary-button"
          disabled={
            readOnly ||
            isSubmitting ||
            !selectedAssetId ||
            !selectedPreview ||
            isHistoryVersion
          }
          onClick={handleConfirm}
          type="button"
        >
          确认用于 H3 的首帧
        </button>
      </div>
      {isLoading ? <p className="status-note">正在读取首帧候选</p> : null}
      {error ? <p className="settings-error">{error}</p> : null}
      {status ? <p className="setup-success">{status}</p> : null}
      {payload ? (
        <>
          {!simplified ? (
            <p className="file-note">
              当前模型：{modelLabel(payload.model)} ·{" "}
              {payload.provider === "apilio" ? "Apilio" : payload.provider}
            </p>
          ) : null}
          {payload.provider === "fake" ? (
            <p className="settings-error">
              模拟输出：尚未调用 Apilio 真实模型。
            </p>
          ) : null}
          {readOnly ? (
            <p className="status-note">只读身份不加载素材预览。</p>
          ) : null}
          <fieldset className="first-frame-options">
            <legend>
              {readOnly
                ? "候选记录（素材预览需要下载权限）"
                : simplified
                  ? "选择一张"
                  : "查看候选效果，选择一张作为已确认首帧"}
            </legend>
            {payload.candidates.map((candidate, index) => (
              <FirstFrameOption
                candidate={candidate}
                checked={selectedAssetId === candidate.asset_id}
                disabled={
                  readOnly ||
                  !previewUrls[candidate.asset_id] ||
                  isHistoryVersion
                }
                index={index}
                key={candidate.asset_id}
                onSelect={() => setSelectedAssetId(candidate.asset_id)}
                previewUrl={previewUrls[candidate.asset_id]}
                readOnly={readOnly}
              />
            ))}
          </fieldset>
        </>
      ) : null}
      <section
        className="first-frame-history"
        aria-labelledby="first-frame-history-title"
      >
        {readOnly ? (
          <p className="status-note">只读身份不能生成或确认首帧。</p>
        ) : null}
        <h4 id="first-frame-history-title">历史生成版本</h4>
        {history.length === 0 ? (
          <p className="file-note">暂无历史版本。</p>
        ) : null}
        <div className="first-frame-history-list">
          {history.map((historyVersion) => (
            <button
              className={
                historyVersion.id === version?.id
                  ? "history-version history-version--active"
                  : "history-version"
              }
              key={historyVersion.id}
              onClick={() => void load(historyVersion)}
              type="button"
            >
              版本 #{historyVersion.version_number}
            </button>
          ))}
        </div>
      </section>
    </section>
  );
}

function FirstFrameOption({
  candidate,
  checked,
  disabled,
  index,
  onSelect,
  previewUrl,
  readOnly,
}: {
  candidate: FirstFrameCandidate;
  checked: boolean;
  disabled: boolean;
  index: number;
  onSelect: () => void;
  previewUrl: string | undefined;
  readOnly: boolean;
}) {
  return (
    <label
      className={
        checked
          ? "source-frame-option source-frame-option--selected"
          : "source-frame-option"
      }
    >
      <input
        checked={checked}
        disabled={disabled}
        name="first-frame"
        onChange={onSelect}
        type="radio"
        value={candidate.asset_id}
      />
      {previewUrl ? (
        <img alt={`首帧候选 ${index + 1}`} src={previewUrl} />
      ) : (
        <span className="source-frame-placeholder">
          {readOnly ? "预览不可用" : "预览加载失败，请重新生成"}
        </span>
      )}
      <span>
        <strong>首帧候选 {index + 1}</strong>
        <small>{candidate.content_type}</small>
      </span>
    </label>
  );
}

function modelLabel(model: FirstFrameModel) {
  return model === "gpt-image-2" ? "GPT Image 2" : "Nano Banana Pro 2K";
}
