import { useCallback, useEffect, useRef, useState } from "react";
import {
  type CustomerProfile,
  createGenerationTaskPreviewUrl,
  createViralImportTask,
  customerVisibleErrorMessage,
  DIRECT_RESULT_RETENTION_HINT,
  getMaterialBatchPreviews,
  getStudioNotificationPreferences,
  getViralImportTask,
  isResultUrlExpired,
  RESULT_URL_EXPIRED_MESSAGE,
  resolveViralLink,
  updateStudioNotificationPreferences,
  type ViralImportTask,
} from "../api";
import { VideoPreview } from "../VideoPreview";
import { useStudio } from "./context";
import { LocalPublishAccountsPanel as PublishAccountsPanel } from "./LocalPublishAccountsPanel";
import {
  cancelStudioTask,
  downloadStudioTaskResult,
  loadMoreGenerationTasks,
  loadMoreOralTasks,
  loadStudioTaskDetail,
  loadTaskPreview,
  readVideoDuration,
  removeStudioGenerationTask,
  renameStudioGenerationTask,
  retryStudioTask,
  studioVideoFromViral,
  uploadWorkbenchSourceVideo,
} from "./live";
import { handOffLongReplicaSource } from "./longReplicaSession";
import { createDraft, draftFromTask } from "./state";
import { taskHasDeliverable } from "./taskResults";
import type {
  StudioAsset,
  StudioData,
  StudioPage,
  StudioTask,
  StudioVideo,
} from "./types";
import {
  Button,
  Empty,
  Field,
  FilterSelect,
  formatTaskTime,
  Hint,
  Icon,
  Media,
  Panel,
  StudioDialog,
  Tabs,
} from "./ui";
import { ViralRecommendations } from "./ViralRecommendations";
import { extractLocalVideoAudio } from "./viralCache";
import {
  clearViralImportIdempotencyKey,
  shouldClearViralImportIdempotencyKey,
  VIRAL_IMPORT_POLLING_TIMEOUT_MESSAGE,
  ViralImportPollingTimeoutError,
  viralImportIdempotencyKey,
} from "./viralImport";

const statusNames: Record<StudioTask["status"], string> = {
  running: "生成中",
  queued: "排队中",
  failed: "待处理",
  completed: "已完成",
  partial: "部分完成",
  uncertain: "状态待确认",
  cancelled: "已取消",
};

function taskDetailPatch(task: StudioTask, returnTo: "workbench" | "tasks") {
  return {
    selectedTaskId: task.id,
    selectedTaskKind: task.backendKind,
    selectedTaskBackendId: task.backendId ?? task.batchId ?? task.id,
    returnTo,
  };
}

/** 任务中心状态列的图标与子文案（与效果图一致：排队中"等待开始"、
 * 待处理"生成失败"），状态待确认保持独立提示避免误导重试。 */
const statusHints: Partial<Record<StudioTask["status"], string>> = {
  queued: "等待开始",
  failed: "生成失败",
  partial: "部分成片未生成成功",
  uncertain: "状态待确认",
};

const statusIcons: Record<StudioTask["status"], string> = {
  running: "clock",
  queued: "clock",
  failed: "warning",
  uncertain: "warning",
  completed: "check",
  partial: "check",
  cancelled: "close",
};

function Status({ task }: { task: StudioTask }) {
  return (
    <span className={`studio-status studio-status--${task.status}`}>
      {statusNames[task.status]}
      {task.progress !== undefined && task.status === "running"
        ? ` ${task.progress}%`
        : ""}
    </span>
  );
}

function StatusCell({ task }: { task: StudioTask }) {
  const hint = statusHints[task.status];
  return (
    <div className="studio-status-cell">
      <span className={`studio-status studio-status--${task.status}`}>
        <Icon name={statusIcons[task.status]} size={16} />
        {statusNames[task.status]}
        {task.progress !== undefined && task.status === "running"
          ? ` ${task.progress}%`
          : ""}
      </span>
      {hint && <small>{hint}</small>}
    </div>
  );
}

function WorkbenchWorks() {
  const { data, navigate, notify, review, updateData, user } = useStudio();
  const work = data.tasks
    .filter(taskHasDeliverable)
    .sort(
      (left, right) =>
        (Date.parse(right.submitted) || 0) - (Date.parse(left.submitted) || 0),
    )
    // 工作台是概览：右列与左列两张爆款卡高度相当，更多成片走「查看全部任务」。
    .slice(0, 4);
  const active = data.tasks.filter((task) =>
    ["running", "queued", "uncertain"].includes(task.status),
  );
  const thumbnailIds = work
    .flatMap((task) => (task.resultId && !task.poster ? [task.resultId] : []))
    .sort()
    .join(",");
  const [thumbnails, setThumbnails] = useState<Record<string, string>>({});
  useEffect(() => {
    if (review || !user.id || !thumbnailIds) return;
    const controller = new AbortController();
    void getMaterialBatchPreviews(
      user.id,
      thumbnailIds.split(",").map((id) => ({ id, populate: false })),
      { signal: controller.signal },
    )
      .then((result) => {
        if (!controller.signal.aborted) setThumbnails(result.thumbnails);
      })
      .catch(() => {
        // 封面缺失时仍可从任务详情查看成片。
      });
    return () => controller.abort();
  }, [review, thumbnailIds, user.id]);

  const publishRequest = useRef(0);
  const publishPending = useRef(false);
  const [publishingTaskId, setPublishingTaskId] = useState<string>();
  useEffect(
    () => () => {
      publishRequest.current += 1;
    },
    [],
  );
  const preparePublish = async (task: StudioTask) => {
    if (publishPending.current || review || user.role === "auditor") return;
    publishPending.current = true;
    const request = ++publishRequest.current;
    setPublishingTaskId(task.id);
    try {
      const asset = await loadTaskPreview(task);
      if (request !== publishRequest.current) return;
      if (!asset?.saved) {
        notify("请先在任务详情保存成片，再前往发布。");
        return;
      }
      updateData((current) => ({
        ...current,
        assets: [
          asset,
          ...current.assets.filter((item) => item.id !== asset.id),
        ],
        tasks: current.tasks.map((item) =>
          item.id === task.id ? { ...item, resultId: asset.id } : item,
        ),
      }));
      navigate("publishing", { selectedAssetId: asset.id });
    } catch (error) {
      if (request === publishRequest.current)
        notify(
          customerVisibleErrorMessage(
            error,
            "成片暂不可用，请从任务详情重试。",
          ),
        );
    } finally {
      if (request === publishRequest.current) {
        publishPending.current = false;
        setPublishingTaskId(undefined);
      }
    }
  };

  return (
    <section
      className="studio-home-works"
      aria-labelledby="studio-home-works-title"
    >
      <div className="studio-home-section-heading">
        <h2 id="studio-home-works-title">我的成片</h2>
        <Button variant="quiet" onClick={() => navigate("tasks")}>
          查看全部任务
        </Button>
      </div>
      {work.length ? (
        <>
          <p className="studio-works-summary">
            共 {work.length} 支成片
            {active.length ? ` · ${active.length} 个任务进行中` : ""}
          </p>
          <div className="studio-works-grid">
            {work.map((task) => {
              const poster =
                task.poster || (task.resultId && thumbnails[task.resultId]);
              const copyCount = task.deliverableCount ?? 0;
              return (
                <article className="studio-work-card" key={task.id}>
                  <button
                    type="button"
                    className="studio-work-cover"
                    aria-label={`查看成片：${task.title}`}
                    onClick={() =>
                      navigate(
                        "task-detail",
                        taskDetailPatch(task, "workbench"),
                      )
                    }
                  >
                    <VideoPreview
                      fitContainer
                      poster={poster || undefined}
                      fallback={<Icon name="video" size={28} />}
                    />
                    <span className="studio-work-play" aria-hidden="true">
                      ▶
                    </span>
                    <span className="studio-work-type">{task.type}</span>
                  </button>
                  <div className="studio-work-body">
                    <strong className="studio-work-title" title={task.title}>
                      {task.title}
                    </strong>
                    <div className="studio-work-meta">
                      <Status task={task} />
                      {copyCount > 1 ? (
                        <span className="studio-work-count">
                          {copyCount} 支成片
                        </span>
                      ) : null}
                      {task.submitted ? (
                        <span className="studio-work-time">
                          {formatTaskTime(task.submitted)}
                        </span>
                      ) : null}
                    </div>
                    <div className="studio-work-actions">
                      <Button
                        variant="quiet"
                        onClick={() =>
                          navigate(
                            "task-detail",
                            taskDetailPatch(task, "workbench"),
                          )
                        }
                      >
                        查看详情
                      </Button>
                      {!review && user.role !== "auditor" && task.resultId ? (
                        <Button
                          aria-label={`发布成片：${task.title}`}
                          disabled={publishingTaskId !== undefined}
                          onClick={() => void preparePublish(task)}
                          variant="outline"
                        >
                          {publishingTaskId === task.id
                            ? "读取成片…"
                            : "去发布"}
                        </Button>
                      ) : null}
                    </div>
                  </div>
                </article>
              );
            })}
          </div>
        </>
      ) : (
        <div className="studio-home-empty-card" role="status">
          <strong>{data.loading ? "正在读取成片…" : "还没有成片"}</strong>
          {!data.loading ? (
            <>
              <span>粘贴链接或上传视频，开始第一支创作。</span>
              <Button variant="outline" onClick={() => navigate("replica")}>
                开始创作
              </Button>
            </>
          ) : null}
        </div>
      )}
      {active.length ? (
        <button
          type="button"
          className="studio-home-progress"
          onClick={() => navigate("tasks")}
        >
          <span>
            生成中 {active.filter((task) => task.status === "running").length}
            {" · "}
            排队 {active.filter((task) => task.status === "queued").length}
            {active.some((task) => task.status === "uncertain") &&
              " · 有任务待核对"}
          </span>
          {active.slice(0, 2).map((task) => (
            <span key={task.id} className="studio-home-progress-item">
              <span title={task.title}>{task.title}</span>
              <Status task={task} />
            </span>
          ))}
          <Icon name="chevron" />
        </button>
      ) : null}
    </section>
  );
}

export function WorkbenchPage() {
  const {
    requireLogin,
    data,
    user,
    review,
    navigate,
    openLive,
    notify,
    patchDraft,
    patchState,
    updateData,
    state,
    discardSavedDraft,
    extractScriptFromUpload,
    refresh,
  } = useStudio();
  const accountId = user.id || "anonymous";
  const accountGenerationRef = useRef({ accountId, generation: 0 });
  if (accountGenerationRef.current.accountId !== accountId) {
    accountGenerationRef.current = {
      accountId,
      generation: accountGenerationRef.current.generation + 1,
    };
  }
  const accountContextKey = `${accountId}:${accountGenerationRef.current.generation}`;
  const [sourceLinkState, setSourceLinkState] = useState({
    accountContextKey,
    value: "",
  });
  const sourceLink =
    sourceLinkState.accountContextKey === accountContextKey
      ? sourceLinkState.value
      : "";
  const [uploadState, setUpload] = useState<{
    accountContextKey: string;
    name: string;
    progress: number;
    error: string;
    projectId: string | null;
    completed: boolean;
  } | null>(null);
  const upload =
    uploadState?.accountContextKey === accountContextKey ? uploadState : null;
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const uploadOperationRef = useRef(0);
  const linkOperationRef = useRef(0);
  const viralOperationRef = useRef(0);
  const viralIdempotencyKeysRef = useRef(new Map<string, string>());
  const uploadAbortRef = useRef<AbortController | null>(null);
  // >15s 原片登记：上传时只抽音轨供提取文案，原片留在本机；「开始复刻」
  // 时按 replica_prompt 用途补传整片并跳长视频拆解页签。仅当前挂载有效。
  const longOriginalRef = useRef<{ file: File; duration: number } | null>(null);
  const [linkStateValue, setLinkState] = useState<{
    accountContextKey: string;
    status: "idle" | "loading" | "error";
    stage?: "resolve" | "import";
    message?: string;
  }>({ accountContextKey, status: "idle" });
  const linkState =
    linkStateValue.accountContextKey === accountContextKey
      ? linkStateValue
      : ({ accountContextKey, status: "idle" } as const);
  const activeAccountRef = useRef(accountId);
  if (activeAccountRef.current !== accountId) {
    activeAccountRef.current = accountId;
    uploadOperationRef.current += 1;
    linkOperationRef.current += 1;
    viralOperationRef.current += 1;
    uploadAbortRef.current?.abort();
    uploadAbortRef.current = null;
  }
  useEffect(
    () => () => {
      uploadOperationRef.current += 1;
      linkOperationRef.current += 1;
      viralOperationRef.current += 1;
      uploadAbortRef.current?.abort();
    },
    [],
  );
  const beginViralCreation = async (
    video: StudioVideo,
    purpose: "copy" | "replica" = "replica",
    providedIdempotencyKey?: string,
    requestAccount = accountId,
  ) => {
    if (review) {
      patchDraft({ sourceId: video.id });
      navigate(purpose, {
        selectedVideoId: video.id,
        returnTo: "workbench",
      });
      return;
    }
    if (!video.platformKey || !video.nativeId) {
      notify("该视频缺少可导入的平台标识");
      return;
    }
    // 工作台粘贴链接两端统一直接服务端导入（2026-11 爆款链路改造 WP3）：
    // 新粘贴的链接尚无本人的成功导入行，claim 的 owner 旁路不成立、视频也未
    // 上架发布，claim-first 只会 404 断链。共享缓存的秒回发生在导入完成后的
    // 转写任务入队（script-from-audio），不在导入入队本身；交付计费走转写
    // 完成后的 claim。公开爆款卡片的 claim-first 在 ContentPages 侧执行。
    await beginViralImport(
      video,
      purpose,
      providedIdempotencyKey,
      requestAccount,
    );
  };

  // 爆款导入主体（服务端拉取素材 → 轮询任务 → 转写/复刻分流）。
  // Web 端「提取文案」先经过 claim-first 分流，未命中才回到这里。
  const beginViralImport = async (
    video: StudioVideo,
    purpose: "copy" | "replica",
    providedIdempotencyKey?: string,
    requestAccount = accountId,
  ) => {
    if (!video.platformKey || !video.nativeId) {
      notify("该视频缺少可导入的平台标识");
      return;
    }
    const operation = ++viralOperationRef.current;
    const actionKey = `${video.platformKey}:${video.nativeId}:${purpose}`;
    const memoryKey = `${requestAccount}:${actionKey}`;
    const idempotencyKey =
      providedIdempotencyKey ??
      viralIdempotencyKeysRef.current.get(memoryKey) ??
      viralImportIdempotencyKey(requestAccount, actionKey);
    viralIdempotencyKeysRef.current.set(memoryKey, idempotencyKey);
    const clearKey = () => {
      viralIdempotencyKeysRef.current.delete(memoryKey);
      clearViralImportIdempotencyKey(requestAccount, actionKey, idempotencyKey);
    };
    try {
      let task: ViralImportTask = await createViralImportTask(
        video.platformKey,
        video.nativeId,
        purpose,
        idempotencyKey,
      );
      if (
        operation !== viralOperationRef.current ||
        requestAccount !== activeAccountRef.current
      )
        return;
      for (let attempt = 0; attempt < 120; attempt += 1) {
        if (
          operation !== viralOperationRef.current ||
          requestAccount !== activeAccountRef.current
        )
          return;
        if (task.status === "SUCCEEDED" || task.status === "FAILED") break;
        const taskId = task.taskId ?? task.id;
        if (!taskId) throw new Error("导入任务缺少任务 ID");
        await new Promise<void>((resolve) => window.setTimeout(resolve, 1_000));
        if (
          operation !== viralOperationRef.current ||
          requestAccount !== activeAccountRef.current
        )
          return;
        task = await getViralImportTask(taskId);
      }
      if (
        operation !== viralOperationRef.current ||
        requestAccount !== activeAccountRef.current
      )
        return;
      if (task.status !== "SUCCEEDED" && task.status !== "FAILED") {
        throw new ViralImportPollingTimeoutError(
          VIRAL_IMPORT_POLLING_TIMEOUT_MESSAGE,
        );
      }
      if (task.status !== "SUCCEEDED") {
        if (task.retryable === false) clearKey();
        throw new Error(
          task.errorMessage || task.error || task.message || "参考素材导入失败",
        );
      }
      if (!task.projectId || !task.sourceAssetId) {
        clearKey();
        throw new Error("导入任务缺少项目或素材结果");
      }
      if (purpose === "replica" && !task.canAnalyze) {
        clearKey();
        throw new Error("该来源暂不支持视频复刻");
      }
      if (purpose === "copy" && !task.canTranscribe) {
        clearKey();
        throw new Error("该来源暂不支持提取文案");
      }
      patchDraft({
        projectId: task.projectId,
        sourceId: task.sourceAssetId,
        sourceAssetId: task.sourceAssetId,
      });
      refresh();
      if (purpose === "copy") {
        // 正文随本人转写任务直出（共享文案缓存已退役），与爆款列表入口
        // （ContentPages goExtract）同口径，不再携带视频身份回「获取文案」。
        extractScriptFromUpload(task.projectId, task.sourceAssetId);
      } else {
        navigate("replica", {
          selectedVideoId: video.id,
          returnTo: "workbench",
        });
      }
    } catch (error) {
      if (shouldClearViralImportIdempotencyKey(error)) clearKey();
      if (
        operation === viralOperationRef.current &&
        requestAccount === activeAccountRef.current
      ) {
        notify(error instanceof Error ? error.message : "参考素材导入失败");
      }
    }
  };
  const pendingViralSource = !state.draft.projectId
    ? data.videos.find((video) => video.id === state.draft.sourceId)
    : undefined;
  // 长视频拆解入口：>15s 原片按 replica_prompt 用途补传整片（进度走同一个
  // 上传状态位），成功后交接给长视频拆解页签自动建任务。本地先卡 180 秒/600MB，
  // 与服务端长视频门禁同口径，避免整片白传。
  const beginLongAnalysis = async (original: {
    file: File;
    duration: number;
  }) => {
    if (original.duration > 180.1) {
      notify("长视频拆解仅支持 4–180 秒的视频，请裁剪后再试。");
      return;
    }
    if (original.file.size > 600 * 1024 * 1024) {
      notify("长视频不能超过 600 MB，请压缩后再上传。");
      return;
    }
    const operation = ++uploadOperationRef.current;
    const requestAccount = accountId;
    uploadAbortRef.current?.abort();
    const abortController = new AbortController();
    uploadAbortRef.current = abortController;
    setUpload({
      accountContextKey,
      name: original.file.name,
      progress: 0,
      error: "",
      projectId: null,
      completed: false,
    });
    notify("正在上传长视频原片…");
    let notifiedStep = -1;
    try {
      const uploaded = await uploadWorkbenchSourceVideo(
        original.file,
        (percent) => {
          if (
            operation !== uploadOperationRef.current ||
            requestAccount !== activeAccountRef.current
          )
            return;
          setUpload((current) =>
            current?.accountContextKey === accountContextKey
              ? { ...current, progress: percent }
              : current,
          );
          const step = Math.min(
            100,
            Math.max(0, Math.floor(percent / 10) * 10),
          );
          if (step <= notifiedStep) return;
          notifiedStep = step;
          notify(`长视频上传中 ${step}%`);
        },
        abortController.signal,
        "replica_prompt",
      );
      if (
        operation !== uploadOperationRef.current ||
        requestAccount !== activeAccountRef.current
      )
        return;
      setUpload((current) =>
        current?.accountContextKey === accountContextKey
          ? {
              ...current,
              progress: 100,
              projectId: uploaded.projectId,
              completed: true,
            }
          : current,
      );
      longOriginalRef.current = null;
      handOffLongReplicaSource(accountId, {
        projectId: uploaded.projectId,
        assetId: uploaded.assetId,
      });
      notify("长视频已上传，开始拆解，预计 5–15 分钟。");
      navigate("long-replica");
    } catch (error) {
      if (
        operation !== uploadOperationRef.current ||
        requestAccount !== activeAccountRef.current
      )
        return;
      setUpload((current) =>
        current?.accountContextKey === accountContextKey
          ? {
              ...current,
              error:
                error instanceof Error && error.message.trim()
                  ? error.message.trim()
                  : "上传失败，请重试。",
            }
          : current,
      );
    }
  };
  const begin = async (mode: "copy" | "replica", overrideLink?: string) => {
    if (requireLogin) {
      requireLogin();
      return;
    }
    if (review) {
      navigate(mode);
      return;
    }
    // 直达意图（诊断报告「一键拆解」）带着链接进来：先回填输入框让用户看得见，
    // 再用 override 值发起解析——闭包里的 sourceLink 还是上一帧的旧值。
    const link = (overrideLink ?? sourceLink).trim();
    if (overrideLink) {
      setSourceLinkState({ accountContextKey, value: overrideLink });
    }
    if (link) {
      if (linkState.status === "loading") return;
      const operation = ++linkOperationRef.current;
      const requestAccount = accountId;
      const linkActionKey = `link:${link}:${mode}`;
      const resolutionKey = viralImportIdempotencyKey(
        requestAccount,
        linkActionKey,
      );
      setLinkState({
        accountContextKey,
        status: "loading",
        stage: "resolve",
      });
      try {
        const resolution = await resolveViralLink(link, mode, resolutionKey);
        if (
          operation !== linkOperationRef.current ||
          requestAccount !== activeAccountRef.current
        )
          return;
        const video = studioVideoFromViral(resolution.item);
        updateData((current) => ({
          ...current,
          videos: [
            video,
            ...current.videos.filter((candidate) => candidate.id !== video.id),
          ],
        }));
        setLinkState({ accountContextKey, status: "loading", stage: "import" });
        await beginViralCreation(
          video,
          mode,
          resolution.importIdempotencyKey,
          requestAccount,
        );
        if (
          operation === linkOperationRef.current &&
          requestAccount === activeAccountRef.current
        ) {
          setLinkState({ accountContextKey, status: "idle" });
        }
      } catch (error) {
        if (
          operation !== linkOperationRef.current ||
          requestAccount !== activeAccountRef.current
        )
          return;
        const requestError = error as { code?: string; status?: number };
        if (
          requestError.code !== "VIRAL_LINK_IN_PROGRESS" &&
          requestError.code !== "VIRAL_LINK_SUBMISSION_UNCERTAIN" &&
          (requestError.code === "VIRAL_LINK_MEDIA_DNS_UNAVAILABLE" ||
            (requestError.status !== undefined &&
              requestError.status >= 400 &&
              requestError.status < 500))
        ) {
          clearViralImportIdempotencyKey(
            requestAccount,
            linkActionKey,
            resolutionKey,
          );
        }
        setLinkState({
          accountContextKey,
          status: "error",
          message:
            error instanceof Error && error.message.trim()
              ? error.message.trim()
              : "视频链接解析失败，请上传 MP4/MOV 文件。",
        });
      }
      return;
    }
    if (upload?.projectId || state.draft.sourceId || state.draft.projectId) {
      // 上传完成的来源已经写进当前草稿：复刻直接进分镜工作区；
      // 文案提取走 script-from-audio 异步管线（抽音轨→转写→回填草稿）。
      if (mode === "replica") {
        // >15s 原片登记在案时走长视频拆解：补传整片后跳长视频页签自动开跑；
        // ≤15s 或无登记保持原 <15s 复刻流程。
        const original = longOriginalRef.current;
        if (original) {
          await beginLongAnalysis(original);
          return;
        }
        navigate("replica");
        return;
      }
      extractScriptFromUpload();
      return;
    }
    openLive("projects");
  };
  const handleUploadFile = (file: File) => {
    if (requireLogin) {
      requireLogin();
      return;
    }
    if (!/\.(mp4|mov)$/i.test(file.name)) {
      notify("目前仅支持 MP4 / MOV 视频文件。");
      return;
    }
    const operation = ++uploadOperationRef.current;
    const requestAccount = accountId;
    uploadAbortRef.current?.abort();
    const abortController = new AbortController();
    uploadAbortRef.current = abortController;
    setUpload({
      accountContextKey,
      name: file.name,
      progress: 0,
      error: "",
      projectId: null,
      completed: false,
    });
    // 首页上传尚未选择复刻或文案提取；只存素材，后续动作入口再推进。
    // 超过 30 秒的视频默认只抽音轨上传供提取文案（原片不出本机）；原片与
    // 实测时长登记进 longOriginalRef——用户点「开始复刻」时按 replica_prompt
    // 用途补传整片并跳长视频拆解页签（4–30 秒源片留在复刻页签直接拆解）。
    // 读不出时长、非桌面端或抽轨失败时回退整片上传（服务端 script 门禁无上限）。
    const pickUploadFile = async (): Promise<{
      file: File;
      longDuration?: number;
    }> => {
      let duration: number | undefined;
      try {
        duration = await readVideoDuration(file);
      } catch {
        duration = undefined;
      }
      if (duration === undefined || duration <= 30.1) return { file };
      notify("正在提取视频音轨…");
      try {
        return {
          file: await extractLocalVideoAudio(file),
          longDuration: duration,
        };
      } catch (error) {
        notify(
          error instanceof Error && error.message
            ? `${error.message}（将改为直接上传视频）`
            : "本地音轨提取失败，将改为直接上传视频。",
        );
        return { file, longDuration: duration };
      }
    };
    longOriginalRef.current = null;
    // >15s 时长在第一个 then 里测得，登记动作在第二个 then（拿到 assetId 后）；
    // 变量提升到 handler 作用域，两个 then 共享。
    let longDuration: number | undefined;
    void pickUploadFile()
      .then(({ file: uploadFile, longDuration: pickedDuration }) => {
        if (
          operation !== uploadOperationRef.current ||
          requestAccount !== activeAccountRef.current
        ) {
          return null;
        }
        longDuration = pickedDuration;
        return uploadWorkbenchSourceVideo(
          uploadFile,
          (progress) => {
            if (
              operation !== uploadOperationRef.current ||
              requestAccount !== activeAccountRef.current
            )
              return;
            setUpload((current) =>
              current?.accountContextKey === accountContextKey
                ? { ...current, progress }
                : current,
            );
          },
          abortController.signal,
          "script",
        );
      })
      .then((uploaded) => {
        if (
          !uploaded ||
          operation !== uploadOperationRef.current ||
          requestAccount !== activeAccountRef.current
        )
          return;
        const { projectId, assetId } = uploaded;
        if (longDuration !== undefined) {
          longOriginalRef.current = { file, duration: longDuration };
        }
        setUpload((current) =>
          current?.accountContextKey === accountContextKey
            ? { ...current, progress: 100, projectId, completed: true }
            : current,
        );
        patchDraft({
          projectId,
          sourceId: assetId,
          sourceAssetId: assetId,
          analysisTaskId: uploaded.analysisTaskId,
          analysisTaskStatus: uploaded.analysisTaskStatus,
          firstFrameId: undefined,
          firstFrameSelectionVersionId: undefined,
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
        notify("视频已上传，来源已加入当前创作。");
      })
      .catch((error) => {
        if (
          operation !== uploadOperationRef.current ||
          requestAccount !== activeAccountRef.current
        )
          return;
        setUpload((current) =>
          current?.accountContextKey === accountContextKey
            ? {
                ...current,
                error:
                  error instanceof Error && error.message.trim()
                    ? error.message.trim()
                    : "上传失败，请重试。",
              }
            : current,
        );
      });
  };
  // 诊断报告「一键拆解」直达：消费一次就清掉，避免换页/重渲染重复发起。
  const consumedReplicateIntentRef = useRef<string | null>(null);
  const replicateIntentLink = state.replicateIntentLink;
  // biome-ignore lint/correctness/useExhaustiveDependencies: 直达意图只在链接值变化时消费一次；begin/patchState 是当帧闭包，纳入依赖会重复发起拆解
  useEffect(() => {
    if (
      !replicateIntentLink ||
      consumedReplicateIntentRef.current === replicateIntentLink
    ) {
      return;
    }
    consumedReplicateIntentRef.current = replicateIntentLink;
    patchState({ replicateIntentLink: undefined });
    void begin("replica", replicateIntentLink);
  }, [replicateIntentLink]);
  // 账号主页链接粘进"视频复刻"输入框时，直接引导到账号诊断，避免"没有可用视频"的报错。
  const accountLinkInInput = /douyin\.com\/user\/[A-Za-z0-9_-]{20,}/.test(
    sourceLink,
  );
  return (
    <section className="studio-home">
      <h2 className="studio-home-page-title">工作台</h2>
      <div className="studio-home-creation">
        <header className="studio-hero">
          <h1>
            粘贴爆款链接，<strong>开始视频复刻</strong>
          </h1>
        </header>
        <div className="studio-start">
          <div className="studio-source-input">
            <button
              type="button"
              className="studio-upload-icon"
              aria-label="上传视频"
              onClick={() => {
                if (requireLogin) {
                  requireLogin();
                  return;
                }
                if (review) {
                  notify("审核示例不执行真实上传。");
                  return;
                }
                fileInputRef.current?.click();
              }}
            >
              <Icon name="upload" />
            </button>
            <input
              aria-label="视频链接"
              placeholder="粘贴抖音 / 小红书 / 视频号分享链接"
              value={sourceLink}
              onChange={(event) => {
                linkOperationRef.current += 1;
                viralOperationRef.current += 1;
                setSourceLinkState({
                  accountContextKey,
                  value: event.target.value,
                });
                setLinkState({ accountContextKey, status: "idle" });
              }}
            />
            <input
              ref={fileInputRef}
              type="file"
              aria-label="选择视频文件"
              accept=".mp4,.mov,video/mp4,video/quicktime"
              hidden
              onChange={(event) => {
                const file = event.target.files?.[0];
                if (file) handleUploadFile(file);
                event.target.value = "";
              }}
            />
            <Button
              disabled={linkState.status === "loading"}
              onClick={() => void begin("copy")}
            >
              <Icon name="pen" />
              提取文案
            </Button>
            <Button
              disabled={linkState.status === "loading"}
              variant="primary"
              onClick={() => void begin("replica")}
            >
              <Icon name="play" />
              {linkState.status === "loading" ? "正在解析链接…" : "开始复刻"}
            </Button>
          </div>
          {upload && (
            <p className="studio-upload-status" role="status">
              {upload.error
                ? `上传失败：${upload.error}`
                : upload.completed
                  ? `已上传：${upload.name}`
                  : `正在上传 ${upload.name}… ${upload.progress}%`}
            </p>
          )}
          <p className="studio-start-helper">
            支持抖音、小红书的分享链接，其他平台请上传 MP4文件；
          </p>
          {accountLinkInInput ? (
            <p className="studio-start-helper">
              这是账号主页链接，不是单条视频。
              <button
                type="button"
                className="studio-link-action"
                onClick={() =>
                  navigate("account-diagnosis", {
                    accountDiagnosisInput: sourceLink,
                  })
                }
              >
                去做账号诊断 →
              </button>
            </p>
          ) : (
            <p className="studio-start-helper">
              已有账号？
              <button
                type="button"
                className="studio-link-action"
                onClick={() => navigate("account-diagnosis")}
              >
                诊断我的账号 →
              </button>
            </p>
          )}
          {linkState.status === "loading" && (
            <p className="studio-upload-status" role="status">
              {linkState.stage === "import"
                ? "链接解析成功，正在导入视频素材并建立项目，请稍候…"
                : "正在解析链接并转存视频素材，通常十几秒内完成…"}
            </p>
          )}
          {linkState.status === "error" && (
            <p className="viral-media-status is-error" role="alert">
              {linkState.message}
            </p>
          )}
          {pendingViralSource && (
            <p className="studio-upload-status" role="status">
              已选参考：{pendingViralSource.title}。请上传该视频的 MP4 或 MOV
              文件， 上传完成后点击“提取文案”。
            </p>
          )}
        </div>
      </div>
      {/* 两列工作台：左侧爆款精选（复用二级页卡片），右侧我的成片。
          爆款池与成片空态均对未登录访客可见；未登录的点击由 requireLogin 引导。 */}
      <div className="studio-home-split">
        <ViralRecommendations />
        <WorkbenchWorks key={`${review}:${accountId}`} />
      </div>
      <WorkbenchTools key={`tools:${review}:${accountId}`} />
    </section>
  );
}

/** 常用工具：四类创作入口 + 空白创作，登录与未登录（引导登录）都可见。 */
const TOOL_ENTRIES: ReadonlyArray<
  [page: StudioPage, title: string, icon: string, blurb: string]
> = [
  ["replica", "视频创作", "video", "导入爆款，AI 拆解分镜与文案"],
  ["replacement", "人物置换", "person", "用数字分身替换视频人物"],
  ["video", "视频生成", "sparkles", "按提示词直接生成视频"],
  ["oral", "数字人口播", "audio", "数字人+克隆声音出成片"],
];

function WorkbenchTools() {
  const { navigate, review, patchState, discardSavedDraft } = useStudio();
  return (
    <section
      className="studio-home-tools"
      aria-labelledby="studio-home-tools-title"
    >
      <div className="studio-home-section-heading">
        <h2 id="studio-home-tools-title">常用工具</h2>
      </div>
      <div className="studio-tools-grid">
        {TOOL_ENTRIES.map(([page, title, icon, blurb]) => (
          <button
            key={page}
            type="button"
            className="studio-tool-card"
            aria-label={title}
            onClick={() => navigate(page)}
          >
            <span className="studio-tool-icon">
              <Icon name={icon} size={22} />
            </span>
            <strong>{title}</strong>
            <small>{blurb}</small>
          </button>
        ))}
        {/* 空白创作同时放弃云端旧草稿，避免再次进入时弹出恢复提示。 */}
        <button
          type="button"
          className="studio-tool-card studio-tool-card--quiet"
          aria-label="空白创作"
          onClick={() => {
            patchState({ draft: createDraft() });
            discardSavedDraft();
            navigate("replica");
          }}
        >
          <span className="studio-tool-icon">
            <Icon name="plus" size={22} />
          </span>
          <strong>空白创作</strong>
          <small>从零开始自由编排</small>
        </button>
      </div>
    </section>
  );
}

export function TasksPage() {
  const {
    data,
    navigate,
    openLive,
    review,
    notify,
    refresh,
    updateData,
    user,
  } = useStudio();
  const [status, setStatus] = useState("all");
  const [kind, setKind] = useState("全部");
  const [cancellingId, setCancellingId] = useState<string | null>(null);
  const [renamingId, setRenamingId] = useState<string | null>(null);
  const [renameDraft, setRenameDraft] = useState("");
  const [renameSavingId, setRenameSavingId] = useState<string | null>(null);
  const [pendingRemove, setPendingRemove] = useState<StudioTask | null>(null);
  const [removingId, setRemovingId] = useState<string | null>(null);
  const [loadingHistory, setLoadingHistory] = useState<
    "generation" | "oral" | null
  >(null);
  const matchesStatus = (task: StudioTask) =>
    status === "all" ||
    (status === "active"
      ? ["running", "queued"].includes(task.status)
      : status === "attention"
        ? ["failed", "uncertain"].includes(task.status)
        : ["completed", "partial"].includes(task.status));
  const tasks = data.tasks.filter(
    (task) => matchesStatus(task) && (kind === "全部" || kind === task.type),
  );
  // 类型菜单计数与状态筛选联动：展示"当前状态下各类型还有几个"。
  const kindItems = [
    "全部",
    "视频复刻",
    "视频生成",
    "数字人口播",
    "人物置换",
  ].map((label) => ({
    id: label,
    label: label === "全部" ? "全部类型" : label,
    count: data.tasks.filter(
      (task) =>
        matchesStatus(task) && (label === "全部" || label === task.type),
    ).length,
  }));
  const activeCount = data.tasks.filter((task) =>
    ["running", "queued"].includes(task.status),
  ).length;
  const attentionCount = data.tasks.filter((task) =>
    ["failed", "uncertain"].includes(task.status),
  ).length;
  const generationPage = data.pagination?.generationTasks;
  const oralPage = data.pagination?.oralTasks;
  const appendTasks = (
    incoming: StudioTask[],
    pagination: NonNullable<StudioData["pagination"]>,
  ) => {
    updateData((current) => {
      const known = new Set(current.tasks.map((task) => task.id));
      return {
        ...current,
        tasks: [
          ...current.tasks,
          ...incoming.filter((task) => !known.has(task.id)),
        ],
        pagination: { ...current.pagination, ...pagination },
      };
    });
  };
  const loadGenerationHistory = async () => {
    if (review || loadingHistory || !generationPage?.nextCursor) return;
    setLoadingHistory("generation");
    try {
      const result = await loadMoreGenerationTasks(generationPage.nextCursor);
      appendTasks(result.items, {
        generationTasks: {
          nextCursor: result.nextCursor,
          total: result.total,
        },
      });
    } catch (cause) {
      notify(
        cause instanceof Error && cause.message.trim()
          ? cause.message
          : "加载更多普通批次失败，请重试。",
      );
    } finally {
      setLoadingHistory(null);
    }
  };
  const loadOralHistory = async () => {
    if (
      review ||
      loadingHistory ||
      !oralPage ||
      oralPage.loaded >= oralPage.total
    )
      return;
    setLoadingHistory("oral");
    try {
      const result = await loadMoreOralTasks(oralPage.loaded);
      appendTasks(result.items, {
        oralTasks: { loaded: result.loaded, total: result.total },
      });
    } catch (cause) {
      notify(
        cause instanceof Error && cause.message.trim()
          ? cause.message
          : "加载更多口播任务失败，请重试。",
      );
    } finally {
      setLoadingHistory(null);
    }
  };
  const cancelTask = async (task: StudioTask) => {
    if (review) {
      notify("审核示例不执行真实取消。");
      return;
    }
    setCancellingId(task.id);
    try {
      const result = await cancelStudioTask(task);
      notify(
        result.billingStatus === "RELEASED" ||
          result.billingStatus === "RELEASE"
          ? "任务已取消，暂扣的积分已退回。"
          : result.billingStatus
            ? "任务已取消，计费状态处理中，请稍后刷新核对。"
            : "任务已取消，请刷新核对计费状态。",
      );
      refresh();
    } catch (error) {
      notify(
        error instanceof Error && error.message.trim()
          ? error.message.trim()
          : "取消失败，请重试。",
      );
    } finally {
      setCancellingId(null);
    }
  };
  const removeTask = async (task: StudioTask) => {
    if (removingId) return;
    setRemovingId(task.id);
    try {
      // 审核示例没有真实批次可隐藏，只做本地移除，保持与人物库删除一致。
      if (!review) await removeStudioGenerationTask(task);
      updateData((current) => ({
        ...current,
        tasks: current.tasks.filter((item) => item.id !== task.id),
      }));
      setPendingRemove(null);
      notify("任务已从列表移除。");
    } catch (cause) {
      notify(
        cause instanceof Error && cause.message.trim()
          ? cause.message.trim()
          : "移除失败，请重试。",
      );
    } finally {
      setRemovingId(null);
    }
  };
  const saveTaskName = async (task: StudioTask) => {
    if (review || renameSavingId) return;
    const name = renameDraft.trim();
    if (!name) {
      notify("视频名称不能为空。");
      return;
    }
    setRenameSavingId(task.id);
    try {
      const updated = await renameStudioGenerationTask(task, name);
      updateData((current) => ({
        ...current,
        tasks: current.tasks.map((item) =>
          item.id === task.id ? { ...item, title: updated.title } : item,
        ),
      }));
      setRenamingId(null);
      setRenameDraft("");
      notify("视频名称已更新。");
    } catch (cause) {
      notify(
        cause instanceof Error && cause.message.trim()
          ? cause.message
          : "重命名失败，请重试。",
      );
    } finally {
      setRenameSavingId(null);
    }
  };
  return (
    <section className="studio-tasks-page">
      {/* 任务列表标题由页面入口提供。
          工具并轨一行（用户拍板 2026-11-15）：页签左、类型筛选与历史任务
          入口右，不再上下错位悬浮。 */}
      <div className="studio-filter-bar">
        <Tabs
          value={status}
          onChange={setStatus}
          items={[
            { id: "all", label: "全部" },
            { id: "active", label: `进行中 ${activeCount}` },
            { id: "attention", label: `待处理 ${attentionCount}` },
            { id: "completed", label: "已完成" },
          ]}
        />
        <div className="studio-task-toolbar-right">
          <FilterSelect
            label="类型"
            items={kindItems}
            value={kind}
            onChange={setKind}
          />
          {!review && (
            <Button onClick={() => openLive("tasks")}>历史任务与下载</Button>
          )}
        </div>
      </div>
      <p className="studio-result-count" role="status">
        共 {tasks.length} 条任务
      </p>
      <div className="studio-table-wrap">
        <table className="studio-table">
          <thead>
            <tr>
              <th>作品</th>
              <th>类型</th>
              <th>状态</th>
              <th>提交时间</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            {tasks.map((task) => (
              <tr key={task.id}>
                <td>
                  <div className="studio-task-name">
                    {task.poster ? (
                      <VideoPreview
                        className="studio-task-thumb"
                        poster={task.poster}
                      />
                    ) : (
                      <span
                        aria-hidden="true"
                        className="studio-task-thumb is-empty"
                      >
                        <Icon name="video" size={16} />
                      </span>
                    )}
                    {renamingId === task.id ? (
                      <input
                        aria-label={`重命名 ${task.title}`}
                        maxLength={120}
                        value={renameDraft}
                        onChange={(event) => setRenameDraft(event.target.value)}
                        onKeyDown={(event) => {
                          if (event.key === "Enter") void saveTaskName(task);
                          if (event.key === "Escape") setRenamingId(null);
                        }}
                      />
                    ) : (
                      <span>{task.title}</span>
                    )}
                  </div>
                </td>
                <td>{task.type}</td>
                <td>
                  <StatusCell task={task} />
                </td>
                <td>{formatTaskTime(task.submitted)}</td>
                <td>
                  <div className="studio-task-actions">
                    {task.backendKind === "generation_batch" &&
                    renamingId === task.id ? (
                      <>
                        <Button
                          variant="quiet"
                          disabled={
                            renameSavingId === task.id || !renameDraft.trim()
                          }
                          onClick={() => void saveTaskName(task)}
                        >
                          {renameSavingId === task.id ? "保存中…" : "保存名称"}
                        </Button>
                        <Button
                          variant="quiet"
                          disabled={renameSavingId === task.id}
                          onClick={() => setRenamingId(null)}
                        >
                          取消
                        </Button>
                      </>
                    ) : (
                      task.backendKind === "generation_batch" && (
                        <Button
                          variant="quiet"
                          onClick={() => {
                            setRenamingId(task.id);
                            setRenameDraft(task.title);
                          }}
                        >
                          重命名
                        </Button>
                      )
                    )}
                    {task.status === "queued" &&
                    (task.backendKind !== "oral_task" ||
                      task.backendStatus === "QUEUED") &&
                    // 委派项目里他人提交的批次只能查看，取消（释放对方预扣）仅限提交者。
                    (!task.createdByUserId ||
                      !user.id ||
                      task.createdByUserId === user.id) ? (
                      <Button
                        variant="quiet"
                        disabled={cancellingId === task.id}
                        onClick={() => void cancelTask(task)}
                      >
                        {cancellingId === task.id ? "取消中…" : "取消任务"}
                      </Button>
                    ) : (
                      <Button
                        variant="quiet"
                        onClick={() =>
                          navigate(
                            "task-detail",
                            taskDetailPatch(task, "tasks"),
                          )
                        }
                      >
                        {taskHasDeliverable(task) ? "查看结果" : "查看详情"}
                      </Button>
                    )}
                    {/* 两类任务都有账号级隐藏接口：仅从列表移除，不取消任务。 */}
                    {(task.backendKind === "generation_batch" ||
                      task.backendKind === "oral_task") &&
                      renamingId !== task.id && (
                        <Button
                          variant="quiet"
                          disabled={removingId === task.id}
                          onClick={() => setPendingRemove(task)}
                        >
                          删除
                        </Button>
                      )}
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {!tasks.length && (
          <Empty
            title={data.loading ? "正在加载任务" : "当前筛选下暂无任务"}
            description="真实生成任务会在这里显示，不会因接口失败填入示例结果。"
            action={
              status !== "all" || kind !== "全部" ? (
                <Button
                  onClick={() => {
                    setStatus("all");
                    setKind("全部");
                  }}
                >
                  清除筛选
                </Button>
              ) : undefined
            }
          />
        )}
      </div>
      {!review && (generationPage || oralPage) ? (
        <fieldset className="studio-page-actions" aria-label="任务历史分页">
          <span>
            普通批次已加载{" "}
            {
              data.tasks.filter((task) => task.backendKind !== "oral_task")
                .length
            }
            {generationPage ? ` / ${generationPage.total}` : ""}
          </span>
          <Button
            disabled={loadingHistory !== null || !generationPage?.nextCursor}
            onClick={() => void loadGenerationHistory()}
          >
            {loadingHistory === "generation"
              ? "加载普通批次中…"
              : "加载更多普通批次"}
          </Button>
          <span>
            口播任务已加载 {oralPage?.loaded ?? 0}
            {oralPage ? ` / ${oralPage.total}` : ""}
          </span>
          <Button
            disabled={
              loadingHistory !== null ||
              !oralPage ||
              oralPage.loaded >= oralPage.total
            }
            onClick={() => void loadOralHistory()}
          >
            {loadingHistory === "oral" ? "加载口播任务中…" : "加载更多口播任务"}
          </Button>
        </fieldset>
      ) : null}
      {!review && (
        <Hint>
          普通批次与口播任务分别分页；状态待确认的任务请先核对，不要直接重复提交。
        </Hint>
      )}
      {pendingRemove ? (
        <StudioDialog
          title="从任务列表移除"
          onClose={() => setPendingRemove(null)}
        >
          <div className="studio-confirm-panel">
            <p>
              确定从任务列表移除「{pendingRemove.title}」吗？
              仅从本账号列表移除，不会取消正在进行的任务，也不影响已产生的费用与生成结果。
            </p>
            <div className="studio-confirm-actions">
              <Button variant="quiet" onClick={() => setPendingRemove(null)}>
                取消
              </Button>
              <Button
                variant="primary"
                disabled={removingId === pendingRemove.id}
                onClick={() => void removeTask(pendingRemove)}
              >
                {removingId === pendingRemove.id ? "移除中…" : "确认移除"}
              </Button>
            </div>
          </div>
        </StudioDialog>
      ) : null}
    </section>
  );
}

export function TaskDetailPage() {
  const {
    state,
    data,
    navigate,
    openLive,
    review,
    patchState,
    updateData,
    notify,
    refresh,
    user,
  } = useStudio();
  const [actionBusy, setActionBusy] = useState<"download" | "retry">();
  const [previewLoad, setPreviewLoad] = useState<{
    key?: string;
    asset?: StudioAsset;
    /** expired：直链交付的成片已失效（服务端 410），重试也无法恢复。 */
    status: "idle" | "loading" | "empty" | "error" | "ready" | "expired";
  }>({ status: "idle" });
  const [detailLoad, setDetailLoad] = useState<{
    key?: string;
    status: "idle" | "loading" | "error" | "ready";
    error?: string;
  }>({ status: "idle" });
  const [detailRetry, setDetailRetry] = useState(0);
  const previewRequestRef = useRef(0);
  const detailRequestRef = useRef(0);
  const detailUserIdRef = useRef(user.id);
  const verifiedDetailRef = useRef<{ key: string; userId: string } | undefined>(
    undefined,
  );
  detailUserIdRef.current = user.id;
  const task = data.tasks.find((item) => item.id === state.selectedTaskId);
  const currentResultIdRef = useRef(task?.resultId);
  currentResultIdRef.current = task?.resultId;
  const currentPreviewUrlRef = useRef<string | undefined>(undefined);
  const detailKind = state.selectedTaskKind;
  const detailId = state.selectedTaskBackendId;
  const detailKey =
    detailKind && detailId ? `${detailKind}:${detailId}` : undefined;
  const detailContextKey = detailKey ? `${user.id}:${detailKey}` : undefined;
  const taskMatchesRoute =
    task !== undefined &&
    task.backendKind === detailKind &&
    (task.backendId ?? task.batchId ?? task.id) === detailId;
  const taskIsVerified =
    review ||
    !detailContextKey ||
    (taskMatchesRoute &&
      verifiedDetailRef.current?.key === detailContextKey &&
      verifiedDetailRef.current?.userId === user.id);
  const previewContextKey = `${user.id}:${detailKey ?? state.selectedTaskId ?? "none"}`;
  const previewContextRef = useRef(previewContextKey);
  previewContextRef.current = previewContextKey;
  useEffect(() => {
    previewContextRef.current = previewContextKey;
    setActionBusy(undefined);
    return () => {
      previewRequestRef.current += 1;
    };
  }, [previewContextKey]);
  useEffect(() => {
    void detailRetry;
    if (review || !detailKind || !detailId) return;
    if (
      taskMatchesRoute &&
      verifiedDetailRef.current?.key === detailContextKey &&
      verifiedDetailRef.current?.userId === user.id
    )
      return;
    const requestId = ++detailRequestRef.current;
    const requestedUserId = user.id;
    let active = true;
    setDetailLoad({ key: detailContextKey, status: "loading" });
    void loadStudioTaskDetail(detailKind, detailId)
      .then((loaded) => {
        if (
          !active ||
          requestId !== detailRequestRef.current ||
          detailUserIdRef.current !== requestedUserId
        )
          return;
        verifiedDetailRef.current = {
          key: detailContextKey as string,
          userId: requestedUserId,
        };
        updateData((current) => ({
          ...current,
          tasks: current.tasks.some((item) => item.id === loaded.id)
            ? current.tasks.map((item) =>
                item.id === loaded.id ? loaded : item,
              )
            : [...current.tasks, loaded],
        }));
        setDetailLoad({ key: detailContextKey, status: "ready" });
      })
      .catch((cause: unknown) => {
        if (
          !active ||
          requestId !== detailRequestRef.current ||
          detailUserIdRef.current !== requestedUserId
        )
          return;
        setDetailLoad({
          key: detailContextKey,
          status: "error",
          error: customerVisibleErrorMessage(
            cause,
            "任务详情暂不可用，请重试。",
          ),
        });
      });
    return () => {
      active = false;
      detailRequestRef.current += 1;
    };
  }, [
    detailId,
    detailContextKey,
    detailKind,
    detailRetry,
    review,
    taskMatchesRoute,
    updateData,
    user.id,
  ]);
  const detailIsLoading =
    Boolean(detailContextKey && !taskIsVerified) &&
    !(detailLoad.key === detailContextKey && detailLoad.status === "error");
  const detailHasError =
    detailLoad.key === detailContextKey && detailLoad.status === "error";
  // Task-list polling contains persisted IDs only. Keep a loaded direct preview
  // in its user/task context so refreshing the list cannot unmount playback.
  const cachedPreview =
    previewLoad.key === previewContextKey ? previewLoad.asset : undefined;
  const result =
    (cachedPreview && (!task?.resultId || cachedPreview.id === task?.resultId)
      ? cachedPreview
      : undefined) ?? data.assets.find((asset) => asset.id === task?.resultId);
  currentPreviewUrlRef.current = result?.url;
  const previewStatus =
    previewLoad.key === previewContextKey ? previewLoad.status : "idle";
  const previewResult = useCallback(async () => {
    if (!task || !taskIsVerified || review || !taskHasDeliverable(task)) return;
    const requestedTask = task;
    const requestedContext = previewContextKey;
    const requestId = ++previewRequestRef.current;
    setPreviewLoad({ key: requestedContext, status: "loading" });
    try {
      const asset = await loadTaskPreview(requestedTask);
      if (
        requestId !== previewRequestRef.current ||
        previewContextRef.current !== requestedContext
      )
        return;
      if (!asset) {
        setPreviewLoad({ key: requestedContext, status: "empty" });
        return;
      }
      // A poll may have announced an archived/replaced asset while the old
      // signing request was in flight. Do not overwrite that physical ID.
      if (
        currentResultIdRef.current &&
        currentResultIdRef.current !== requestedTask.resultId &&
        currentResultIdRef.current !== asset.id
      ) {
        setPreviewLoad({ key: requestedContext, status: "idle" });
        return;
      }
      updateData((current) => ({
        ...current,
        assets: current.assets.some((item) => item.id === asset.id)
          ? current.assets.map((item) => (item.id === asset.id ? asset : item))
          : [...current.assets, asset],
        tasks: current.tasks.map((item) =>
          item.id === requestedTask.id ? { ...item, resultId: asset.id } : item,
        ),
      }));
      setPreviewLoad({ key: requestedContext, status: "ready", asset });
    } catch (cause) {
      if (
        requestId === previewRequestRef.current &&
        previewContextRef.current === requestedContext
      )
        setPreviewLoad({
          key: requestedContext,
          status: isResultUrlExpired(cause) ? "expired" : "error",
        });
    }
  }, [task, taskIsVerified, review, previewContextKey, updateData]);
  // 部分完成 / 待核对的批次里已成功的成片同样可看、可存、可发布，
  // 不能只认「已完成」，否则已付费的成片在详情页不可见。
  const taskDeliverable = task ? taskHasDeliverable(task) : false;
  useEffect(() => {
    if (
      !review &&
      taskIsVerified &&
      taskDeliverable &&
      !result?.url &&
      previewStatus === "idle"
    )
      void previewResult();
  }, [
    review,
    taskIsVerified,
    taskDeliverable,
    result?.url,
    previewStatus,
    previewResult,
  ]);
  if (!task || !taskIsVerified || detailIsLoading || detailHasError)
    return (
      <Empty
        title={
          detailIsLoading
            ? "正在读取任务详情"
            : detailHasError
              ? "任务详情暂不可用"
              : "未选择任务"
        }
        description={
          detailLoad.key === detailContextKey ? detailLoad.error : undefined
        }
        action={
          detailHasError ? (
            <Button onClick={() => setDetailRetry((value) => value + 1)}>
              重试读取任务详情
            </Button>
          ) : (
            <Button onClick={() => navigate(state.returnTo ?? "tasks")}>
              返回任务中心
            </Button>
          )
        }
      />
    );
  const person = data.people.find((item) => item.id === task.ipId);
  const info = [
    ["任务类型", task.type],
    [
      "信息来源",
      task.driverMode === "audio"
        ? "完整口播音频"
        : task.scriptVersion
          ? `终稿 V${task.scriptVersion}`
          : task.backendKind === "oral_task"
            ? "文案口播"
            : task.projectId
              ? "项目分镜"
              : "独立视频创作",
    ],
    ["IP", person?.name || "—"],
    ...(task.driverMode !== "audio"
      ? [
          [
            "声音",
            person?.voices.find((voice) => voice.id === task.voiceId)?.name ||
              "—",
          ],
        ]
      : []),
    [
      "口播分身",
      person?.avatars.find((avatar) => avatar.id === task.avatarId)?.name ||
        "—",
    ],
    ["提交时间", formatTaskTime(task.submitted)],
    // IA 改版第 5 项：成片不再归档进素材库（素材库已替换为平台培训内容）。
    ["资产状态", result?.saved ? "已保存到素材库" : "以任务返回结果为准"],
  ];
  const recreate = (page: "copy" | "oral" | "replica") => {
    const hasOralInputs =
      task.backendKind === "oral_task" &&
      (task.scriptText?.trim() ||
        (page === "oral" && task.driverMode === "audio"));
    if (!review && !task.draftSnapshot && !hasOralInputs) {
      openLive("tasks");
      notify(
        "请在原任务记录中使用重新生成，以保留服务端确认的镜头和素材参数。",
      );
      return;
    }
    patchState({ draft: draftFromTask(task) });
    navigate(page, { returnTo: "task-detail" });
    if (!review) notify("已创建新的创作草稿，请核对正文与素材后重新确认。");
  };
  const downloadResult = async () => {
    if (review) {
      notify(
        "这是效果审核示例，未提供可下载成片；真实任务通过原有下载接口获取。",
      );
      return;
    }
    if (task.backendKind !== "oral_task") {
      openLive("tasks");
      return;
    }
    setActionBusy("download");
    try {
      await downloadStudioTaskResult(task);
    } catch (error) {
      notify(
        error instanceof Error && error.message.trim()
          ? error.message.trim()
          : "口播成片下载失败，请重试。",
      );
    } finally {
      setActionBusy(undefined);
    }
  };
  const retryTask = async () => {
    setActionBusy("retry");
    try {
      await retryStudioTask(task);
      notify("已提交成片归档重试。");
      refresh();
    } catch (error) {
      notify(
        error instanceof Error && error.message.trim()
          ? error.message.trim()
          : "重试失败，请刷新后再试。",
      );
    } finally {
      setActionBusy(undefined);
    }
  };
  return (
    <section className="studio-task-detail">
      <h1>任务详情与结果</h1>
      <Button onClick={() => navigate(state.returnTo ?? "tasks")}>
        <Icon name="back" />
        返回任务中心
      </Button>
      <header>
        <h2>{task.title}</h2>
        <span className="studio-tag">{task.type}</span>
        <Status task={task} />
      </header>
      <p>
        {task.status === "partial"
          ? `本批已有 ${task.deliverableCount ?? 0} 条成片可查看，其余未生成成功，失败部分暂扣的积分会自动退回。`
          : task.status === "uncertain" && taskDeliverable
            ? "已生成的成片可正常查看；其余任务的提交结果待核对，暂扣的积分会在核对后结算或退回。"
            : taskDeliverable
              ? "作品已生成完成，可查看与管理生成结果"
              : "任务状态实时以服务端记录为准"}
      </p>
      <div className="studio-result-grid">
        <Media
          asset={
            (taskDeliverable && result) ||
            (taskDeliverable && task.poster
              ? {
                  id: task.id,
                  name: task.title,
                  kind: "video",
                  poster: task.poster,
                  group: "任务",
                  source: "任务中心",
                  saved: false,
                }
              : undefined)
          }
          alt={
            taskDeliverable ? task.title : "任务处理中或待核对，尚无可预览成片"
          }
          className="studio-result-preview"
          onError={(failedUrl) => {
            if (
              !failedUrl ||
              previewContextRef.current !== previewContextKey ||
              currentPreviewUrlRef.current !== failedUrl
            )
              return;
            setPreviewLoad({
              key: previewContextKey,
              status: "error",
              asset: result,
            });
            // 直链交付的成片播放失败时复查一次直链：已失效就明确告诉用户，
            // 而不是一直提示「预览加载失败，请重试」。
            const directTaskId =
              result && !result.saved ? result.generationTaskId : undefined;
            if (!directTaskId || review) return;
            const requestedContext = previewContextKey;
            void createGenerationTaskPreviewUrl(directTaskId, {
              verify: true,
            }).catch((cause: unknown) => {
              if (
                isResultUrlExpired(cause) &&
                previewContextRef.current === requestedContext
              )
                setPreviewLoad({
                  key: requestedContext,
                  status: "expired",
                  asset: result,
                });
            });
          }}
        />
        <Panel>
          <h2>任务信息</h2>
          <dl className="studio-details">
            {info.map(([label, value]) => (
              <div key={label}>
                <dt>{label}</dt>
                <dd>{value}</dd>
              </div>
            ))}
          </dl>
        </Panel>
        <Panel>
          <h2>动作</h2>
          <div className="studio-result-actions">
            {!review &&
              taskDeliverable &&
              previewStatus !== "expired" &&
              (!result?.url || previewStatus === "error") && (
                <Button
                  variant="primary"
                  onClick={previewResult}
                  disabled={previewStatus === "loading"}
                >
                  <Icon name="play" />
                  {previewStatus === "loading"
                    ? "正在加载预览…"
                    : previewStatus === "error"
                      ? "重试预览"
                      : previewStatus === "empty"
                        ? "重新尝试"
                        : "预览成片"}
                </Button>
              )}
            <Button
              onClick={() => void downloadResult()}
              disabled={!taskDeliverable || actionBusy === "download"}
            >
              <Icon name="download" />
              {actionBusy === "download" ? "正在下载…" : "下载成片"}
            </Button>
            <Button
              variant="primary"
              disabled={!result?.saved || !taskDeliverable}
              onClick={() =>
                navigate("publishing", { selectedAssetId: result?.id })
              }
            >
              <Icon name="upload" />
              去发布管理
            </Button>
          </div>
          {previewStatus === "empty" && (
            <Hint>该批次暂时没有可预览的成功结果。</Hint>
          )}
          {previewStatus === "error" && <Hint>预览加载失败，请重试。</Hint>}
          {previewStatus === "expired" && (
            <Hint>{RESULT_URL_EXPIRED_MESSAGE}</Hint>
          )}
          {result && !result.saved && previewStatus !== "expired" && (
            <Hint>
              {DIRECT_RESULT_RETENTION_HINT}
              成片请在有效期内下载到本地留存；发布可从发布管理页直接发起。
            </Hint>
          )}
          {!review && taskDeliverable && (
            <Hint>
              预览仅展示首个可用结果；完整结果与下载请进入“历史任务与下载”。
            </Hint>
          )}
          <Hint>进入发布管理仅创建发布草稿，不会自动发布。</Hint>
          {!review && task.retryAction && (
            <Button
              disabled={actionBusy === "retry"}
              onClick={() => void retryTask()}
            >
              重试归档
            </Button>
          )}
          {task.status === "uncertain" && !task.retryAction && (
            <Button onClick={() => openLive("tasks")}>核对任务状态</Button>
          )}
        </Panel>
        <Panel>
          <h2>基于此任务再创作</h2>
          <div className="studio-recreate">
            <Button
              onClick={() => recreate("copy")}
              disabled={
                !review &&
                task.backendKind === "oral_task" &&
                !task.scriptText?.trim() &&
                !task.draftSnapshot?.script.text.trim()
              }
            >
              <Icon name="pen" />
              调整脚本
            </Button>
            <Button
              onClick={() =>
                navigate("materials", {
                  returnTo: "task-detail",
                  selectedTaskId: task.id,
                })
              }
            >
              <Icon name="image" />
              更换素材
            </Button>
            <Button
              onClick={() =>
                recreate(task.type === "数字人口播" ? "oral" : "replica")
              }
            >
              <Icon name="refresh" />
              新建草稿
            </Button>
          </div>
        </Panel>
      </div>
    </section>
  );
}

export type StudioAccountSummary = {
  walletStatus: "loading" | "ready" | "error" | "unknown";
  availableCredits: number | null;
  retryWallet: () => void;
  retryProfile?: () => void;
  profile: CustomerProfile | null;
  profileLoadError: string;
};

export function ProfilePage({
  accountSummary,
}: {
  accountSummary?: StudioAccountSummary;
}) {
  const { user, review, openLive, notify } = useStudio();
  const [name, setName] = useState(user.display_name || user.username);
  const [profileTab, setProfileTab] = useState("overview");
  // C10b 通知偏好：进页拉取，乐观保存、失败回退；审核模式只演示不落库。
  const [notificationsEnabled, setNotificationsEnabled] = useState<
    boolean | null
  >(null);
  const [savingNotifications, setSavingNotifications] = useState(false);

  useEffect(() => {
    setName(user.display_name || user.username);
  }, [user.display_name, user.username]);

  useEffect(() => {
    if (review) {
      setNotificationsEnabled(true);
      return;
    }
    let cancelled = false;
    getStudioNotificationPreferences()
      .then((prefs) => {
        if (!cancelled) setNotificationsEnabled(prefs.enabled);
      })
      .catch(() => {
        if (!cancelled) setNotificationsEnabled(null);
      });
    return () => {
      cancelled = true;
    };
  }, [review]);

  async function toggleNotifications() {
    if (review) {
      notify("审核模式下为演示开关，不保存设置。");
      return;
    }
    if (notificationsEnabled === null || savingNotifications) return;
    const previous = notificationsEnabled;
    const next = !previous;
    setNotificationsEnabled(next);
    setSavingNotifications(true);
    try {
      await updateStudioNotificationPreferences(next);
      notify(next ? "已开启通知。" : "已关闭通知。");
    } catch (cause) {
      setNotificationsEnabled(previous);
      notify(cause instanceof Error ? cause.message : "保存通知偏好失败");
    } finally {
      setSavingNotifications(false);
    }
  }
  return (
    <section className="studio-profile">
      <h1>用户档案</h1>
      <div className="studio-profile-identity">
        {review ? (
          <img src="/studio/li.png" alt="登录账户" />
        ) : (
          <span className="studio-user-initial">
            {user.display_name?.slice(0, 1) || "我"}
          </span>
        )}
        <div>
          <h2>
            {user.display_name || user.username}
            <span className="studio-tag">登录账户</span>
          </h2>
          <p>{review ? "众墅之家" : "账户资料"}</p>
          <small>与创作用的人物 IP 分开管理</small>
        </div>
      </div>
      <Tabs
        value={profileTab}
        onChange={(id) =>
          id === "billing"
            ? openLive("wallet")
            : id === "devices"
              ? openLive("profile")
              : setProfileTab(id)
        }
        items={[
          { id: "overview", label: "账户资料" },
          { id: "publishing", label: "发布账号" },
          { id: "billing", label: "使用记录" },
          { id: "devices", label: "设备管理" },
        ]}
      />
      {profileTab === "publishing" ? (
        <PublishAccountsPanel notify={notify} />
      ) : (
        <>
          <div className="studio-profile-grid">
            <Panel>
              <h2>账户资料</h2>
              <Field label="用户名">
                <input
                  value={name}
                  onChange={(event) => setName(event.target.value)}
                  readOnly={!review}
                />
              </Field>
              <dl className="studio-details">
                <div>
                  <dt>账号</dt>
                  <dd>{accountSummary?.profile?.username ?? user.username}</dd>
                </div>
                <div>
                  <dt>加入时间</dt>
                  <dd>
                    {accountSummary?.profile?.joined_at
                      ? new Date(
                          accountSummary.profile.joined_at,
                        ).toLocaleDateString("zh-CN")
                      : review
                        ? "审核示例"
                        : "未查询"}
                  </dd>
                </div>
                <div>
                  <dt>
                    通知偏好<small>接收任务完成及异常状态通知</small>
                  </dt>
                  <dd>
                    <button
                      type="button"
                      className="studio-switch"
                      aria-label="通知偏好"
                      aria-pressed={notificationsEnabled === true}
                      disabled={
                        notificationsEnabled === null || savingNotifications
                      }
                      onClick={() => void toggleNotifications()}
                    >
                      {notificationsEnabled === false
                        ? "关闭"
                        : notificationsEnabled === null
                          ? "—"
                          : "开启"}
                    </button>
                  </dd>
                </div>
              </dl>
            </Panel>
            <Panel>
              <h2>发布账号概览</h2>
              <p>扫码账号与登录状态保存在本机。</p>
              <Button onClick={() => setProfileTab("publishing")}>
                <Icon name="person" />
                管理发布账号
              </Button>
            </Panel>
            <Panel>
              <h2>账户使用概览</h2>
              <dl className="studio-details">
                <div>
                  <dt>账户余额</dt>
                  <dd>
                    {accountSummary?.walletStatus === "ready" &&
                    accountSummary.availableCredits !== null
                      ? `${accountSummary.availableCredits} 积分`
                      : accountSummary?.walletStatus === "loading"
                        ? "查询中"
                        : accountSummary?.walletStatus === "error"
                          ? "读取失败"
                          : "未查询"}
                  </dd>
                </div>
                {accountSummary?.walletStatus === "error" && (
                  <div>
                    <dt>余额查询</dt>
                    <dd>
                      <Button onClick={accountSummary.retryWallet}>
                        重试余额查询
                      </Button>
                    </dd>
                  </div>
                )}
                {accountSummary?.profileLoadError && (
                  <div>
                    <dt>资料状态</dt>
                    <dd>
                      {accountSummary.profileLoadError}
                      {accountSummary.retryProfile && (
                        <Button onClick={accountSummary.retryProfile}>
                          重试资料查询
                        </Button>
                      )}
                    </dd>
                  </div>
                )}
                <div>
                  <dt>
                    使用记录<small>查看详细的使用记录</small>
                  </dt>
                  <dd>
                    <Button onClick={() => openLive("wallet")}>
                      查看使用记录
                    </Button>
                  </dd>
                </div>
              </dl>
            </Panel>
            <Panel>
              <h2>设备摘要</h2>
              <div className="studio-device-summary">
                <span>
                  {review
                    ? "多台设备可同时在线"
                    : "进入设备管理查看当前绑定记录"}
                </span>
                <Button onClick={() => openLive("profile")}>管理设备</Button>
              </div>
            </Panel>
          </div>
          <div className="studio-profile-save">
            <Button
              variant="primary"
              onClick={() =>
                review
                  ? notify("示例账户，不保存真实资料。")
                  : openLive("profile")
              }
            >
              {review ? "保存资料" : "编辑账户资料"}
            </Button>
          </div>
        </>
      )}
    </section>
  );
}

export { PublishAccountsPanel };
