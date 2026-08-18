import {
  type FormEvent,
  useCallback,
  useEffect,
  useMemo,
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
  type SourceFrameCharacterFeatures,
  saveShotCards,
  startVideoAnalysis,
} from "./api";
import { CharacterReferenceSelection } from "./CharacterReferenceSelection";
import { CharacterSelection } from "./CharacterSelection";
import { FirstFrameSelection } from "./FirstFrameSelection";
import { GenerationComposer } from "./GenerationComposer";
import { ScriptEditor } from "./ScriptEditor";
import { SourceFrameSelection } from "./SourceFrameSelection";
import {
  readPayloadNumber,
  readPayloadString,
  useGenerationDrafts,
} from "./useGenerationDrafts";
import {
  type ReadinessKey,
  type ReadinessMissingItem,
  useWorkspaceReadiness,
} from "./useWorkspaceReadiness";
import {
  type WorkspaceTab,
  type WorkspaceTabKey,
  WorkspaceTabs,
} from "./WorkspaceTabs";

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
  const [activeTab, setActiveTab] = useState<WorkspaceTabKey>("content");
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
  const isSavingRef = useRef(false);

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
          setError("拆解数据无效，请重新拆解。");
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

  const isWorkspaceBusy = isGenerationBusy || isUpstreamBusy;
  const draftsReadOnly =
    readOnly || isSaving || shotCardsDirty || isWorkspaceBusy;

  // P0-03-02：源画面特征预填建议（镜头卡首镜头映射；未确认时一次性预填，
  // 用户改过的字段不被覆盖，确认仍为人工动作）。
  const sourceFrameFeatureSuggestion = useMemo(() => {
    const firstShot = shots[0];
    return firstShot ? shotCardFeatureSuggestion(firstShot) : null;
  }, [shots]);

  // P0-02-03：口播稿/生成草稿状态提升至工作区，标签页①的口播稿编辑与
  // 标签页③的生成面板共享单一状态源（契约 §2）。Prompt 就绪输入待
  // P0-02-05 接入（契约 §1.2）。
  const generationDrafts = useGenerationDrafts({
    characterVersionId: characterSelection?.character_version_id ?? null,
    currentUserId,
    durationSeconds,
    firstFrameAssetId: firstFramePayload?.first_frame_asset_id ?? null,
    firstFrameSelectionVersionId: firstFrameSelection?.id ?? "",
    originalScript,
    projectId: project.id,
    readOnly: draftsReadOnly,
    referenceSelectionId: characterReferenceSelection?.id ?? null,
    shotCardVersionId,
  });

  const readiness = useWorkspaceReadiness({
    shotCard: {
      versionId: shotCardVersionId || null,
      dirty: shotCardsDirty,
      saving: isSaving,
    },
    script: {
      versionId: generationDrafts.scriptVersion?.id ?? null,
      dirty: generationDrafts.scriptDirty,
      stale: generationDrafts.scriptStale,
    },
    character: {
      versionId: characterSelection?.character_version_id ?? null,
      legacyCharacterId: characterSelection?.character_id ?? null,
    },
    sourceFrame: { selectionId: sourceFrameSelection?.id ?? null },
    characterReference: {
      selectionId: characterReferenceSelection?.id ?? null,
    },
    firstFrame: {
      selectionId: firstFrameSelection?.id ?? null,
      assetId: firstFramePayload?.first_frame_asset_id ?? null,
    },
    prompt: {
      versionId: generationDrafts.promptVersion?.id ?? null,
      status:
        generationDrafts.promptStatus === "LOCKED" ||
        generationDrafts.promptStatus === "SAVED" ||
        generationDrafts.promptStatus === "USED"
          ? generationDrafts.promptStatus
          : null,
      stale: generationDrafts.promptStale,
      outputDurationSeconds: generationDrafts.duration,
      resolution: generationDrafts.resolution,
      quantity: generationDrafts.quantity,
      limits: {
        minQuantity: generationDrafts.limits.min_quantity,
        maxQuantity: generationDrafts.limits.max_quantity,
      },
      lockedSnapshot:
        generationDrafts.promptStatus === "LOCKED"
          ? {
              outputDurationSeconds: readPayloadNumber(
                generationDrafts.promptVersion,
                "output_duration_seconds",
              ),
              resolution: readPayloadString(
                generationDrafts.promptVersion,
                "resolution",
              ),
              quantity: readPayloadNumber(
                generationDrafts.promptVersion,
                "quantity",
              ),
            }
          : null,
    },
  });

  // P0-02-05/P0-04-01：主操作栏「开始生成」——可自动补齐项（脏口播稿、
  // Prompt 未锁定）由一键流水线接手，其余缺失仍弹缺失项模态引导逐项
  // 处理；跳转后目标区块短暂高亮。
  const [isMissingModalOpen, setMissingModalOpen] = useState(false);
  const [highlightKey, setHighlightKey] = useState<ReadinessKey | null>(null);

  useEffect(() => {
    if (!highlightKey) {
      return;
    }
    const timer = window.setTimeout(() => setHighlightKey(null), 3500);
    return () => window.clearTimeout(timer);
  }, [highlightKey]);

  // 模态打开时把焦点移入关闭按钮，保证键盘用户立即落在对话框内
  // （评审 Minor：aria-modal 需配套焦点管理，完整 trap 随 P0-04 加固）。
  const missingModalCloseRef = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    if (isMissingModalOpen) {
      missingModalCloseRef.current?.focus();
    }
  }, [isMissingModalOpen]);

  // P0-04-01：流水线可自动补齐的缺失项——脏口播稿（有已保存版本且非
  // stale，保存后即可续跑）与 Prompt 未锁定/参数不一致（流水线内编译+
  // 锁定）；其余缺失（无口播稿版本、上游未确认等）仍需人工处理。
  const pipelineFixableKeys = useMemo(() => {
    const keys = new Set<ReadinessKey>(["promptLocked"]);
    const scriptBlocked = readiness.missing.some(
      (item) => item.key === "scriptVersion",
    );
    if (
      scriptBlocked &&
      generationDrafts.scriptVersion &&
      generationDrafts.scriptDirty &&
      !generationDrafts.scriptStale
    ) {
      keys.add("scriptVersion");
    }
    return keys;
  }, [readiness.missing, generationDrafts]);

  function startGenerationPipeline() {
    // 流水线反馈（错误/恢复记录）集中在标签页③的生成面板，点击后自动
    // 跳转并高亮，避免与标签页①的草稿错误双渲染。
    setActiveTab("launch");
    setHighlightKey("promptLocked");
    setMissingModalOpen(false);
    void generationDrafts.runGenerationPipeline(onBatchCreated);
  }

  function handleStartGeneration() {
    const blocked = readiness.missing.filter(
      (item) => !pipelineFixableKeys.has(item.key),
    );
    if (blocked.length === 0) {
      startGenerationPipeline();
      return;
    }
    setMissingModalOpen(true);
  }

  function handleGoFixMissing(item: ReadinessMissingItem) {
    setActiveTab(item.tab);
    setHighlightKey(item.key);
    setMissingModalOpen(false);
  }
  const contentMissingCount = readiness.missing.filter(
    (item) => item.tab === "content",
  ).length;
  const peopleMissingCount = readiness.missing.filter(
    (item) => item.tab === "people",
  ).length;
  const launchMissingCount = readiness.missing.filter(
    (item) => item.tab === "launch",
  ).length;
  const readyTabCount = [
    readiness.content,
    readiness.people,
    readiness.launch,
  ].filter(Boolean).length;

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

  // P0-02-03：草稿动作的忙态由提升后的工作区统一上报（原
  // GenerationComposer 的 busy effect 迁移至此）。
  useEffect(() => {
    handleGenerationBusyChange(Boolean(generationDrafts.busyAction));
  }, [generationDrafts.busyAction, handleGenerationBusyChange]);

  useEffect(
    () => () => {
      handleGenerationBusyChange(false);
    },
    [handleGenerationBusyChange],
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

  function handleClose() {
    if (isWorkspaceBusyRef.current) {
      return;
    }
    onClose();
  }

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

  const persistShotCards = useCallback(async (): Promise<boolean> => {
    if (readOnly || isSavingRef.current || isWorkspaceBusyRef.current) {
      return false;
    }
    if (!analysisId) {
      return false;
    }
    if (shots.some((shot) => shot.end_time < shot.start_time)) {
      setError("镜头时间无效：结束时间不能早于开始时间。");
      return false;
    }
    isSavingRef.current = true;
    setIsSaving(true);
    setError("");
    setSaveMessage("");
    try {
      const savedVersion = await saveShotCards(analysisId, shots);
      savedShotsRef.current = copyShotCards(shots);
      setShotCardVersionId(savedVersion.id);
      setShotCardsDirty(false);
      setSaveMessage(`已自动保存 · 版本 #${savedVersion.version_number}`);
      return true;
    } catch (requestError) {
      setError(
        requestError instanceof Error
          ? requestError.message
          : "保存镜头卡片失败。",
      );
      return false;
    } finally {
      isSavingRef.current = false;
      setIsSaving(false);
    }
  }, [analysisId, readOnly, shots]);

  useEffect(() => {
    if (readOnly || !analysisId || !shotCardsDirty) {
      return;
    }
    if (shots.some((shot) => shot.end_time < shot.start_time)) {
      setError("镜头时间无效：结束时间不能早于开始时间。");
      return;
    }
    const timer = window.setTimeout(() => {
      void persistShotCards();
    }, 800);
    return () => window.clearTimeout(timer);
  }, [analysisId, persistShotCards, readOnly, shotCardsDirty, shots]);

  async function handleSave(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    await persistShotCards();
  }

  const workspaceTabs: WorkspaceTab[] = [
    {
      badge: readiness.content
        ? { kind: "ready" }
        : { kind: "missing", count: contentMissingCount },
      content: (
        <div className="analysis-primary">
          <section className="stage-block" aria-label="镜头与口播">
            <header className="stage-block__head">
              <span className="stage-block__index">02</span>
              <h3>镜头与口播</h3>
            </header>
            <div
              className={highlighted(
                "shot-table-wrap",
                highlightKey === "shotCardVersion",
              )}
            >
              {/* 需求：镜头与口播改为中文表格呈现（每行一个镜头）。
                  单元格内保留编辑与自动保存；aria-label 与旧卡片一致，
                  兼容既有测试与无障碍读屏。 */}
              <table className="shot-table">
                <thead>
                  <tr>
                    <th scope="col">镜头</th>
                    <th scope="col">开始(秒)</th>
                    <th scope="col">结束(秒)</th>
                    {SHOT_TEXT_FIELDS.filter(
                      (field) => field.key !== "shot_id",
                    ).map((field) => (
                      <th key={field.key} scope="col">
                        {field.label}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {shots.map((shot, index) => (
                    <tr key={shot.shot_id}>
                      <td className="shot-table__id">
                        <ShotCellInput
                          ariaLabel={`${shot.shot_id} 镜头编号`}
                          onChange={(value) =>
                            updateShot(index, "shot_id", value)
                          }
                          readOnly={readOnly || isSaving || isWorkspaceBusy}
                          value={shot.shot_id}
                        />
                      </td>
                      <td className="shot-table__time">
                        <ShotCellInput
                          ariaLabel={`${shot.shot_id} 开始时间`}
                          onChange={(value) =>
                            updateShot(index, "start_time", value)
                          }
                          readOnly={readOnly || isSaving || isWorkspaceBusy}
                          type="number"
                          value={String(shot.start_time)}
                        />
                      </td>
                      <td className="shot-table__time">
                        <ShotCellInput
                          ariaLabel={`${shot.shot_id} 结束时间`}
                          onChange={(value) =>
                            updateShot(index, "end_time", value)
                          }
                          readOnly={readOnly || isSaving || isWorkspaceBusy}
                          type="number"
                          value={String(shot.end_time)}
                        />
                      </td>
                      {SHOT_TEXT_FIELDS.filter(
                        (field) => field.key !== "shot_id",
                      ).map(({ key, label }) => (
                        <td
                          key={key}
                          className={
                            key === "spoken_text"
                              ? "shot-table__spoken"
                              : undefined
                          }
                        >
                          <ShotCellInput
                            ariaLabel={`${shot.shot_id} ${label}`}
                            onChange={(value) => updateShot(index, key, value)}
                            readOnly={readOnly || isSaving || isWorkspaceBusy}
                            value={shot[key]}
                          />
                        </td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <div
              className={highlighted(
                "script-editor-slot",
                highlightKey === "scriptVersion",
              )}
            >
              {generationDrafts.isLoading ? (
                <p className="status-note">正在读取口播稿</p>
              ) : (
                <>
                  {/* 草稿反馈需在口播稿区可见：首帧未确认时标签页③不挂载，
                      saveScript 的守卫反馈不能只依赖生成面板渲染（P0-02-03
                      评审 Major 2）；成功反馈同理（P0-05-02 评审 C1）——生成
                      面板挂载后反馈回归③渲染，保持全文唯一副本。 */}
                  {generationDrafts.error ? (
                    <p className="settings-error">{generationDrafts.error}</p>
                  ) : null}
                  {generationDrafts.message &&
                  !(firstFramePayload && shotCardVersionId) ? (
                    <p className="setup-success">{generationDrafts.message}</p>
                  ) : null}
                  <ScriptEditor
                    busyAction={generationDrafts.busyAction}
                    onChooseSource={generationDrafts.chooseScriptSource}
                    onRewriteScript={generationDrafts.rewriteScriptWithAi}
                    onSaveScript={generationDrafts.saveScript}
                    onScriptTextChange={generationDrafts.setScriptText}
                    readOnly={draftsReadOnly}
                    scriptDirty={generationDrafts.scriptDirty}
                    scriptSource={generationDrafts.scriptSource}
                    scriptStale={generationDrafts.scriptStale}
                    scriptText={generationDrafts.scriptText}
                    shotMappings={generationDrafts.shotMappings}
                  />
                </>
              )}
            </div>
          </section>
        </div>
      ),
      key: "content",
      label: "内容配置",
    },
    {
      badge: readiness.people
        ? { kind: "ready" }
        : { kind: "missing", count: peopleMissingCount },
      content: (
        <div className="analysis-primary">
          <section className="stage-block" aria-label="画面与人物">
            <header className="stage-block__head">
              <span className="stage-block__index">01</span>
              <h3>画面与人物</h3>
            </header>
            {/* P0-02-04：四区块纵向流水线（角色 → 源画面 → 人物参考 → 首帧），
                未就绪区块用骨架 + 引导替代门禁（契约 §2.2：子组件内部不改，
                仅改布局与外围包装；stale 级联清空逻辑保持不变）。 */}
            <div className="stage-pipeline">
              <section
                aria-label="角色版本"
                className={highlighted(
                  "stage-pipeline__item",
                  highlightKey === "characterVersion",
                )}
              >
                <header className="stage-pipeline__head">
                  <span className="stage-pipeline__step">1</span>
                  <h4>角色版本</h4>
                </header>
                <CharacterSelection
                  onBusyChange={handleCharacterBusyChange}
                  onVersionChange={handleCharacterSelectionChange}
                  projectId={project.id}
                  readOnly={readOnly}
                />
              </section>
              <section
                aria-label="源画面选择"
                className={highlighted(
                  "stage-pipeline__item",
                  highlightKey === "sourceFrame",
                )}
              >
                <header className="stage-pipeline__head">
                  <span className="stage-pipeline__step">2</span>
                  <h4>源画面选择</h4>
                </header>
                {characterSelection ? (
                  <SourceFrameSelection
                    featureSuggestion={sourceFrameFeatureSuggestion}
                    onBusyChange={handleSourceFrameBusyChange}
                    onSelectionChange={handleSourceFrameSelectionChange}
                    projectId={project.id}
                    referenceAssetId={project.reference_asset_id}
                    readOnly={readOnly}
                  />
                ) : (
                  <PipelineSkeleton note="先在上方选择角色版本" />
                )}
              </section>
              <section
                aria-label="人物参考"
                className={highlighted(
                  "stage-pipeline__item",
                  highlightKey === "characterReference",
                )}
              >
                <header className="stage-pipeline__head">
                  <span className="stage-pipeline__step">3</span>
                  <h4>人物参考</h4>
                </header>
                {legacyCharacterSelected ? (
                  <p className="pipeline-note">
                    历史兼容角色无需人物参考，可直接进行首帧。
                  </p>
                ) : characterSelection?.character_version_id &&
                  sourceFrameSelection ? (
                  <CharacterReferenceSelection
                    characterSelection={characterSelection}
                    onBusyChange={handleReferenceBusyChange}
                    onSelectionChange={handleCharacterReferenceSelectionChange}
                    projectId={project.id}
                    readOnly={readOnly}
                    sourceFrameSelection={sourceFrameSelection}
                  />
                ) : (
                  <PipelineSkeleton
                    note={
                      characterSelection
                        ? "先在上方完成源画面选择"
                        : "先在上方选择角色版本"
                    }
                  />
                )}
              </section>
              <section
                aria-label="置换首帧"
                className={highlighted(
                  "stage-pipeline__item",
                  highlightKey === "firstFrame",
                )}
              >
                <header className="stage-pipeline__head">
                  <span className="stage-pipeline__step">4</span>
                  <h4>置换首帧</h4>
                </header>
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
                ) : (
                  <PipelineSkeleton note="先在上方选择角色版本" />
                )}
              </section>
            </div>
          </section>
        </div>
      ),
      key: "people",
      label: "人物设定",
    },
    {
      badge: readiness.launch
        ? { kind: "ready" }
        : { kind: "missing", count: launchMissingCount },
      content: (
        <div className="analysis-primary">
          <section
            aria-label="生成"
            className={highlighted(
              "stage-block",
              highlightKey === "promptLocked",
            )}
          >
            <header className="stage-block__head">
              <span className="stage-block__index">03</span>
              <h3>生成</h3>
            </header>
            {firstFramePayload && shotCardVersionId ? (
              <>
                {isSaving || shotCardsDirty ? (
                  <p className="workflow-gate-note">
                    {isSaving ? "镜头保存中…" : "镜头编辑自动保存中…"}
                  </p>
                ) : null}
                <GenerationComposer
                  analysisVersionId={analysisId}
                  characterVersionId={
                    characterSelection?.character_version_id ?? null
                  }
                  drafts={generationDrafts}
                  firstFrameAssetId={firstFramePayload.first_frame_asset_id}
                  firstFrameSelectionVersionId={firstFrameSelection?.id ?? ""}
                  onBatchCreated={onBatchCreated}
                  readOnly={draftsReadOnly}
                  referenceSelectionId={characterReferenceSelection?.id ?? null}
                  shotCardVersionId={shotCardVersionId}
                />
              </>
            ) : (
              <PipelineSkeleton
                note={
                  firstFramePayload
                    ? "先在「内容配置」标签页保存镜头卡片。"
                    : "先在「人物设定」标签页确认置换首帧。"
                }
              />
            )}
          </section>
        </div>
      ),
      key: "launch",
      label: "生成设置",
    },
  ];

  return (
    <section className="analysis-workspace" aria-labelledby="analysis-title">
      <div className="section-heading workspace-toolbar">
        <div className="workspace-toolbar__lead">
          <h2 id="analysis-title">复刻工作台</h2>
          {readOnly ? null : (
            <span className="workspace-toolbar__save-status" role="status">
              {isSaving
                ? "保存中…"
                : shotCardsDirty
                  ? "编辑后自动保存"
                  : saveMessage || (shotCardVersionId ? "已是最新" : "")}
            </span>
          )}
        </div>
        <div className="workspace-toolbar__actions">
          <span
            className={
              readiness.valid
                ? "workspace-toolbar__readiness workspace-toolbar__readiness--ready"
                : "workspace-toolbar__readiness"
            }
          >
            就绪 {readyTabCount}/3
          </span>
          <button
            className="secondary-button"
            disabled={isWorkspaceBusy}
            onClick={handleClose}
            type="button"
          >
            返回
          </button>
          <button
            disabled={readOnly || isWorkspaceBusy || generationDrafts.isLoading}
            onClick={handleStartGeneration}
            type="button"
          >
            开始生成
          </button>
        </div>
      </div>
      {/* P0-04-02：N>1 时在主按钮确认前展示付费提醒（文案与
          GenerationLauncher 逐条一致），单击主按钮即显式付费确认；
          N=1 为默认数量免打扰，费用在标签页③ GenerationLauncher 恒可见
          （产品决策）。容器常驻 + aria-live，避免动态挂载的 live region
          不被读屏播报。 */}
      <div
        aria-live="polite"
        className={
          generationDrafts.quantity !== null && generationDrafts.quantity > 1
            ? "paid-task-warning paid-task-warning--toolbar"
            : "paid-task-warning--toolbar paid-task-warning--toolbar--empty"
        }
      >
        {generationDrafts.quantity !== null && generationDrafts.quantity > 1 ? (
          <>
            <strong>将创建 {generationDrafts.quantity} 个付费生成任务</strong>
            <span>
              {generationDrafts.limits.estimated_cost_per_task == null
                ? "预计费用暂不可用"
                : `预计费用：¥${(
                    generationDrafts.limits.estimated_cost_per_task *
                      generationDrafts.quantity
                  ).toFixed(2)}`}
            </span>
          </>
        ) : null}
      </div>
      {isMissingModalOpen ? (
        <div
          aria-label="缺失项清单"
          aria-modal="true"
          className="missing-modal"
          onKeyDown={(event) => {
            if (event.key === "Escape") {
              setMissingModalOpen(false);
            }
          }}
          role="dialog"
        >
          <div className="missing-modal__panel">
            <h3>开始生成前需补齐以下项</h3>
            <ul className="missing-modal__list">
              {readiness.missing.map((item) => (
                <li className="missing-modal__item" key={item.key}>
                  <span>{item.label}</span>
                  <button
                    onClick={() => handleGoFixMissing(item)}
                    type="button"
                  >
                    前往处理
                  </button>
                </li>
              ))}
            </ul>
            <div className="missing-modal__actions">
              <button
                className="secondary-button"
                onClick={() => setMissingModalOpen(false)}
                ref={missingModalCloseRef}
                type="button"
              >
                关闭
              </button>
            </div>
          </div>
        </div>
      ) : null}
      {isLoading ? <p className="status-note">正在读取视频拆解</p> : null}
      {error ? <p className="settings-error">{error}</p> : null}
      {!isLoading && error && !isAnalysisMissing ? (
        <button
          className="secondary-button analysis-retry-button"
          onClick={reloadWorkspace}
          type="button"
        >
          重新加载
        </button>
      ) : null}
      {!isLoading && isAnalysisMissing ? (
        <div className="analysis-missing-state">
          <strong>待拆解</strong>
          <p>参考视频已就绪。</p>
          {readOnly ? (
            <>
              <span className="status-note">只读身份无法启动拆解。</span>
              <button
                className="secondary-button"
                onClick={reloadWorkspace}
                type="button"
              >
                重新检查
              </button>
            </>
          ) : (
            <button
              disabled={isStartingAnalysis || !project.reference_asset_id}
              onClick={handleStartAnalysis}
              type="button"
            >
              {isStartingAnalysis ? "正在拆解" : "开始拆解"}
            </button>
          )}
        </div>
      ) : null}
      {!isLoading && !error && !isAnalysisMissing ? (
        <form className="analysis-form" onSubmit={handleSave}>
          <div className="analysis-summary">
            <strong>{analysisSummary}</strong>
          </div>
          {analysisProvider === "fake_gemini" ? (
            <p className="status-note">
              演示数据 · 在设置中配置 Gemini 后可重新拆解
            </p>
          ) : null}
          {readOnly ? (
            <p className="status-note">只读身份：仅可查看。</p>
          ) : null}
          <fieldset
            aria-busy={isWorkspaceBusy}
            className="analysis-workspace-grid"
            disabled={isWorkspaceBusy}
          >
            <WorkspaceTabs
              activeKey={activeTab}
              busy={isWorkspaceBusy}
              onTabChange={setActiveTab}
              readOnly={readOnly}
              tabs={workspaceTabs}
            />
          </fieldset>
        </form>
      ) : null}
    </section>
  );
}

// P0-03-02：镜头卡首镜头 → 源画面人物特征预填建议（shot_type 中文映射
// 景别/身体完整度，朝向与可见性取口播人物常规默认）。仅作预填建议，
// 字段可改，确认仍为人工动作（任务 12 红线：不从默认值猜测落库）。
export function shotCardFeatureSuggestion(
  shot: ShotCard,
): SourceFrameCharacterFeatures {
  const text = shot.shot_type;
  if (text.includes("近景") || text.includes("特写")) {
    return {
      orientation: "FRONT",
      shot_size: "CLOSE_UP",
      face_visible: true,
      body_completeness: "FACE_ONLY",
    };
  }
  if (text.includes("全景") || text.includes("远景") || text.includes("全身")) {
    return {
      orientation: "FRONT",
      shot_size: "FULL_BODY",
      face_visible: true,
      body_completeness: "FULL_BODY",
    };
  }
  return {
    orientation: "FRONT",
    shot_size: "HALF_BODY",
    face_visible: true,
    body_completeness: "UPPER_BODY",
  };
}

// P0-02-05：缺失项跳转后的目标区块高亮类名拼接。
function highlighted(base: string, active: boolean) {
  return active ? `${base} workspace-highlight` : base;
}

// P0-02-04：未就绪区块的骨架占位——上游选择未完成时以引导文案替代门禁。
function PipelineSkeleton({ note }: { note: string }) {
  return (
    <div className="pipeline-skeleton">
      <div aria-hidden="true" className="pipeline-skeleton__bars">
        <span />
        <span />
        <span />
      </div>
      <p className="pipeline-skeleton__note">{note}</p>
    </div>
  );
}

// 表格单元格内的镜头字段输入：列名由表头统一提供，控件本身只用
// aria-label 标识（与旧卡片的可见 label 文字保持一致）。
function ShotCellInput({
  ariaLabel,
  onChange,
  readOnly = false,
  type = "text",
  value,
}: {
  ariaLabel: string;
  onChange: (value: string) => void;
  readOnly?: boolean;
  type?: "number" | "text";
  value: string;
}) {
  return (
    <input
      aria-label={ariaLabel}
      className="shot-table-input"
      disabled={readOnly}
      min={type === "number" ? 0 : undefined}
      onChange={(event) => onChange(event.target.value)}
      step={type === "number" ? "0.1" : undefined}
      type={type}
      value={value}
    />
  );
}
