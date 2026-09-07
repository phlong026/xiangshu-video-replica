import { useCallback, useEffect, useRef, useState } from "react";

import {
  type AnalysisVersion,
  type CharacterReferenceSelection,
  compileGenerationPrompt,
  createGenerationBatch,
  createScriptVersion,
  defaultBatchProvider,
  type GenerationBatch,
  type GenerationBatchInput,
  type GenerationPriceQuote,
  type GenerationRatio,
  getGenerationPriceQuote,
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
  saveGenerationPrompt,
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
type QuoteStatus = "loading" | "ready" | "error";

type IdempotencyEnvelope = {
  request: GenerationBatchInput;
  sourceFingerprint: string;
};

const frozenDetailRequests = new Map<string, IdempotencyEnvelope>();

function detailRequestKey(project: Project, sourceFingerprint: string): string {
  return JSON.stringify([project.owner_user_id, project.id, sourceFingerprint]);
}

function newIdempotencyKey(): string {
  return typeof globalThis.crypto?.randomUUID === "function"
    ? globalThis.crypto.randomUUID()
    : `detail-flow-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

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
  const [generationRatio, setGenerationRatio] =
    useState<GenerationRatio>("adaptive");
  const [generationDuration, setGenerationDuration] = useState<4 | 15>(15);
  const [generationQuantity, setGenerationQuantity] = useState<1 | 2 | 4>(1);
  const [priceQuote, setPriceQuote] = useState<GenerationPriceQuote | null>(
    null,
  );
  const [priceQuoteStatus, setPriceQuoteStatus] =
    useState<QuoteStatus>("loading");
  const [priceQuoteError, setPriceQuoteError] = useState("");
  const [priceQuoteRevision, setPriceQuoteRevision] = useState(0);
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
  // 只有完全相同的提交内容才复用幂等键。上一次请求响应丢失时可安全重试；
  // 用户修改文案、Prompt、首帧或生成参数后必须生成新键，避免服务端 409。
  const idempotencyEnvelopeRef = useRef<IdempotencyEnvelope | null>(null);
  const submissionBusyRef = useRef(false);
  const submissionOperationRef = useRef(0);
  const submissionContextRef = useRef("");
  const autoMatchAttemptedRef = useRef<Set<string>>(new Set());
  const firstFrameStepRef = useRef<HTMLFieldSetElement>(null);
  const onBusyChangeRef = useRef(onBusyChange);
  onBusyChangeRef.current = onBusyChange;

  const firstFramePayload = firstFrameSelection
    ? readFirstFrameSelectionPayload(firstFrameSelection)
    : null;
  const firstFrameAssetId = firstFramePayload?.first_frame_asset_id ?? null;
  const isUpstreamBusy = upstreamBusyRef.current.size > 0;
  const isBusy = generationPhase === "running" || isUpstreamBusy;
  const scriptWasEdited = scriptText.trim() !== originalScript.trim();
  submissionContextRef.current = JSON.stringify({
    projectId: project.id,
    firstFrameAssetId,
    generationDuration,
    generationQuantity,
    generationRatio,
    scriptText: scriptText.trim(),
    revisedPromptText: revisedPromptText?.trim() ?? "",
  });
  const priceQuoteReady = Boolean(
    priceQuoteStatus === "ready" &&
      priceQuote &&
      priceQuote.resolution === "768P" &&
      priceQuote.duration_seconds === generationDuration &&
      priceQuote.quantity === generationQuantity &&
      priceQuote.estimated_seconds === generationDuration * generationQuantity,
  );
  const canStart =
    Boolean(firstFrameAssetId) &&
    priceQuoteReady &&
    !isBusy &&
    !firstFrameGenerationBusy &&
    !readOnly &&
    generationPhase !== "done";

  useEffect(() => {
    onBusyChangeRef.current?.(isBusy);
  }, [isBusy]);

  // biome-ignore lint/correctness/useExhaustiveDependencies: 切换项目必须显式失效正在进行的付费提交。
  useEffect(() => {
    submissionOperationRef.current += 1;
    submissionBusyRef.current = false;
    setGenerationPhase("idle");
    setGenerationError("");
    setGenerationMessage("");
    return () => {
      submissionOperationRef.current += 1;
    };
  }, [project.id]);

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
    return () => {
      active = false;
    };
  }, [project.id]);

  useEffect(() => {
    void priceQuoteRevision;
    let active = true;
    if (typeof getGenerationPriceQuote !== "function") return;
    const input = {
      resolution: "768P" as const,
      duration_seconds: generationDuration,
      quantity: generationQuantity,
    };
    setPriceQuote(null);
    setPriceQuoteStatus("loading");
    setPriceQuoteError("");
    getGenerationPriceQuote(input)
      .then((quote) => {
        if (!active) return;
        if (
          quote.resolution !== input.resolution ||
          quote.duration_seconds !== input.duration_seconds ||
          quote.quantity !== input.quantity ||
          quote.estimated_seconds !== input.duration_seconds * input.quantity
        ) {
          setPriceQuoteStatus("error");
          setPriceQuoteError("生成报价参数与当前生成参数不一致，请重新获取。");
          return;
        }
        setPriceQuote(quote);
        setPriceQuoteStatus("ready");
      })
      .catch((quoteError: unknown) => {
        if (active) {
          setPriceQuote(null);
          setPriceQuoteStatus("error");
          setPriceQuoteError(
            quoteError instanceof Error && quoteError.message.trim()
              ? quoteError.message
              : "读取生成费用失败，请稍后重试。",
          );
        }
      });
    return () => {
      active = false;
    };
  }, [generationDuration, generationQuantity, priceQuoteRevision]);

  function retryPriceQuote() {
    setPriceQuoteRevision((value) => value + 1);
  }

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

  async function handleSavePrompt(text: string) {
    await saveGenerationPrompt(project.id, {
      name: `反推提示词 ${new Date().toLocaleString("zh-CN")}`,
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
    const raw = sourceVideoDurationSeconds();
    const duration = typeof raw === "number" && raw > 0 ? Math.round(raw) : 10;
    return Math.max(4, Math.min(15, duration));
  }

  function sourceVideoDurationSeconds(): number {
    const raw = analysisVersion
      ? readAnalysisPayload(analysisVersion)?.duration_seconds
      : null;
    return typeof raw === "number" && Number.isFinite(raw) && raw > 0
      ? raw
      : 10;
  }

  async function handleStartGeneration() {
    if (
      submissionBusyRef.current ||
      !firstFrameAssetId ||
      isBusy ||
      firstFrameGenerationBusy ||
      readOnly
    ) {
      return;
    }
    if (!priceQuoteReady) {
      setGenerationError(
        priceQuoteError || "请先取得与当前参数一致的生成报价后再提交。",
      );
      return;
    }
    const operation = submissionOperationRef.current + 1;
    submissionOperationRef.current = operation;
    const sourceFingerprint = submissionContextRef.current;
    const isCurrent = () =>
      submissionOperationRef.current === operation &&
      submissionContextRef.current === sourceFingerprint;
    submissionBusyRef.current = true;
    setGenerationPhase("running");
    setGenerationError("");
    setGenerationMessage("");
    try {
      let envelope = idempotencyEnvelopeRef.current;
      const frozenRequestKey = detailRequestKey(project, sourceFingerprint);
      if (!envelope || envelope.sourceFingerprint !== sourceFingerprint) {
        envelope = frozenDetailRequests.get(frozenRequestKey) ?? null;
        idempotencyEnvelopeRef.current = envelope;
      }
      if (!envelope || envelope.sourceFingerprint !== sourceFingerprint) {
        const shotCardVersionId = await ensureShotCardVersion();
        const scriptVersionId = await ensureScriptVersion(shotCardVersionId);
        const duration = generationDuration;
        const compiled = await compileGenerationPrompt(project.id, {
          script_version_id: scriptVersionId,
          shot_card_version_id: shotCardVersionId,
          first_frame_asset_id: firstFrameAssetId,
          output_duration_seconds: duration,
          resolution: "768P",
          ratio: generationRatio,
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
        if (!isCurrent()) {
          throw new Error("生成参数已变化，请按最新报价重新提交。");
        }
        envelope = {
          sourceFingerprint,
          request: {
            quantity: generationQuantity,
            prompt_version_id: locked.id,
            first_frame_asset_id: firstFrameAssetId,
            output_duration_seconds: duration,
            resolution: "768P",
            ratio: generationRatio,
            provider: defaultBatchProvider(),
            fake_audio_quality: "ok",
            idempotency_key: newIdempotencyKey(),
          },
        };
        idempotencyEnvelopeRef.current = envelope;
        frozenDetailRequests.set(frozenRequestKey, envelope);
      }
      if (!isCurrent()) {
        throw new Error("生成参数已变化，请按最新报价重新提交。");
      }
      const batch = await createGenerationBatch(project.id, envelope.request);
      if (idempotencyEnvelopeRef.current === envelope) {
        idempotencyEnvelopeRef.current = null;
      }
      if (frozenDetailRequests.get(frozenRequestKey) === envelope) {
        frozenDetailRequests.delete(frozenRequestKey);
      }
      if (!isCurrent()) return;
      setGenerationPhase("done");
      setGenerationMessage("生成任务已创建，正在前往任务记录…");
      onBatchCreated(batch);
    } catch (error) {
      if (submissionOperationRef.current === operation) {
        setGenerationPhase("idle");
        setGenerationError(
          error instanceof Error ? error.message : "创建生成任务失败，请重试。",
        );
      }
    } finally {
      if (submissionOperationRef.current === operation) {
        submissionBusyRef.current = false;
      }
    }
  }

  function renderPaidWarning() {
    return (
      <p className="flow-cost">
        预计消耗 {generationDuration * generationQuantity} 秒额度
        {priceQuoteReady && priceQuote
          ? `，约 ¥${(priceQuote.estimated_price_fen / 100).toFixed(2)}（${priceQuote.unit_price_fen_per_second} 分/秒）`
          : ""}
        。
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
          人物参考已匹配，可直接生成人物置换首帧。
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
    return (
      <p className="status-note" role="status">
        源画面已确认，正在自动匹配人物五视图…
      </p>
    );
  })();
  const outputDurationSeconds = defaultDurationSeconds();
  const sourceDurationSeconds = sourceVideoDurationSeconds();
  const scriptCharacterCount = countSpeechCharacters(scriptText);
  const suggestedScriptMin = outputDurationSeconds * 4;
  const suggestedScriptMax = outputDurationSeconds * 5;
  const scriptTooLong = scriptCharacterCount > suggestedScriptMax;

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

      <fieldset className="flow-step" disabled={generationPhase === "running"}>
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
            onSave={
              readOnly || generationPhase === "running"
                ? undefined
                : handleSavePrompt
            }
            text={preview.prompt_text}
          />
        ) : null}
      </fieldset>

      <fieldset
        className="flow-step"
        disabled={firstFrameGenerationBusy || generationPhase === "running"}
      >
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
          onBusyChange={(busy) => markUpstreamBusy("source-frame", busy)}
          onConfirmed={() =>
            firstFrameStepRef.current?.scrollIntoView?.({
              behavior: "smooth",
              block: "start",
            })
          }
          onSelectionChange={handleSourceFrameChange}
          projectId={project.id}
          readOnly={readOnly}
          referenceAssetId={project.reference_asset_id}
          simplified
          videoDurationSeconds={sourceDurationSeconds}
        />
      </fieldset>

      <fieldset
        className="flow-step"
        disabled={generationPhase === "running"}
        ref={firstFrameStepRef}
      >
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

      <fieldset className="flow-step" disabled={generationPhase === "running"}>
        <legend>④ 自定义文案</legend>
        <div className="flow-script">
          <p className="flow-hint">
            已带入拆解原文，可直接修改；不修改则沿用原文。提交时会把当前内容重新编译进视频
            Prompt。
          </p>
          <textarea
            aria-label="自定义文案"
            disabled={readOnly || generationPhase === "running"}
            onChange={(event) => setScriptText(event.target.value)}
            value={scriptText}
          />
          <p className="status-note">
            当前 {scriptCharacterCount} 字；{outputDurationSeconds} 秒成片建议约{" "}
            {suggestedScriptMin}–{suggestedScriptMax} 字（按每秒 4–5 字）。
          </p>
          {scriptTooLong ? (
            <p
              aria-label="文案时长警告"
              className="settings-error"
              role="alert"
            >
              当前文案已超过建议上限，口播可能无法在成片时长内完整表达；建议缩短后再提交。
            </p>
          ) : null}
        </div>
      </fieldset>

      {priceQuoteStatus === "error" ? (
        <div className="settings-error" role="alert">
          <p>{priceQuoteError}</p>
          <button
            className="secondary-button"
            onClick={retryPriceQuote}
            type="button"
          >
            重新获取生成报价
          </button>
        </div>
      ) : null}

      <fieldset
        className="flow-step"
        disabled={!firstFrameAssetId || generationPhase === "running"}
      >
        <legend>⑤ 提交生成</legend>
        {generationError ? (
          <p className="settings-error" role="alert">
            {generationError}
          </p>
        ) : null}
        {firstFrameAssetId ? (
          <>
            <div className="generation-parameter-grid">
              <label>
                画面比例
                <select
                  aria-label="画面比例"
                  onChange={(event) =>
                    setGenerationRatio(event.target.value as GenerationRatio)
                  }
                  value={generationRatio}
                >
                  <option value="adaptive">自动</option>
                  <option value="21:9">21:9</option>
                  <option value="16:9">16:9</option>
                  <option value="4:3">4:3</option>
                  <option value="1:1">1:1</option>
                  <option value="3:4">3:4</option>
                  <option value="9:16">9:16</option>
                </select>
              </label>
              <label>
                成片时长
                <select
                  aria-label="成片时长"
                  onChange={(event) =>
                    setGenerationDuration(Number(event.target.value) as 4 | 15)
                  }
                  value={generationDuration}
                >
                  <option value={4}>4 秒</option>
                  <option value={15}>15 秒</option>
                </select>
              </label>
              <label>
                生成数量
                <select
                  aria-label="生成数量"
                  onChange={(event) =>
                    setGenerationQuantity(
                      Number(event.target.value) as 1 | 2 | 4,
                    )
                  }
                  value={generationQuantity}
                >
                  <option value={1}>1</option>
                  <option value={2}>2</option>
                  <option value={4}>4</option>
                </select>
              </label>
            </div>
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
                : `提交生成（${generationQuantity} 条）`}
            </button>
          </>
        ) : (
          <p className="flow-hint">确认首帧后即可提交生成。</p>
        )}
      </fieldset>
    </section>
  );
}

function countSpeechCharacters(value: string): number {
  return Array.from(value.replace(/\s+/g, "")).length;
}
