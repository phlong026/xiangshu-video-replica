import {
  type ReactNode,
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
} from "react";
import type {
  ReplicaPromptResult,
  ReplicaPromptTask,
  ReplicaPromptVersion,
} from "../api";
import {
  cancelReplicaPromptTask,
  getLatestReplicaPromptResult,
  getLatestReplicaPromptTask,
  startReplicaPromptTask,
  waitForReplicaPromptTask,
} from "../api";
import { isInsufficientCredits } from "../insufficientCredits";
import { CreationNavigation } from "./CreationNavigation";
import { useStudio } from "./context";
import { readVideoDuration, uploadWorkbenchSourceVideo } from "./live";
import {
  getLongReplicaSession,
  isCurrentLongReplicaSession,
  type LongReplicaRun,
} from "./longReplicaSession";
import { Button, Empty, Icon, Panel } from "./ui";

import "./longReplica.css";

// 与服务端 media.py 的 replica_prompt 门禁同口径；放开时两边一起改。
const MAX_SOURCE_BYTES = 600 * 1024 * 1024;
const MIN_SOURCE_SECONDS = 4;
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
  const total = Math.round(value);
  const minutes = Math.floor(total / 60);
  const seconds = total % 60;
  return `${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;
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
  const [uploadPercent, setUploadPercent] = useState(-1);
  const [elapsed, setElapsed] = useState(0);
  const [cancelling, setCancelling] = useState(false);
  const [cancelError, setCancelError] = useState("");
  const sessionRef = useRef(0);
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
      const version = await getLatestReplicaPromptResult(run.projectId);
      if (!isActive(session)) return;
      cache.result = version;
      setResult(version);
      setCancelError("");
      setStage(version ? "ready" : "idle");
    },
    [cache, isActive, showFailure],
  );

  // 创建请求在页间恢复和 StrictMode 重挂载时复用，避免同一来源重复提交。
  const startTask = useCallback(
    async (run: LongReplicaRun, forceNew = false) => {
      const pending =
        cache.starting ??
        (forceNew
          ? startReplicaPromptTask(run.projectId, run.assetId, true)
          : startReplicaPromptTask(run.projectId, run.assetId));
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
          const task = await getLatestReplicaPromptTask(run.projectId);
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

  const handleUpload = async (file: File) => {
    const session = ++sessionRef.current;
    if (!isActive(session)) return;
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
    // 读取本地时长期间也可能换账号，必须在发起任何上传前再次核对。
    if (!isActive(session)) return;
    if (typeof duration === "number") {
      if (duration > MAX_SOURCE_SECONDS + DURATION_TOLERANCE_SECONDS) {
        notify("长视频时长不能超过 180 秒，请裁剪后再上传。");
        return;
      }
      if (duration < MIN_SOURCE_SECONDS - DURATION_TOLERANCE_SECONDS) {
        notify("长视频时长不能少于 4 秒，请更换素材。");
        return;
      }
    }
    abortRef.current?.abort();
    const abortController = new AbortController();
    abortRef.current = abortController;
    cache.run = null;
    cache.result = null;
    cache.pendingAutoStart = false;
    cache.starting = null;
    setCurrentRun(null);
    setResult(null);
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
        spoken_script: Array.isArray(result.payload.spoken_script)
          ? result.payload.spoken_script
          : [],
        replica_prompt: String(result.payload.replica_prompt ?? ""),
        warnings: Array.isArray(result.payload.warnings)
          ? result.payload.warnings
          : [],
      }
    : null;

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
            if (file) void handleUpload(file);
          }}
          type="file"
        />
        <header className="long-replica-head">
          <div>
            <h2>长视频拆解</h2>
            <p className="status-note">
              上传 4–180 秒的爆款视频，AI
              看完整片后产出一段可直接使用的复刻提示词、
              逐句口播文案与全片摘要。拆解预计 5–15
              分钟，可以离开本页稍后回来查看。
            </p>
          </div>
          {stage === "idle" || stage === "failed" || stage === "cancelled" ? (
            <Button
              variant="primary"
              disabled={cancelling}
              onClick={() => fileInputRef.current?.click()}
            >
              {stage === "failed" ? "重新上传视频" : "上传长视频"}
            </Button>
          ) : null}
        </header>

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
            {payload.warnings?.length ? (
              <Panel className="long-replica-warnings">
                <ul role="status">
                  {payload.warnings.map((warning) => (
                    <li key={warning.code}>{warning.message}</li>
                  ))}
                </ul>
              </Panel>
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
          <Empty
            action={
              <Button
                variant="primary"
                onClick={() => fileInputRef.current?.click()}
              >
                选择 MP4 / MOV 文件
              </Button>
            }
            description="支持 4–180 秒、600 MB 以内的 MP4 / MOV；提交后按次计费。"
            title="还没有上传长视频"
          />
        ) : null}
      </div>
    </section>
  );
}
