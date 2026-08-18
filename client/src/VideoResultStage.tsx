import { useEffect, useState, type CSSProperties } from "react";

import type { GenerationBatch, GenerationTask } from "./api";

// 生成结果舞台 = 客户视角的结果消费视图：大预览框 + 真实进度叙事 +
// 等待安抚内容 + 视频信息栏 + 轻量付费再次生成。运维操作（对账、安全
// 重试、确认未计费、整批重生成）保留在任务记录的运维视图，由
// onOpenOpsDetail 引导切换。

// H3 Provider 没有真实百分比进度接口；RUNNING 期间以预估时长做时间
// 插值（ease-out 前快后慢、85% 封顶），阶段锚点来自后端状态机轮询。
const ESTIMATED_RENDER_SECONDS = 240;
const SLOW_RENDER_WARNING_FACTOR = 1.5;
const REASSURANCE_ROTATE_SECONDS = 15;
const TICK_MS = 1_000;

const STEP_LABELS = ["已提交", "排队中", "渲染中", "云端归档", "完成"] as const;

const PHASE_MESSAGES: Record<string, string> = {
  PENDING: "正在准备你的生成任务…",
  SUBMITTING: "正在把首帧与 Prompt 提交给渲染引擎…",
  QUEUED: "已进入渲染队列，即将开始生成…",
  RUNNING: "AI 正在基于你的首帧渲染画面、动作与口型…",
  ARCHIVING: "渲染完成，正在上传云端存储…",
  SUCCEEDED: "已归档，正在自动进行音频质检…",
};

const REASSURANCE_FACTS = [
  "渲染在云端进行，离开此页面不会中断任务，回来时进度仍在。",
  "768P 成片通常需要 2–4 分钟，2K 略久，请放心等待。",
  "生成结果会自动保存在云端，可随时回来播放或下载。",
  "同一批次的多个结果会并行处理，完成一个就能先看一个。",
] as const;

const REGENERATION_REASONS = [
  "画面质量不佳",
  "人物相似度不足",
  "动作不够自然",
  "口型与文案不同步",
  "其他原因",
] as const;

type VideoResultStageProps = {
  activeResultAction: string;
  activeTaskAction: string;
  batch: GenerationBatch;
  canOperate: boolean;
  onDownload: (task: GenerationTask) => void;
  onOpenOpsDetail: () => void;
  onRegenerate: (task: GenerationTask, reason: string) => void;
  onRequestPreview: (task: GenerationTask) => void;
  previewUrls: Record<string, string>;
  resultErrors: Record<string, string>;
};

export function VideoResultStage({
  activeResultAction,
  activeTaskAction,
  batch,
  canOperate,
  onDownload,
  onOpenOpsDetail,
  onRegenerate,
  onRequestPreview,
  previewUrls,
  resultErrors,
}: VideoResultStageProps) {
  const tasks = batch.tasks;
  const [activeTaskId, setActiveTaskId] = useState(() =>
    pickDefaultTaskId(tasks),
  );

  // 批次推进或重生成切换后校正选中任务（选中项被替代/消失时）。
  useEffect(() => {
    if (!tasks.some((task) => task.id === activeTaskId)) {
      setActiveTaskId(pickDefaultTaskId(tasks));
    }
  }, [tasks, activeTaskId]);

  const activeTask = tasks.find((task) => task.id === activeTaskId) ?? tasks[0];
  const batchInProgress = tasks.some(
    (task) => taskOutcome(task) === "in_progress",
  );

  // 本地秒级 tick：驱动进度插值与安抚文案轮换（轮询仍在父级 2s 一次）。
  const [nowMs, setNowMs] = useState(() => Date.now());
  useEffect(() => {
    if (!batchInProgress) {
      return;
    }
    const timer = window.setInterval(() => setNowMs(Date.now()), TICK_MS);
    return () => window.clearInterval(timer);
  }, [batchInProgress]);

  const previewUrl = activeTask ? previewUrls[activeTask.id] : undefined;
  const previewBusy = activeTask
    ? activeResultAction === `${activeTask.id}:preview`
    : false;

  // 完成态自动签发在线播放地址：以视频为主角的视图不应要求手动点
  // 「加载预览」。失败后停止自动重试（resultErrors 门控），改由手动
  // 重试，避免循环拉取。审计只读不签发。
  const activePreviewError = activeTask ? resultErrors[activeTask.id] : "";
  useEffect(() => {
    if (!activeTask || !canOperate) {
      return;
    }
    if (
      !activeTask.result_asset_id ||
      previewUrl ||
      previewBusy ||
      activePreviewError
    ) {
      return;
    }
    onRequestPreview(activeTask);
  }, [
    activeTask,
    canOperate,
    previewUrl,
    previewBusy,
    activePreviewError,
    onRequestPreview,
  ]);

  const [isRegenerateOpen, setIsRegenerateOpen] = useState(false);
  const [regenerateReason, setRegenerateReason] = useState("");
  const [regenerateDetail, setRegenerateDetail] = useState("");
  const [regenerateConfirmed, setRegenerateConfirmed] = useState(false);

  if (!activeTask) {
    return null;
  }

  const outcome = taskOutcome(activeTask);
  const stage = effectiveStage(activeTask);
  const percent =
    outcome === "in_progress" ? stagePercent(activeTask, nowMs) : 100;
  const stepIndex = stepIndexForStage(stage, outcome);
  const elapsed =
    outcome === "in_progress" ? elapsedSeconds(activeTask, nowMs) : 0;
  const remaining = Math.max(0, ESTIMATED_RENDER_SECONDS - elapsed);
  const showSlowWarning =
    outcome === "in_progress" &&
    stage === "RUNNING" &&
    elapsed > ESTIMATED_RENDER_SECONDS * SLOW_RENDER_WARNING_FACTOR;
  const reassuranceIndex =
    Math.floor(elapsed / REASSURANCE_ROTATE_SECONDS) % REASSURANCE_FACTS.length;

  const canRegenerate =
    canOperate && activeTask.available_actions?.includes("REGENERATE");
  const regenerateBusy = Boolean(activeTaskAction);
  const resultError = resultErrors[activeTask.id];

  function handleRegenerateSubmit() {
    const detail = regenerateDetail.trim();
    const reason = detail ? `${regenerateReason}：${detail}` : regenerateReason;
    onRegenerate(activeTask, reason);
  }

  return (
    <section className="video-stage" aria-labelledby="video-stage-title">
      <header className="video-stage-header">
        <div>
          <h2 id="video-stage-title">生成结果</h2>
          <p className="video-stage-batch-note">
            {batch.progress.terminal_count} / {batch.progress.total_count}{" "}
            个结果已完成
            {batch.source_batch_id ? " · 冻结输入重生成批次" : ""}
          </p>
        </div>
        <span
          className={`batch-status batch-status--${batch.status.toLowerCase()}`}
        >
          {formatStatus(batch.status)}
        </span>
      </header>

      <div className="video-stage-player">
        {outcome === "quality_failed" && activeTask.result_asset_id ? (
          <p className="video-stage-quality-banner" role="status">
            音频质检未通过：该结果仅供对比查看，建议再次生成。
          </p>
        ) : null}
        {outcome === "in_progress" ? (
          <StageProgressView
            elapsed={elapsed}
            factIndex={reassuranceIndex}
            percent={percent}
            phaseMessage={PHASE_MESSAGES[stage] ?? "正在生成…"}
            remaining={remaining}
            showSlowWarning={showSlowWarning}
            stepIndex={stepIndex}
          />
        ) : previewUrl ? (
          <video
            aria-label={`结果预览 ${activeTask.id}`}
            autoPlay
            className="video-stage-video"
            controls
            loop
            muted
            playsInline
            preload="auto"
            src={previewUrl}
          />
        ) : activeTask.result_asset_id && !canOperate ? (
          <p className="video-stage-player-note">
            审计只读，不可预览或下载结果
          </p>
        ) : activeTask.result_asset_id ? (
          <p className="video-stage-player-note" role="status">
            {previewBusy ? "正在打开在线播放…" : "正在准备播放地址…"}
          </p>
        ) : (
          <StageOutcomeView outcome={outcome} task={activeTask} />
        )}
        {resultError ? (
          <p className="task-error-summary" role="status">
            {resultError}{" "}
            {canOperate && activeTask.result_asset_id && !previewUrl ? (
              <button
                className="link-button"
                onClick={() => onRequestPreview(activeTask)}
                type="button"
              >
                重试加载
              </button>
            ) : null}
          </p>
        ) : null}
      </div>

      <StageInfoBar task={activeTask} />

      <div className="video-stage-actions">
        {canOperate && activeTask.result_asset_id ? (
          <button
            disabled={activeResultAction === `${activeTask.id}:download`}
            onClick={() => onDownload(activeTask)}
            type="button"
          >
            下载 MP4
          </button>
        ) : null}
        {canRegenerate ? (
          <button
            className="secondary-button"
            disabled={regenerateBusy}
            onClick={() => {
              setIsRegenerateOpen(!isRegenerateOpen);
              setRegenerateReason("");
              setRegenerateDetail("");
              setRegenerateConfirmed(false);
            }}
            type="button"
          >
            {isRegenerateOpen ? "收起再次生成" : "再次生成"}
          </button>
        ) : null}
        {outcome === "needs_attention" || outcome === "failed" ? (
          <button
            className="secondary-button"
            onClick={onOpenOpsDetail}
            type="button"
          >
            查看处理方式
          </button>
        ) : null}
      </div>

      {canRegenerate && isRegenerateOpen ? (
        <section
          aria-label={`再次生成 ${activeTask.id}`}
          className="video-stage-regenerate"
        >
          <strong>付费再次生成</strong>
          <p>
            只复用该任务的冻结 Prompt，新建一次 Provider 调用； 金额快照：
            {formatCost(activeTask.estimated_cost)}
          </p>
          <fieldset>
            <legend>再次生成原因</legend>
            {REGENERATION_REASONS.map((reason) => (
              <label key={reason}>
                <input
                  checked={regenerateReason === reason}
                  name={`regenerate-reason-${activeTask.id}`}
                  onChange={() => setRegenerateReason(reason)}
                  type="radio"
                  value={reason}
                />
                <span>{reason}</span>
              </label>
            ))}
          </fieldset>
          <label>
            <span>补充说明（可选）</span>
            <input
              aria-label={`再次生成补充说明 ${activeTask.id}`}
              disabled={regenerateBusy}
              maxLength={200}
              onChange={(event) => setRegenerateDetail(event.target.value)}
              value={regenerateDetail}
            />
          </label>
          <label className="paid-confirmation-check">
            <input
              aria-label={`确认为任务 ${activeTask.id} 新增一次付费生成`}
              checked={regenerateConfirmed}
              disabled={regenerateBusy}
              onChange={(event) => setRegenerateConfirmed(event.target.checked)}
              type="checkbox"
            />
            <span>我已确认本次将产生一次新的 Provider 付费调用</span>
          </label>
          <button
            disabled={
              !regenerateReason || !regenerateConfirmed || regenerateBusy
            }
            onClick={handleRegenerateSubmit}
            type="button"
          >
            确认再次生成
          </button>
        </section>
      ) : null}

      {tasks.length > 1 ? (
        <nav aria-label="生成结果列表" className="video-stage-filmstrip">
          {tasks.map((task, index) => {
            const taskOutcomeValue = taskOutcome(task);
            const isActive = task.id === activeTask.id;
            const badge =
              taskOutcomeValue === "in_progress"
                ? `${Math.round(stagePercent(task, nowMs))}%`
                : outcomeBadgeLabel(taskOutcomeValue);
            return (
              <button
                aria-label={`查看结果 ${index + 1}：${task.id}`}
                aria-pressed={isActive}
                className={
                  isActive
                    ? "video-stage-cell video-stage-cell--active"
                    : "video-stage-cell"
                }
                key={task.id}
                onClick={() => setActiveTaskId(task.id)}
                type="button"
              >
                <span className="video-stage-cell__index">{index + 1}</span>
                <span
                  className={`video-stage-cell__badge video-stage-cell__badge--${taskOutcomeValue}`}
                >
                  {badge}
                </span>
              </button>
            );
          })}
        </nav>
      ) : null}
    </section>
  );
}

function StageProgressView({
  elapsed,
  factIndex,
  percent,
  phaseMessage,
  remaining,
  showSlowWarning,
  stepIndex,
}: {
  elapsed: number;
  factIndex: number;
  percent: number;
  phaseMessage: string;
  remaining: number;
  showSlowWarning: boolean;
  stepIndex: number;
}) {
  return (
    <div className="video-stage-progress">
      <div
        aria-label="生成进度"
        aria-valuemax={100}
        aria-valuemin={0}
        aria-valuenow={Math.round(percent)}
        className="video-stage-progress-ring"
        role="progressbar"
        style={{ "--stage-percent": `${Math.round(percent)}%` } as CSSProperties}
      >
        <span className="video-stage-progress-ring__value">
          {Math.round(percent)}%
        </span>
      </div>
      <ol aria-label="生成阶段" className="video-stage-steps">
        {STEP_LABELS.map((label, index) => (
          <li
            className={
              index < stepIndex
                ? "video-stage-step video-stage-step--done"
                : index === stepIndex
                  ? "video-stage-step video-stage-step--active"
                  : "video-stage-step"
            }
            key={label}
          >
            <span className="video-stage-step__dot" aria-hidden="true" />
            <span>{label}</span>
          </li>
        ))}
      </ol>
      <p className="video-stage-phase" role="status">
        {phaseMessage}
      </p>
      <p className="video-stage-timing">
        已用时 {formatClock(elapsed)}
        {remaining > 0 ? ` · 预计还需约 ${formatClock(remaining)}` : ""}
      </p>
      <p className="video-stage-fact" key={factIndex}>
        {REASSURANCE_FACTS[factIndex]}
      </p>
      {showSlowWarning ? (
        <p className="video-stage-slow-note" role="status">
          仍在渲染中，任务没有丢失，请放心等待。
        </p>
      ) : null}
    </div>
  );
}

function StageOutcomeView({
  outcome,
  task,
}: {
  outcome: ReturnType<typeof taskOutcome>;
  task: GenerationTask;
}) {
  const copy = outcomeCopy(outcome, task);
  return (
    <div className={`video-stage-outcome video-stage-outcome--${outcome}`}>
      <strong>{copy.title}</strong>
      <p>{copy.detail}</p>
    </div>
  );
}

function StageInfoBar({ task }: { task: GenerationTask }) {
  const resolution = readSnapshotString(task, "resolution");
  const outputDuration = readSnapshotNumber(task, "output_duration_seconds");
  const quality =
    task.quality_status === "AUDIO_OK"
      ? "音频质检通过"
      : task.quality_status === "AUDIO_QUALITY_FAILED"
        ? "音频质检未通过"
        : "质检待完成";
  return (
    <div className="video-stage-info">
      <dl className="video-stage-facts">
        <div>
          <dt>生成模型</dt>
          <dd>{task.model}</dd>
        </div>
        {resolution ? (
          <div>
            <dt>分辨率</dt>
            <dd>{resolution}</dd>
          </div>
        ) : null}
        {outputDuration !== null ? (
          <div>
            <dt>成片时长</dt>
            <dd>{outputDuration} 秒</dd>
          </div>
        ) : null}
        <div>
          <dt>生成耗时</dt>
          <dd>{formatDuration(task.duration_seconds)}</dd>
        </div>
        <div>
          <dt>质检</dt>
          <dd>{quality}</dd>
        </div>
        <div>
          <dt>费用</dt>
          <dd>{formatCost(task.actual_cost ?? task.estimated_cost)}</dd>
        </div>
      </dl>
      <details className="video-stage-tech">
        <summary>技术详情</summary>
        <dl>
          <div>
            <dt>任务 ID</dt>
            <dd>{task.id}</dd>
          </div>
          <div>
            <dt>Provider 尾号</dt>
            <dd>{task.provider_task_id_tail ?? "未公开"}</dd>
          </div>
          <div>
            <dt>尝试次数</dt>
            <dd>
              {task.attempt ?? 0} 次 · 归档重试 {task.archive_retry_count ?? 0}{" "}
              次
            </dd>
          </div>
          <div>
            <dt>提交时间</dt>
            <dd>{formatTimestamp(task.submitted_at)}</dd>
          </div>
        </dl>
      </details>
    </div>
  );
}

type TaskOutcome =
  | "in_progress"
  | "completed"
  | "quality_failed"
  | "failed"
  | "needs_attention"
  | "superseded";

function taskOutcome(task: GenerationTask): TaskOutcome {
  const stage = effectiveStage(task);
  if (task.superseded_by_task_id) {
    return "superseded";
  }
  if (stage === "COMPLETED") {
    return "completed";
  }
  if (stage === "QUALITY_FAILED") {
    return "quality_failed";
  }
  if (stage === "FAILED" || stage === "CANCELLED") {
    return "failed";
  }
  if (stage === "SUBMISSION_UNCERTAIN" || stage === "ARCHIVE_FAILED") {
    return "needs_attention";
  }
  return "in_progress";
}

function effectiveStage(task: GenerationTask): string {
  if (task.stage) {
    return task.stage;
  }
  if (task.archive_status === "ARCHIVE_FAILED") {
    return "ARCHIVE_FAILED";
  }
  if (task.status === "SUCCEEDED" && task.archive_status === "ARCHIVED") {
    return "COMPLETED";
  }
  return task.status;
}

function pickDefaultTaskId(tasks: GenerationTask[]): string {
  const inProgress = tasks.find((task) => taskOutcome(task) === "in_progress");
  if (inProgress) {
    return inProgress.id;
  }
  const viewable = tasks.find((task) => task.result_asset_id);
  return (viewable ?? tasks[0])?.id ?? "";
}

function stagePercent(task: GenerationTask, nowMs: number): number {
  const stage = effectiveStage(task);
  switch (stage) {
    case "PENDING":
      return 3;
    case "SUBMITTING":
      return 8;
    case "QUEUED":
      return 15;
    case "RUNNING": {
      const elapsed = elapsedSeconds(task, nowMs);
      const ratio = Math.min(elapsed / ESTIMATED_RENDER_SECONDS, 1);
      const eased = 1 - (1 - ratio) ** 2;
      if (elapsed > ESTIMATED_RENDER_SECONDS) {
        return Math.min(85, 80 + (elapsed - ESTIMATED_RENDER_SECONDS) / 30);
      }
      return 25 + 55 * eased;
    }
    case "ARCHIVING":
      return 90;
    case "SUCCEEDED":
      return 92;
    default:
      return 0;
  }
}

function stepIndexForStage(stage: string, outcome: TaskOutcome): number {
  if (outcome !== "in_progress") {
    return 4;
  }
  switch (stage) {
    case "PENDING":
    case "SUBMITTING":
      return 0;
    case "QUEUED":
      return 1;
    case "RUNNING":
      return 2;
    case "ARCHIVING":
    case "SUCCEEDED":
      return 3;
    default:
      return 4;
  }
}

function outcomeCopy(outcome: TaskOutcome, task: GenerationTask) {
  switch (outcome) {
    case "failed":
      return {
        detail:
          task.error_message_redacted ??
          "本次生成未成功，可在运维详情查看原因。",
        title: "生成失败",
      };
    case "needs_attention":
      return {
        detail: "该任务需要人工处理（对账、归档重试或账单确认）后才能继续。",
        title: "需要处理",
      };
    case "superseded":
      return {
        detail: `已由任务 ${task.superseded_by_task_id} 替代；本记录仅保留历史事实。`,
        title: "已替代",
      };
    default:
      return {
        detail: "该结果暂不可用。",
        title: "暂不可用",
      };
  }
}

function outcomeBadgeLabel(outcome: TaskOutcome): string {
  switch (outcome) {
    case "completed":
      return "完成";
    case "quality_failed":
      return "质检未过";
    case "failed":
      return "失败";
    case "needs_attention":
      return "需处理";
    case "superseded":
      return "已替代";
    default:
      return "生成中";
  }
}

function elapsedSeconds(task: GenerationTask, nowMs: number): number {
  const start =
    parseServerTime(task.started_at) ??
    parseServerTime(task.submitted_at) ??
    nowMs;
  return Math.max(0, Math.round((nowMs - start) / 1000));
}

function parseServerTime(value: string | null | undefined): number | null {
  if (!value) {
    return null;
  }
  const normalized = value.trim().replace(" ", "T");
  const withZone = /[Z+-]\d{2}:?\d{2}$/.test(normalized)
    ? normalized
    : `${normalized}Z`;
  const ms = Date.parse(withZone);
  return Number.isNaN(ms) ? null : ms;
}

function readSnapshotString(task: GenerationTask, key: string): string | null {
  const snapshot = task.prompt_snapshot;
  if (!snapshot || typeof snapshot !== "object") {
    return null;
  }
  const value = (snapshot as Record<string, unknown>)[key];
  return typeof value === "string" && value ? value : null;
}

function readSnapshotNumber(task: GenerationTask, key: string): number | null {
  const snapshot = task.prompt_snapshot;
  if (!snapshot || typeof snapshot !== "object") {
    return null;
  }
  const value = (snapshot as Record<string, unknown>)[key];
  if (typeof value === "number" && Number.isFinite(value)) {
    return value;
  }
  return null;
}

function formatClock(totalSeconds: number): string {
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  if (minutes === 0) {
    return `${seconds} 秒`;
  }
  return `${minutes} 分 ${seconds} 秒`;
}

function formatDuration(value: number | null | undefined) {
  return value === null || value === undefined ? "—" : `${value.toFixed(1)} 秒`;
}

function formatCost(value: number | null | undefined) {
  return value === null || value === undefined
    ? "待回填"
    : `¥${value.toFixed(2)}`;
}

function formatTimestamp(value: string | null | undefined) {
  return value ? value.replace("T", " ").replace("Z", "").slice(0, 19) : "—";
}

function formatStatus(status: string) {
  const labels: Record<string, string> = {
    CANCELLED: "已取消",
    COMPLETED_WITH_FAILURES: "部分失败",
    FAILED: "失败",
    NEEDS_ATTENTION: "需要处理",
    PENDING: "生成中",
    QUEUED: "生成中",
    SUCCEEDED: "已完成",
  };
  return labels[status] ?? status;
}
