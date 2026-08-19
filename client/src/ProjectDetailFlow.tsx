import { useCallback, useEffect, useRef, useState } from "react";

import {
  type AnalysisVersion,
  type CharacterReferenceSelection,
  compileGenerationPrompt,
  createGenerationBatch,
  createScriptVersion,
  defaultBatchProvider,
  type GenerationBatch,
  type GenerationRuntimeLimits,
  getGenerationRuntimeLimits,
  getLatestGenerationPrompt,
  getLatestProjectAnalysis,
  getLatestProjectShotCards,
  getLatestScriptVersion,
  lockGenerationPrompt,
  type Project,
  type ProjectMainCharacter,
  type PromptPreviewResult,
  previewGenerationPrompt,
  readAnalysisPayload,
  readFirstFrameSelectionPayload,
  reviseGenerationPrompt,
  type SourceFrameCharacterFeatures,
  saveShotCards,
  selectCharacterReferences,
} from "./api";
import { CharacterSelection } from "./CharacterSelection";
import { FirstFrameSelection } from "./FirstFrameSelection";
import { PromptMarkdown } from "./PromptMarkdown";
import { SourceFrameSelection } from "./SourceFrameSelection";

type ProjectDetailFlowProps = {
  onBack: () => void;
  onBatchCreated: (batch: GenerationBatch) => void;
  onBusyChange?: (isBusy: boolean) => void;
  project: Project;
  readOnly: boolean;
};

type GenerationPhase = "idle" | "running" | "done";

// 源帧特征缺省建议值与服务端 DEFAULT_SOURCE_FRAME_FEATURES 对齐：
// 自动匹配推荐集依赖这组特征（正面半身 → FRONT_HALF + 正脸）。
const DEFAULT_FEATURE_SUGGESTION: SourceFrameCharacterFeatures = {
  orientation: "FRONT",
  shot_size: "HALF_BODY",
  face_visible: true,
  body_completeness: "UPPER_BODY",
};

// 项目详情流程页 = 「解析提示词 → 源画面与人物 → 人物置换首帧 → 自定义文案 → 提交生成」
// 五段自上而下滚动。人物参考在角色与源画面就绪后全自动匹配（无人工确认）；
// stale 级联（角色/源画面变 → 清参考与首帧）、幂等建批、付费红线全部沿用
// 快速生成动线的服务端语义。
export function ProjectDetailFlow({
  onBack,
  onBatchCreated,
  onBusyChange,
  project,
  readOnly,
}: ProjectDetailFlowProps) {
  const [analysisVersion, setAnalysisVersion] =
    useState<AnalysisVersion | null>(null);
  const [analysisError, setAnalysisError] = useState("");
  const [limits, setLimits] = useState<GenerationRuntimeLimits | null>(null);
  const [preview, setPreview] = useState<PromptPreviewResult | null>(null);
  const [isPreviewLoading, setIsPreviewLoading] = useState(false);
  const [previewError, setPreviewError] = useState("");
  const [characterSelection, setCharacterSelection] =
    useState<ProjectMainCharacter | null>(null);
  const [sourceFrameSelection, setSourceFrameSelection] =
    useState<AnalysisVersion | null>(null);
  const [referenceSelection, setReferenceSelection] =
    useState<CharacterReferenceSelection | null>(null);
  const [referenceError, setReferenceError] = useState("");
  const [firstFrameSelection, setFirstFrameSelection] =
    useState<AnalysisVersion | null>(null);
  const [originalScript, setOriginalScript] = useState("");
  const [scriptText, setScriptText] = useState("");
  const [firstFrameGenerationBusy, setFirstFrameGenerationBusy] =
    useState(false);
  const [generationPhase, setGenerationPhase] =
    useState<GenerationPhase>("idle");
  const [generationError, setGenerationError] = useState("");
  const [generationMessage, setGenerationMessage] = useState("");
  // 用户在第一段编辑并另存过的提示词文本。自定义文案未变时提交会复用；
  // 文案变化时必须让服务端重新编译，避免旧的完整 Prompt 覆盖新文案。
  const [revisedPromptText, setRevisedPromptText] = useState<string | null>(
    null,
  );
  const [referenceRetryCount, setReferenceRetryCount] = useState(0);
  const upstreamBusyRef = useRef<Set<string>>(new Set());
  const [, forceRender] = useState(0);
  const idempotencyKeyRef = useRef("");
  const autoMatchAttemptedRef = useRef<Set<string>>(new Set());
  const onBusyChangeRef = useRef(onBusyChange);
  onBusyChangeRef.current = onBusyChange;

  const firstFramePayload = firstFrameSelection
    ? readFirstFrameSelectionPayload(firstFrameSelection)
    : null;
  const firstFrameAssetId = firstFramePayload?.first_frame_asset_id ?? null;
  const isUpstreamBusy = upstreamBusyRef.current.size > 0;
  const isBusy = generationPhase === "running" || isUpstreamBusy;
  const scriptWasEdited = scriptText.trim() !== originalScript.trim();
  const canStart =
    Boolean(firstFrameAssetId) &&
    !isBusy &&
    !firstFrameGenerationBusy &&
    !readOnly &&
    generationPhase !== "done";

  useEffect(() => {
    onBusyChangeRef.current?.(isBusy);
  }, [isBusy]);

  const markUpstreamBusy = useCallback((key: string, busy: boolean) => {
    if (busy) {
      upstreamBusyRef.current.add(key);
    } else {
      upstreamBusyRef.current.delete(key);
    }
    forceRender((value) => value + 1);
  }, []);

  useEffect(() => {
    let active = true;
    getLatestProjectAnalysis(project.id)
      .then((version) => {
        if (active) {
          setAnalysisVersion(version);
          setAnalysisError("");
        }
      })
      .catch(() => {
        if (active) {
          setAnalysisError("该项目还没有可用的拆解结果，请先等待拆解完成。");
        }
      });
    getGenerationRuntimeLimits()
      .then((nextLimits) => {
        if (active) {
          setLimits(nextLimits);
        }
      })
      .catch(() => {
        // 费用上限读取失败不阻断生成，仅缺少预计费用展示。
      });
    return () => {
      active = false;
    };
  }, [project.id]);

  // 第一段提示词预览：进入页面即由拆解结果自动编译，不依赖首帧。
  useEffect(() => {
    let active = true;
    setIsPreviewLoading(true);
    setPreviewError("");
    previewGenerationPrompt(project.id)
      .then((result) => {
        if (active) {
          setPreview(result);
        }
      })
      .catch((error: unknown) => {
        if (active) {
          const reason = error instanceof Error ? error.message : "请稍后重试";
          setPreviewError(`提示词预览暂不可用（${reason}）。`);
        }
      })
      .finally(() => {
        if (active) {
          setIsPreviewLoading(false);
        }
      });
    return () => {
      active = false;
    };
  }, [project.id]);

  // 拆解就绪后把原文案直接带入自定义文案框，用户可在原内容上修改。
  // readAnalysisPayload 解包服务端落库的 payload.analysis 包装结构。
  useEffect(() => {
    if (!analysisVersion) {
      return;
    }
    const payload = readAnalysisPayload(analysisVersion);
    setOriginalScript(payload ? payload.original_script : "");
    setScriptText(payload ? payload.original_script : "");
  }, [analysisVersion]);

  // 人物参考全自动匹配：角色与源画面确认后服务端自动创建推荐集
  // （selected 省略 → 推荐集），无人工确认。每个「角色版本 × 源画面版本」
  // 组合只自动尝试一次，失败可手动重试。
  // biome-ignore lint/correctness/useExhaustiveDependencies(referenceRetryCount): 重试按钮递增该计数器以触发本 effect 重新匹配。
  useEffect(() => {
    if (
      readOnly ||
      !characterSelection ||
      !sourceFrameSelection ||
      referenceSelection
    ) {
      return;
    }
    const characterVersionId = characterSelection.character_version_id ?? "";
    const matchKey = `${project.id}:${characterVersionId}:${sourceFrameSelection.id}`;
    if (autoMatchAttemptedRef.current.has(matchKey)) {
      return;
    }
    autoMatchAttemptedRef.current.add(matchKey);
    let active = true;
    markUpstreamBusy("reference", true);
    selectCharacterReferences(project.id, {
      character_version_id: characterVersionId,
      source_frame_selection_version_id: sourceFrameSelection.id,
    })
      .then((selection) => {
        if (active) {
          setReferenceSelection(selection);
          setReferenceError("");
        }
      })
      .catch((matchError: unknown) => {
        if (active) {
          setReferenceError(
            matchError instanceof Error
              ? matchError.message
              : "自动匹配人物参考失败。",
          );
        }
      })
      .finally(() => {
        markUpstreamBusy("reference", false);
      });
    return () => {
      active = false;
    };
  }, [
    characterSelection,
    markUpstreamBusy,
    project.id,
    readOnly,
    referenceRetryCount,
    referenceSelection,
    sourceFrameSelection,
  ]);

  function retryReferenceMatch() {
    if (!characterSelection || !sourceFrameSelection) {
      return;
    }
    const characterVersionId = characterSelection.character_version_id ?? "";
    autoMatchAttemptedRef.current.delete(
      `${project.id}:${characterVersionId}:${sourceFrameSelection.id}`,
    );
    setReferenceError("");
    setReferenceRetryCount((count) => count + 1);
  }

  // stale 级联（本页顺序为源画面在前）：任一变化 → 清参考匹配与首帧；
  // 源画面确认本身不依赖角色，角色变化不清源画面。
  const handleCharacterChange = useCallback(
    (selection: ProjectMainCharacter | null) => {
      setCharacterSelection(selection);
      setReferenceSelection(null);
      setFirstFrameSelection(null);
    },
    [],
  );

  const handleSourceFrameChange = useCallback(
    (selection: AnalysisVersion | null) => {
      setSourceFrameSelection(selection);
      setReferenceSelection(null);
      setFirstFrameSelection(null);
    },
    [],
  );

  const handleFirstFrameChange = useCallback(
    (selection: AnalysisVersion | null) => {
      setFirstFrameSelection(selection);
    },
    [],
  );

  // 第一段「另存 Prompt 新版本」：基于服务端最新已编译版本 revise；
  // 尚无编译版本（首次流程未提交过）时提示先完成提交或直接使用预览。
  async function handleSavePrompt(text: string) {
    const latest = await getLatestGenerationPrompt(project.id);
    if (!latest.version) {
      throw new Error(
        "还没有已编译的 Prompt 版本。可先在第五段提交一次生成，之后再编辑另存。",
      );
    }
    await reviseGenerationPrompt(project.id, {
      base_prompt_version_id: latest.version.id,
      prompt_text: text,
    });
    setRevisedPromptText(text);
    setPreview((current) =>
      current ? { ...current, prompt_text: text } : current,
    );
  }

  async function ensureShotCardVersion(): Promise<string> {
    const latest = await getLatestProjectShotCards(project.id);
    if (
      latest &&
      analysisVersion &&
      String(latest.payload?.source_analysis_version_id ?? "") ===
        String(analysisVersion.id)
    ) {
      return latest.id;
    }
    const shots = analysisVersion
      ? (readAnalysisPayload(analysisVersion)?.shots ?? null)
      : null;
    if (!shots || !analysisVersion) {
      throw new Error("拆解结果缺少镜头数据，请先检查拆解结果。");
    }
    const saved = await saveShotCards(analysisVersion.id, shots);
    return saved.id;
  }

  // 幂等复用条件在快速生成基础上加文本比较：同镜头卡版本且文案未变时
  // 不重复建版本；文案与原文一致按 original 落库，任何修改按 custom 另存。
  async function ensureScriptVersion(
    shotCardVersionId: string,
  ): Promise<string> {
    const text = scriptText.trim();
    if (!text) {
      throw new Error("自定义文案为空，请填写文案后再提交生成。");
    }
    const source = text === originalScript.trim() ? "original" : "custom";
    const latest = await getLatestScriptVersion(project.id);
    const payload = latest.version?.payload as
      | Record<string, unknown>
      | undefined;
    if (
      latest.version &&
      !latest.stale &&
      payload?.shot_card_version_id === shotCardVersionId &&
      payload?.full_text === text
    ) {
      return latest.version.id;
    }
    const saved = await createScriptVersion(project.id, {
      source,
      text,
      shot_card_version_id: shotCardVersionId,
    });
    return saved.id;
  }

  function defaultDurationSeconds(): number {
    const raw = analysisVersion
      ? readAnalysisPayload(analysisVersion)?.duration_seconds
      : null;
    const duration = typeof raw === "number" && raw > 0 ? Math.round(raw) : 10;
    return Math.max(4, Math.min(15, duration));
  }

  async function handleStartGeneration() {
    if (!firstFrameAssetId || isBusy || firstFrameGenerationBusy || readOnly) {
      return;
    }
    setGenerationPhase("running");
    setGenerationError("");
    setGenerationMessage("");
    try {
      const shotCardVersionId = await ensureShotCardVersion();
      const scriptVersionId = await ensureScriptVersion(shotCardVersionId);
      const duration = defaultDurationSeconds();
      const compiled = await compileGenerationPrompt(project.id, {
        script_version_id: scriptVersionId,
        shot_card_version_id: shotCardVersionId,
        first_frame_asset_id: firstFrameAssetId,
        output_duration_seconds: duration,
        resolution: "768P",
      });
      // 只有文案未改时才复用第一段保存的完整 Prompt。自定义文案变化后，
      // compiled 已包含新文本，不能再被此前保存的旧 Prompt 覆盖。
      let promptVersionId = compiled.id;
      if (revisedPromptText?.trim() && !scriptWasEdited) {
        const revised = await reviseGenerationPrompt(project.id, {
          base_prompt_version_id: compiled.id,
          prompt_text: revisedPromptText,
        });
        promptVersionId = revised.id;
      }
      const locked = await lockGenerationPrompt(project.id, promptVersionId);
      if (!idempotencyKeyRef.current) {
        idempotencyKeyRef.current =
          typeof globalThis.crypto?.randomUUID === "function"
            ? globalThis.crypto.randomUUID()
            : `detail-flow-${Date.now()}-${Math.random().toString(16).slice(2)}`;
      }
      const batch = await createGenerationBatch(project.id, {
        quantity: 1,
        prompt_version_id: locked.id,
        first_frame_asset_id: firstFrameAssetId,
        output_duration_seconds: duration,
        resolution: "768P",
        idempotency_key: idempotencyKeyRef.current,
        provider: defaultBatchProvider(),
        fake_audio_quality: "ok",
      });
      setGenerationPhase("done");
      setGenerationMessage("生成任务已创建，正在前往任务记录…");
      onBatchCreated(batch);
    } catch (error) {
      setGenerationPhase("idle");
      setGenerationError(
        error instanceof Error ? error.message : "创建生成任务失败，请重试。",
      );
    }
  }

  function renderPaidWarning() {
    if (!limits) {
      return (
        <p className="flow-cost">
          本次将创建 1 个付费视频生成任务（MiniMax H3）。
        </p>
      );
    }
    const cost = limits.estimated_cost_per_task;
    return (
      <p className="flow-cost">
        本次将创建 1 个付费视频生成任务（MiniMax H3）
        {cost == null ? "" : `，预计费用 ¥${cost.toFixed(2)}`}。
      </p>
    );
  }

  const referenceStatus = (() => {
    if (!characterSelection || !sourceFrameSelection) {
      return null;
    }
    if (referenceSelection) {
      return (
        <p className="setup-success" role="status">
          已自动匹配人物参考。
        </p>
      );
    }
    if (referenceError) {
      return (
        <p className="settings-error" role="alert">
          {referenceError}{" "}
          <button
            className="secondary-button"
            onClick={retryReferenceMatch}
            type="button"
          >
            重试匹配
          </button>
        </p>
      );
    }
    return <p className="status-note">正在自动匹配人物参考…</p>;
  })();

  return (
    <section aria-label={`生成流程 ${project.name}`} className="flow-page">
      <header className="flow-header">
        <div>
          <h2>生成流程</h2>
          <p className="flow-header__note">
            解析 → 源画面人物 → 人物置换首帧 → 自定义文案 → 提交生成
          </p>
        </div>
        <button
          className="secondary-button"
          disabled={isBusy}
          onClick={onBack}
          type="button"
        >
          返回项目列表
        </button>
      </header>

      {analysisError ? (
        <p className="settings-error" role="alert">
          {analysisError}
        </p>
      ) : null}

      <fieldset className="flow-step">
        <legend>① 解析提示词</legend>
        {isPreviewLoading ? (
          <p className="status-note">正在编译提示词预览…</p>
        ) : null}
        {previewError ? <p className="status-note">{previewError}</p> : null}
        {preview ? (
          <PromptMarkdown
            meta={`成片 ${preview.output_duration_seconds} 秒 · ${preview.resolution} · 口播来源：${
              preview.script_source === "script_version"
                ? "已保存口播稿"
                : "拆解原稿"
            }`}
            onSave={readOnly ? undefined : handleSavePrompt}
            text={preview.prompt_text}
          />
        ) : null}
      </fieldset>

      <fieldset className="flow-step" disabled={firstFrameGenerationBusy}>
        <legend>② 源画面与人物</legend>
        {firstFrameGenerationBusy ? (
          <p className="status-note">
            当前首帧正在使用这组源画面与人物，生成结束前暂不能更改。
          </p>
        ) : null}
        <CharacterSelection
          onBusyChange={(busy) => markUpstreamBusy("character", busy)}
          onVersionChange={handleCharacterChange}
          projectId={project.id}
          readOnly={readOnly}
          variant="inline"
        />
        <SourceFrameSelection
          featureSuggestion={DEFAULT_FEATURE_SUGGESTION}
          onBusyChange={(busy) => markUpstreamBusy("source-frame", busy)}
          onSelectionChange={handleSourceFrameChange}
          projectId={project.id}
          readOnly={readOnly}
          referenceAssetId={project.reference_asset_id}
          simplified
        />
      </fieldset>

      <fieldset className="flow-step">
        <legend>③ 人物置换首帧</legend>
        {referenceStatus ? (
          referenceStatus
        ) : (
          <p className="flow-hint">确认源画面与角色后，即可生成首帧。</p>
        )}
        {sourceFrameSelection ? (
          <FirstFrameSelection
            onBusyChange={setFirstFrameGenerationBusy}
            onSelectionChange={handleFirstFrameChange}
            projectId={project.id}
            readOnly={readOnly}
            referenceSelection={referenceSelection}
            simplified
            sourceFrameSelectionId={sourceFrameSelection.id}
          />
        ) : null}
      </fieldset>

      <fieldset className="flow-step">
        <legend>④ 自定义文案</legend>
        <div className="flow-script">
          <p className="flow-hint">
            已带入拆解原文，可直接修改；不修改则沿用原文。提交时会把当前内容重新编译进视频
            Prompt。
          </p>
          <textarea
            aria-label="自定义文案"
            disabled={readOnly}
            onChange={(event) => setScriptText(event.target.value)}
            value={scriptText}
          />
        </div>
      </fieldset>

      <fieldset className="flow-step" disabled={!firstFrameAssetId}>
        <legend>⑤ 提交生成</legend>
        {firstFrameAssetId ? (
          <>
            {scriptWasEdited ? (
              <p className="status-note">
                将以当前自定义文案重新编译视频 Prompt；第一步保存过的旧 Prompt
                不会覆盖本次文案。
              </p>
            ) : revisedPromptText ? (
              <p className="status-note">
                将以你在第一段编辑后的提示词文本提交。
              </p>
            ) : null}
            {renderPaidWarning()}
            {generationError ? (
              <p className="settings-error" role="alert">
                {generationError}
              </p>
            ) : null}
            {generationMessage ? (
              <p className="setup-success" role="status">
                {generationMessage}
              </p>
            ) : null}
            <button
              disabled={!canStart}
              onClick={() => void handleStartGeneration()}
              type="button"
            >
              {generationPhase === "running"
                ? "正在创建生成任务"
                : "开始生成（1 个付费任务）"}
            </button>
          </>
        ) : (
          <p className="flow-hint">确认首帧后即可提交生成。</p>
        )}
      </fieldset>
    </section>
  );
}
