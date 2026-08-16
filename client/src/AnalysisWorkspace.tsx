import {
  type FormEvent,
  useCallback,
  useEffect,
  useRef,
  useState,
} from "react";

import {
  type AnalysisProvider,
  type AnalysisVersion,
  type CharacterReferenceSelection as CharacterReferenceSelectionValue,
  type GenerationBatch,
  getLatestProjectAnalysis,
  getLatestProjectShotCards,
  type Project,
  type ProjectMainCharacter,
  readAnalysisPayload,
  readAnalysisProvider,
  readFirstFrameSelectionPayload,
  readShotCardPayload,
  type ShotCard,
  saveShotCards,
  startVideoAnalysis,
} from "./api";
import { CharacterReferenceSelection } from "./CharacterReferenceSelection";
import { CharacterSelection } from "./CharacterSelection";
import { FirstFrameSelection } from "./FirstFrameSelection";
import { GenerationComposer } from "./GenerationComposer";
import { ProjectWorkflowSteps } from "./ProjectWorkflowSteps";
import { SourceFrameSelection } from "./SourceFrameSelection";

function toNonNegativeTime(value: string): number {
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed >= 0 ? parsed : 0;
}

function copyShotCards(shots: ShotCard[]): ShotCard[] {
  return shots.map((shot) => ({ ...shot }));
}

function shotCardsEqual(left: ShotCard[], right: ShotCard[]): boolean {
  return (
    left.length === right.length &&
    left.every((shot, index) => {
      const other = right[index];
      return (
        other !== undefined &&
        shot.shot_id === other.shot_id &&
        shot.start_time === other.start_time &&
        shot.end_time === other.end_time &&
        shot.shot_type === other.shot_type &&
        shot.composition === other.composition &&
        shot.camera_motion === other.camera_motion &&
        shot.subject === other.subject &&
        shot.action === other.action &&
        shot.scene === other.scene &&
        shot.spoken_text === other.spoken_text &&
        shot.transition === other.transition
      );
    })
  );
}

const SHOT_TEXT_FIELDS: Array<{
  key: Exclude<keyof ShotCard, "start_time" | "end_time">;
  label: string;
}> = [
  { key: "shot_id", label: "镜头编号" },
  { key: "shot_type", label: "景别" },
  { key: "composition", label: "构图" },
  { key: "camera_motion", label: "运镜" },
  { key: "subject", label: "主体" },
  { key: "action", label: "动作" },
  { key: "scene", label: "场景" },
  { key: "spoken_text", label: "原口播" },
  { key: "transition", label: "转场" },
];

type UpstreamBusySource =
  | "character"
  | "source-frame"
  | "reference"
  | "first-frame";

export function AnalysisWorkspace({
  currentUserId,
  onAnalysisReady,
  onBatchCreated,
  onClose,
  onWorkspaceBusyChange,
  project,
  readOnly = false,
}: {
  currentUserId: string;
  onAnalysisReady: (projectId: string) => void;
  onBatchCreated: (batch: GenerationBatch) => void;
  onClose: () => void;
  onWorkspaceBusyChange?: (isBusy: boolean) => void;
  project: Project;
  readOnly?: boolean;
}) {
  const [analysisId, setAnalysisId] = useState("");
  const [analysisProvider, setAnalysisProvider] =
    useState<AnalysisProvider | null>(null);
  const [analysisSummary, setAnalysisSummary] = useState("");
  const [originalScript, setOriginalScript] = useState("");
  const [durationSeconds, setDurationSeconds] = useState(0);
  const [shots, setShots] = useState<ShotCard[]>([]);
  const [shotCardVersionId, setShotCardVersionId] = useState("");
  const [shotCardsDirty, setShotCardsDirty] = useState(false);
  const [error, setError] = useState("");
  const [saveMessage, setSaveMessage] = useState("");
  const [isLoading, setIsLoading] = useState(true);
  const [isSaving, setIsSaving] = useState(false);
  const [isGenerationBusy, setIsGenerationBusy] = useState(false);
  const [isUpstreamBusy, setIsUpstreamBusy] = useState(false);
  const [isAnalysisMissing, setIsAnalysisMissing] = useState(false);
  const [isStartingAnalysis, setIsStartingAnalysis] = useState(false);
  const [characterSelection, setCharacterSelection] =
    useState<ProjectMainCharacter | null>(null);
  const [sourceFrameSelection, setSourceFrameSelection] =
    useState<AnalysisVersion | null>(null);
  const [characterReferenceSelection, setCharacterReferenceSelection] =
    useState<CharacterReferenceSelectionValue | null>(null);
  const [firstFrameSelection, setFirstFrameSelection] =
    useState<AnalysisVersion | null>(null);
  const [generationStep, setGenerationStep] = useState(7);
  const [reloadToken, setReloadToken] = useState(0);
  const [forceLoadProjectId, setForceLoadProjectId] = useState<string | null>(
    null,
  );
  const reloadTokenRef = useRef(0);
  const isGenerationBusyRef = useRef(false);
  const isWorkspaceBusyRef = useRef(false);
  const upstreamBusyCountsRef = useRef(new Map<UpstreamBusySource, number>());
  const characterSelectionVersionIdRef = useRef<string | null>(null);
  const sourceFrameSelectionIdRef = useRef<string | null>(null);
  const characterReferenceSelectionIdRef = useRef<string | null>(null);
  const savedShotsRef = useRef<ShotCard[]>([]);

  useEffect(() => {
    if (
      !readOnly &&
      project.analysis_status === "PENDING" &&
      forceLoadProjectId !== project.id
    ) {
      setIsLoading(false);
      setError("");
      setSaveMessage("");
      setAnalysisProvider(null);
      setIsAnalysisMissing(true);
      return;
    }

    const loadAttempt = reloadToken;
    let isActive = true;
    setIsLoading(true);
    setError("");
    setSaveMessage("");
    setAnalysisProvider(null);
    setIsAnalysisMissing(false);
    setShotCardVersionId("");
    setShotCardsDirty(false);
    savedShotsRef.current = [];

    async function loadWorkspace() {
      try {
        const version = await getLatestProjectAnalysis(project.id);
        if (!isActive || loadAttempt !== reloadTokenRef.current) {
          return;
        }
        const payload = readAnalysisPayload(version);
        if (!payload) {
          setError("视频拆解数据格式无效，请重新执行拆解。");
          return;
        }
        setAnalysisId(version.id);
        setIsAnalysisMissing(false);
        setAnalysisProvider(readAnalysisProvider(version));
        setAnalysisSummary(payload.summary);
        setOriginalScript(payload.original_script);
        setDurationSeconds(payload.duration_seconds);
        setShots(payload.shots);
        savedShotsRef.current = copyShotCards(payload.shots);
        setShotCardsDirty(false);
        const savedShotCardVersion = await getLatestProjectShotCards(
          project.id,
        );
        if (!isActive || !savedShotCardVersion) {
          return;
        }
        const savedShotCards = readShotCardPayload(savedShotCardVersion);
        if (savedShotCards?.source_analysis_version_id === version.id) {
          setShots(savedShotCards.shots);
          savedShotsRef.current = copyShotCards(savedShotCards.shots);
          setShotCardVersionId(savedShotCardVersion.id);
          setShotCardsDirty(false);
        }
      } catch (requestError) {
        if (isActive) {
          if ((requestError as { status?: number }).status === 404) {
            setIsAnalysisMissing(true);
            setError("");
          } else {
            setError(
              requestError instanceof Error
                ? requestError.message
                : "读取视频拆解失败。",
            );
          }
        }
      } finally {
        if (isActive) {
          setIsLoading(false);
        }
      }
    }

    void loadWorkspace();

    return () => {
      isActive = false;
    };
  }, [
    forceLoadProjectId,
    project.analysis_status,
    project.id,
    readOnly,
    reloadToken,
  ]);

  useEffect(() => {
    if (!project.id) {
      return;
    }
    setCharacterSelection(null);
    setSourceFrameSelection(null);
    setCharacterReferenceSelection(null);
    setFirstFrameSelection(null);
    setGenerationStep(7);
    setShotCardsDirty(false);
    isGenerationBusyRef.current = false;
    isWorkspaceBusyRef.current = false;
    upstreamBusyCountsRef.current.clear();
    setIsGenerationBusy(false);
    setIsUpstreamBusy(false);
    savedShotsRef.current = [];
    characterSelectionVersionIdRef.current = null;
    sourceFrameSelectionIdRef.current = null;
    characterReferenceSelectionIdRef.current = null;
  }, [project.id]);

  const handleCharacterSelectionChange = useCallback(
    (selection: ProjectMainCharacter | null) => {
      if (isGenerationBusyRef.current) {
        return;
      }
      const nextVersionId = selection?.version_id ?? null;
      const bindingChanged =
        nextVersionId !== characterSelectionVersionIdRef.current;
      characterSelectionVersionIdRef.current = nextVersionId;
      setCharacterSelection(selection);
      if (bindingChanged) {
        sourceFrameSelectionIdRef.current = null;
        characterReferenceSelectionIdRef.current = null;
        setSourceFrameSelection(null);
        setCharacterReferenceSelection(null);
        setFirstFrameSelection(null);
      }
    },
    [],
  );

  const handleSourceFrameSelectionChange = useCallback(
    (selection: AnalysisVersion | null) => {
      if (isGenerationBusyRef.current) {
        return;
      }
      const nextSelectionId = selection?.id ?? null;
      const bindingChanged =
        nextSelectionId !== sourceFrameSelectionIdRef.current;
      sourceFrameSelectionIdRef.current = nextSelectionId;
      setSourceFrameSelection(selection);
      if (bindingChanged) {
        characterReferenceSelectionIdRef.current = null;
        setCharacterReferenceSelection(null);
        setFirstFrameSelection(null);
      }
    },
    [],
  );

  const handleCharacterReferenceSelectionChange = useCallback(
    (selection: CharacterReferenceSelectionValue | null) => {
      if (isGenerationBusyRef.current) {
        return;
      }
      const nextSelectionId = selection?.id ?? null;
      const bindingChanged =
        nextSelectionId !== characterReferenceSelectionIdRef.current;
      characterReferenceSelectionIdRef.current = nextSelectionId;
      setCharacterReferenceSelection(selection);
      if (bindingChanged) {
        setFirstFrameSelection(null);
      }
    },
    [],
  );

  const handleFirstFrameSelectionChange = useCallback(
    (selection: AnalysisVersion | null) => {
      if (isGenerationBusyRef.current) {
        return;
      }
      setFirstFrameSelection(selection);
    },
    [],
  );

  const legacyCharacterSelected = Boolean(characterSelection?.character_id);
  const firstFramePayload = firstFrameSelection
    ? readFirstFrameSelectionPayload(firstFrameSelection)
    : null;

  const handleGenerationBusyChange = useCallback(
    (busy: boolean) => {
      isGenerationBusyRef.current = busy;
      const workspaceBusy = busy || upstreamBusyCountsRef.current.size > 0;
      isWorkspaceBusyRef.current = workspaceBusy;
      onWorkspaceBusyChange?.(workspaceBusy);
      setIsGenerationBusy(busy);
    },
    [onWorkspaceBusyChange],
  );

  const handleUpstreamBusyChange = useCallback(
    (source: UpstreamBusySource, busy: boolean) => {
      const currentCount = upstreamBusyCountsRef.current.get(source) ?? 0;
      if (busy) {
        upstreamBusyCountsRef.current.set(source, currentCount + 1);
      } else if (currentCount > 1) {
        upstreamBusyCountsRef.current.set(source, currentCount - 1);
      } else {
        upstreamBusyCountsRef.current.delete(source);
      }
      const upstreamBusy = upstreamBusyCountsRef.current.size > 0;
      const workspaceBusy = isGenerationBusyRef.current || upstreamBusy;
      isWorkspaceBusyRef.current = workspaceBusy;
      onWorkspaceBusyChange?.(workspaceBusy);
      setIsUpstreamBusy(upstreamBusy);
    },
    [onWorkspaceBusyChange],
  );

  const handleCharacterBusyChange = useCallback(
    (busy: boolean) => handleUpstreamBusyChange("character", busy),
    [handleUpstreamBusyChange],
  );
  const handleSourceFrameBusyChange = useCallback(
    (busy: boolean) => handleUpstreamBusyChange("source-frame", busy),
    [handleUpstreamBusyChange],
  );
  const handleReferenceBusyChange = useCallback(
    (busy: boolean) => handleUpstreamBusyChange("reference", busy),
    [handleUpstreamBusyChange],
  );
  const handleFirstFrameBusyChange = useCallback(
    (busy: boolean) => handleUpstreamBusyChange("first-frame", busy),
    [handleUpstreamBusyChange],
  );

  const isWorkspaceBusy = isGenerationBusy || isUpstreamBusy;

  function handleClose() {
    if (isWorkspaceBusyRef.current) {
      return;
    }
    onClose();
  }

  const workflowStep = !analysisId
    ? 2
    : !characterSelection
      ? 3
      : !sourceFrameSelection
        ? 4
        : !legacyCharacterSelected && !characterReferenceSelection
          ? 5
          : !firstFrameSelection
            ? 6
            : shotCardsDirty
              ? 7
              : generationStep;

  function reloadWorkspace() {
    reloadTokenRef.current += 1;
    setReloadToken(reloadTokenRef.current);
  }

  async function handleStartAnalysis() {
    if (readOnly || !project.reference_asset_id) {
      return;
    }
    setIsStartingAnalysis(true);
    setError("");
    try {
      await startVideoAnalysis(project.id, project.reference_asset_id);
      onAnalysisReady(project.id);
      setForceLoadProjectId(project.id);
      reloadWorkspace();
    } catch (requestError) {
      setError(
        requestError instanceof Error
          ? requestError.message
          : "启动视频拆解失败。",
      );
    } finally {
      setIsStartingAnalysis(false);
    }
  }

  function updateShot(index: number, key: keyof ShotCard, value: string) {
    if (readOnly || isSaving || isWorkspaceBusyRef.current) {
      return;
    }
    const nextShots = shots.map((shot, shotIndex) => {
      if (shotIndex !== index) {
        return shot;
      }
      const nextValue =
        key === "start_time" || key === "end_time"
          ? toNonNegativeTime(value)
          : value;
      return { ...shot, [key]: nextValue } as ShotCard;
    });
    const isDirty = !shotCardsEqual(nextShots, savedShotsRef.current);
    setShots(nextShots);
    setSaveMessage("");
    setShotCardsDirty(isDirty);
  }

  async function handleSave(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (readOnly || isSaving || isWorkspaceBusyRef.current) {
      return;
    }
    if (!analysisId) {
      return;
    }
    if (shotCardVersionId && !shotCardsDirty) {
      setError("");
      setSaveMessage("镜头卡片没有改动，无需重复保存。");
      return;
    }
    if (shots.some((shot) => shot.end_time < shot.start_time)) {
      setError("镜头时间无效：结束时间不能早于开始时间。");
      return;
    }
    setIsSaving(true);
    setError("");
    setSaveMessage("");
    try {
      const savedVersion = await saveShotCards(analysisId, shots);
      savedShotsRef.current = copyShotCards(shots);
      setShotCardVersionId(savedVersion.id);
      setGenerationStep(7);
      setShotCardsDirty(false);
      setSaveMessage(`镜头卡片已保存为版本 #${savedVersion.version_number}。`);
    } catch (requestError) {
      setError(
        requestError instanceof Error
          ? requestError.message
          : "保存镜头卡片失败。",
      );
    } finally {
      setIsSaving(false);
    }
  }

  return (
    <section className="analysis-workspace" aria-labelledby="analysis-title">
      <div className="section-heading">
        <div>
          <span className="eyebrow">VIDEO ANALYSIS</span>
          <h2 id="analysis-title">镜头卡片</h2>
          <p>
            “{project.name}”的自动拆解结果。保存后会创建独立的人工修订版本。
          </p>
        </div>
        <button
          className="secondary-button"
          disabled={isWorkspaceBusy}
          onClick={handleClose}
          type="button"
        >
          返回项目
        </button>
      </div>
      <ProjectWorkflowSteps currentStep={workflowStep} />
      {isLoading ? <p className="status-note">正在读取视频拆解</p> : null}
      {error ? <p className="settings-error">{error}</p> : null}
      {!isLoading && error && !isAnalysisMissing ? (
        <button
          className="secondary-button analysis-retry-button"
          onClick={reloadWorkspace}
          type="button"
        >
          重新加载视频拆解
        </button>
      ) : null}
      {!isLoading && isAnalysisMissing ? (
        <div className="analysis-missing-state">
          <strong>视频拆解待开始</strong>
          <p>
            参考视频已经就绪。系统会先确认是否已有拆解结果，再安全地启动本步骤。
          </p>
          {readOnly ? (
            <>
              <span className="status-note">
                当前身份只能查看，不能启动拆解。
              </span>
              <button
                className="secondary-button"
                onClick={reloadWorkspace}
                type="button"
              >
                重新检查视频拆解
              </button>
            </>
          ) : (
            <button
              disabled={isStartingAnalysis || !project.reference_asset_id}
              onClick={handleStartAnalysis}
              type="button"
            >
              {isStartingAnalysis ? "正在开始拆解" : "开始视频拆解"}
            </button>
          )}
        </div>
      ) : null}
      {!isLoading && !error && !isAnalysisMissing ? (
        <form className="analysis-form" onSubmit={handleSave}>
          <div className="analysis-summary">
            <strong>{analysisSummary}</strong>
            <div className="analysis-meta">
              <span>参考时长：{durationSeconds.toFixed(1)} 秒</span>
              {analysisProvider ? (
                <span>拆解来源：{providerLabel(analysisProvider)}</span>
              ) : null}
            </div>
          </div>
          {analysisProvider === "fake_gemini" ? (
            <p className="status-note">
              当前显示的是内置模拟结果。请在设置中保存 Gemini 视频分析 API
              Key，并配置可用的 COS 或 OSS 存储后重新拆解。
            </p>
          ) : null}
          {readOnly ? (
            <p className="status-note">
              当前为只读身份，可查看拆解、候选记录和历史版本；不会请求素材下载链接，也不能保存、选择或生成。
            </p>
          ) : null}
          <fieldset
            aria-busy={isWorkspaceBusy}
            className="analysis-workspace-grid"
            disabled={isWorkspaceBusy}
          >
            <div className="analysis-primary">
              {characterSelection ? (
                <SourceFrameSelection
                  onBusyChange={handleSourceFrameBusyChange}
                  onSelectionChange={handleSourceFrameSelectionChange}
                  projectId={project.id}
                  referenceAssetId={project.reference_asset_id}
                  readOnly={readOnly}
                />
              ) : (
                <p className="workflow-gate-note">
                  请先在右侧选择角色版本，再确认源画面。
                </p>
              )}
              {!legacyCharacterSelected &&
              characterSelection?.character_version_id &&
              sourceFrameSelection ? (
                <CharacterReferenceSelection
                  characterSelection={characterSelection}
                  onBusyChange={handleReferenceBusyChange}
                  onSelectionChange={handleCharacterReferenceSelectionChange}
                  projectId={project.id}
                  readOnly={readOnly}
                  sourceFrameSelection={sourceFrameSelection}
                />
              ) : null}
              {characterSelection ? (
                <FirstFrameSelection
                  legacyCharacterSelected={legacyCharacterSelected}
                  onBusyChange={handleFirstFrameBusyChange}
                  onSelectionChange={handleFirstFrameSelectionChange}
                  projectId={project.id}
                  readOnly={readOnly}
                  referenceSelection={characterReferenceSelection}
                  sourceFrameSelectionId={sourceFrameSelection?.id ?? null}
                />
              ) : null}
              <div className="shot-card-list">
                {shots.map((shot, index) => (
                  <fieldset className="shot-card" key={shot.shot_id}>
                    <legend>镜头 {index + 1}</legend>
                    <div className="shot-time-grid">
                      <ShotInput
                        label={`${shot.shot_id} 开始时间`}
                        onChange={(value) =>
                          updateShot(index, "start_time", value)
                        }
                        readOnly={readOnly || isSaving || isWorkspaceBusy}
                        type="number"
                        value={String(shot.start_time)}
                      />
                      <ShotInput
                        label={`${shot.shot_id} 结束时间`}
                        onChange={(value) =>
                          updateShot(index, "end_time", value)
                        }
                        readOnly={readOnly || isSaving || isWorkspaceBusy}
                        type="number"
                        value={String(shot.end_time)}
                      />
                    </div>
                    <div className="shot-field-grid">
                      {SHOT_TEXT_FIELDS.map(({ key, label }) => (
                        <ShotInput
                          key={key}
                          label={`${shot.shot_id} ${label}`}
                          onChange={(value) => updateShot(index, key, value)}
                          readOnly={readOnly || isSaving || isWorkspaceBusy}
                          value={shot[key]}
                        />
                      ))}
                    </div>
                  </fieldset>
                ))}
              </div>
              {saveMessage ? (
                <p className="setup-success">{saveMessage}</p>
              ) : null}
              {readOnly ? null : (
                <button
                  disabled={isSaving || isWorkspaceBusy || !analysisId}
                  type="submit"
                >
                  {isSaving ? "正在保存" : "保存镜头卡片"}
                </button>
              )}
              {firstFramePayload && shotCardVersionId ? (
                <>
                  {isSaving || shotCardsDirty ? (
                    <p className="workflow-gate-note">
                      {isSaving
                        ? "镜头卡片正在保存，请完成后再继续生成。"
                        : "镜头卡片已编辑，请保存后再继续生成。"}
                    </p>
                  ) : null}
                  <GenerationComposer
                    analysisVersionId={analysisId}
                    characterVersionId={
                      characterSelection?.character_version_id ?? null
                    }
                    currentUserId={currentUserId}
                    durationSeconds={durationSeconds}
                    firstFrameAssetId={firstFramePayload.first_frame_asset_id}
                    firstFrameSelectionVersionId={firstFrameSelection?.id ?? ""}
                    onBatchCreated={onBatchCreated}
                    onBusyChange={handleGenerationBusyChange}
                    onWorkflowStepChange={setGenerationStep}
                    originalScript={originalScript}
                    projectId={project.id}
                    readOnly={
                      readOnly || isSaving || shotCardsDirty || isWorkspaceBusy
                    }
                    referenceSelectionId={
                      characterReferenceSelection?.id ?? null
                    }
                    shotCardVersionId={shotCardVersionId}
                  />
                </>
              ) : firstFramePayload ? (
                <p className="workflow-gate-note">
                  请先保存当前镜头卡片，再确认口播稿并编译 Prompt。
                </p>
              ) : null}
            </div>
            <aside className="analysis-sidebar" aria-label="当前人物设定">
              <CharacterSelection
                onBusyChange={handleCharacterBusyChange}
                onVersionChange={handleCharacterSelectionChange}
                projectId={project.id}
                readOnly={readOnly}
              />
            </aside>
          </fieldset>
        </form>
      ) : null}
    </section>
  );
}

function providerLabel(provider: AnalysisProvider) {
  return provider === "apilio_gemini"
    ? "Gemini 3.1 Pro（Apilio）"
    : "内置模拟拆解（尚未调用 Gemini）";
}

function ShotInput({
  label,
  onChange,
  readOnly = false,
  type = "text",
  value,
}: {
  label: string;
  onChange: (value: string) => void;
  readOnly?: boolean;
  type?: "number" | "text";
  value: string;
}) {
  return (
    <label>
      {label}
      <input
        disabled={readOnly}
        min={type === "number" ? 0 : undefined}
        onChange={(event) => onChange(event.target.value)}
        step={type === "number" ? "0.1" : undefined}
        type={type}
        value={value}
      />
    </label>
  );
}
