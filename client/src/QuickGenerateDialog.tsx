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
  getLatestProjectAnalysis,
  getLatestProjectShotCards,
  getLatestScriptVersion,
  lockGenerationPrompt,
  type Project,
  type ProjectMainCharacter,
  type PromptPreviewResult,
  previewGenerationPrompt,
  readFirstFrameSelectionPayload,
  type ShotCard,
  saveShotCards,
} from "./api";
import { CharacterReferenceSelection as CharacterReferencePanel } from "./CharacterReferenceSelection";
import { CharacterSelection } from "./CharacterSelection";
import { FirstFrameSelection } from "./FirstFrameSelection";
import { SourceFrameSelection } from "./SourceFrameSelection";

type QuickGenerateDialogProps = {
  onBatchCreated: (batch: GenerationBatch) => void;
  onClose: () => void;
  project: Project;
  readOnly: boolean;
};

type GenerationPhase = "idle" | "running" | "done";

// 快速生成 = 项目页直接闭环的最短动线：人物与画面 → 首帧 → 生成。
// 首帧确认后的「开始生成」内部自动补齐镜头卡版本、原稿口播稿、Prompt
// 编译与锁定（与工作区一键流水线同一服务端语义），付费红线（首帧付费
// 生成、首帧人工确认、批次付费确认）全部保留在显式点击上。
export function QuickGenerateDialog({
  onBatchCreated,
  onClose,
  project,
  readOnly,
}: QuickGenerateDialogProps) {
  const [characterSelection, setCharacterSelection] =
    useState<ProjectMainCharacter | null>(null);
  const [sourceFrameSelection, setSourceFrameSelection] =
    useState<AnalysisVersion | null>(null);
  const [referenceSelection, setReferenceSelection] =
    useState<CharacterReferenceSelection | null>(null);
  const [firstFrameSelection, setFirstFrameSelection] =
    useState<AnalysisVersion | null>(null);
  const [analysisVersion, setAnalysisVersion] =
    useState<AnalysisVersion | null>(null);
  const [analysisError, setAnalysisError] = useState("");
  const [limits, setLimits] = useState<GenerationRuntimeLimits | null>(null);
  const [preview, setPreview] = useState<PromptPreviewResult | null>(null);
  const [isPreviewLoading, setIsPreviewLoading] = useState(false);
  const [previewError, setPreviewError] = useState("");
  const [generationPhase, setGenerationPhase] =
    useState<GenerationPhase>("idle");
  const [generationError, setGenerationError] = useState("");
  const [generationMessage, setGenerationMessage] = useState("");
  const upstreamBusyRef = useRef<Set<string>>(new Set());
  const [, forceRender] = useState(0);
  const idempotencyKeyRef = useRef("");

  const firstFramePayload = firstFrameSelection
    ? readFirstFrameSelectionPayload(firstFrameSelection)
    : null;
  const firstFrameAssetId = firstFramePayload?.first_frame_asset_id ?? null;
  const isUpstreamBusy = upstreamBusyRef.current.size > 0;
  const isBusy = generationPhase === "running" || isUpstreamBusy;
  const canStart =
    Boolean(firstFrameAssetId) &&
    !isBusy &&
    !readOnly &&
    generationPhase !== "done";

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

  // 首帧确认后自动请求一次提示词预览：让用户在点击付费生成前看到
  // 将提交给 MiniMax H3 的完整 Prompt 文本（与服务端编译模板一致）。
  useEffect(() => {
    if (!firstFrameAssetId || preview || isPreviewLoading) {
      return;
    }
    let active = true;
    setIsPreviewLoading(true);
    setPreviewError("");
    previewGenerationPrompt(project.id)
      .then((result) => {
        if (active) {
          setPreview(result);
        }
      })
      .catch(() => {
        if (active) {
          setPreviewError("提示词预览暂不可用，可稍后重试或直接生成。");
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
  }, [firstFrameAssetId, isPreviewLoading, preview, project.id]);

  // stale 级联清空链与工作区一致：角色变 → 清源画面/参考/首帧；
  // 源画面变 → 清参考/首帧；参考变 → 清首帧。
  const handleCharacterChange = useCallback(
    (selection: ProjectMainCharacter | null) => {
      setCharacterSelection(selection);
      setSourceFrameSelection(null);
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

  const handleReferenceChange = useCallback(
    (selection: CharacterReferenceSelection | null) => {
      setReferenceSelection(selection);
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

  function readAnalysisShots(version: AnalysisVersion): ShotCard[] | null {
    const shots = version.payload?.shots;
    if (!Array.isArray(shots) || shots.length === 0) {
      return null;
    }
    return shots as ShotCard[];
  }

  function readOriginalScript(version: AnalysisVersion, shots: ShotCard[]) {
    const original = version.payload?.original_script;
    if (typeof original === "string" && original.trim()) {
      return original;
    }
    return shots
      .map((shot) =>
        typeof shot.spoken_text === "string" ? shot.spoken_text : "",
      )
      .join("");
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
    const shots = analysisVersion ? readAnalysisShots(analysisVersion) : null;
    if (!shots || !analysisVersion) {
      throw new Error("拆解结果缺少镜头数据，请先在工作区检查拆解结果。");
    }
    const saved = await saveShotCards(analysisVersion.id, shots);
    return saved.id;
  }

  async function ensureScriptVersion(
    shotCardVersionId: string,
  ): Promise<string> {
    const latest = await getLatestScriptVersion(project.id);
    const payload = latest.version?.payload as
      | Record<string, unknown>
      | undefined;
    if (
      latest.version &&
      !latest.stale &&
      payload?.shot_card_version_id === shotCardVersionId
    ) {
      return latest.version.id;
    }
    const shots = analysisVersion ? readAnalysisShots(analysisVersion) : null;
    if (!shots || !analysisVersion) {
      throw new Error("拆解结果缺少镜头数据，请先在工作区检查拆解结果。");
    }
    const originalScript = readOriginalScript(analysisVersion, shots);
    if (!originalScript.trim()) {
      throw new Error("原稿口播稿为空，请先在工作区补充口播稿。");
    }
    const saved = await createScriptVersion(project.id, {
      source: "original",
      text: originalScript,
      shot_card_version_id: shotCardVersionId,
    });
    return saved.id;
  }

  function defaultDurationSeconds(): number {
    const raw = analysisVersion?.payload?.duration_seconds;
    const duration = typeof raw === "number" && raw > 0 ? Math.round(raw) : 10;
    return Math.max(4, Math.min(15, duration));
  }

  async function handleStartGeneration() {
    if (!firstFrameAssetId || isBusy || readOnly) {
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
      const locked = await lockGenerationPrompt(project.id, compiled.id);
      if (!idempotencyKeyRef.current) {
        idempotencyKeyRef.current =
          typeof globalThis.crypto?.randomUUID === "function"
            ? globalThis.crypto.randomUUID()
            : `quick-generate-${Date.now()}-${Math.random().toString(16).slice(2)}`;
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
        <p className="quickgen-cost">
          本次将创建 1 个付费视频生成任务（MiniMax H3）。
        </p>
      );
    }
    const cost = limits.estimated_cost_per_task;
    return (
      <p className="quickgen-cost">
        本次将创建 1 个付费视频生成任务（MiniMax H3）
        {cost == null ? "" : `，预计费用 ¥${cost.toFixed(2)}`}。
      </p>
    );
  }

  return (
    <div
      aria-label={`快速生成 ${project.name}`}
      aria-modal="true"
      className="dialog-backdrop"
      onClick={(event) => {
        if (event.target === event.currentTarget && !isBusy) {
          onClose();
        }
      }}
      onKeyDown={(event) => {
        if (event.key === "Escape" && !isBusy) {
          onClose();
        }
      }}
      role="dialog"
    >
      <section className="quickgen-dialog">
        <header className="quickgen-header">
          <div>
            <h2>生成视频 · {project.name}</h2>
            <p>选人物 → 确认首帧 → 开始生成，全程无需离开本页。</p>
          </div>
          <button
            className="secondary-button"
            disabled={isBusy}
            onClick={onClose}
            type="button"
          >
            关闭
          </button>
        </header>

        {analysisError ? (
          <p className="settings-error" role="alert">
            {analysisError}
          </p>
        ) : null}

        <div className="quickgen-body">
          <fieldset className="quickgen-step">
            <legend>① 人物与画面</legend>
            <CharacterSelection
              onBusyChange={(busy) => markUpstreamBusy("character", busy)}
              onVersionChange={handleCharacterChange}
              projectId={project.id}
              readOnly={readOnly}
            />
            {characterSelection ? (
              <SourceFrameSelection
                onBusyChange={(busy) => markUpstreamBusy("source-frame", busy)}
                onSelectionChange={handleSourceFrameChange}
                projectId={project.id}
                readOnly={readOnly}
                referenceAssetId={project.reference_asset_id}
              />
            ) : (
              <p className="quickgen-hint">选择角色后继续选择源画面。</p>
            )}
            {characterSelection && sourceFrameSelection ? (
              <CharacterReferencePanel
                characterSelection={characterSelection}
                onBusyChange={(busy) => markUpstreamBusy("reference", busy)}
                onSelectionChange={handleReferenceChange}
                projectId={project.id}
                readOnly={readOnly}
                sourceFrameSelection={sourceFrameSelection}
              />
            ) : null}
          </fieldset>

          <fieldset className="quickgen-step" disabled={!sourceFrameSelection}>
            <legend>② 首帧</legend>
            {sourceFrameSelection ? (
              <FirstFrameSelection
                onBusyChange={(busy) => markUpstreamBusy("first-frame", busy)}
                onSelectionChange={handleFirstFrameChange}
                projectId={project.id}
                readOnly={readOnly}
                referenceSelection={referenceSelection}
                sourceFrameSelectionId={sourceFrameSelection.id}
              />
            ) : (
              <p className="quickgen-hint">
                确认人物参考后，在这里付费生成并确认首帧。
              </p>
            )}
          </fieldset>

          <fieldset className="quickgen-step" disabled={!firstFrameAssetId}>
            <legend>③ 生成</legend>
            {firstFrameAssetId ? (
              <>
                <details className="quickgen-prompt-preview" open>
                  <summary>将提交的提示词（由拆解结果自动编译）</summary>
                  {isPreviewLoading ? (
                    <p className="status-note">正在编译提示词预览…</p>
                  ) : null}
                  {previewError ? (
                    <p className="status-note">{previewError}</p>
                  ) : null}
                  {preview ? (
                    <>
                      <pre className="quickgen-prompt-text">
                        {preview.prompt_text}
                      </pre>
                      <p className="status-note">
                        成片 {preview.output_duration_seconds} 秒 ·{" "}
                        {preview.resolution} · 口播来源：
                        {preview.script_source === "script_version"
                          ? "已保存口播稿"
                          : "拆解原稿"}
                      </p>
                    </>
                  ) : null}
                </details>
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
              <p className="quickgen-hint">确认首帧后即可一键生成视频。</p>
            )}
          </fieldset>
        </div>
      </section>
    </div>
  );
}
