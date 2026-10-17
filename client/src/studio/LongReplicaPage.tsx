import {
  type ReactNode,
  useCallback,
  useEffect,
  useRef,
  useState,
} from "react";
import type { ReplicaPromptResult, ReplicaPromptVersion } from "../api";
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
import { Button, Empty, Icon, Panel } from "./ui";

import "./longReplica.css";

// 与服务端 media.py 的 replica_prompt 门禁同口径；放开时两边一起改。
const MAX_SOURCE_BYTES = 600 * 1024 * 1024;
const MIN_SOURCE_SECONDS = 4;
const MAX_SOURCE_SECONDS = 180;
const DURATION_TOLERANCE_SECONDS = 0.1;
const COPY_FEEDBACK_MS = 2000;

type Stage = "idle" | "uploading" | "analyzing" | "ready" | "failed";

type LastRun = {
  projectId: string;
  assetId: string;
  taskId: string;
};

// 会话内记忆最近一次运行：离开页面再回来时凭它接回在途任务/已出结果。
// 跨重启不恢复——重新上传即重建项目，云端草稿里没有这个链路的合同。
let lastRun: LastRun | null = null;
let lastResult: ReplicaPromptVersion | null = null;

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
  const { notify, openLive } = useStudio();
  const [stage, setStage] = useState<Stage>("idle");
  const [error, setError] = useState("");
  const [result, setResult] = useState<ReplicaPromptVersion | null>(lastResult);
  const [uploadPercent, setUploadPercent] = useState(-1);
  const [elapsed, setElapsed] = useState(0);
  const [, setTick] = useState(0);
  const stageRef = useRef<Stage>("idle");
  const sessionRef = useRef(0);
  const abortRef = useRef<AbortController | null>(null);
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const resumedRef = useRef(false);
  stageRef.current = stage;

  useEffect(() => {
    return () => {
      sessionRef.current += 1;
      abortRef.current?.abort();
    };
  }, []);

  useEffect(() => {
    if (stage !== "analyzing") return;
    setElapsed(0);
    const startedAt = Date.now();
    const timer = window.setInterval(() => {
      setElapsed(Math.floor((Date.now() - startedAt) / 1000));
    }, 1000);
    return () => window.clearInterval(timer);
  }, [stage]);

  const resumeRun = useCallback((run: LastRun) => {
    const session = ++sessionRef.current;
    setStage("analyzing");
    setError("");
    void (async () => {
      try {
        const task = await getLatestReplicaPromptTask(run.projectId);
        if (session !== sessionRef.current) return;
        if (!task || task.asset_id !== run.assetId) {
          setStage(lastResult ? "ready" : "idle");
          return;
        }
        if (task.status === "SUCCEEDED") {
          const version = await getLatestReplicaPromptResult(run.projectId);
          if (session !== sessionRef.current) return;
          if (version) {
            lastResult = version;
            setResult(version);
            setStage("ready");
          } else {
            setStage("idle");
          }
          return;
        }
        if (task.status === "FAILED") {
          setStage("failed");
          setError(task.error_message || "长视频拆解失败，请重新提交。");
          return;
        }
        await waitForReplicaPromptTask(task.id, (updated) => {
          if (session !== sessionRef.current) return;
          setTick((value) => value + 1);
          if (updated.status === "RUNNING") setStage("analyzing");
        });
        if (session !== sessionRef.current) return;
        const version = await getLatestReplicaPromptResult(run.projectId);
        if (session !== sessionRef.current) return;
        if (!version) {
          setStage("idle");
          return;
        }
        lastResult = version;
        setResult(version);
        setStage("ready");
      } catch (cause: unknown) {
        if (session !== sessionRef.current) return;
        setStage("failed");
        setError(
          cause instanceof Error
            ? cause.message
            : "长视频拆解失败，请重新提交。",
        );
      }
    })();
  }, []);

  // 等一个已知 taskId 的任务跑到终态并拉取结果（上传启动 / 重新拆解共用）。
  const awaitTaskRun = useCallback(
    (run: LastRun, session: number) => {
      void (async () => {
        try {
          await waitForReplicaPromptTask(run.taskId, () => {
            if (session !== sessionRef.current) return;
            setTick((value) => value + 1);
          });
          if (session !== sessionRef.current) return;
          const version = await getLatestReplicaPromptResult(run.projectId);
          if (session !== sessionRef.current) return;
          if (!version) {
            setStage("idle");
            return;
          }
          lastResult = version;
          setResult(version);
          setStage("ready");
        } catch (cause: unknown) {
          if (session !== sessionRef.current) return;
          setStage("failed");
          if (isInsufficientCredits(cause)) {
            setError("积分余额不足，请先充值后重试。");
            openLive("wallet");
            return;
          }
          setError(
            cause instanceof Error
              ? cause.message
              : "长视频拆解失败，请重新提交。",
          );
        }
      })();
    },
    [openLive],
  );

  // 挂载接回：会话内有过运行（含正在跑的）就恢复，不要求用户重新上传。
  useEffect(() => {
    if (resumedRef.current || !lastRun) return;
    resumedRef.current = true;
    resumeRun(lastRun);
  }, [resumeRun]);

  const handleUpload = async (file: File) => {
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
    const session = ++sessionRef.current;
    const abortController = new AbortController();
    abortRef.current = abortController;
    setUploadPercent(0);
    setStage("uploading");
    setError("");
    notify("正在上传长视频…");
    let notifiedStep = -1;
    try {
      const uploaded = await uploadWorkbenchSourceVideo(
        file,
        (percent) => {
          if (session !== sessionRef.current) return;
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
      if (session !== sessionRef.current) return;
      setUploadPercent(-1);
      lastRun = {
        projectId: uploaded.projectId,
        assetId: uploaded.assetId,
        taskId: "",
      };
      notify("上传完成，已开始长视频拆解，预计 5–15 分钟。");
      const task = await startReplicaPromptTask(
        uploaded.projectId,
        uploaded.assetId,
      );
      if (session !== sessionRef.current) return;
      const run: LastRun = { ...lastRun, taskId: task.id };
      lastRun = run;
      awaitTaskRun(run, session);
    } catch (cause: unknown) {
      if (session !== sessionRef.current) return;
      setUploadPercent(-1);
      setStage("failed");
      if (isInsufficientCredits(cause)) {
        setError("积分余额不足，请先充值后重试。");
        openLive("wallet");
        return;
      }
      setError(
        cause instanceof Error ? cause.message : "上传失败，请检查网络后重试。",
      );
    }
  };

  const handleCancel = async () => {
    const run = lastRun;
    if (!run?.taskId) return;
    try {
      await cancelReplicaPromptTask(run.taskId);
    } catch {
      // 取消失败不阻断界面：任务可能刚好完成，等轮询收尾给出终态。
    }
    sessionRef.current += 1;
    setStage(lastResult ? "ready" : "idle");
    notify("已取消本次长视频拆解，预留积分将退回。");
  };

  const handleRetry = () => {
    const previous = lastRun;
    if (!previous) return;
    const session = ++sessionRef.current;
    setStage("analyzing");
    setError("");
    void (async () => {
      try {
        const task = await startReplicaPromptTask(
          previous.projectId,
          previous.assetId,
          true,
        );
        if (session !== sessionRef.current) return;
        const run: LastRun = { ...previous, taskId: task.id };
        lastRun = run;
        awaitTaskRun(run, session);
      } catch (cause: unknown) {
        if (session !== sessionRef.current) return;
        setStage("failed");
        if (isInsufficientCredits(cause)) {
          setError("积分余额不足，请先充值后重试。");
          openLive("wallet");
          return;
        }
        setError(
          cause instanceof Error
            ? cause.message
            : "长视频拆解失败，请重新提交。",
        );
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
          {stage === "idle" || stage === "failed" ? (
            <Button
              variant="primary"
              onClick={() => fileInputRef.current?.click()}
            >
              {stage === "failed" ? "重新上传视频" : "上传长视频"}
            </Button>
          ) : null}
        </header>

        {stage === "uploading" ? (
          <Panel className="long-replica-progress">
            <p>
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
            {lastRun?.taskId ? (
              <Button variant="outline" onClick={() => void handleCancel()}>
                取消拆解
              </Button>
            ) : null}
          </Panel>
        ) : null}

        {stage === "failed" && error ? (
          <Panel className="long-replica-progress">
            <p role="alert" className="settings-error">
              {error}
            </p>
            {lastRun ? (
              <Button variant="primary" onClick={handleRetry}>
                重新拆解
              </Button>
            ) : null}
          </Panel>
        ) : null}

        {stage === "ready" && payload ? (
          <div className="long-replica-results">
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
