import {
  type ReactNode,
  useCallback,
  useEffect,
  useRef,
  useState,
} from "react";
import {
  type AnalysisVersion,
  type CharacterReferenceSelection,
  customerVisibleErrorMessage,
  type GenerationPriceQuote,
  type GenerationRatio,
  getAssetDownloadUrl,
  getGenerationPriceQuote,
  getLatestGenerationPrompt,
  getLatestProjectAnalysis,
  getLatestProjectFirstFrameSelection,
  getLatestProjectShotCards,
  getLatestScriptRewriteTask,
  getLatestScriptVersion,
  listUserSavedPrompts,
  type Project,
  type ProjectMainCharacter,
  readAnalysisPayload,
  readFirstFrameSelectionPayload,
  rewriteProjectScript,
  type SavedPromptItem,
  type ScriptRewriteTask,
  ScriptRewriteTaskError,
  type ShotCard,
  type ShotCardPayload,
  saveGenerationPrompt,
  saveShotCards,
  selectCharacterReferences,
  startVideoAnalysis,
  waitForAnalysisTask,
  waitForScriptRewriteTask,
} from "../api";
import { CharacterSelection } from "../CharacterSelection";
import { FirstFrameSelection } from "../FirstFrameSelection";
import { SourceFrameSelection } from "../SourceFrameSelection";
import { CreationNavigation } from "./CreationNavigation";
import { useStudio } from "./context";
import {
  loadSavedScriptList,
  readAudioDuration,
  runReplicaGeneration,
  uploadOralAudioMaterial,
  uploadVideoMaterial,
  uploadWorkbenchSourceVideo,
  validateOralAudioFile,
} from "./live";
import {
  clearScriptRewriteIdempotencyKey,
  type ScriptRewriteScope,
  scriptRewriteIdempotencyKey,
  shouldClearScriptRewriteIdempotencyKey,
} from "./scriptRewrite";
import {
  buildReplicaPromptText,
  createDraft,
  DEFAULT_MAX_REFERENCE_IMAGES,
  SUPPORTED_VIDEO_RATIOS,
  validateReferenceImages,
} from "./state";
import type {
  StudioAsset,
  StudioDraft,
  StudioPerson,
  StudioScript,
  StudioTask,
  StudioVideo,
} from "./types";
import {
  Button,
  Empty,
  Field,
  Hint,
  Icon,
  Media,
  Panel,
  Tabs,
  Waveform,
} from "./ui";
import "./creation.css";

function findAsset(assets: StudioAsset[], id?: string) {
  return id ? assets.find((asset) => asset.id === id) : undefined;
}

async function getProjectAnalysisOrNull(
  projectId: string,
): Promise<AnalysisVersion | null> {
  try {
    return await getLatestProjectAnalysis(projectId);
  } catch (cause: unknown) {
    const error = cause as { code?: string; status?: number };
    if (error.status === 404 && error.code === "ANALYSIS_NOT_FOUND")
      return null;
    throw cause;
  }
}

function findSource(
  assets: StudioAsset[],
  videos: StudioVideo[],
  id?: string,
): StudioAsset | undefined {
  const asset = findAsset(assets, id);
  if (asset) return asset;
  const video = id ? videos.find((item) => item.id === id) : undefined;
  return video
    ? {
        id: video.id,
        name: video.title,
        kind: "video",
        poster: video.poster,
        duration: video.duration,
        group: video.category,
        source: video.platform,
        saved: true,
      }
    : undefined;
}

function activePerson(
  people: StudioPerson[],
  selectedId?: string,
): StudioPerson | undefined {
  return selectedId
    ? people.find((person) => person.id === selectedId)
    : undefined;
}

function SourceStrip({ source }: { source?: StudioAsset }) {
  return (
    <div className="creation-source-strip">
      <span className="creation-status-dot" />
      <span>
        来源：
        {source ? `${source.name} · ${source.source}` : "尚未选择来源视频"}
      </span>
    </div>
  );
}

function ControlGroup({
  label,
  children,
}: {
  label: string;
  children: ReactNode;
}) {
  return (
    <div className="studio-field">
      <span>{label}</span>
      {children}
    </div>
  );
}

export function CopyPage() {
  const {
    state,
    data,
    patchDraft,
    navigate,
    saveDraft,
    confirmFinalDraft,
    openLive,
    openPicker,
    patchState,
    notify,
    review,
    user,
    draftSaveStatus,
  } = useStudio();
  const readOnly = user.role === "auditor";
  const [tab, setTab] = useState<"rewrite" | "saved">("rewrite");
  const [rewriting, setRewriting] = useState(false);
  const [candidate, setCandidate] = useState("");
  const [savedLoading, setSavedLoading] = useState(false);
  const [savedError, setSavedError] = useState("");
  const savedOperation = useRef(0);
  const current = useRef({ state, patchDraft, patchState, notify });
  current.current = { state, patchDraft, patchState, notify };
  const rewritePendingRef = useRef(false);
  const rewriteOperationRef = useRef(0);
  const scopeGenerationRef = useRef(0);
  const currentRef = useRef({ state, patchDraft, notify });
  currentRef.current = { state, patchDraft, notify };
  const rewriteScopeKey = JSON.stringify([
    user.id,
    state.draft.id,
    state.draft.projectId,
    state.draft.sourceId,
    state.draft.sourceAssetId,
    state.draft.ipId,
    state.draft.script.id,
    state.draft.script.version,
    state.draft.script.title,
    state.draft.script.original,
    state.draft.script.text,
    state.draft.script.confirmed,
    state.draft.scriptEdited,
  ]);
  const activeScopeRef = useRef({ key: rewriteScopeKey, generation: 0 });
  if (activeScopeRef.current.key !== rewriteScopeKey) {
    activeScopeRef.current = {
      key: rewriteScopeKey,
      generation: ++scopeGenerationRef.current,
    };
  }
  const activeScope = activeScopeRef.current;

  const finishRewrite = useCallback(
    async (
      task: ScriptRewriteTask,
      expectedScope: { key: string; generation: number },
      operation: number,
      requestScope: ScriptRewriteScope,
      idempotencyKey: string,
    ) => {
      try {
        const completed =
          task.status === "PENDING" || task.status === "RUNNING"
            ? await waitForScriptRewriteTask(task.id)
            : task;
        if (
          rewriteOperationRef.current !== operation ||
          activeScopeRef.current !== expectedScope
        )
          return;
        const current = currentRef.current.state.draft;
        const rewritten = completed.result?.rewritten_text?.trim();
        const currentSourceAssetId = current.sourceAssetId ?? current.sourceId;
        if (
          completed.status !== "SUCCEEDED" ||
          completed.project_id !== current.projectId ||
          completed.identity_id !== current.ipId ||
          completed.source_asset_id !== currentSourceAssetId ||
          completed.source_text !== current.script.text ||
          !rewritten
        )
          throw completed.status === "FAILED" ||
            completed.status === "SUBMISSION_UNCERTAIN"
            ? new ScriptRewriteTaskError(completed)
            : new Error(
                completed.error_message || "改写未返回完整正文，请重试。",
              );
        clearScriptRewriteIdempotencyKey(requestScope, idempotencyKey);
        if (current.script.text !== requestScope.text) {
          // 改写期间用户手改了正文：保留人工稿，结果转候选待显式应用。
          setCandidate(rewritten);
          currentRef.current.notify(
            "改写已完成，当前正文保持不变；可对照后应用候选稿。",
          );
          return;
        }
        currentRef.current.patchDraft({
          script: { ...current.script, text: rewritten, confirmed: false },
          scriptEdited: true,
        });
        currentRef.current.notify("改写已完成，请核对并保存当前版本。");
      } catch (cause) {
        if (shouldClearScriptRewriteIdempotencyKey(cause)) {
          clearScriptRewriteIdempotencyKey(requestScope, idempotencyKey);
        }
        if (
          rewriteOperationRef.current === operation &&
          activeScopeRef.current === expectedScope
        )
          currentRef.current.notify(
            customerVisibleErrorMessage(cause, "文案改写失败，请重试。"),
          );
      } finally {
        if (
          rewriteOperationRef.current === operation &&
          activeScopeRef.current === expectedScope
        ) {
          rewritePendingRef.current = false;
          setRewriting(false);
        }
      }
    },
    [],
  );

  useEffect(() => {
    const operation = ++rewriteOperationRef.current;
    rewritePendingRef.current = false;
    setRewriting(false);
    setCandidate("");
    const draft = currentRef.current.state.draft;
    if (
      !review &&
      user.role !== "auditor" &&
      draft.projectId &&
      draft.sourceId &&
      draft.ipId &&
      draft.script.text.trim() &&
      draft.scriptEdited !== true
    ) {
      const sourceAssetId = draft.sourceAssetId ?? draft.sourceId;
      const requestScope: ScriptRewriteScope = {
        accountId: user.id,
        projectId: draft.projectId,
        sourceAssetId,
        identityId: draft.ipId,
        scriptId: draft.script.id,
        scriptVersion: draft.script.version,
        text: draft.script.text,
      };
      const idempotencyKey = scriptRewriteIdempotencyKey(requestScope);
      void getLatestScriptRewriteTask(
        draft.projectId,
        draft.ipId,
        sourceAssetId,
      )
        .then((task) => {
          if (
            rewriteOperationRef.current !== operation ||
            activeScopeRef.current !== activeScope ||
            !task ||
            task.source_asset_id !== sourceAssetId ||
            task.source_text !== draft.script.text
          )
            return;
          rewritePendingRef.current = true;
          setRewriting(true);
          void finishRewrite(
            task,
            activeScope,
            operation,
            requestScope,
            idempotencyKey,
          );
        })
        .catch((cause) => {
          if (
            rewriteOperationRef.current === operation &&
            activeScopeRef.current === activeScope
          )
            currentRef.current.notify(
              customerVisibleErrorMessage(
                cause,
                "读取上次改写任务失败，可直接重新提交。",
              ),
            );
        });
    }
    return () => {
      rewriteOperationRef.current += 1;
    };
  }, [activeScope, finishRewrite, review, user.id, user.role]);

  const refreshSaved = useCallback(async () => {
    const operation = ++savedOperation.current;
    setSavedLoading(true);
    setSavedError("");
    try {
      const scripts = await loadSavedScriptList();
      if (savedOperation.current === operation)
        current.current.patchState({ savedScripts: scripts });
    } catch (cause) {
      if (savedOperation.current === operation)
        setSavedError(
          customerVisibleErrorMessage(cause, "读取文案失败，请重试。"),
        );
    } finally {
      if (savedOperation.current === operation) setSavedLoading(false);
    }
  }, []);
  useEffect(() => {
    if (tab === "saved" && !review) void refreshSaved();
    return () => {
      savedOperation.current += 1;
    };
  }, [tab, review, refreshSaved]);

  const rewriteUnavailableReason = review
    ? "审核示例不调用业务接口。"
    : user.role === "auditor"
      ? "当前账号为只读权限，不能改写文案。"
      : !state.draft.projectId
        ? "当前文案缺少来源项目，暂不能按 IP 二创。"
        : !state.draft.sourceId
          ? "当前文案缺少来源视频，请重新选择来源。"
          : !state.draft.ipId
            ? "请先选择参与二创的人物 IP。"
            : !state.draft.script.text.trim()
              ? "请输入待改写正文。"
              : state.draft.script.text.length > 20_000
                ? "待改写正文不能超过 20000 字符。"
                : state.draft.scriptEdited === true
                  ? "请先保存当前编辑，再按 IP 二创。"
                  : "";

  const rewrite = async () => {
    if (rewriteUnavailableReason || rewriting || rewritePendingRef.current)
      return;
    const draft = state.draft;
    const projectId = draft.projectId;
    const identityId = draft.ipId;
    const sourceAssetId = draft.sourceAssetId ?? draft.sourceId;
    if (!projectId || !identityId || !sourceAssetId) return;
    const operation = ++rewriteOperationRef.current;
    const expectedScope = activeScopeRef.current;
    const requestScope: ScriptRewriteScope = {
      accountId: user.id,
      projectId,
      sourceAssetId,
      identityId,
      scriptId: draft.script.id,
      scriptVersion: draft.script.version,
      text: draft.script.text,
    };
    const idempotencyKey = scriptRewriteIdempotencyKey(requestScope);
    rewritePendingRef.current = true;
    setRewriting(true);
    try {
      const task = await rewriteProjectScript(
        projectId,
        draft.script.text,
        identityId,
        sourceAssetId,
        idempotencyKey,
      );
      if (
        rewriteOperationRef.current !== operation ||
        activeScopeRef.current !== expectedScope
      )
        return;
      await finishRewrite(
        task,
        expectedScope,
        operation,
        requestScope,
        idempotencyKey,
      );
    } catch (cause) {
      if (shouldClearScriptRewriteIdempotencyKey(cause)) {
        clearScriptRewriteIdempotencyKey(requestScope, idempotencyKey);
      }
      if (
        rewriteOperationRef.current === operation &&
        activeScopeRef.current === expectedScope
      ) {
        rewritePendingRef.current = false;
        setRewriting(false);
        notify(
          customerVisibleErrorMessage(cause, "提交文案改写失败，请重试。"),
        );
      }
    }
  };
  const applySavedScript = (script: StudioScript) => {
    const project = data.projects.find(
      (item) => item.id === script.sourceProjectId,
    );
    patchDraft({
      ...createDraft(),
      projectId: script.sourceProjectId,
      ipId: script.ipId,
      sourceId: project?.reference_asset_id ?? undefined,
      sourceAssetId: project?.reference_asset_id ?? undefined,
      originalImageId: undefined,
      imageId: undefined,
      firstFrameId: undefined,
      firstFrameSelectionVersionId: undefined,
      tailFrameId: undefined,
      avatarId: undefined,
      voiceId: undefined,
      audioId: undefined,
      videoBatchId: undefined,
      script: { ...script, confirmed: false },
      scriptEdited: true,
    });
    patchState({
      selectedVideoId: undefined,
      selectedTaskId: undefined,
      selectedAssetId: undefined,
      selectedPersonId: script.ipId,
      returnTo: undefined,
    });
    setTab("rewrite");
  };
  const source = findSource(
    data.assets,
    data.videos,
    state.draft.sourceId ?? state.selectedVideoId,
  );
  const person = activePerson(data.people, state.draft.ipId);
  const saved = state.savedScripts;
  const oralLengthExceeded =
    state.draft.script.text.length > 10000 ||
    state.draft.script.title.length > 120;

  return (
    <section className="creation-page creation-copy">
      <header className="creation-heading">
        <h1>文案工坊</h1>
        <p>提取与二创文案，优化表达，匹配乡墅场景</p>
        {state.returnTo && state.returnTo !== "copy" ? (
          <Button
            variant="quiet"
            onClick={() =>
              navigate(state.returnTo ?? "workbench", { returnTo: undefined })
            }
          >
            返回上一步
          </Button>
        ) : null}
      </header>
      <Tabs
        items={[
          { id: "rewrite", label: "文案改写" },
          { id: "saved", label: "我的文案" },
        ]}
        value={tab}
        onChange={(value) => setTab(value as "rewrite" | "saved")}
      />

      {tab === "saved" ? (
        <Panel className="creation-saved-list">
          {!review ? (
            <Button
              variant="outline"
              disabled={savedLoading}
              onClick={() => void refreshSaved()}
            >
              刷新文案
            </Button>
          ) : null}
          {savedLoading ? <Hint>正在读取文案…</Hint> : null}
          {savedError ? (
            <Empty
              title={savedError}
              action={
                <Button onClick={() => void refreshSaved()}>
                  重试读取文案
                </Button>
              }
            />
          ) : saved.length ? (
            saved.map((script) => (
              <button
                className="creation-script-row"
                disabled={readOnly}
                key={script.id}
                onClick={() => applySavedScript(script)}
                type="button"
              >
                <span>{script.title}</span>
                <small>{script.confirmed ? "已确认" : "草稿"}</small>
              </button>
            ))
          ) : !savedLoading ? (
            <Empty
              title="还没有保存的文案"
              description="保存当前稿后，文案会集中显示在这里；再次保存会更新同一篇。"
            />
          ) : null}
        </Panel>
      ) : (
        <>
          <SourceStrip source={source} />
          <div className="creation-copy-grid">
            <Panel className="creation-copy-source">
              <div className="creation-panel-title">原文（提取自来源）</div>
              {state.draft.script.original ? (
                <div className="creation-script-copy">
                  {state.draft.script.original}
                </div>
              ) : (
                <Empty
                  title="尚未提取文案"
                  description="可先从爆款视频或视频来源进入文案工坊。"
                  action={
                    <Button
                      variant="outline"
                      onClick={() => openLive("analysis")}
                    >
                      打开来源分析
                    </Button>
                  }
                />
              )}
            </Panel>
            <Panel className="creation-copy-editor">
              <div className="creation-panel-title-row">
                <span>二创文案（可编辑）</span>
                <small>
                  {state.draft.script.confirmed ? "终稿" : "草稿"} V
                  {state.draft.script.version}
                </small>
              </div>
              <input
                aria-label="作品名称"
                className="creation-input"
                disabled={readOnly}
                onChange={(event) =>
                  patchDraft({
                    script: {
                      ...state.draft.script,
                      title: event.target.value,
                      confirmed: false,
                    },
                  })
                }
                placeholder="作品名称"
                value={state.draft.script.title}
              />
              <textarea
                aria-label="二创文案"
                className="creation-textarea creation-copy-textarea"
                disabled={readOnly}
                onChange={(event) =>
                  patchDraft({
                    script: {
                      ...state.draft.script,
                      text: event.target.value,
                      confirmed: false,
                    },
                  })
                }
                placeholder="在这里编辑乡墅口播文案"
                value={state.draft.script.text}
              />
              <div className="creation-saved-state" aria-live="polite">
                {draftSaveStatus === "dirty"
                  ? "有未保存修改"
                  : draftSaveStatus === "saving"
                    ? "正在保存到云端…"
                    : draftSaveStatus === "saved"
                      ? "已保存到云端"
                      : draftSaveStatus === "error"
                        ? "云端保存失败，可点击保存版本重试"
                        : "内容变动后需重新确认终稿"}
              </div>
              <Hint>
                {state.draft.script.text.length} / 10000
                字符（数字人口播上限）；标题 {state.draft.script.title.length} /
                120 字符。AI 改写最多 20000 字符。
              </Hint>
              {candidate ? (
                <Panel>
                  <p>{candidate}</p>
                  <Button
                    onClick={() => {
                      patchDraft({
                        script: {
                          ...state.draft.script,
                          text: candidate,
                          confirmed: false,
                        },
                      });
                      setCandidate("");
                    }}
                  >
                    应用候选稿
                  </Button>
                </Panel>
              ) : null}
            </Panel>
            <Panel className="creation-copy-person">
              <div className="creation-panel-title">IP 选择</div>
              {person ? (
                <div className="creation-person-card">
                  <Media
                    asset={
                      person.portrait
                        ? {
                            id: person.id,
                            name: person.name,
                            kind: "image",
                            url: person.portrait,
                            group: "人物",
                            source: "人物库",
                            saved: true,
                          }
                        : undefined
                    }
                    alt={person.name}
                    className="creation-avatar"
                  />
                  <div>
                    <strong>
                      {person.name} · {person.role}
                    </strong>
                    <p>{person.scope}</p>
                    <small>{person.expression}</small>
                  </div>
                </div>
              ) : (
                <Empty
                  title="未选择人物 IP"
                  description="二创时可带入人物定位与表达方式。"
                />
              )}
              <Button
                variant="outline"
                onClick={() => void rewrite()}
                disabled={Boolean(rewriteUnavailableReason) || rewriting}
              >
                {rewriting ? "正在按 IP 二创…" : "按 IP 二创"}
              </Button>
              {rewriteUnavailableReason ? (
                <Hint>{rewriteUnavailableReason}</Hint>
              ) : null}
              <Button
                disabled={readOnly}
                variant="outline"
                onClick={() => {
                  if (readOnly) return;
                  openPicker("person");
                }}
              >
                更换人物
              </Button>
              <Button
                variant="quiet"
                onClick={() => {
                  if (readOnly) return;
                  confirmFinalDraft();
                }}
                disabled={readOnly || !state.draft.script.text.trim()}
              >
                确认终稿
              </Button>
            </Panel>
          </div>
          <footer className="creation-action-bar">
            <Button
              variant="outline"
              disabled={readOnly}
              onClick={() => {
                if (readOnly) return;
                saveDraft();
              }}
            >
              保存版本
            </Button>
            <Button
              variant="primary"
              disabled={
                !state.draft.script.confirmed || !person || oralLengthExceeded
              }
              onClick={() => navigate("oral", { returnTo: "copy" })}
            >
              用于数字人口播
            </Button>
          </footer>
        </>
      )}
    </section>
  );
}

type ReplicaStage = "source" | "analyzing" | "ready";

// 审核包样例分镜：仅 review 视觉演示，不参与真实拆解流程。
const REVIEW_SAMPLE_SHOTS: ShotCard[] = [
  {
    shot_id: "shot-1",
    start_time: 0,
    end_time: 8,
    shot_type: "中景",
    composition: "",
    camera_motion: "推进",
    subject: "院落",
    action: "镜头缓推庭院",
    scene: "乡墅庭院",
    spoken_text: "",
    transition: "切镜",
  },
  {
    shot_id: "shot-2",
    start_time: 8,
    end_time: 16,
    shot_type: "近景",
    composition: "",
    camera_motion: "固定",
    subject: "讲解人物",
    action: "人物出镜讲解",
    scene: "庭院",
    spoken_text: "这栋房子的采光设计",
    transition: "切镜",
  },
  {
    shot_id: "shot-3",
    start_time: 16,
    end_time: 24,
    shot_type: "特写",
    composition: "",
    camera_motion: "摇移",
    subject: "外立面",
    action: "外立面细节展示",
    scene: "建筑外立面",
    spoken_text: "",
    transition: "切镜",
  },
];

/** 客户档位归一：非 4/15 秒的时长映射到最近的客户可选档（≤9s→4s，>9s→15s）。 */
function normalizeCustomerDuration(seconds: number): 4 | 15 {
  return seconds === 4 || seconds === 15 ? seconds : seconds <= 9 ? 4 : 15;
}

type ReplicaQuoteInput = {
  resolution: "768P" | "2K";
  duration_seconds: 4 | 15;
  quantity: 1 | 2 | 4;
};

function replicaQuoteInput(draft: StudioDraft): ReplicaQuoteInput {
  return {
    resolution: draft.resolution === "2K" ? "2K" : "768P",
    duration_seconds: normalizeCustomerDuration(draft.duration),
    quantity: draft.count === 2 || draft.count === 4 ? draft.count : 1,
  };
}

function replicaQuoteMatches(
  quote: GenerationPriceQuote | null,
  input: ReplicaQuoteInput,
): quote is GenerationPriceQuote {
  return Boolean(
    quote &&
      quote.resolution === input.resolution &&
      quote.duration_seconds === input.duration_seconds &&
      quote.quantity === input.quantity &&
      quote.estimated_seconds === input.duration_seconds * input.quantity,
  );
}

export function ReplicaPage() {
  const {
    state,
    data,
    review,
    patchDraft,
    updateData,
    navigate,
    notify,
    saveDraft,
    openLive,
    user,
  } = useStudio();
  const readOnly = user.role === "auditor";
  const project = data.projects.find(
    (item) => item.id === state.draft.projectId,
  );
  const source = findSource(
    data.assets,
    data.videos,
    state.draft.sourceId ?? state.selectedVideoId,
  );
  const [stage, setStage] = useState<ReplicaStage>(() =>
    state.draft.projectId ? "ready" : "source",
  );
  const [shots, setShots] = useState<ShotCard[]>([]);
  const [shotCardVersionId, setShotCardVersionId] = useState<string>();
  const [originalScript, setOriginalScript] = useState("");
  const [analysisBusy, setAnalysisBusy] = useState(false);
  const [promptText, setPromptText] = useState(state.draft.prompt);
  const [promptNameOpen, setPromptNameOpen] = useState(false);
  const [promptName, setPromptName] = useState("");
  const [savingPrompt, setSavingPrompt] = useState(false);
  const [generating, setGenerating] = useState(false);
  const [replicaQuote, setReplicaQuote] = useState<GenerationPriceQuote | null>(
    null,
  );
  const [replicaQuoteStatus, setReplicaQuoteStatus] = useState<
    "idle" | "loading" | "ready" | "error"
  >("idle");
  const [replicaQuoteError, setReplicaQuoteError] = useState("");
  const [replicaQuoteRevision, setReplicaQuoteRevision] = useState(0);
  const [restoreBusy, setRestoreBusy] = useState(false);
  const [restoreError, setRestoreError] = useState("");
  const uploadInputRef = useRef<HTMLInputElement>(null);
  const uploadOperationRef = useRef(0);
  const uploadAbortRef = useRef<AbortController | null>(null);
  const restoreOperationRef = useRef(0);
  const restoredProjectIdRef = useRef<string | undefined>(undefined);
  const restoreSuppressedRef = useRef(false);
  const promptSaveOperationRef = useRef(0);
  const promptEditVersionRef = useRef(0);
  // 拆解完成回调用：比对发起时的项目，防止换视频后的旧结果覆盖新状态。
  const analysisProjectRef = useRef<string | undefined>(undefined);
  const promptTextRef = useRef(promptText);
  const promptEditedRef = useRef(
    state.draft.projectId === project?.id && state.draft.promptEdited === true,
  );
  const promptTypedThisMountRef = useRef(false);
  const latestDraftRef = useRef(state.draft);
  const patchDraftRef = useRef(patchDraft);
  const replicaSubmittingRef = useRef(false);
  const replicaOperationRef = useRef(0);
  const replicaContextRef = useRef("");
  const replicaSubmissionRef = useRef<{
    fingerprint: string;
    key: string;
  } | null>(null);
  replicaContextRef.current = JSON.stringify({
    draftId: state.draft.id,
    projectId: state.draft.projectId,
    sourceId: state.draft.sourceId,
    shotCardVersionId,
    promptText,
    originalScript,
    shots,
    scriptText: state.draft.script.text,
    scriptConfirmed: state.draft.script.confirmed,
    duration: state.draft.duration,
    resolution: state.draft.resolution,
    count: state.draft.count,
    ratio: state.draft.ratio,
  });
  if (
    !promptTypedThisMountRef.current &&
    state.draft.projectId === project?.id &&
    state.draft.promptEdited === true
  ) {
    promptTextRef.current = state.draft.prompt;
    promptEditedRef.current = true;
  } else {
    promptTextRef.current = promptText;
  }
  latestDraftRef.current = state.draft;
  patchDraftRef.current = patchDraft;
  useEffect(
    () => () => {
      uploadOperationRef.current += 1;
      uploadAbortRef.current?.abort();
      restoreOperationRef.current += 1;
      promptSaveOperationRef.current += 1;
      restoredProjectIdRef.current = undefined;
      restoreSuppressedRef.current = false;
    },
    [],
  );

  const resetReplicaState = () => {
    setShots([]);
    setShotCardVersionId(undefined);
    setOriginalScript("");
  };

  const restoreSavedProject = useCallback(
    async (target: Project, force = false) => {
      if (restoreSuppressedRef.current) return;
      if (!force && restoredProjectIdRef.current === target.id) return;
      const operation = ++restoreOperationRef.current;
      const promptVersionAtStart = promptEditVersionRef.current;
      restoredProjectIdRef.current = target.id;
      setRestoreBusy(true);
      setRestoreError("");
      try {
        const [shotVersion, analysisVersion, promptState, scriptState] =
          await Promise.all([
            getLatestProjectShotCards(target.id),
            getProjectAnalysisOrNull(target.id),
            getLatestGenerationPrompt(target.id),
            getLatestScriptVersion(target.id),
          ]);
        if (operation !== restoreOperationRef.current) return;

        const shotPayload = shotVersion
          ? (shotVersion.payload as ShotCardPayload)
          : null;
        const restoredShots = shotPayload?.shots ?? [];
        const analysis = analysisVersion
          ? readAnalysisPayload(analysisVersion)
          : null;
        const original = analysis?.original_script ?? "";
        const savedPromptText = promptState.version?.payload.prompt_text;
        const savedPrompt =
          !promptState.stale && typeof savedPromptText === "string"
            ? savedPromptText
            : "";
        const prompt =
          savedPrompt || buildReplicaPromptText(restoredShots, original);
        const savedScriptText = scriptState.version?.payload.full_text;
        const savedScript =
          !scriptState.stale && typeof savedScriptText === "string"
            ? scriptState.version
            : null;
        const currentDraft = latestDraftRef.current;
        const keepLocalDraft = currentDraft.projectId === target.id;
        const keepLocalScript =
          keepLocalDraft && currentDraft.scriptEdited === true;
        const keepLocalPrompt =
          keepLocalDraft &&
          (currentDraft.promptEdited === true ||
            promptEditedRef.current ||
            promptEditVersionRef.current !== promptVersionAtStart);
        const promptStillEdited =
          keepLocalDraft &&
          (currentDraft.promptEdited === true || promptEditedRef.current);
        const blankScript = createDraft().script;
        const script = keepLocalScript
          ? currentDraft.script
          : {
              ...blankScript,
              id: savedScript?.id ?? blankScript.id,
              title: target.name,
              original,
              text: savedScript
                ? (savedScript.payload.full_text as string)
                : original,
              version: savedScript?.version_number ?? 1,
              confirmed: false,
            };
        const restoredPrompt = keepLocalPrompt ? promptTextRef.current : prompt;

        setShots(restoredShots);
        setShotCardVersionId(shotVersion?.id || undefined);
        setOriginalScript(original);
        setPromptText(restoredPrompt);
        promptTextRef.current = restoredPrompt;
        patchDraftRef.current({
          projectId: target.id,
          sourceId: target.reference_asset_id ?? undefined,
          sourceAssetId: target.reference_asset_id ?? undefined,
          prompt: restoredPrompt,
          promptEdited: promptStillEdited,
          script,
          scriptEdited: keepLocalScript,
        });
        setStage("ready");
      } catch (cause: unknown) {
        if (operation !== restoreOperationRef.current) return;
        restoredProjectIdRef.current = undefined;
        setRestoreError(
          customerVisibleErrorMessage(cause, "历史分镜读取失败，请重试。"),
        );
      } finally {
        if (operation === restoreOperationRef.current) setRestoreBusy(false);
      }
    },
    [],
  );

  useEffect(() => {
    if (
      promptTypedThisMountRef.current ||
      state.draft.projectId !== project?.id ||
      state.draft.promptEdited !== true
    )
      return;
    promptTextRef.current = state.draft.prompt;
    promptEditedRef.current = true;
    setPromptText(state.draft.prompt);
  }, [
    project?.id,
    state.draft.projectId,
    state.draft.prompt,
    state.draft.promptEdited,
  ]);

  useEffect(() => {
    if (review || !project) return;
    void restoreSavedProject(project);
  }, [project, restoreSavedProject, review]);

  useEffect(
    () => () => {
      replicaOperationRef.current += 1;
    },
    [],
  );

  const replicaProjectId = state.draft.projectId;
  const replicaDuration = normalizeCustomerDuration(state.draft.duration);
  const replicaResolution: ReplicaQuoteInput["resolution"] =
    state.draft.resolution === "2K" ? "2K" : "768P";
  const replicaQuantity: ReplicaQuoteInput["quantity"] =
    state.draft.count === 2 || state.draft.count === 4 ? state.draft.count : 1;
  const currentReplicaQuoteInput: ReplicaQuoteInput = {
    resolution: replicaResolution,
    duration_seconds: replicaDuration,
    quantity: replicaQuantity,
  };
  const replicaQuoteReady =
    replicaQuoteStatus === "ready" &&
    replicaQuoteMatches(replicaQuote, currentReplicaQuoteInput);
  useEffect(() => {
    void replicaQuoteRevision;
    if (
      review ||
      stage !== "ready" ||
      !replicaProjectId ||
      !shotCardVersionId ||
      shots.length === 0
    ) {
      setReplicaQuote(null);
      setReplicaQuoteStatus("idle");
      setReplicaQuoteError("");
      return;
    }
    let active = true;
    const input = {
      resolution: replicaResolution,
      duration_seconds: replicaDuration,
      quantity: replicaQuantity,
    };
    setReplicaQuote(null);
    setReplicaQuoteStatus("loading");
    setReplicaQuoteError("");
    void getGenerationPriceQuote(input)
      .then((quote) => {
        if (!active) return;
        if (!replicaQuoteMatches(quote, input)) {
          setReplicaQuoteStatus("error");
          setReplicaQuoteError(
            "复刻报价参数与当前生成参数不一致，请重新获取。",
          );
          return;
        }
        setReplicaQuote(quote);
        setReplicaQuoteStatus("ready");
      })
      .catch((cause: unknown) => {
        if (!active) return;
        setReplicaQuote(null);
        setReplicaQuoteStatus("error");
        setReplicaQuoteError(
          customerVisibleErrorMessage(cause, "复刻报价读取失败，请重试。"),
        );
      });
    return () => {
      active = false;
    };
  }, [
    review,
    stage,
    replicaProjectId,
    replicaDuration,
    replicaResolution,
    replicaQuantity,
    shotCardVersionId,
    shots.length,
    replicaQuoteRevision,
  ]);

  const retryReplicaQuote = useCallback(
    () => setReplicaQuoteRevision((value) => value + 1),
    [],
  );

  const handleUpload = async (file: File) => {
    if (review || readOnly) {
      notify("审核示例不上传视频。");
      return;
    }
    const operation = ++uploadOperationRef.current;
    promptSaveOperationRef.current += 1;
    setSavingPrompt(false);
    uploadAbortRef.current?.abort();
    restoreSuppressedRef.current = true;
    setRestoreError("");
    const abortController = new AbortController();
    uploadAbortRef.current = abortController;
    notify("正在上传参考视频…");
    try {
      const uploaded = await uploadWorkbenchSourceVideo(
        file,
        (percent) => {
          if (operation === uploadOperationRef.current)
            notify(`参考视频上传中 ${percent}%`);
        },
        abortController.signal,
      );
      if (operation !== uploadOperationRef.current) return;
      restoreOperationRef.current += 1;
      setRestoreBusy(false);
      resetReplicaState();
      setPromptText("");
      promptTextRef.current = "";
      promptEditedRef.current = false;
      promptTypedThisMountRef.current = false;
      const blankScript = {
        ...createDraft().script,
        title: uploaded.project?.name ?? file.name,
      };
      patchDraft({
        projectId: uploaded.projectId,
        sourceId: uploaded.assetId,
        sourceAssetId: uploaded.assetId,
        prompt: "",
        promptEdited: false,
        script: blankScript,
        scriptEdited: false,
      });
      if (uploaded.project || uploaded.asset) {
        updateData((current) => ({
          ...current,
          projects: uploaded.project
            ? [
                uploaded.project,
                ...current.projects.filter(
                  (project) => project.id !== uploaded.project?.id,
                ),
              ]
            : current.projects,
          assets: uploaded.asset
            ? [
                uploaded.asset,
                ...current.assets.filter(
                  (asset) => asset.id !== uploaded.asset?.id,
                ),
              ]
            : current.assets,
        }));
      }
      restoredProjectIdRef.current = uploaded.projectId;
      restoreSuppressedRef.current = false;
      setStage("ready");
      notify("参考视频已上传，点击「启动 AI 拆解」反推分镜与提示词。");
    } catch {
      if (operation !== uploadOperationRef.current) return;
      restoreSuppressedRef.current = false;
      notify("参考视频上传失败，请稍后重试。");
    }
  };

  const selectExistingProject = (selectedId: string) => {
    const selected = data.projects.find((item) => item.id === selectedId);
    if (!selected) {
      return;
    }
    uploadOperationRef.current += 1;
    promptSaveOperationRef.current += 1;
    setSavingPrompt(false);
    uploadAbortRef.current?.abort();
    restoreSuppressedRef.current = false;
    restoreOperationRef.current += 1;
    restoredProjectIdRef.current = undefined;
    setRestoreBusy(false);
    resetReplicaState();
    setPromptText("");
    promptTextRef.current = "";
    promptEditedRef.current = false;
    promptTypedThisMountRef.current = false;
    setRestoreError("");
    const blankScript = { ...createDraft().script, title: selected.name };
    patchDraft({
      projectId: selected.id,
      sourceId: selected.reference_asset_id ?? undefined,
      sourceAssetId: selected.reference_asset_id ?? undefined,
      prompt: "",
      promptEdited: false,
      script: blankScript,
      scriptEdited: false,
    });
    setStage("ready");
    void restoreSavedProject(selected, true).then(() => {
      if (restoredProjectIdRef.current === selected.id)
        notify(`已载入项目「${selected.name}」的历史分镜，可继续编辑。`);
    });
  };

  const startAnalysis = async () => {
    if (review || readOnly) {
      notify("审核示例不调用真实接口。");
      return;
    }
    const projectId = state.draft.projectId;
    const assetId =
      state.draft.sourceAssetId ?? project?.reference_asset_id ?? null;
    if (!projectId || !assetId) {
      notify("请先上传或选择来源视频。");
      return;
    }
    restoreOperationRef.current += 1;
    restoredProjectIdRef.current = projectId;
    restoreSuppressedRef.current = false;
    setRestoreBusy(false);
    setRestoreError("");
    analysisProjectRef.current = projectId;
    setAnalysisBusy(true);
    setStage("analyzing");
    notify("AI 拆解进行中，约需一到数分钟，请保持页面打开…");
    try {
      const task = await startVideoAnalysis(projectId, assetId);
      await waitForAnalysisTask(task.id);
      if (analysisProjectRef.current !== projectId) {
        return; // 等待期间用户更换了来源视频，丢弃旧项目的拆解结果。
      }
      const analysisVersion = await getLatestProjectAnalysis(projectId).catch(
        () => undefined,
      );
      const analysisShots: ShotCard[] = analysisVersion
        ? (readAnalysisPayload(analysisVersion)?.shots ?? [])
        : [];
      const script = analysisVersion
        ? (readAnalysisPayload(analysisVersion)?.original_script ?? "")
        : "";
      // 拆解任务只写 analysis 版本；shot_card 版本由客户端落库（与成熟工作区一致）。
      let shotVersion = await getLatestProjectShotCards(projectId).catch(
        () => null,
      );
      const shotPayload = shotVersion
        ? (shotVersion.payload as ShotCardPayload)
        : null;
      const shotsFresh =
        shotPayload &&
        shotPayload.source_analysis_version_id === analysisVersion?.id;
      if (!shotsFresh) {
        shotVersion = await saveShotCards(
          analysisVersion?.id ?? "",
          analysisShots,
        );
      }
      const finalShots = shotVersion
        ? ((shotVersion.payload as ShotCardPayload).shots ?? [])
        : analysisShots;
      setShots(finalShots);
      setShotCardVersionId(shotVersion?.id || undefined);
      setOriginalScript(script);
      const text = buildReplicaPromptText(finalShots, script);
      if (!promptTextRef.current.trim()) {
        setPromptText(text);
        patchDraft({ prompt: text, promptEdited: false });
      }
      setStage("ready");
      setAnalysisBusy(false);
      notify("拆解完成：分镜与 Prompt 已生成，可编辑后保存或送生成。");
    } catch (cause: unknown) {
      setAnalysisBusy(false);
      setStage("ready");
      notify(customerVisibleErrorMessage(cause, "AI 拆解失败，请稍后重试。"));
    }
  };

  const saveAsCustomPrompt = async () => {
    if (review || readOnly) {
      notify("审核示例不调用真实接口。");
      return;
    }
    const projectId = state.draft.projectId;
    if (!projectId) {
      notify("请先上传或选择来源视频。");
      return;
    }
    setSavingPrompt(true);
    const operation = ++promptSaveOperationRef.current;
    const editVersion = promptEditVersionRef.current;
    const submittedPrompt = promptTextRef.current;
    try {
      await saveGenerationPrompt(projectId, {
        name:
          promptName.trim() ||
          `复刻提示词 ${new Date().toLocaleDateString("zh-CN")}`,
        prompt_text: submittedPrompt,
      });
      const stillCurrent =
        operation === promptSaveOperationRef.current &&
        editVersion === promptEditVersionRef.current &&
        latestDraftRef.current.projectId === projectId &&
        promptTextRef.current === submittedPrompt;
      if (!stillCurrent) {
        if (latestDraftRef.current.projectId === projectId)
          notify("提交时的 Prompt 已保存，当前修改仍需再次保存。");
        return;
      }
      promptEditVersionRef.current += 1;
      promptEditedRef.current = false;
      promptTypedThisMountRef.current = false;
      latestDraftRef.current = {
        ...latestDraftRef.current,
        promptEdited: false,
      };
      patchDraftRef.current({ promptEdited: false });
      notify("已保存到我的提示词，视频生成页可直接导入。");
      setPromptNameOpen(false);
    } catch (cause: unknown) {
      if (operation === promptSaveOperationRef.current)
        notify(
          customerVisibleErrorMessage(
            cause,
            "保存自定义提示词失败，请稍后重试。",
          ),
        );
    } finally {
      if (operation === promptSaveOperationRef.current) setSavingPrompt(false);
    }
  };

  const sendToGeneration = async () => {
    if (review || readOnly) {
      notify("审核示例不调用真实接口。");
      return;
    }
    if (replicaSubmittingRef.current) return;
    const operation = replicaOperationRef.current + 1;
    replicaOperationRef.current = operation;
    const contextFingerprint = replicaContextRef.current;
    const isCurrent = () =>
      replicaOperationRef.current === operation &&
      replicaContextRef.current === contextFingerprint;
    const draft = latestDraftRef.current;
    const quoteInput = replicaQuoteInput(draft);
    if (
      replicaQuoteStatus !== "ready" ||
      !replicaQuoteMatches(replicaQuote, quoteInput)
    ) {
      notify("请先取得与当前参数一致的复刻报价后再提交。");
      return;
    }
    const projectId = draft.projectId;
    if (!projectId || !shotCardVersionId) {
      notify("请先完成 AI 拆解。");
      return;
    }
    replicaSubmittingRef.current = true;
    setGenerating(true);
    try {
      const selection = await getLatestProjectFirstFrameSelection(projectId);
      const firstFrameAssetId =
        selection.version && !selection.stale
          ? (readFirstFrameSelectionPayload(selection.version)
              ?.first_frame_asset_id ?? null)
          : null;
      if (!isCurrent()) return;
      if (!firstFrameAssetId) {
        notify(
          "还没有确认过的置换首帧：请先到「人物置换」生成并确认首帧，再回来送生成。",
        );
        return;
      }
      const scriptFallback =
        originalScript.trim() ||
        shots
          .map((shot) => shot.spoken_text)
          .filter(Boolean)
          .join(" ") ||
        "纯画面叙事，无口播。";
      const request = {
        promptText,
        originalScriptText: scriptFallback,
        confirmedScriptText: draft.script.confirmed
          ? draft.script.text
          : undefined,
        shotCardVersionId,
        firstFrameAssetId,
        outputDurationSeconds: quoteInput.duration_seconds,
        resolution: quoteInput.resolution,
        ratio: (SUPPORTED_VIDEO_RATIOS as readonly string[]).includes(
          draft.ratio,
        )
          ? (draft.ratio as GenerationRatio)
          : "adaptive",
        quantity: quoteInput.quantity,
      };
      const fingerprint = JSON.stringify({ request, replicaQuote });
      if (replicaSubmissionRef.current?.fingerprint !== fingerprint) {
        replicaSubmissionRef.current = {
          fingerprint,
          key: crypto.randomUUID(),
        };
      }
      await runReplicaGeneration(projectId, {
        ...request,
        idempotencyKey: replicaSubmissionRef.current.key,
        isCurrent,
      });
      replicaSubmissionRef.current = null;
      if (!isCurrent()) return;
      notify("复刻任务已提交，可在任务中心查看进度。");
      navigate("tasks");
    } catch (cause: unknown) {
      if (isCurrent()) {
        notify(customerVisibleErrorMessage(cause, "送生成失败，请稍后重试。"));
      }
    } finally {
      replicaSubmittingRef.current = false;
      setGenerating(false);
    }
  };

  const displayShots =
    shots.length > 0
      ? shots
      : review && stage === "ready"
        ? REVIEW_SAMPLE_SHOTS
        : [];
  const hasShots = stage === "ready" && displayShots.length > 0;

  return (
    <section className="creation-page creation-replica">
      <header className="creation-heading">
        <h1>视频复刻</h1>
      </header>
      <CreationNavigation />
      {stage === "source" && !project ? (
        <Panel className="creation-empty-workspace">
          <Empty
            title="先导入参考视频"
            description="上传本地视频后由 AI 拆解分镜并反推提示词；也可以选择已有项目直接续作。"
            action={
              <div className="creation-upload-row">
                <Button
                  disabled={readOnly}
                  variant="primary"
                  onClick={() => uploadInputRef.current?.click()}
                >
                  上传参考视频
                </Button>
                {data.projects.length > 0 && (
                  <select
                    aria-label="选择已有项目"
                    className="creation-project-select"
                    defaultValue=""
                    disabled={readOnly}
                    onChange={(event) => {
                      if (event.target.value) {
                        selectExistingProject(event.target.value);
                      }
                    }}
                  >
                    <option value="" disabled>
                      选择已有项目
                    </option>
                    {data.projects.map((item) => (
                      <option key={item.id} value={item.id}>
                        {item.name}
                      </option>
                    ))}
                  </select>
                )}
              </div>
            }
          />
        </Panel>
      ) : (
        <>
          <SourceStrip source={source} />
          <Panel className="creation-replica-analyze">
            <div className="creation-panel-title-row">
              <span>
                来源视频 ·{" "}
                {project?.name ?? state.draft.projectId ?? "未命名项目"}
              </span>
              {displayShots.length > 0 && (
                <small>已拆解 {displayShots.length} 个镜头</small>
              )}
            </div>
            <div className="creation-upload-row">
              <Button
                variant="primary"
                disabled={readOnly || analysisBusy}
                onClick={() => void startAnalysis()}
              >
                {analysisBusy
                  ? "AI 拆解进行中…"
                  : displayShots.length > 0
                    ? "重新拆解"
                    : "启动 AI 拆解"}
              </Button>
              <Button
                disabled={readOnly || analysisBusy}
                variant="outline"
                onClick={() => uploadInputRef.current?.click()}
              >
                更换来源视频
              </Button>
            </div>
            <Hint>
              拆解会反推分镜与提示词；上传新视频会创建新项目，历史任务不受影响。
            </Hint>
            <Hint>
              离开页面会保留当前工作区草稿；重新打开时会同时读取该项目已保存的版本。
            </Hint>
            {restoreBusy && <Hint>正在读取已保存的分镜、文案和 Prompt…</Hint>}
            {restoreError && (
              <div className="creation-inline-error" role="alert">
                <span>{restoreError}</span>
                <Button
                  variant="outline"
                  onClick={() => {
                    if (project) void restoreSavedProject(project, true);
                  }}
                >
                  重试读取历史分镜
                </Button>
              </div>
            )}
          </Panel>
          {hasShots && (
            <Panel className="creation-shot-list">
              <div className="creation-panel-title">
                分镜（共 {displayShots.length} 个）
              </div>
              {displayShots.map((shot, index) => (
                <div className="creation-shot-row" key={shot.shot_id}>
                  <span className="creation-shot-index">
                    {String(index + 1).padStart(2, "0")}
                  </span>
                  <span className="creation-shot-body">
                    <strong>
                      {shot.start_time.toFixed(1)}s–{shot.end_time.toFixed(1)}s
                      · {shot.shot_type || "未标注景别"}
                    </strong>
                    <small>
                      {[
                        shot.subject && `主体：${shot.subject}`,
                        shot.action && `动作：${shot.action}`,
                        shot.spoken_text && `台词：${shot.spoken_text}`,
                      ]
                        .filter(Boolean)
                        .join(" ｜ ")}
                    </small>
                  </span>
                </div>
              ))}
            </Panel>
          )}
          <Panel className="creation-prompt-output">
            <div className="creation-panel-title-row">
              <span>拆解 Prompt（可编辑）</span>
              {displayShots.length === 0 && <small>完成拆解后自动生成</small>}
            </div>
            <textarea
              aria-label="拆解 Prompt"
              disabled={readOnly}
              className="creation-textarea"
              onChange={(event) => {
                setPromptText(event.target.value);
                promptTextRef.current = event.target.value;
                promptEditedRef.current = true;
                promptTypedThisMountRef.current = true;
                promptEditVersionRef.current += 1;
                patchDraft({
                  prompt: event.target.value,
                  promptEdited: true,
                });
              }}
              placeholder="完成 AI 拆解后，这里会生成逐镜头的反推提示词；也可手动撰写。"
              rows={10}
              value={promptText}
            />
            <div className="creation-upload-row">
              {promptNameOpen ? (
                <>
                  <input
                    aria-label="自定义提示词名称"
                    className="creation-project-select"
                    disabled={readOnly}
                    onChange={(event) => setPromptName(event.target.value)}
                    placeholder="提示词名称"
                    value={promptName}
                  />
                  <Button
                    disabled={readOnly || savingPrompt}
                    onClick={() => void saveAsCustomPrompt()}
                    variant="primary"
                  >
                    {savingPrompt ? "保存中…" : "确认保存"}
                  </Button>
                  <Button
                    onClick={() => setPromptNameOpen(false)}
                    variant="quiet"
                  >
                    取消
                  </Button>
                </>
              ) : (
                <>
                  <Button
                    disabled={readOnly}
                    onClick={() => setPromptNameOpen(true)}
                    variant="outline"
                  >
                    保存为自定义提示词
                  </Button>
                  {replicaQuoteStatus === "loading" ? (
                    <Hint>正在读取复刻报价…</Hint>
                  ) : null}
                  {replicaQuoteError ? (
                    <div className="settings-error" role="alert">
                      <p>{replicaQuoteError}</p>
                      <Button onClick={retryReplicaQuote} variant="outline">
                        重新获取复刻报价
                      </Button>
                    </div>
                  ) : null}
                  {replicaQuoteReady ? (
                    <Hint>
                      预计费用{" "}
                      {(replicaQuote.estimated_price_fen / 100).toFixed(2)} 元
                      （{replicaQuote.unit_price_fen_per_second} 分/秒 ×{" "}
                      {replicaQuote.estimated_seconds} 秒）
                    </Hint>
                  ) : null}
                  <Button
                    disabled={
                      readOnly ||
                      generating ||
                      analysisBusy ||
                      displayShots.length === 0 ||
                      !replicaQuoteReady
                    }
                    onClick={() => void sendToGeneration()}
                    variant="primary"
                  >
                    {generating ? "提交中…" : "确认费用并送生成"}
                  </Button>
                </>
              )}
            </div>
            <Hint>
              编辑后的 Prompt
              可保存为自定义提示词（视频生成页可导入）；「送生成」需要项目已有确认首帧，未确认时请先到人物置换页完成。
            </Hint>
            {state.draft.script.confirmed && state.draft.script.text.trim() ? (
              <Hint>
                送生成将使用文案工坊已确认的终稿重新编译
                Prompt，替换原片台词；当前自定义 Prompt 仍保留在编辑区。
              </Hint>
            ) : null}
          </Panel>
        </>
      )}
      <footer className="creation-action-bar">
        <div>
          <strong>分镜、Prompt 与生成批次能力已在本页打通</strong>
          <Hint>需要逐镜头精修可进入成熟分镜工作区。</Hint>
        </div>
        <Button
          variant="outline"
          disabled={readOnly}
          onClick={() => {
            if (readOnly) return;
            saveDraft();
          }}
        >
          保存草稿
        </Button>
        <Button variant="outline" onClick={() => openLive("analysis")}>
          进入分镜工作区
        </Button>
      </footer>
      <input
        accept="video/mp4,video/quicktime"
        aria-label="上传参考视频"
        hidden
        onChange={(event) => {
          const file = event.target.files?.[0];
          if (file) void handleUpload(file);
          event.target.value = "";
        }}
        ref={uploadInputRef}
        type="file"
      />
    </section>
  );
}

export function ReplacementPage() {
  const { state, data, review, patchDraft, navigate, notify, saveDraft, user } =
    useStudio();
  const readOnly = user.role === "auditor";
  const project = data.projects.find(
    (item) => item.id === state.draft.projectId,
  );
  const [characterSelection, setCharacterSelection] =
    useState<ProjectMainCharacter | null>(null);
  const [sourceFrameSelection, setSourceFrameSelection] =
    useState<AnalysisVersion | null>(null);
  const [referenceSelection, setReferenceSelection] =
    useState<CharacterReferenceSelection | null>(null);
  const [referenceError, setReferenceError] = useState("");
  const [referenceMatching, setReferenceMatching] = useState(false);
  const [referenceMatchRevision, setReferenceMatchRevision] = useState(0);
  const [referenceInputRevision, setReferenceInputRevision] = useState(0);
  const [firstFrameSelection, setFirstFrameSelection] =
    useState<AnalysisVersion | null>(null);
  const [leafBusy, setLeafBusy] = useState(false);
  const [sourceDurationSeconds, setSourceDurationSeconds] = useState<
    number | null
  >(null);
  const referenceMatchInFlightRef = useRef<string | undefined>(undefined);
  const referenceRetryScheduledRef = useRef(false);
  const readOnlyRef = useRef(readOnly);
  readOnlyRef.current = readOnly;
  const characterVersionIdRef = useRef<string | null | undefined>(undefined);
  const sourceFrameSelectionIdRef = useRef<string | undefined>(undefined);
  const referenceMatchPromiseRef = useRef<
    | {
        key: string;
        promise: ReturnType<typeof selectCharacterReferences>;
      }
    | undefined
  >(undefined);
  // patchDraft 每次壳层渲染都是新引用，effect 依赖一律走 ref，避免无限置位循环。
  const patchDraftRef = useRef(patchDraft);
  patchDraftRef.current = patchDraft;
  const confirmedSelectionKeyRef = useRef<string | undefined>(undefined);

  const firstFrameAssetId = firstFrameSelection
    ? (readFirstFrameSelectionPayload(firstFrameSelection)
        ?.first_frame_asset_id ?? null)
    : null;

  const clearConfirmedFirstFrame = useCallback(() => {
    if (readOnlyRef.current) return;
    confirmedSelectionKeyRef.current = undefined;
    patchDraftRef.current({
      firstFrameId: undefined,
      firstFrameSelectionVersionId: undefined,
      frameConfirmed: false,
    } as Partial<StudioDraft>);
  }, []);

  // 换项目时重置全部下游状态与草稿中的旧首帧。
  const projectId = project?.id;
  useEffect(() => {
    setCharacterSelection(null);
    setSourceFrameSelection(null);
    setReferenceSelection(null);
    setFirstFrameSelection(null);
    setReferenceError("");
    setReferenceMatching(false);
    referenceMatchInFlightRef.current = undefined;
    referenceRetryScheduledRef.current = false;
    characterVersionIdRef.current = undefined;
    sourceFrameSelectionIdRef.current = undefined;
    confirmedSelectionKeyRef.current = undefined;
    if (projectId && !readOnlyRef.current) {
      patchDraftRef.current({
        firstFrameId: undefined,
        firstFrameSelectionVersionId: undefined,
        frameConfirmed: false,
      } as Partial<StudioDraft>);
    }
  }, [projectId]);

  // 载入拆解时长供源画面取帧边界使用（缺省时叶子组件退化为固定前 2.5 秒）。
  useEffect(() => {
    if (review || !projectId) {
      setSourceDurationSeconds(null);
      return;
    }
    let active = true;
    void getLatestProjectAnalysis(projectId)
      .then((version) => {
        if (active) {
          setSourceDurationSeconds(
            readAnalysisPayload(version)?.duration_seconds ?? null,
          );
        }
      })
      .catch(() => {
        if (active) setSourceDurationSeconds(null);
      });
    return () => {
      active = false;
    };
  }, [review, projectId]);

  const characterVersionId = characterSelection?.character_version_id ?? "";
  const sourceFrameSelectionId = sourceFrameSelection?.id ?? "";
  const referenceSelectionId = referenceSelection?.id ?? "";

  // 人物参考自动匹配：仅业务输入或明确重试变化时重新请求。
  useEffect(() => {
    if (
      review ||
      readOnly ||
      !projectId ||
      !characterVersionId ||
      !sourceFrameSelectionId ||
      referenceSelectionId
    ) {
      setReferenceMatching(false);
      referenceRetryScheduledRef.current = false;
      return;
    }
    const matchKey = `${projectId}:${characterVersionId}:${sourceFrameSelectionId}`;
    const requestKey = `${matchKey}:${referenceInputRevision}:${referenceMatchRevision}`;
    let active = true;
    referenceMatchInFlightRef.current = requestKey;
    setReferenceMatching(true);
    const existing = referenceMatchPromiseRef.current;
    const promise =
      existing?.key === requestKey
        ? existing.promise
        : selectCharacterReferences(projectId, {
            character_version_id: characterVersionId,
            source_frame_selection_version_id: sourceFrameSelectionId,
          });
    referenceMatchPromiseRef.current = { key: requestKey, promise };
    promise
      .then((selection) => {
        if (active) {
          setReferenceSelection(selection);
          setReferenceError("");
        }
      })
      .catch((cause: unknown) => {
        if (active) {
          setReferenceError(
            cause instanceof Error ? cause.message : "自动匹配人物参考失败。",
          );
        }
      })
      .finally(() => {
        if (!active) return;
        if (referenceMatchInFlightRef.current === requestKey)
          referenceMatchInFlightRef.current = undefined;
        referenceRetryScheduledRef.current = false;
        setReferenceMatching(false);
      });
    return () => {
      active = false;
      if (referenceMatchInFlightRef.current === requestKey) {
        referenceMatchInFlightRef.current = undefined;
        referenceRetryScheduledRef.current = false;
      }
    };
  }, [
    review,
    readOnly,
    projectId,
    characterVersionId,
    sourceFrameSelectionId,
    referenceSelectionId,
    referenceInputRevision,
    referenceMatchRevision,
  ]);

  // 叶子组件的 effect 依赖回调身份：必须 useCallback 保持稳定，否则引发重取风暴。
  const handleCharacterChange = useCallback(
    (selection: ProjectMainCharacter | null) => {
      const nextVersionId = selection?.character_version_id;
      if (characterVersionIdRef.current === nextVersionId) return;
      characterVersionIdRef.current = nextVersionId;
      setCharacterSelection(selection);
      setReferenceInputRevision((revision) => revision + 1);
      setReferenceSelection(null);
      setReferenceError("");
      setFirstFrameSelection(null);
      clearConfirmedFirstFrame();
    },
    [clearConfirmedFirstFrame],
  );

  const handleSourceFrameChange = useCallback(
    (selection: AnalysisVersion | null) => {
      const nextSelectionId = selection?.id;
      if (sourceFrameSelectionIdRef.current === nextSelectionId) return;
      sourceFrameSelectionIdRef.current = nextSelectionId;
      setSourceFrameSelection(selection);
      setReferenceInputRevision((revision) => revision + 1);
      setReferenceSelection(null);
      setReferenceError("");
      setFirstFrameSelection(null);
      clearConfirmedFirstFrame();
    },
    [clearConfirmedFirstFrame],
  );

  const handleFirstFrameChange = useCallback(
    (selection: AnalysisVersion | null) => {
      setFirstFrameSelection(selection);
      if (readOnlyRef.current) return;
      if (!selection) {
        clearConfirmedFirstFrame();
        return;
      }
      const assetId =
        readFirstFrameSelectionPayload(selection)?.first_frame_asset_id;
      if (!assetId) {
        clearConfirmedFirstFrame();
        return;
      }
      const selectionKey = `${selection.id}:${assetId}`;
      if (confirmedSelectionKeyRef.current === selectionKey) return;
      confirmedSelectionKeyRef.current = selectionKey;
      patchDraftRef.current({
        firstFrameId: assetId,
        firstFrameSelectionVersionId: selection.id,
        frameConfirmed: true,
      });
    },
    [clearConfirmedFirstFrame],
  );

  const retryReferenceMatch = () => {
    if (
      readOnly ||
      referenceMatching ||
      referenceMatchInFlightRef.current ||
      referenceRetryScheduledRef.current
    )
      return;
    referenceRetryScheduledRef.current = true;
    setReferenceError("");
    setReferenceMatchRevision((revision) => revision + 1);
  };

  const firstFrameReady = Boolean(firstFrameAssetId);

  return (
    <section className="creation-page">
      <header className="creation-heading">
        <h1>人物替换</h1>
      </header>
      <CreationNavigation />
      {!project ? (
        <Panel className="creation-empty-workspace">
          <Empty
            title="先在视频复刻中准备好项目"
            description="人物替换需要一个已完成 AI 拆解的项目：选择下方项目即可开始提取源画面并生成置换首帧。"
            action={
              data.projects.length > 0 ? (
                <select
                  aria-label="选择项目"
                  className="creation-project-select"
                  defaultValue=""
                  disabled={readOnly}
                  onChange={(event) => {
                    if (readOnly) return;
                    const selected = data.projects.find(
                      (item) => item.id === event.target.value,
                    );
                    if (selected) {
                      patchDraft({ projectId: selected.id });
                      notify(`已切换到项目「${selected.name}」。`);
                    }
                  }}
                >
                  <option value="" disabled>
                    选择已有项目
                  </option>
                  {data.projects
                    .filter((item) => item.analysis_status === "READY")
                    .map((item) => (
                      <option key={item.id} value={item.id}>
                        {item.name}
                      </option>
                    ))}
                </select>
              ) : undefined
            }
          />
        </Panel>
      ) : review ? (
        <Panel className="creation-empty-workspace">
          <Empty
            title="人物替换（审核示例）"
            description="真实模式将在此完成：确认源画面 → 匹配 IP 五视图参考 → 生成并确认置换首帧。"
          />
        </Panel>
      ) : (
        <>
          <Panel className="creation-replacement-step">
            <div className="creation-panel-title">① 选择人物 IP</div>
            <CharacterSelection
              onBusyChange={setLeafBusy}
              onVersionChange={handleCharacterChange}
              projectId={project.id}
              readOnly={readOnly}
              variant="inline"
            />
          </Panel>
          <Panel className="creation-replacement-step">
            <div className="creation-panel-title">② 提取并确认源画面</div>
            <SourceFrameSelection
              onBusyChange={setLeafBusy}
              onSelectionChange={handleSourceFrameChange}
              projectId={project.id}
              readOnly={readOnly}
              referenceAssetId={project.reference_asset_id}
              simplified
              videoDurationSeconds={sourceDurationSeconds}
            />
          </Panel>
          <Panel className="creation-replacement-step">
            <div className="creation-panel-title">③ 生成置换首帧</div>
            {referenceError ? (
              <>
                <p className="settings-error" role="alert">
                  {referenceError}
                </p>
                <Button
                  disabled={readOnly || referenceMatching}
                  onClick={retryReferenceMatch}
                  variant="outline"
                >
                  重试匹配人物参考
                </Button>
              </>
            ) : null}
            {characterSelection && sourceFrameSelection ? (
              <FirstFrameSelection
                onBusyChange={setLeafBusy}
                onSelectionChange={handleFirstFrameChange}
                projectId={project.id}
                readOnly={readOnly}
                referenceSelection={referenceSelection}
                simplified
                sourceFrameSelectionId={sourceFrameSelection.id}
              />
            ) : (
              <Empty
                title="等待前置步骤"
                description="确认人物与源画面后，即可结合 IP 五视图生成置换首帧。"
              />
            )}
          </Panel>
          <Panel className="creation-replacement-step">
            <div className="creation-panel-title">④ 用于视频生成</div>
            {firstFrameReady ? (
              <>
                <p>
                  置换首帧已确认并写入当前创作草稿，可在「视频生成」中作为首帧图生视频。
                </p>
                <Button
                  disabled={leafBusy || referenceMatching}
                  variant="primary"
                  onClick={() => navigate("video")}
                >
                  用于文/图生视频
                </Button>
              </>
            ) : (
              <Hint>
                完成上方置换首帧确认后，这里会提供一键跳转视频生成的入口。
              </Hint>
            )}
          </Panel>
        </>
      )}
      <footer className="creation-action-bar">
        <div>
          <strong>
            源画面 + IP 五视图 → 置换首帧，全流程复用后端已实现链路
          </strong>
          <Hint>确认后的首帧可直接用于文/图生视频。</Hint>
        </div>
        <Button
          variant="outline"
          disabled={readOnly}
          onClick={() => {
            if (readOnly) return;
            saveDraft();
          }}
        >
          保存草稿
        </Button>
      </footer>
    </section>
  );
}

const ratios = ["自动", "21:9", "16:9", "4:3", "1:1", "3:4", "9:16"];
const assetKindNames: Record<StudioAsset["kind"], string> = {
  image: "图片",
  video: "视频",
  audio: "音频",
};

function ParameterControls() {
  const { state, patchDraft, user } = useStudio();
  const readOnly = user.role === "auditor";
  const draft = state.draft;
  return (
    <div className="creation-parameters">
      <ControlGroup label="分辨率">
        <div className="creation-segmented">
          {["768P", "2K"].map((resolution) => (
            <button
              className={draft.resolution === resolution ? "active" : ""}
              disabled={readOnly}
              key={resolution}
              onClick={() => patchDraft({ resolution })}
              type="button"
            >
              {resolution}
            </button>
          ))}
        </div>
      </ControlGroup>
      <ControlGroup label={`时长 ${draft.duration} 秒`}>
        <input
          aria-label="时长"
          disabled={readOnly}
          max={15}
          min={4}
          onChange={(event) =>
            patchDraft({ duration: Number(event.target.value) })
          }
          type="range"
          value={draft.duration}
        />
      </ControlGroup>
      <ControlGroup label="画面比例">
        <div className="creation-ratios">
          {ratios.map((ratio) => (
            <button
              className={draft.ratio === ratio ? "active" : ""}
              disabled={readOnly}
              key={ratio}
              onClick={() => patchDraft({ ratio })}
              type="button"
            >
              {ratio}
            </button>
          ))}
        </div>
      </ControlGroup>
      <ControlGroup label="生成数量">
        <div className="creation-segmented">
          {[1, 2, 4].map((count) => (
            <button
              className={draft.count === count ? "active" : ""}
              disabled={readOnly}
              key={count}
              onClick={() => patchDraft({ count })}
              type="button"
            >
              {count}个
            </button>
          ))}
        </div>
      </ControlGroup>
    </div>
  );
}

// 生成等待期的安抚文案：按阶段轮换，降低等待焦虑（不伪造进度）。
const REASSURANCE_COPY = [
  "AI 正在理解你的提示词与画面结构…",
  "视频生成通常需要 1–3 分钟，可以先去处理其他创作。",
  "任务已进入公平队列，关掉页面也不会丢失进度。",
  "生成完成后可以直接在下方预览成片。",
];

const VIDEO_STAGE_LABELS = ["已提交", "排队中", "生成中", "完成"] as const;

function videoStageIndex(status: StudioTask["status"]): number {
  if (status === "queued") return 1;
  if (status === "running") return 2;
  if (status === "completed") return 3;
  return 0;
}

function formatElapsed(from: string): string {
  // 服务端 CURRENT_TIMESTAMP 是 UTC 文本；补 Z 防止按本地时区解析出巨幅偏差。
  const started = new Date(`${from.replace(" ", "T")}Z`).getTime();
  if (Number.isNaN(started)) return "";
  const seconds = Math.max(0, Math.round((Date.now() - started) / 1000));
  const minutes = Math.floor(seconds / 60);
  return minutes > 0 ? `${minutes} 分 ${seconds % 60} 秒` : `${seconds} 秒`;
}

/** 生成等待视图：阶段时间线 + 进度百分比 + 轮换安抚文案。 */
function VideoProgressView({ task }: { task: StudioTask }) {
  const [copyIndex, setCopyIndex] = useState(0);
  const [, setTick] = useState(0);
  const failed = task.status === "failed" || task.status === "uncertain";

  useEffect(() => {
    if (failed) return;
    const timer = window.setInterval(() => {
      setCopyIndex((value) => (value + 1) % REASSURANCE_COPY.length);
      setTick((value) => value + 1);
    }, 6000);
    return () => window.clearInterval(timer);
  }, [failed]);

  if (failed) {
    return (
      <div className="creation-progress failed" role="alert">
        <div className="creation-progress-headline">
          生成未完成{task.status === "uncertain" ? "（状态待确认）" : ""}
        </div>
        <p className="creation-progress-copy">
          积分未结算的失败不会扣费；可在任务中心重试或对账。
        </p>
        <Empty
          title="这条视频没有生成成功"
          description="可回到上方调整提示词或素材后重新生成。"
        />
      </div>
    );
  }

  const stageIndex = videoStageIndex(task.status);
  const progress = task.progress ?? 0;

  return (
    <div className="creation-progress" aria-live="polite">
      <ol className="creation-progress-stages">
        {VIDEO_STAGE_LABELS.map((label, index) => (
          <li
            key={label}
            className={
              index === stageIndex
                ? "is-active"
                : index < stageIndex
                  ? "is-done"
                  : ""
            }
          >
            {label}
          </li>
        ))}
      </ol>
      <progress
        aria-label="生成进度"
        className="creation-progress-bar"
        max={100}
        value={task.status === "completed" ? 100 : progress}
      />
      <div className="creation-progress-meta">
        <span>{task.status === "completed" ? "生成完成" : `${progress}%`}</span>
        <span>已等待 {formatElapsed(task.submitted)}</span>
      </div>
      <p className="creation-progress-copy">{REASSURANCE_COPY[copyIndex]}</p>
    </div>
  );
}

function SavedPromptImporter({
  onImport,
}: {
  onImport: (promptText: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const [prompts, setPrompts] = useState<SavedPromptItem[]>();
  const [error, setError] = useState<string>();
  const { review, notify, user } = useStudio();
  const readOnly = user.role === "auditor";

  const toggle = () => {
    if (readOnly) return;
    const next = !open;
    setOpen(next);
    if (next && prompts === undefined && !error && !review) {
      void listUserSavedPrompts()
        .then(setPrompts)
        .catch(() => setError("我的提示词暂时读取失败，请稍后重试。"));
    }
  };

  return (
    <div className="creation-prompt-import">
      <Button variant="quiet" disabled={readOnly} onClick={toggle}>
        <Icon name="arrow" size={16} /> 导入提示词
      </Button>
      {open && (
        <div
          className="creation-prompt-list"
          role="listbox"
          aria-label="我的提示词"
        >
          {review ? (
            <p>审核示例不提供提示词库。</p>
          ) : error ? (
            <p>{error}</p>
          ) : prompts === undefined ? (
            <p>正在读取我的提示词…</p>
          ) : prompts.length === 0 ? (
            <p>
              还没有保存过提示词。在项目拆解中修订反推提示词并保存后会出现在这里。
            </p>
          ) : (
            prompts.map((item) => (
              <button
                key={item.id}
                type="button"
                onClick={() => {
                  onImport(item.prompt_text);
                  notify(`已导入「${item.name}」，可继续修改。`);
                  setOpen(false);
                }}
              >
                <strong>{item.name}</strong>
                <small>{item.prompt_text.slice(0, 60)}</small>
              </button>
            ))
          )}
        </div>
      )}
    </div>
  );
}

function VideoMaterialUpload({
  disabled = false,
  group,
  label,
  onUploaded,
}: {
  disabled?: boolean;
  group: string;
  label: string;
  onUploaded: (asset: StudioAsset) => void;
}) {
  const inputRef = useRef<HTMLInputElement>(null);
  const [progress, setProgress] = useState<number>();
  const { review, notify, user } = useStudio();
  const readOnly = user.role === "auditor";
  const onUploadedRef = useRef(onUploaded);
  const mountedRef = useRef(false);
  onUploadedRef.current = onUploaded;

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  const upload = async (file: File) => {
    if (readOnly) return;
    if (!["image/png", "image/jpeg"].includes(file.type)) {
      notify("仅支持 PNG 或 JPEG 图片。");
      return;
    }
    if (review) {
      notify("审核示例不上传素材。");
      return;
    }
    setProgress(0);
    try {
      const asset = await uploadVideoMaterial(file, group, setProgress);
      if (mountedRef.current) {
        onUploadedRef.current(asset);
        notify(`${label}「${file.name}」已上传到素材库。`);
      }
    } catch {
      if (mountedRef.current) notify("素材上传失败，请稍后重试。");
    } finally {
      if (mountedRef.current) setProgress(undefined);
    }
  };

  return (
    <>
      <button
        className="creation-upload-mini"
        disabled={readOnly || disabled || progress !== undefined}
        onClick={() => inputRef.current?.click()}
        type="button"
      >
        {progress !== undefined ? `上传中 ${progress}%` : "本机上传"}
      </button>
      <input
        accept="image/png,image/jpeg"
        aria-label={`上传${label}`}
        disabled={readOnly || disabled}
        hidden
        onChange={(event) => {
          const file = event.target.files?.[0];
          if (file) void upload(file);
          event.target.value = "";
        }}
        ref={inputRef}
        type="file"
      />
    </>
  );
}

export function VideoPage() {
  const {
    state,
    data,
    patchDraft,
    navigate,
    openPicker,
    saveDraft,
    requestGeneration,
    updateData,
    notify,
    review,
    videoCapabilities,
    videoCapabilitiesStatus,
    retryVideoCapabilities,
    referenceAssetsPending,
    referenceAssetsError,
    retryReferenceAssets,
    user,
  } = useStudio();
  const readOnly = user.role === "auditor";
  const referenceMode = state.page === "reference";
  const storedFirstFrame =
    findAsset(data.assets, state.draft.firstFrameId) ??
    findAsset(data.materials, state.draft.firstFrameId);
  const [resolvedFirstFrame, setResolvedFirstFrame] = useState<StudioAsset>();
  const [firstFrameLoading, setFirstFrameLoading] = useState(false);
  const [firstFrameError, setFirstFrameError] = useState("");
  const [firstFrameLoadAttempt, setFirstFrameLoadAttempt] = useState(0);
  const firstFrameRequestRef = useRef(0);
  const firstFramePromiseRef = useRef<
    | {
        key: string;
        promise: ReturnType<typeof getAssetDownloadUrl>;
      }
    | undefined
  >(undefined);
  const firstFrameId = state.draft.firstFrameId;
  const usableStoredFirstFrame =
    storedFirstFrame?.kind === "image" && storedFirstFrame.url
      ? storedFirstFrame
      : undefined;
  const firstFrame =
    usableStoredFirstFrame ??
    (resolvedFirstFrame?.id === firstFrameId ? resolvedFirstFrame : undefined);

  useEffect(() => {
    const requestId = ++firstFrameRequestRef.current;
    if (referenceMode || review || !firstFrameId || usableStoredFirstFrame) {
      setResolvedFirstFrame(undefined);
      setFirstFrameLoading(false);
      setFirstFrameError("");
      return;
    }

    setResolvedFirstFrame(undefined);
    setFirstFrameLoading(true);
    setFirstFrameError("");
    const key = `${firstFrameId}:${firstFrameLoadAttempt}`;
    const existing = firstFramePromiseRef.current;
    const promise =
      existing?.key === key
        ? existing.promise
        : getAssetDownloadUrl(firstFrameId);
    firstFramePromiseRef.current = { key, promise };

    void promise
      .then(({ url }) => {
        if (firstFrameRequestRef.current !== requestId) return;
        const asset: StudioAsset = {
          ...storedFirstFrame,
          id: firstFrameId,
          assetId: storedFirstFrame?.assetId ?? firstFrameId,
          name: storedFirstFrame?.name ?? "已确认置换首帧",
          kind: "image",
          url,
          group: storedFirstFrame?.group ?? "置换首帧",
          source: storedFirstFrame?.source ?? "人物置换",
          saved: true,
          delivery: storedFirstFrame?.delivery ?? "stored",
        };
        setResolvedFirstFrame(asset);
        setFirstFrameLoading(false);
        updateData((previous) => ({
          ...previous,
          assets: [
            asset,
            ...previous.assets.filter((item) => item.id !== asset.id),
          ],
        }));
      })
      .catch((cause: unknown) => {
        if (firstFrameRequestRef.current !== requestId) return;
        setFirstFrameLoading(false);
        setFirstFrameError(
          customerVisibleErrorMessage(cause, "首帧预览读取失败，请重试。"),
        );
      });

    return () => {
      if (firstFrameRequestRef.current === requestId)
        firstFrameRequestRef.current += 1;
    };
  }, [
    firstFrameId,
    firstFrameLoadAttempt,
    referenceMode,
    review,
    storedFirstFrame,
    updateData,
    usableStoredFirstFrame,
  ]);
  const tailFrame =
    findAsset(data.assets, state.draft.tailFrameId) ??
    findAsset(data.materials, state.draft.tailFrameId);
  const referenceValidation = validateReferenceImages(
    state.draft.referenceIds,
    [...data.assets, ...data.materials],
    videoCapabilities?.max_reference_images ?? DEFAULT_MAX_REFERENCE_IMAGES,
  );
  const references = referenceValidation.images;
  const effectiveCapabilitiesStatus = review
    ? "ready"
    : (videoCapabilitiesStatus ?? (videoCapabilities ? "ready" : "loading"));
  const referenceCapabilityPending =
    referenceMode && effectiveCapabilitiesStatus === "loading";
  const referenceCapabilityError =
    referenceMode && effectiveCapabilitiesStatus === "error";
  const referenceModeDisabled =
    referenceMode && !review && videoCapabilities?.r2v_enabled === false;
  const referenceHasIssues =
    !referenceAssetsPending &&
    !referenceAssetsError &&
    referenceValidation.issues.length > 0;
  const referenceAtLimit =
    references.length >= referenceValidation.limit && !referenceHasIssues;
  const ready =
    Boolean(state.draft.prompt.trim()) &&
    (referenceMode
      ? references.length > 0 &&
        !referenceCapabilityPending &&
        !referenceCapabilityError &&
        !referenceModeDisabled &&
        !referenceHasIssues &&
        !referenceAssetsPending &&
        !referenceAssetsError
      : !firstFrameId || Boolean(firstFrame));
  const videoTask = state.draft.videoBatchId
    ? data.tasks.find((task) => task.id === state.draft.videoBatchId)
    : undefined;

  const appendMaterial = (asset: StudioAsset) => {
    updateData((previous) => ({
      ...previous,
      materials: [
        asset,
        ...previous.materials.filter((item) => item.id !== asset.id),
      ],
    }));
  };

  const addReference = (asset: StudioAsset) => {
    appendMaterial(asset);
    if (asset.kind !== "image") {
      notify("参考图仅支持图片，请重新选择。");
      return;
    }
    if (referenceHasIssues) {
      notify("请先整理旧草稿中的无效参考素材。");
      return;
    }
    if (state.draft.referenceIds.includes(asset.id)) {
      notify("该参考图已选择，请勿重复添加。");
      return;
    }
    if (referenceAtLimit) {
      notify(`当前最多选择 ${referenceValidation.limit} 张参考图。`);
      return;
    }
    patchDraft({
      referenceIds: [...state.draft.referenceIds, asset.id],
    });
  };

  return (
    <section className="creation-page">
      <header className="creation-heading">
        <h1>{referenceMode ? "视频生成-参考生视频" : "视频生成-文图生视频"}</h1>
      </header>
      <CreationNavigation />
      <Tabs
        items={[
          { id: "video", label: "文/图生视频" },
          { id: "reference", label: "参考生视频" },
        ]}
        value={referenceMode ? "reference" : "video"}
        onChange={(value) =>
          navigate(value === "reference" ? "reference" : "video")
        }
      />
      <div
        className={`creation-video-grid ${referenceMode ? "reference" : ""}`}
      >
        <Panel className="creation-video-form">
          <Field label="提示词">
            <textarea
              aria-label="提示词"
              className="creation-textarea"
              disabled={readOnly}
              onChange={(event) => patchDraft({ prompt: event.target.value })}
              placeholder="描述镜头、场景、运动与光线"
              value={state.draft.prompt}
            />
            <SavedPromptImporter
              onImport={(promptText) => patchDraft({ prompt: promptText })}
            />
          </Field>
          {referenceMode ? (
            <ControlGroup label="参考素材">
              <div className="creation-upload-row">
                <button
                  className="creation-upload"
                  disabled={
                    readOnly ||
                    referenceCapabilityPending ||
                    referenceCapabilityError ||
                    referenceModeDisabled ||
                    referenceHasIssues ||
                    referenceAssetsPending ||
                    referenceAssetsError ||
                    referenceAtLimit
                  }
                  onClick={() => openPicker("reference")}
                  type="button"
                >
                  <Icon name="upload" />
                  <span>从素材库选择</span>
                  <small>
                    {referenceAtLimit
                      ? `已选 ${references.length}/${referenceValidation.limit} 张参考图，需移除后才能继续添加。`
                      : `本批生成最多 ${referenceValidation.limit} 张参考图`}
                  </small>
                </button>
                <VideoMaterialUpload
                  key={`reference-upload-${state.draft.id}`}
                  disabled={
                    readOnly ||
                    referenceCapabilityPending ||
                    referenceCapabilityError ||
                    referenceModeDisabled ||
                    referenceHasIssues ||
                    referenceAssetsPending ||
                    referenceAssetsError ||
                    referenceAtLimit
                  }
                  group="参考素材"
                  label="参考图"
                  onUploaded={addReference}
                />
              </div>
              {referenceCapabilityPending && (
                <p className="settings-error" role="status">
                  正在读取参考生视频能力，请稍候。
                </p>
              )}
              {referenceCapabilityError && (
                <div>
                  <p className="settings-error" role="alert">
                    视频生成能力读取失败，请重试。
                  </p>
                  <Button onClick={retryVideoCapabilities} variant="outline">
                    重试读取视频能力
                  </Button>
                </div>
              )}
              {referenceAssetsPending && (
                <p className="settings-error" role="status">
                  正在恢复草稿参考图，请稍候。
                </p>
              )}
              {referenceAssetsError && (
                <div>
                  <p className="settings-error" role="alert">
                    草稿参考图读取失败，请重试。
                  </p>
                  <Button onClick={retryReferenceAssets} variant="outline">
                    重试读取草稿参考图
                  </Button>
                </div>
              )}
              {referenceModeDisabled && (
                <p className="settings-error" role="alert">
                  参考生视频当前未开放，请等待能力开启后再提交。
                </p>
              )}
              {!referenceAssetsPending &&
                !referenceAssetsError &&
                referenceValidation.issues.map((issue) => (
                  <p className="settings-error" key={issue} role="alert">
                    {issue}
                  </p>
                ))}
              {referenceHasIssues && (
                <Button
                  disabled={readOnly}
                  onClick={() =>
                    !readOnly &&
                    patchDraft({ referenceIds: referenceValidation.repairIds })
                  }
                  variant="outline"
                >
                  整理参考图
                </Button>
              )}
              <div className="creation-reference-list">
                {references.map((asset, index) => (
                  <div className="creation-reference-row" key={asset.id}>
                    <Media asset={asset} alt={asset.name} />
                    <span className="creation-reference-copy">
                      <strong>
                        @{index + 1} {asset.name}
                      </strong>
                      <small>
                        {assetKindNames[asset.kind]} · {asset.source}
                      </small>
                    </span>
                    <Button
                      aria-label={`移除 ${asset.name}`}
                      className="creation-reference-remove"
                      disabled={readOnly}
                      onClick={() =>
                        patchDraft({
                          referenceIds: state.draft.referenceIds.filter(
                            (id) => id !== asset.id,
                          ),
                        })
                      }
                      variant="quiet"
                    >
                      <Icon name="close" size={18} />
                    </Button>
                  </div>
                ))}
              </div>
            </ControlGroup>
          ) : (
            <ControlGroup label="首尾帧">
              <Hint>无首帧时文生视频；添加首帧后图生视频。</Hint>
              <div className="creation-frame-row">
                <div className="creation-frame-slot">
                  <button
                    disabled={readOnly}
                    onClick={() => openPicker("first-frame")}
                    type="button"
                  >
                    <Media asset={firstFrame} alt="首帧" />
                    <span>首帧（选填）</span>
                  </button>
                  <VideoMaterialUpload
                    group="首帧素材"
                    label="首帧"
                    onUploaded={(asset) => {
                      appendMaterial(asset);
                      patchDraft({ firstFrameId: asset.id });
                    }}
                  />
                </div>
                <Icon name="arrow" />
                <div className="creation-frame-slot">
                  <button
                    disabled={readOnly}
                    onClick={() => openPicker("tail-frame")}
                    type="button"
                  >
                    <Media asset={tailFrame} alt="尾帧" />
                    <span>尾帧（可选）</span>
                  </button>
                  <VideoMaterialUpload
                    group="尾帧素材"
                    label="尾帧"
                    onUploaded={(asset) => {
                      appendMaterial(asset);
                      patchDraft({ tailFrameId: asset.id });
                    }}
                  />
                </div>
              </div>
            </ControlGroup>
          )}
        </Panel>
        <Panel className="creation-video-controls">
          <div className="creation-panel-title">参数设置</div>
          <ParameterControls />
          <div className="creation-form-actions">
            <Button
              variant="outline"
              disabled={readOnly}
              onClick={() => {
                if (readOnly) return;
                saveDraft();
              }}
            >
              保存草稿
            </Button>
            <Button
              variant="primary"
              disabled={readOnly || !ready}
              onClick={() => requestGeneration("视频生成")}
            >
              生成视频
            </Button>
          </div>
          <Hint>提交前确认费用；生成结果进入任务中心。</Hint>
        </Panel>
        <Panel className="creation-video-preview">
          <div className="creation-panel-title">
            预览（{referenceMode ? "参考画布" : "首帧预览"}）
          </div>
          {!referenceMode && firstFrameLoading ? (
            <Empty
              title="正在加载首帧预览"
              description="正在读取已确认置换首帧的签名地址。"
            />
          ) : !referenceMode && firstFrameError ? (
            <Empty
              title="首帧预览加载失败"
              description={firstFrameError}
              action={
                <Button
                  variant="outline"
                  onClick={() =>
                    setFirstFrameLoadAttempt((attempt) => attempt + 1)
                  }
                >
                  重试加载首帧
                </Button>
              }
            />
          ) : videoTask ? (
            <VideoProgressView task={videoTask} />
          ) : referenceMode ? (
            references.length ? (
              <Media
                asset={references[0]}
                alt="参考画布"
                className="creation-preview-media"
              />
            ) : (
              <Empty
                title="还没有参考素材"
                description="设置参考素材与参数后再生成视频。"
              />
            )
          ) : firstFrame ? (
            <Media
              asset={firstFrame}
              alt="首帧预览"
              className="creation-preview-media"
            />
          ) : (
            <Empty
              title="当前为文生视频"
              description="添加首帧后会在这里显示图生预览。"
            />
          )}
          {videoTask && (
            <Hint>成片与历史进度可在任务中心查看，任务记录不会丢失。</Hint>
          )}
        </Panel>
      </div>
    </section>
  );
}

function PersonIdentity({ person }: { person?: StudioPerson }) {
  const { openPicker, user } = useStudio();
  const readOnly = user.role === "auditor";
  return (
    <div className="creation-identity-row">
      <span>人物 IP</span>
      <strong>{person ? `${person.name} · ${person.role}` : "未选择"}</strong>
      <Button
        variant="outline"
        disabled={readOnly}
        onClick={() => openPicker("person")}
      >
        更换 IP
      </Button>
    </div>
  );
}

function avatarDisplayName(person: StudioPerson | undefined, name: string) {
  if (!person || name.startsWith(person.name)) return name;
  return `${person.name} · ${name}`;
}

export function OralPage() {
  const {
    state,
    data,
    patchDraft,
    updateData,
    navigate,
    openPicker,
    saveDraft,
    requestGeneration,
    notify,
    review,
    user,
  } = useStudio();
  const readOnly = user.role === "auditor";
  const audioUploadInputRef = useRef<HTMLInputElement>(null);
  const audioUploadAbortRef = useRef<AbortController | undefined>(undefined);
  const audioUploadOperationRef = useRef(0);
  const [audioUploadProgress, setAudioUploadProgress] = useState<number>();
  const audioMode = state.page === "oral-audio";
  const person = activePerson(data.people, state.draft.ipId);
  const avatar = person?.avatars.find(
    (item) => item.id === state.draft.avatarId && item.ready,
  );
  const voice = person?.voices.find(
    (item) => item.id === state.draft.voiceId && item.confirmed,
  );
  const audio = findAsset(data.assets, state.draft.audioId);
  const speechAudio =
    audio?.kind === "audio" &&
    (!audio.allowedUses || audio.allowedUses.includes("oral_audio"))
      ? audio
      : undefined;
  const avatarImage = findAsset(data.assets, avatar?.imageId);
  const ready = audioMode
    ? Boolean(person && avatar && speechAudio)
    : Boolean(
        person &&
          avatar &&
          voice &&
          state.draft.script.confirmed &&
          state.draft.script.text.trim(),
      );

  useEffect(() => {
    if (audioMode) return;
    audioUploadOperationRef.current += 1;
    audioUploadAbortRef.current?.abort();
    audioUploadAbortRef.current = undefined;
    setAudioUploadProgress(undefined);
  }, [audioMode]);

  useEffect(
    () => () => {
      audioUploadOperationRef.current += 1;
      audioUploadAbortRef.current?.abort();
    },
    [],
  );

  const cancelAudioUpload = () => {
    audioUploadOperationRef.current += 1;
    audioUploadAbortRef.current?.abort();
    audioUploadAbortRef.current = undefined;
    setAudioUploadProgress(undefined);
    notify("口播音频上传已取消");
  };

  const uploadSpeechAudio = async (file: File) => {
    if (review || readOnly) {
      notify("审核模式不执行真实上传");
      return;
    }
    const validationError = validateOralAudioFile(file);
    if (validationError) {
      notify(validationError);
      return;
    }
    const operation = ++audioUploadOperationRef.current;
    audioUploadAbortRef.current?.abort();
    const controller = new AbortController();
    audioUploadAbortRef.current = controller;
    setAudioUploadProgress(0);
    try {
      const duration = await readAudioDuration(file);
      if (operation !== audioUploadOperationRef.current) return;
      const uploaded = await uploadOralAudioMaterial(
        file,
        "oral_audio",
        duration,
        (progress) => {
          if (operation === audioUploadOperationRef.current)
            setAudioUploadProgress(progress);
        },
        controller.signal,
      );
      if (operation !== audioUploadOperationRef.current) return;
      updateData((current) => ({
        ...current,
        assets: [
          uploaded,
          ...current.assets.filter((item) => item.id !== uploaded.id),
        ],
      }));
      patchDraft({ audioId: uploaded.id, voiceId: undefined });
      notify(`音频“${uploaded.name}”已上传并永久保存`);
    } catch (cause) {
      if (operation !== audioUploadOperationRef.current) return;
      notify(customerVisibleErrorMessage(cause, "上传口播音频失败"));
    } finally {
      if (operation === audioUploadOperationRef.current) {
        audioUploadAbortRef.current = undefined;
        setAudioUploadProgress(undefined);
        if (audioUploadInputRef.current) audioUploadInputRef.current.value = "";
      }
    }
  };

  return (
    <section className="creation-page creation-oral">
      <header className="creation-heading creation-heading-back">
        <Button
          variant="quiet"
          onClick={() => navigate(state.returnTo ?? "workbench")}
        >
          ← 返回
        </Button>
        <div>
          <h1>{audioMode ? "数字人口播 - 用已有音频生成" : "数字人口播"}</h1>
        </div>
      </header>
      <CreationNavigation />
      <div className="creation-oral-grid">
        <div className="creation-oral-left">
          <Tabs
            items={[
              { id: "oral", label: "用文案生成" },
              { id: "oral-audio", label: "用已有音频生成" },
            ]}
            value={audioMode ? "oral-audio" : "oral"}
            onChange={(value) =>
              navigate(value === "oral-audio" ? "oral-audio" : "oral")
            }
          />
          <Panel className="creation-oral-inputs">
            {audioMode ? (
              <ControlGroup label="口播音频">
                {speechAudio ? (
                  <div className="creation-audio-card">
                    <div className="creation-audio-title">
                      <Icon name="audio" />
                      <span>
                        <strong>{speechAudio.name}</strong>
                        <small>
                          {speechAudio.source} ·{" "}
                          {speechAudio.duration ?? "时长未知"}
                        </small>
                      </span>
                    </div>
                    <Waveform />
                  </div>
                ) : (
                  <Empty
                    title="未选择完整口播音频"
                    description="使用录音原声直接驱动口型。"
                  />
                )}
                <div className="creation-inline-actions">
                  <Button
                    variant="outline"
                    disabled={readOnly}
                    onClick={() => openPicker("audio")}
                  >
                    从素材库选择
                  </Button>
                  <input
                    accept=".mp3,audio/mpeg"
                    aria-label="选择口播音频"
                    disabled={readOnly}
                    hidden
                    onChange={(event) => {
                      const file = event.target.files?.[0];
                      if (file) void uploadSpeechAudio(file);
                    }}
                    ref={audioUploadInputRef}
                    type="file"
                  />
                  <Button
                    disabled={readOnly || audioUploadProgress !== undefined}
                    variant="outline"
                    onClick={() => audioUploadInputRef.current?.click()}
                  >
                    {audioUploadProgress === undefined
                      ? "上传音频"
                      : `上传中 ${audioUploadProgress}%`}
                  </Button>
                  {audioUploadProgress !== undefined && (
                    <Button variant="quiet" onClick={cancelAudioUpload}>
                      取消上传
                    </Button>
                  )}
                </div>
                <Hint>使用音频中的原声直接驱动口型，无需另选克隆声音。</Hint>
              </ControlGroup>
            ) : (
              <>
                <ControlGroup label="口播文案">
                  <div className="creation-readonly-script">
                    <div>
                      <span>来源：文案工坊</span>
                      <strong>
                        {state.draft.script.confirmed ? "终稿" : "待确认"} V
                        {state.draft.script.version}
                      </strong>
                    </div>
                    <h3>{state.draft.script.title || "未命名作品"}</h3>
                    <p>{state.draft.script.text || "尚未带入已确认终稿。"}</p>
                    <button
                      onClick={() => navigate("copy", { returnTo: "oral" })}
                      type="button"
                    >
                      去文案工坊修改
                    </button>
                  </div>
                </ControlGroup>
                <ControlGroup label="声音">
                  <div className="creation-voice-row">
                    <span>{voice?.name ?? "未选择已确认声音"}</span>
                    <small>{voice ? "已就绪" : "需在人物库试听确认"}</small>
                    <Button
                      variant="outline"
                      disabled={readOnly}
                      onClick={() => openPicker("voice")}
                    >
                      更换
                    </Button>
                  </div>
                  <button
                    className="creation-text-link"
                    onClick={() =>
                      navigate("person-voices", {
                        returnTo: state.page,
                        selectedPersonId: state.draft.ipId,
                      })
                    }
                    type="button"
                  >
                    管理声音
                  </button>
                </ControlGroup>
              </>
            )}
          </Panel>
        </div>
        <Panel className="creation-oral-avatar">
          <PersonIdentity person={person} />
          <div className="creation-panel-title-row">
            <span>口播分身{audioMode ? "预览" : ""}</span>
            <Button
              variant="outline"
              disabled={readOnly}
              onClick={() => openPicker("avatar")}
            >
              更换分身
            </Button>
          </div>
          {avatar ? (
            <>
              <Media
                asset={avatarImage}
                alt={avatar.name}
                className="creation-avatar-preview"
              />
              <div className="creation-avatar-meta">
                <strong>{avatarDisplayName(person, avatar.name)}</strong>
                <small>已就绪 · 来源：人物库</small>
              </div>
              <button
                className="creation-text-link"
                onClick={() =>
                  navigate("person-avatars", {
                    returnTo: state.page,
                    selectedPersonId: state.draft.ipId,
                  })
                }
                type="button"
              >
                去人物库管理口播分身
              </button>
            </>
          ) : (
            <Empty
              title="还没有可用口播分身"
              description="请先在当前人物下制作并完成分身。"
              action={
                <Button
                  variant="outline"
                  onClick={() =>
                    navigate("person-avatars", {
                      returnTo: state.page,
                      selectedPersonId: state.draft.ipId,
                    })
                  }
                >
                  去人物库制作口播分身
                </Button>
              }
            />
          )}
        </Panel>
      </div>
      <footer className="creation-action-bar">
        {!audioMode && (
          <div className="creation-style-controls">
            <span>成片样式</span>
            <Button
              disabled={readOnly}
              variant={state.draft.style === "standard" ? "outline" : "quiet"}
              onClick={() => patchDraft({ style: "standard" })}
            >
              标准口播
            </Button>
            <Button
              disabled={readOnly}
              variant={state.draft.style === "template" ? "outline" : "quiet"}
              onClick={() => patchDraft({ style: "template" })}
            >
              网感模板
            </Button>
            {state.draft.style === "standard" && (
              <>
                <span>字幕</span>
                <Button
                  disabled={readOnly}
                  variant={!state.draft.subtitles ? "outline" : "quiet"}
                  onClick={() => patchDraft({ subtitles: false })}
                >
                  不添加
                </Button>
                <Button
                  disabled={readOnly}
                  variant={state.draft.subtitles ? "outline" : "quiet"}
                  onClick={() => patchDraft({ subtitles: true })}
                >
                  添加
                </Button>
              </>
            )}
          </div>
        )}
        <Button
          variant="outline"
          disabled={readOnly}
          onClick={() => {
            if (readOnly) return;
            saveDraft();
          }}
        >
          保存草稿
        </Button>
        <Button
          variant="primary"
          disabled={readOnly || !ready}
          onClick={() => requestGeneration("数字人口播")}
        >
          生成口播视频
        </Button>
      </footer>
      <Hint>
        确认费用后提交；数字人服务未配置时会明确提示，不会生成伪造成片。
      </Hint>
    </section>
  );
}
