import { useCallback, useEffect, useRef, useState } from "react";

import {
  type AnalysisVersion,
  type CharacterReferenceSelection,
  confirmFirstFrame,
  type FirstFrameCandidate,
  type FirstFrameModel,
  type FirstFrameTask,
  generateFirstFrames,
  getAssetDownloadUrl,
  getLatestFirstFrameTask,
  getLatestProjectFirstFrameSelection,
  getLatestProjectFirstFrames,
  getProjectFirstFrameHistory,
  readFirstFrameCandidates,
  readFirstFrameSelectionPayload,
  resumeFirstFrameGeneration,
} from "./api";

const DEFAULT_PROMPT =
  "保留原图的镜头位置、人物姿态、动作、场景、构图、道具、光线与色调，只将原人物身份替换为角色库人物；保持自然皮肤、正确肢体和真实透视；不得增加或删除主体。";

type PendingFirstFrameGeneration = {
  promise: Promise<AnalysisVersion>;
  startedAt: number;
};

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
  const [generationStartedAt, setGenerationStartedAt] = useState<number | null>(
    null,
  );
  const [elapsedSeconds, setElapsedSeconds] = useState(0);
  const [generationTask, setGenerationTask] = useState<FirstFrameTask | null>(
    null,
  );
  // 质检未通过的候选需要两次点击：第一次是“知情”，第二次才真正确认。
  const [overrideArmed, setOverrideArmed] = useState(false);
  const loadRequestId = useRef(0);
  const previewRetryCounts = useRef(new Map<string, number>());
  const generationWatchId = useRef(0);
  const confirmationLifecycleId = useRef(0);
  const onBusyChangeRef = useRef(onBusyChange);
  onBusyChangeRef.current = onBusyChange;
  const referenceSelectionId = referenceSelection?.id ?? "";
  const confirmationBindingKey = [
    projectId,
    sourceFrameSelectionId ?? "",
    referenceSelectionId,
    legacyCharacterSelected ? "legacy" : "versioned",
    version?.id ?? "",
    selectedAssetId,
  ].join("\0");
  const confirmationBindingKeyRef = useRef(confirmationBindingKey);
  confirmationBindingKeyRef.current = confirmationBindingKey;
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
        setPreviewUrls({});
        previewRetryCounts.current.clear();
        // 换版本后，未完成的两段式覆盖确认必须重新开始。
        setOverrideArmed(false);
        if (!displayVersion) {
          setSelectedAssetId("");
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
          setSelectedAssetId("");
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
        const canPreserveSelection =
          !(latestState.stale || selection.stale) &&
          displayVersion.id === latest?.id;
        setSelectedAssetId((currentAssetId) => {
          if (canAutoSelect) {
            return payload.candidates[0]?.asset_id ?? "";
          }
          return canPreserveSelection &&
            payload.candidates.some(
              (candidate) => candidate.asset_id === currentAssetId,
            )
            ? currentAssetId
            : "";
        });
        if (latestState.stale || selection.stale) {
          setStatus("上游输入已更新，请重新生成人物置换首帧。");
        } else if (displayVersion.id !== latest?.id) {
          setStatus("正在查看历史版本；仅最新候选可确认用于视频生成。");
        } else if (currentSelection && typeof confirmedAssetId === "string") {
          setSelectedAssetId(confirmedAssetId);
          setStatus(
            "当前候选首帧已确认，将作为后续视频生成提示词的唯一首帧输入。",
          );
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
      simplified,
      sourceFrameSelectionId,
    ],
  );

  const followGeneration = useCallback(
    async (pending: PendingFirstFrameGeneration) => {
      const watchId = generationWatchId.current + 1;
      generationWatchId.current = watchId;
      onBusyChangeRef.current?.(true);
      setIsSubmitting(true);
      setGenerationStartedAt(pending.startedAt);
      setElapsedSeconds(
        Math.max(0, Math.floor((Date.now() - pending.startedAt) / 1000)),
      );
      setError("");
      setStatus("");
      try {
        const generated = await pending.promise;
        if (watchId !== generationWatchId.current) {
          return;
        }
        setStatus("候选首帧已更新，正在读取候选…");
        await load(generated, true);
      } catch (requestError) {
        if (watchId !== generationWatchId.current) {
          return;
        }
        setError(
          requestError instanceof Error
            ? requestError.message
            : "生成人物置换首帧失败。",
        );
      } finally {
        if (watchId === generationWatchId.current) {
          setIsSubmitting(false);
          setGenerationStartedAt(null);
          onBusyChangeRef.current?.(false);
        }
      }
    },
    [load],
  );

  useEffect(() => {
    onBusyChangeRef.current?.(false);
    void (async () => {
      await load();
      try {
        const task = await getLatestFirstFrameTask(projectId);
        if (
          task &&
          (task.status === "PENDING" ||
            task.status === "RUNNING" ||
            task.status === "SUCCEEDED")
        ) {
          setGenerationTask(task);
          void followGeneration({
            promise: resumeFirstFrameGeneration(
              projectId,
              task.id,
              setGenerationTask,
            ),
            startedAt: Date.parse(task.started_at ?? task.created_at),
          });
        } else if (
          task &&
          (task.status === "FAILED" || task.status === "SUBMISSION_UNCERTAIN")
        ) {
          setError(
            task.error_message ??
              (task.status === "SUBMISSION_UNCERTAIN"
                ? "云端任务状态需要确认，请重试。"
                : "云端首帧生成失败，请重试。"),
          );
        }
      } catch {
        // The normal page load already reports API availability. A separate
        // recovery probe must not replace valid candidate/history content.
      }
    })();
    return () => {
      loadRequestId.current += 1;
      generationWatchId.current += 1;
    };
  }, [followGeneration, load, projectId]);

  useEffect(
    () => () => {
      confirmationLifecycleId.current += 1;
      onBusyChangeRef.current?.(false);
    },
    [],
  );

  useEffect(() => {
    if (generationStartedAt === null) {
      return;
    }
    const updateElapsed = () => {
      setElapsedSeconds(
        Math.max(0, Math.floor((Date.now() - generationStartedAt) / 1000)),
      );
    };
    updateElapsed();
    const intervalId = window.setInterval(updateElapsed, 1000);
    return () => window.clearInterval(intervalId);
  }, [generationStartedAt]);

  const payload = version ? readFirstFrameCandidates(version) : null;
  const isHistoryVersion = Boolean(version && version.id !== latestVersionId);
  const selectedPreview = previewUrls[selectedAssetId];
  const selectedCandidate = payload?.candidates.find(
    (candidate) => candidate.asset_id === selectedAssetId,
  );

  async function handlePreviewError(assetId: string) {
    setPreviewUrls((current) =>
      Object.fromEntries(
        Object.entries(current).filter(([currentId]) => currentId !== assetId),
      ),
    );
    const retries = previewRetryCounts.current.get(assetId) ?? 0;
    if (retries >= 1) {
      return;
    }
    previewRetryCounts.current.set(assetId, retries + 1);
    try {
      const download = await getAssetDownloadUrl(assetId);
      setPreviewUrls((current) => ({ ...current, [assetId]: download.url }));
    } catch {
      // Leave the preview absent after the single bounded re-sign attempt.
    }
  }

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
    const binding = referenceSelection
      ? {
          character_version_id: referenceSelection.character_version_id,
          character_reference_selection_id: referenceSelection.id,
        }
      : {};
    const pending: PendingFirstFrameGeneration = {
      startedAt: Date.now(),
      promise: generateFirstFrames(
        projectId,
        {
          model: simplified ? "gpt-image-2" : model,
          // In simplified mode the server owns the stable business template.
          // Sending the UI placeholder here previously bypassed the stronger
          // contact-sheet/reference-role prompt assembly on the server.
          prompt: simplified ? undefined : prompt,
          quantity,
          ...binding,
        },
        setGenerationTask,
      ),
    };
    await followGeneration(pending);
  }

  async function handleConfirm() {
    if (readOnly) {
      return;
    }
    if (!selectedAssetId || !selectedPreview || isHistoryVersion) {
      setError("请先加载并查看最新候选首帧预览，再进行确认。");
      return;
    }
    const needsOverride = selectedCandidate?.quality?.passed !== true;
    if (needsOverride && !overrideArmed) {
      setOverrideArmed(true);
      setStatus(
        "该候选未通过自动质检。再次点击确认按钮，表示你已查看并接受此首帧。",
      );
      return;
    }
    const submittedBindingKey = confirmationBindingKey;
    const submittedLifecycleId = confirmationLifecycleId.current;
    const isCurrentConfirmation = () =>
      submittedLifecycleId === confirmationLifecycleId.current &&
      submittedBindingKey === confirmationBindingKeyRef.current;
    onBusyChangeRef.current?.(true);
    setIsSubmitting(true);
    setError("");
    try {
      const selection = needsOverride
        ? await confirmFirstFrame(projectId, selectedAssetId, {
            allowUnverified: true,
          })
        : await confirmFirstFrame(projectId, selectedAssetId);
      if (!isCurrentConfirmation()) {
        return;
      }
      const selectedIndex = payload?.candidates.findIndex(
        (candidate) => candidate.asset_id === selectedAssetId,
      );
      setStatus(
        `已确认首帧候选 ${(selectedIndex ?? 0) + 1}。保存镜头卡片并锁定视频生成提示词后，才能创建视频批次。`,
      );
      onSelectionChange?.(selection);
    } catch (requestError) {
      if (!isCurrentConfirmation()) {
        return;
      }
      setError(
        requestError instanceof Error ? requestError.message : "确认首帧失败。",
      );
    } finally {
      if (submittedLifecycleId === confirmationLifecycleId.current) {
        setIsSubmitting(false);
        onBusyChangeRef.current?.(false);
      }
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
            首帧生成模式
            <select
              aria-label="首帧生成模式"
              disabled={readOnly || isSubmitting || !canGenerate}
              onChange={(event) =>
                setModel(event.target.value as FirstFrameModel)
              }
              value={model}
            >
              <option value="gpt-image-2">标准图像（默认）</option>
              <option value="nano-banana-pro-2k">高清图像</option>
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
          {isSubmitting
            ? "正在生成"
            : payload
              ? "重新生成候选首帧"
              : "生成人物置换首帧"}
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
          {overrideArmed
            ? "质检未通过，仍要使用此首帧"
            : "确认用于视频生成的首帧"}
        </button>
      </div>
      {generationStartedAt !== null ? (
        <div className="first-frame-generation-progress" role="status">
          <div className="first-frame-generation-progress__heading">
            <strong>{firstFrameTaskStageLabel(generationTask)}</strong>
            <span>已等待 {elapsedSeconds} 秒</span>
          </div>
          <progress aria-label="人物置换首帧生成进度" />
          {generationTask ? (
            <p>
              任务 {generationTask.id} · 第{" "}
              {Math.max(1, generationTask.attempt)} 次执行
            </p>
          ) : (
            <p>正在创建可恢复的云端任务…</p>
          )}
          <p>
            任务在云端继续执行，可以关闭或离开当前页面；返回后会自动恢复进度并显示结果。请勿重复提交。
          </p>
        </div>
      ) : null}
      {isLoading ? <p className="status-note">正在读取首帧候选</p> : null}
      {error ? <p className="settings-error">{error}</p> : null}
      {status ? <p className="setup-success">{status}</p> : null}
      {payload ? (
        <>
          {payload.project_appearance ? (
            <aside
              className="project-appearance-summary"
              aria-label="本项目人物造型"
            >
              <div>
                <strong>本项目人物造型</strong>
                <span>后台自动匹配</span>
              </div>
              <p>{payload.project_appearance.outfit_description}</p>
              <small>{payload.project_appearance.selection_reason}</small>
            </aside>
          ) : null}
          {!simplified ? (
            <p className="file-note">
              当前模式：{modelLabel(payload.model)} ·{" "}
              {payload.provider === "fake" ? "测试模式" : "正式服务"}
            </p>
          ) : null}
          {payload.provider === "fake" ? (
            <p className="settings-error">
              模拟输出：尚未调用正式图像生成服务。
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
                  isSubmitting ||
                  !previewUrls[candidate.asset_id] ||
                  isHistoryVersion
                }
                index={index}
                key={candidate.asset_id}
                onSelect={() => {
                  setSelectedAssetId(candidate.asset_id);
                  // 换候选后，未完成的两段式覆盖确认必须重新开始。
                  setOverrideArmed(false);
                }}
                onPreviewError={() =>
                  void handlePreviewError(candidate.asset_id)
                }
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
              disabled={isSubmitting}
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

function firstFrameTaskStageLabel(task: FirstFrameTask | null): string {
  switch (task?.stage) {
    case "QUEUED":
      return "任务已提交，正在等待云端工作节点";
    case "PREPARING":
      return "正在读取源画面与人物五视图";
    case "GENERATING":
      return "正在调用图片模型生成首帧";
    case "VERIFYING":
      return "图片已生成，正在进行 AI 质量检测";
    case "SUCCEEDED":
      return "首帧已生成，正在加载结果";
    case "FAILED":
      return "首帧生成失败";
    case "NEEDS_REVIEW":
      return "生成结果需要人工核对";
    default:
      return "正在提交人物置换首帧任务";
  }
}

function FirstFrameOption({
  candidate,
  checked,
  disabled,
  index,
  onSelect,
  onPreviewError,
  previewUrl,
  readOnly,
}: {
  candidate: FirstFrameCandidate;
  checked: boolean;
  disabled: boolean;
  index: number;
  onSelect: () => void;
  onPreviewError: () => void;
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
        <img
          alt={`首帧候选 ${index + 1}`}
          onError={onPreviewError}
          src={previewUrl}
        />
      ) : (
        <span className="source-frame-placeholder">
          {readOnly ? "预览不可用" : "预览加载失败，请重新生成"}
        </span>
      )}
      <span>
        <strong>首帧候选 {index + 1}</strong>
        <small>{candidate.content_type}</small>
        {candidate.quality?.passed ? (
          <small className="first-frame-quality-pass">
            整身人物质检通过
            {candidate.quality.attempt > 1
              ? ` · 自动修正 ${candidate.quality.attempt - 1} 次`
              : ""}
          </small>
        ) : null}
        {candidate.quality && !candidate.quality.passed ? (
          <small className="first-frame-quality-fail">
            质检未通过：
            {candidate.quality.issue_codes.join("、") || "详见任务记录"}
          </small>
        ) : null}
      </span>
    </label>
  );
}

function modelLabel(model: FirstFrameModel) {
  return model === "gpt-image-2" ? "标准图像" : "高清图像";
}
