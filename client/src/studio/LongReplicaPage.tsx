import {
  type ReactNode,
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
} from "react";
import type {
  ReplicaPromptHistoryItem,
  ReplicaPromptResult,
  ReplicaPromptTask,
  ReplicaPromptVersion,
} from "../api";
import {
  cancelReplicaPromptTask,
  fetchReplicaPromptHistory,
  getLatestReplicaPromptTask,
  getReplicaPromptVersionById,
  readReplicaPromptShots,
  readReplicaPromptSpokenScript,
  readShotConsistencyIssues,
  readSourceReview,
  startReplicaPromptTask,
  waitForReplicaPromptTask,
} from "../api";
import { isInsufficientCredits } from "../insufficientCredits";
import {
  formatShotCardsForClipboard,
  formatShotMotion,
} from "../ShotCardEditor";
import { ShotFactEvidence } from "../ShotFactsEditor";
import { SourceReviewNotice } from "../SourceReviewNotice";
import { formatShotFacts, readShotFacts, shotFactsView } from "../shotFacts";
import { CreationNavigation } from "./CreationNavigation";
import { useStudio } from "./context";
import { readVideoDuration, uploadWorkbenchSourceVideo } from "./live";
import {
  getLongReplicaSession,
  isCurrentLongReplicaSession,
  type LongReplicaRun,
} from "./longReplicaSession";
import { Button, Icon, Panel } from "./ui";

import "./longReplica.css";

// 与服务端 media.py 的 replica_prompt 门禁同口径；放开时两边一起改。
// 下限 30 秒：本页签承接 >30s 拆解（2026-11-16 分界线口径），30 秒以内
// 的视频走「<30s 视频拆解」页签的完整复刻链路。
const MAX_SOURCE_BYTES = 600 * 1024 * 1024;
const MIN_SOURCE_SECONDS = 30;
const MAX_SOURCE_SECONDS = 180;
const DURATION_TOLERANCE_SECONDS = 0.1;
const COPY_FEEDBACK_MS = 2000;

type Stage =
  | "idle"
  | "uploading"
  | "analyzing"
  | "ready"
  | "failed"
  | "cancelled";

const CANCELLED_CODE = "REPLICA_PROMPT_TASK_CANCELLED";

const SUMMARY_FIELDS: {
  key: keyof ReplicaPromptResult["summary"];
  label: string;
}[] = [
  { key: "theme", label: "主题" },
  { key: "visual_style", label: "视觉风格" },
  { key: "pace", label: "节奏" },
  { key: "camera_language", label: "镜头语言" },
  { key: "color_tone", label: "色调" },
];

function formatSeconds(value: number | null | undefined): string {
  if (typeof value !== "number" || !Number.isFinite(value) || value < 0) {
    return "--:--";
  }
  const milliseconds = Math.round(value * 1000);
  const total = Math.floor(milliseconds / 1000);
  const minutes = Math.floor(total / 60);
  const seconds = total % 60;
  const fraction = String(milliseconds % 1000)
    .padStart(3, "0")
    .replace(/0+$/, "");
  return `${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}${fraction ? `.${fraction}` : ""}`;
}

function CopyBlock({
  title,
  text,
  emptyHint,
  children,
}: {
  title: string;
  text: string;
  emptyHint?: string;
  children?: ReactNode;
}) {
  const [isCopied, setIsCopied] = useState(false);
  const timerRef = useRef<number | undefined>(undefined);
  useEffect(() => {
    return () => window.clearTimeout(timerRef.current);
  }, []);
  const hasText = Boolean(text.trim());
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(text);
      setIsCopied(true);
      window.clearTimeout(timerRef.current);
      timerRef.current = window.setTimeout(
        () => setIsCopied(false),
        COPY_FEEDBACK_MS,
      );
    } catch {
      setIsCopied(false);
    }
  };
  return (
    <section className="long-replica-block">
      <div className="long-replica-block__head">
        <h3>{title}</h3>
        <div className="long-replica-block__actions">
          {hasText ? (
            <Button variant="outline" onClick={() => void copy()}>
              {isCopied ? "已复制" : "复制"}
            </Button>
          ) : null}
        </div>
      </div>
      {hasText ? (
        <div className="long-replica-block__body">{children ?? text}</div>
      ) : (
        <p className="long-replica-block__empty">{emptyHint ?? "暂无内容。"}</p>
      )}
    </section>
  );
}

export function LongReplicaPage() {
  const { user } = useStudio();
  // key 使同一页面内的账号切换也立即卸载旧视图，局部结果和错误不会沿用。
  return <LongReplicaAccountPage key={user.id} accountId={user.id} />;
}

function LongReplicaAccountPage({ accountId }: { accountId: string }) {
  const { notify, openLive } = useStudio();
  const [cache] = useState(() => getLongReplicaSession(accountId));
  const [stage, setStage] = useState<Stage>(cache.result ? "ready" : "idle");
  const [error, setError] = useState("");
  const [result, setResult] = useState<ReplicaPromptVersion | null>(
    cache.result,
  );
  const [currentRun, setCurrentRun] = useState(cache.run);
  const resultTaskIdRef = useRef(cache.result ? cache.run?.taskId : null);
  const [uploadPercent, setUploadPercent] = useState(-1);
  const [elapsed, setElapsed] = useState(0);
  const [cancelling, setCancelling] = useState(false);
  const [cancelError, setCancelError] = useState("");
  // 上传占位交互：选中文件先停在「待确认」信息卡，用户点「开始拆解」才上传提交。
  const [pendingFile, setPendingFile] = useState<File | null>(null);
  const [pendingDuration, setPendingDuration] = useState<number | undefined>(
    undefined,
  );
  const [dragOver, setDragOver] = useState(false);
  const [historyOpen, setHistoryOpen] = useState(false);
  const [historyItems, setHistoryItems] = useState<ReplicaPromptHistoryItem[]>(
    [],
  );
  const [historyLoading, setHistoryLoading] = useState(false);
  const [historyError, setHistoryError] = useState("");
  const sessionRef = useRef(0);
  const selectionRef = useRef(0);
  const mountedRef = useRef(false);
  const abortRef = useRef<AbortController | null>(null);
  const pollAbortRef = useRef<AbortController | null>(null);
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const resumedRef = useRef(false);
  const cancelPendingRef = useRef(false);
  const openLiveRef = useRef(openLive);
  openLiveRef.current = openLive;

  useLayoutEffect(() => {
    mountedRef.current = true;
    return () => {
      // 在切换账号提交新界面时同步失效；旧上传、建任务、取消和结果查询都不能再推进。
      mountedRef.current = false;
      sessionRef.current += 1;
      resumedRef.current = false;
      abortRef.current?.abort();
      pollAbortRef.current?.abort();
    };
  }, []);

  const isActive = useCallback(
    (session: number) =>
      mountedRef.current &&
      session === sessionRef.current &&
      isCurrentLongReplicaSession(accountId, cache),
    [accountId, cache],
  );

  useEffect(() => {
    if (stage !== "analyzing") return;
    setElapsed(0);
    const startedAt = Date.now();
    const timer = window.setInterval(() => {
      setElapsed(Math.floor((Date.now() - startedAt) / 1000));
    }, 1000);
    return () => window.clearInterval(timer);
  }, [stage]);

  const showFailure = useCallback(
    (cause: unknown, session: number) => {
      if (!isActive(session)) return;
      const code =
        cause && typeof cause === "object" && "code" in cause
          ? cause.code
          : null;
      setCancelError("");
      if (code === CANCELLED_CODE) {
        setStage("cancelled");
        setError("");
        return;
      }
      setStage("failed");
      if (isInsufficientCredits(cause)) {
        setError("积分余额不足，请先充值后重试。");
        openLiveRef.current("wallet");
        return;
      }
      setError(
        cause instanceof Error ? cause.message : "长视频拆解失败，请重新提交。",
      );
    },
    [isActive],
  );

  const showTerminalTask = useCallback(
    async (task: ReplicaPromptTask, run: LongReplicaRun, session: number) => {
      if (!isActive(session)) return;
      if (task.status === "FAILED") {
        showFailure(
          Object.assign(
            new Error(task.error_message || "长视频拆解失败，请重新提交。"),
            {
              code: task.error_code,
            },
          ),
          session,
        );
        return;
      }
      const matchesTask = (version: ReplicaPromptVersion) =>
        version.id === task.result_version_id &&
        version.project_id === task.project_id &&
        version.project_id === run.projectId &&
        version.asset_id === task.asset_id &&
        version.asset_id === run.assetId &&
        version.kind === "long_analysis";
      // 轮询可能先于取消回执拿到结果；仅复用精确匹配的版本，避免旧缓存串到本次任务。
      if (
        task.result_version_id &&
        resultTaskIdRef.current === task.id &&
        cache.result &&
        matchesTask(cache.result)
      ) {
        setResult(cache.result);
        setCancelError("");
        setStage("ready");
        return;
      }
      cache.result = null;
      resultTaskIdRef.current = null;
      setResult(null);
      if (!task.result_version_id) {
        throw new Error("本次拆解缺少结果版本，请重新拆解。");
      }
      const version = await getReplicaPromptVersionById(task.result_version_id);
      if (!isActive(session)) return;
      if (!version) {
        throw new Error("未找到本次拆解的结果版本，请重新拆解。");
      }
      if (!matchesTask(version)) {
        throw new Error("结果版本与本次拆解不匹配，请重新拆解。");
      }
      cache.result = version;
      resultTaskIdRef.current = task.id;
      setResult(version);
      setCancelError("");
      setStage("ready");
    },
    [cache, isActive, showFailure],
  );

  // 创建请求在页间恢复和 StrictMode 重挂载时复用，避免同一来源重复提交。
  // 本页签固定 storyboard 模式：分镜表 + 提示词 + 口播 + 摘要一次同出。
  const startTask = useCallback(
    async (run: LongReplicaRun, forceNew = false) => {
      const pending =
        cache.starting ??
        (forceNew
          ? startReplicaPromptTask(
              run.projectId,
              run.assetId,
              true,
              "storyboard",
            )
          : startReplicaPromptTask(
              run.projectId,
              run.assetId,
              false,
              "storyboard",
            ));
      cache.starting = pending;
      try {
        const task = await pending;
        if (
          isCurrentLongReplicaSession(accountId, cache) &&
          cache.starting === pending
        ) {
          cache.run = { ...run, taskId: task.id };
        }
        return task;
      } finally {
        if (cache.starting === pending) cache.starting = null;
      }
    },
    [accountId, cache],
  );

  const awaitTaskRun = useCallback(
    (run: LongReplicaRun, session: number) => {
      pollAbortRef.current?.abort();
      const controller = new AbortController();
      pollAbortRef.current = controller;
      void (async () => {
        try {
          const task = await waitForReplicaPromptTask(
            run.taskId,
            undefined,
            controller.signal,
          );
          if (!isActive(session)) return;
          await showTerminalTask(task, run, session);
        } catch (cause: unknown) {
          showFailure(cause, session);
        }
      })();
    },
    [isActive, showFailure, showTerminalTask],
  );

  const resumeRun = useCallback(
    (run: LongReplicaRun) => {
      const session = ++sessionRef.current;
      setCurrentRun({ ...run, taskId: "" });
      setStage("analyzing");
      setError("");
      void (async () => {
        try {
          if (cache.pendingAutoStart || cache.starting) {
            cache.pendingAutoStart = false;
            const task = await startTask(run);
            if (!isActive(session)) return;
            const startedRun = { ...run, taskId: task.id };
            setCurrentRun(startedRun);
            awaitTaskRun(startedRun, session);
            return;
          }
          const task = await getLatestReplicaPromptTask(
            run.projectId,
            "storyboard",
          );
          if (!isActive(session)) return;
          if (!task || task.asset_id !== run.assetId) {
            cache.run = null;
            cache.result = null;
            setCurrentRun(null);
            setResult(null);
            setStage("idle");
            return;
          }
          const restoredRun = { ...run, taskId: task.id };
          // 服务端可能已由另一个页面启动新任务；更新任务定位时不能保留旧任务的结果。
          if (task.id !== run.taskId) {
            cache.result = null;
            resultTaskIdRef.current = null;
            setResult(null);
          }
          cache.run = restoredRun;
          setCurrentRun(restoredRun);
          if (task.status === "SUCCEEDED" || task.status === "FAILED") {
            await showTerminalTask(task, restoredRun, session);
            return;
          }
          awaitTaskRun(restoredRun, session);
        } catch (cause: unknown) {
          showFailure(cause, session);
        }
      })();
    },
    [awaitTaskRun, cache, isActive, showFailure, showTerminalTask, startTask],
  );

  useEffect(() => {
    if (resumedRef.current || !cache.run) return;
    resumedRef.current = true;
    resumeRun(cache.run);
  }, [cache, resumeRun]);

  const validateAndStage = async (file: File) => {
    const selection = ++selectionRef.current;
    const validationSession = sessionRef.current;
    if (!isActive(validationSession)) return;
    if (file.size > MAX_SOURCE_BYTES) {
      notify("长视频不能超过 600 MB，请压缩后再上传。");
      return;
    }
    let duration: number | undefined;
    try {
      duration = await readVideoDuration(file);
    } catch {
      duration = undefined;
    }
    // 读取本地时长期间也可能换账号，读完后再次核对再进入待确认态。
    if (!isActive(validationSession) || selection !== selectionRef.current)
      return;
    if (typeof duration === "number") {
      if (duration > MAX_SOURCE_SECONDS + DURATION_TOLERANCE_SECONDS) {
        notify("长视频时长不能超过 180 秒，请裁剪后再上传。");
        return;
      }
      if (duration < MIN_SOURCE_SECONDS - DURATION_TOLERANCE_SECONDS) {
        notify(
          "长视频拆解仅支持 30 秒以上的视频，30 秒以内请到「<30s 视频拆解」页签。",
        );
        return;
      }
    }
    // 进入待确认占位态：文件信息卡 + 「开始拆解」按钮；不上传、不提交。
    abortRef.current?.abort();
    pollAbortRef.current?.abort();
    cache.run = null;
    cache.result = null;
    resultTaskIdRef.current = null;
    cache.pendingAutoStart = false;
    cache.starting = null;
    setCurrentRun(null);
    setResult(null);
    setUploadPercent(-1);
    setPendingFile(file);
    setPendingDuration(duration);
    setStage("idle");
    setError("");
    setCancelError("");
  };

  const startPendingUpload = async () => {
    const file = pendingFile;
    if (!file) return;
    // 新文件通过校验才替换实际任务；无效选择不能让已有上传的回调失效。
    const session = ++sessionRef.current;
    abortRef.current?.abort();
    pollAbortRef.current?.abort();
    const abortController = new AbortController();
    abortRef.current = abortController;
    cache.run = null;
    cache.result = null;
    resultTaskIdRef.current = null;
    cache.pendingAutoStart = false;
    cache.starting = null;
    setCurrentRun(null);
    setResult(null);
    setPendingFile(null);
    setUploadPercent(0);
    setStage("uploading");
    setError("");
    setCancelError("");
    notify("正在上传长视频…");
    let notifiedStep = -1;
    try {
      const uploaded = await uploadWorkbenchSourceVideo(
        file,
        (percent) => {
          if (!isActive(session)) return;
          setUploadPercent(percent);
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
      if (!isActive(session)) return;
      setUploadPercent(-1);
      setStage("analyzing");
      const run = { ...uploaded, taskId: "" };
      cache.run = run;
      setCurrentRun(run);
      const task = await startTask(run);
      if (!isActive(session)) return;
      const startedRun = { ...run, taskId: task.id };
      setCurrentRun(startedRun);
      notify("上传完成，已开始长视频拆解，预计 5–15 分钟。");
      awaitTaskRun(startedRun, session);
    } catch (cause: unknown) {
      if (!isActive(session)) return;
      setUploadPercent(-1);
      showFailure(cause, session);
    }
  };

  const handleCancel = async () => {
    const run = currentRun;
    const session = sessionRef.current;
    if (!run?.taskId || cancelPendingRef.current || !isActive(session)) return;
    cancelPendingRef.current = true;
    setCancelling(true);
    setCancelError("");
    try {
      const task = await cancelReplicaPromptTask(run.taskId);
      if (!isActive(session)) return;
      if (task.status === "SUCCEEDED" || task.status === "FAILED") {
        // 所有已确认终态都停止旧轮询，迟到的网络错误不能覆盖成功结果。
        const terminalSession = ++sessionRef.current;
        pollAbortRef.current?.abort();
        if (task.status === "FAILED" && task.error_code === CANCELLED_CODE) {
          setStage("cancelled");
          setError("");
          notify("本次长视频拆解已取消。");
        } else {
          await showTerminalTask(task, run, terminalSession).catch((cause) =>
            showFailure(cause, terminalSession),
          );
        }
      } else {
        setCancelError("取消尚未确认，任务仍在跟踪中，请稍后重试。");
      }
    } catch {
      if (isActive(session)) {
        setCancelError("取消结果暂未确认，任务仍在跟踪中，请稍后重试。");
      }
    } finally {
      if (mountedRef.current && isCurrentLongReplicaSession(accountId, cache)) {
        cancelPendingRef.current = false;
        setCancelling(false);
      }
    }
  };

  const handleRetry = () => {
    const previous = currentRun;
    if (!previous || cancelPendingRef.current) return;
    const session = ++sessionRef.current;
    if (!isActive(session)) return;
    cache.result = null;
    resultTaskIdRef.current = null;
    const pendingRun = { ...previous, taskId: "" };
    cache.run = pendingRun;
    setCurrentRun(pendingRun);
    setResult(null);
    setStage("analyzing");
    setError("");
    setCancelError("");
    void (async () => {
      try {
        const task = await startTask(previous, true);
        if (!isActive(session)) return;
        const run = { ...previous, taskId: task.id };
        setCurrentRun(run);
        awaitTaskRun(run, session);
      } catch (cause: unknown) {
        showFailure(cause, session);
      }
    })();
  };

  const openHistory = async () => {
    const session = sessionRef.current;
    if (!isActive(session)) return;
    setHistoryOpen(true);
    setHistoryLoading(true);
    setHistoryError("");
    try {
      const items = await fetchReplicaPromptHistory();
      if (!isActive(session)) return;
      setHistoryItems(items);
    } catch (cause: unknown) {
      if (!isActive(session)) return;
      setHistoryError(
        cause instanceof Error ? cause.message : "读取拆解历史失败。",
      );
    } finally {
      if (isActive(session)) setHistoryLoading(false);
    }
  };

  const loadHistoryVersion = async (versionId: string) => {
    const session = ++sessionRef.current;
    if (!isActive(session)) return;
    try {
      const version = await getReplicaPromptVersionById(versionId);
      if (!isActive(session) || !version) return;
      abortRef.current?.abort();
      pollAbortRef.current?.abort();
      // 历史是选定版本的快照，重新进入页面时不能接回旧任务并覆盖它。
      cache.run = null;
      cache.pendingAutoStart = false;
      cache.starting = null;
      cache.result = version;
      setCurrentRun(null);
      setPendingFile(null);
      setPendingDuration(undefined);
      setResult(version);
      resultTaskIdRef.current = null;
      setHistoryOpen(false);
      setError("");
      setCancelError("");
      setStage("ready");
    } catch (cause: unknown) {
      if (!isActive(session)) return;
      setHistoryError(
        cause instanceof Error ? cause.message : "载入历史版本失败。",
      );
    }
  };

  const payload: ReplicaPromptResult | null = result
    ? {
        schema_version: String(result.payload.schema_version ?? ""),
        summary: {
          theme: String(result.payload.summary?.theme ?? ""),
          visual_style: String(result.payload.summary?.visual_style ?? ""),
          pace: String(result.payload.summary?.pace ?? ""),
          camera_language: String(
            result.payload.summary?.camera_language ?? "",
          ),
          color_tone: String(result.payload.summary?.color_tone ?? ""),
        },
        spoken_script: readReplicaPromptSpokenScript(
          result.payload.spoken_script,
        ),
        shots: readReplicaPromptShots(result.payload.shots),
        source_review: readSourceReview(result.payload.source_review),
        replica_prompt: String(result.payload.replica_prompt ?? ""),
        warnings: [
          ...readShotConsistencyIssues(result.payload.warnings),
          ...readShotConsistencyIssues(result.payload.consistency_issues),
        ].filter(
          (issue, index, issues) =>
            issues.findIndex(
              (item) =>
                item.code === issue.code &&
                item.shot_id === issue.shot_id &&
                item.message === issue.message,
            ) === index,
        ),
      }
    : null;

  const copyShotTable = async () => {
    if (!payload?.shots?.length) return;
    try {
      await navigator.clipboard.writeText(
        formatShotCardsForClipboard(payload.shots, {
          ...payload.summary,
          source_review: payload.source_review,
        }),
      );
      notify("分镜表已复制，包含结构化事实；导入后仍需核对转换结果。");
    } catch {
      notify("复制失败，请检查剪贴板权限后重试。");
    }
  };

  return (
    <section className="creation-page creation-long-replica">
      <CreationNavigation />
      <div className="long-replica">
        <input
          ref={fileInputRef}
          accept="video/mp4,video/quicktime,.mp4,.mov"
          hidden
          onChange={(event) => {
            const file = event.target.files?.[0];
            event.target.value = "";
            if (file) void validateAndStage(file);
          }}
          type="file"
        />
        {/* 页签名已是「长视频拆解」，页内不再重复标题与长说明：产出/限制
            信息收敛进空态描述（去冗余 2026-11-15），预计时长只在拆解进行态
            提示。头部按钮仅失败/取消态保留——那是当时唯一的重传入口；
            idle 态由空态卡 CTA 承接，避免同一屏两个 gold 按钮抢焦点。 */}
        {stage === "failed" || stage === "cancelled" ? (
          <header className="long-replica-head">
            <span />
            <Button
              variant="primary"
              disabled={cancelling}
              onClick={() => fileInputRef.current?.click()}
            >
              重新上传视频
            </Button>
          </header>
        ) : null}

        {stage === "uploading" ? (
          <Panel className="long-replica-progress">
            <p role="status">
              正在上传视频…{" "}
              {uploadPercent >= 0 ? `${Math.round(uploadPercent)}%` : ""}
            </p>
          </Panel>
        ) : null}

        {stage === "analyzing" ? (
          <Panel className="long-replica-progress">
            <p role="status">
              <Icon name="clock" size={16} /> 正在看片拆解… 已进行{" "}
              {formatSeconds(elapsed)}
            </p>
            <p className="status-note">
              长视频拆解比 15 秒片段慢，预计 5–15
              分钟；可以先去处理其他事情，稍后回到本页查看。
            </p>
            {cancelError ? <p role="alert">{cancelError}</p> : null}
            {currentRun?.taskId ? (
              <Button
                variant="outline"
                disabled={cancelling}
                onClick={() => void handleCancel()}
              >
                {cancelling ? "正在取消…" : "取消拆解"}
              </Button>
            ) : null}
          </Panel>
        ) : null}

        {stage === "failed" && error ? (
          <Panel className="long-replica-progress">
            <p role="alert" className="settings-error">
              {error}
            </p>
            {currentRun ? (
              <Button
                variant="primary"
                disabled={cancelling}
                onClick={handleRetry}
              >
                重新拆解
              </Button>
            ) : null}
          </Panel>
        ) : null}

        {stage === "cancelled" ? (
          <Panel className="long-replica-progress">
            <p role="status">
              本次长视频拆解已取消。积分变动请以消费记录为准。
            </p>
            <Button
              variant="outline"
              disabled={cancelling}
              onClick={handleRetry}
            >
              重新拆解
            </Button>
          </Panel>
        ) : null}

        {stage === "ready" && payload ? (
          <div className="long-replica-results">
            <header className="long-replica-head">
              <span />
              <Button variant="outline" onClick={() => void openHistory()}>
                拆解历史
              </Button>
            </header>
            <SourceReviewNotice review={payload.source_review} />
            {payload.warnings?.length ? (
              <Panel className="long-replica-warnings">
                <ul role="status">
                  {payload.warnings.map((warning) => (
                    <li
                      key={`${warning.code}:${warning.shot_id ?? ""}:${warning.message}`}
                    >
                      {warning.shot_id ? `${warning.shot_id}：` : ""}
                      {warning.message}
                    </li>
                  ))}
                </ul>
              </Panel>
            ) : null}
            {payload.shots?.length ? (
              <section className="long-replica-block">
                <div className="long-replica-block__head">
                  <h3>分镜表（{payload.shots.length} 段）</h3>
                  <Button
                    variant="outline"
                    onClick={() => void copyShotTable()}
                  >
                    复制分镜表
                  </Button>
                </div>
                <div className="long-replica-shots">
                  <table>
                    <thead>
                      <tr>
                        <th>#</th>
                        <th>时间</th>
                        <th>景别</th>
                        <th>运镜</th>
                        <th>主体</th>
                        <th>动作与画面变化</th>
                        <th>运动</th>
                        <th>分段</th>
                        <th>场景</th>
                        <th>口播</th>
                        <th>衔接</th>
                      </tr>
                    </thead>
                    <tbody>
                      {payload.shots.map((sourceShot, index) => {
                        const shot = shotFactsView(sourceShot);
                        const facts = readShotFacts(shot.facts);
                        return (
                          <tr key={shot.shot_id ?? `shot-${index}`}>
                            <td>{shot.shot_id ?? index + 1}</td>
                            <td className="long-replica-shots__time">
                              {formatSeconds(shot.start_time)}-
                              {formatSeconds(shot.end_time)}
                            </td>
                            <td>{shot.shot_type || "—"}</td>
                            <td>{shot.camera_motion || "—"}</td>
                            <td>{shot.subject || "—"}</td>
                            <td>{shot.action || "—"}</td>
                            <td>
                              {facts ? (
                                <>
                                  {formatShotFacts(facts)}
                                  <ShotFactEvidence facts={facts} />
                                </>
                              ) : shot.facts != null ? (
                                "结构化事实待确认"
                              ) : (
                                formatShotMotion(shot.motion) || "未标注"
                              )}
                            </td>
                            <td>
                              {[
                                shot.segment_kind === "ACTION_BEAT"
                                  ? "连续动作段"
                                  : shot.segment_kind === "SHOT_CUT"
                                    ? "切镜"
                                    : "",
                                shot.boundary_reason,
                              ]
                                .filter(Boolean)
                                .join(" · ") || "—"}
                            </td>
                            <td>
                              {[
                                shot.scene,
                                shot.scene_dressing,
                                shot.scene_lighting,
                                shot.wardrobe_pose_detail,
                                shot.ambient_sound,
                                shot.music_style_hint,
                              ]
                                .filter(Boolean)
                                .join("；") || "—"}
                            </td>
                            <td>{shot.spoken_text || "—"}</td>
                            <td>{shot.transition || "—"}</td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                </div>
              </section>
            ) : null}
            <CopyBlock
              emptyHint="本次拆解没有产出复刻提示词。"
              text={payload.replica_prompt}
              title="复刻提示词"
            />
            <CopyBlock
              emptyHint="这条视频没有可辨识的口播。"
              title="逐句口播文案"
              text={payload.spoken_script
                .map(
                  (line) =>
                    `${formatSeconds(line.start_time)}-${formatSeconds(line.end_time)} ${line.text}`,
                )
                .join("\n")}
            >
              {payload.spoken_script.length ? (
                <ol className="long-replica-script">
                  {payload.spoken_script.map((line, index) => (
                    // biome-ignore lint/suspicious/noArrayIndexKey: 每次渲染由结果重新解析的纯展示列表，无重排/增删，顺序即内容。
                    <li key={`${line.start_time ?? "na"}-${index}`}>
                      <span className="long-replica-script__time">
                        {formatSeconds(line.start_time)}-
                        {formatSeconds(line.end_time)}
                      </span>
                      <span>{line.text}</span>
                    </li>
                  ))}
                </ol>
              ) : null}
            </CopyBlock>
            <section className="long-replica-block">
              <div className="long-replica-block__head">
                <h3>全片摘要</h3>
              </div>
              <dl className="long-replica-summary">
                {SUMMARY_FIELDS.map((field) => (
                  <div className="long-replica-summary__row" key={field.key}>
                    <dt>{field.label}</dt>
                    <dd>{payload.summary[field.key] || "—"}</dd>
                  </div>
                ))}
              </dl>
            </section>
          </div>
        ) : null}

        {stage === "idle" ? (
          <div className="long-replica-idle">
            <header className="long-replica-head">
              <span />
              <Button variant="outline" onClick={() => void openHistory()}>
                拆解历史
              </Button>
            </header>
            {pendingFile ? (
              <Panel className="long-replica-pending">
                <p className="long-replica-pending__name">{pendingFile.name}</p>
                <p className="status-note">
                  {typeof pendingDuration === "number"
                    ? `时长 ${formatSeconds(pendingDuration)}`
                    : "时长未知"}{" "}
                  · {(pendingFile.size / (1024 * 1024)).toFixed(1)} MB
                </p>
                <p className="status-note">
                  确认后开始上传并拆解：AI 输出分镜表、复刻提示词、口播文案与
                  全片摘要，预计 5–15 分钟。
                </p>
                <div className="long-replica-pending__actions">
                  <Button
                    variant="primary"
                    onClick={() => void startPendingUpload()}
                  >
                    开始拆解
                  </Button>
                  <Button
                    variant="outline"
                    onClick={() => fileInputRef.current?.click()}
                  >
                    重新选择
                  </Button>
                </div>
              </Panel>
            ) : (
              // biome-ignore lint/a11y/noStaticElementInteractions: 拖拽上传区域依赖文件拖放；键盘用户的等效路径是「选择文件」按钮。
              <div
                className={
                  dragOver
                    ? "long-replica-dropzone is-dragover"
                    : "long-replica-dropzone"
                }
                onDragOver={(event) => {
                  event.preventDefault();
                  setDragOver(true);
                }}
                onDragLeave={() => setDragOver(false)}
                onDrop={(event) => {
                  event.preventDefault();
                  setDragOver(false);
                  const file = event.dataTransfer.files?.[0];
                  if (file) void validateAndStage(file);
                }}
              >
                <p className="long-replica-dropzone__title">
                  拖拽视频到此处，或
                </p>
                <Button
                  variant="primary"
                  onClick={() => fileInputRef.current?.click()}
                >
                  选择 MP4 / MOV 文件
                </Button>
                <p className="long-replica-dropzone__hint">
                  支持 30 秒–180 秒、600 MB 内的视频，按次计费。AI 拆解出
                  分镜表、复刻提示词、口播文案与全片摘要。
                </p>
              </div>
            )}
          </div>
        ) : null}

        {historyOpen ? (
          <div
            className="long-replica-history"
            role="dialog"
            aria-label="长视频拆解历史"
          >
            <div className="long-replica-history__panel">
              <div className="long-replica-history__head">
                <h3>拆解历史</h3>
                <Button variant="outline" onClick={() => setHistoryOpen(false)}>
                  关闭
                </Button>
              </div>
              {historyLoading ? (
                <p role="status">正在读取拆解历史…</p>
              ) : historyError ? (
                <p role="alert" className="settings-error">
                  {historyError}
                </p>
              ) : historyItems.length === 0 ? (
                <p>当前账号还没有长视频拆解记录。</p>
              ) : (
                <ul className="long-replica-history__list">
                  {historyItems.map((item) => (
                    <li key={item.id}>
                      <span className="long-replica-history__meta">
                        {item.kind === "long_analysis"
                          ? "分镜表拆解"
                          : "提示词拆解"}{" "}
                        · 第 {item.version_number} 版 · {item.created_at}
                      </span>
                      <Button
                        variant="outline"
                        onClick={() => void loadHistoryVersion(item.id)}
                      >
                        载入此版本
                      </Button>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          </div>
        ) : null}
      </div>
    </section>
  );
}
