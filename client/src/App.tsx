import { type FormEvent, useEffect, useState } from "react";

import {
  type GenerationBatch,
  type GenerationTask,
  getGenerationBatch,
  getHealth,
} from "./api";
import { SettingsPanel } from "./SettingsPanel";
import "./styles.css";

type Page = "login" | "projects" | "settings";
type ServiceState = "checking" | "connected" | "disconnected";

const BATCH_STORAGE_KEY = "generation.batchId";
const POLL_INTERVAL_MS = 2_000;
const MAX_RETRY_DELAY_MS = 16_000;
const TERMINAL_BATCH_STATUSES = new Set([
  "SUCCEEDED",
  "COMPLETED_WITH_FAILURES",
  "FAILED",
  "CANCELLED",
  "NEEDS_ATTENTION",
]);

export function App() {
  const [page, setPage] = useState<Page>("login");
  const [serviceState, setServiceState] = useState<ServiceState>("checking");
  const [batchIdInput, setBatchIdInput] = useState("");
  const [activeBatchId, setActiveBatchId] = useState("");
  const [batch, setBatch] = useState<GenerationBatch | null>(null);
  const [batchError, setBatchError] = useState("");
  const [isBatchLoading, setIsBatchLoading] = useState(false);
  const [retryDelaySeconds, setRetryDelaySeconds] = useState<number | null>(
    null,
  );

  useEffect(() => {
    if (page === "login") {
      return;
    }

    let isActive = true;
    setServiceState("checking");

    getHealth()
      .then(() => {
        if (isActive) {
          setServiceState("connected");
        }
      })
      .catch(() => {
        if (isActive) {
          setServiceState("disconnected");
        }
      });

    return () => {
      isActive = false;
    };
  }, [page]);

  useEffect(() => {
    if (page !== "projects" || activeBatchId) {
      return;
    }

    const restoredBatchId = window.localStorage.getItem(BATCH_STORAGE_KEY);
    if (restoredBatchId) {
      setBatchIdInput(restoredBatchId);
      setActiveBatchId(restoredBatchId);
    }
  }, [page, activeBatchId]);

  useEffect(() => {
    if (page !== "projects" || !activeBatchId) {
      return;
    }

    let isActive = true;
    let timeoutId: number | undefined;
    let nextRetryDelayMs = POLL_INTERVAL_MS;

    async function loadBatch() {
      setIsBatchLoading(true);
      try {
        const nextBatch = await getGenerationBatch(activeBatchId);
        if (!isActive) {
          return;
        }
        setBatch(nextBatch);
        setBatchError("");
        setRetryDelaySeconds(null);
        nextRetryDelayMs = POLL_INTERVAL_MS;
        window.localStorage.setItem(BATCH_STORAGE_KEY, activeBatchId);

        if (!isTerminalBatch(nextBatch)) {
          timeoutId = window.setTimeout(loadBatch, POLL_INTERVAL_MS);
        }
      } catch {
        if (!isActive) {
          return;
        }
        nextRetryDelayMs = Math.min(nextRetryDelayMs * 2, MAX_RETRY_DELAY_MS);
        setBatchError("网络连接失败");
        setRetryDelaySeconds(nextRetryDelayMs / 1_000);
        timeoutId = window.setTimeout(loadBatch, nextRetryDelayMs);
      } finally {
        if (isActive) {
          setIsBatchLoading(false);
        }
      }
    }

    loadBatch();

    return () => {
      isActive = false;
      if (timeoutId !== undefined) {
        window.clearTimeout(timeoutId);
      }
    };
  }, [page, activeBatchId]);

  function handleBatchSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const nextBatchId = batchIdInput.trim();
    if (!nextBatchId) {
      return;
    }
    setBatchIdInput(nextBatchId);
    setActiveBatchId(nextBatchId);
  }

  if (page === "login") {
    return (
      <main className="centered-shell">
        <section className="login-card" aria-labelledby="app-title">
          <span className="eyebrow">INTERNAL PREVIEW</span>
          <h1 id="app-title">短视频复刻工作台</h1>
          <p>面向内部员工的 P0 工程骨架</p>
          <button type="button" onClick={() => setPage("projects")}>
            进入工作台
          </button>
        </section>
      </main>
    );
  }

  return (
    <main className="app-shell">
      <header>
        <div>
          <span className="eyebrow">VIDEO REPLICA</span>
          <h1>{page === "projects" ? "项目" : "设置"}</h1>
        </div>
        <div className="header-actions">
          <nav className="top-nav" aria-label="主导航">
            <button
              type="button"
              className={
                page === "projects"
                  ? "nav-button nav-button--active"
                  : "nav-button"
              }
              onClick={() => setPage("projects")}
            >
              项目
            </button>
            <button
              type="button"
              className={
                page === "settings"
                  ? "nav-button nav-button--active"
                  : "nav-button"
              }
              onClick={() => setPage("settings")}
            >
              设置
            </button>
          </nav>
          <ServiceBadge state={serviceState} />
        </div>
      </header>
      {page === "settings" ? <SettingsPanel /> : null}
      {page === "projects" ? (
        <section className="task-records" aria-labelledby="task-records-title">
          <div className="section-heading">
            <div>
              <span className="eyebrow">TASK RECORDS</span>
              <h2 id="task-records-title">任务记录</h2>
            </div>
            {activeBatchId ? (
              <span className="batch-id">{activeBatchId}</span>
            ) : null}
          </div>

          <form className="batch-form" onSubmit={handleBatchSubmit}>
            <label htmlFor="batch-id">Batch ID</label>
            <div className="batch-input-row">
              <input
                id="batch-id"
                value={batchIdInput}
                placeholder="粘贴 generation batch id"
                onChange={(event) => setBatchIdInput(event.target.value)}
              />
              <button type="submit" disabled={!batchIdInput.trim()}>
                查询任务记录
              </button>
            </div>
          </form>

          <BatchStatusMessage
            error={batchError}
            isLoading={isBatchLoading}
            retryDelaySeconds={retryDelaySeconds}
          />

          {batch ? <BatchPanel batch={batch} /> : <EmptyBatchState />}
        </section>
      ) : null}
    </main>
  );
}

function ServiceBadge({ state }: { state: ServiceState }) {
  const labels: Record<ServiceState, string> = {
    checking: "正在连接本地服务",
    connected: "本地服务已连接",
    disconnected: "本地服务未连接",
  };

  return (
    <span className={`service-badge service-badge--${state}`} role="status">
      {labels[state]}
    </span>
  );
}

function BatchStatusMessage({
  error,
  isLoading,
  retryDelaySeconds,
}: {
  error: string;
  isLoading: boolean;
  retryDelaySeconds: number | null;
}) {
  if (error) {
    return (
      <p className="status-note" role="status">
        {retryDelaySeconds ? `${error}，${retryDelaySeconds} 秒后重试` : error}
      </p>
    );
  }

  if (isLoading) {
    return (
      <p className="status-note" role="status">
        正在刷新任务记录
      </p>
    );
  }

  return null;
}

function BatchPanel({ batch }: { batch: GenerationBatch }) {
  const counts = batch.progress.counts;

  return (
    <div className="batch-panel">
      <div className="progress-header">
        <div>
          <span className="progress-percent">
            {batch.progress.progress_percent}%
          </span>
          <p>
            已完成 {batch.progress.terminal_count} /{" "}
            {batch.progress.total_count}
          </p>
        </div>
        <span
          className={`batch-status batch-status--${batch.status.toLowerCase()}`}
        >
          {formatStatus(batch.status)}
        </span>
      </div>
      <div
        className="progress-track"
        role="progressbar"
        aria-label="批次进度"
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={batch.progress.progress_percent}
      >
        <span style={{ width: `${batch.progress.progress_percent}%` }} />
      </div>
      <div className="count-grid">
        {statusCountItems(counts).map(([label, value]) => (
          <span key={label}>
            {label} {value}
          </span>
        ))}
      </div>
      {counts.needs_attention ? (
        <p className="attention-banner">需要处理 {counts.needs_attention}</p>
      ) : null}
      <ul className="task-list">
        {batch.tasks.map((task) => (
          <TaskItem key={task.id} task={task} />
        ))}
      </ul>
    </div>
  );
}

function TaskItem({ task }: { task: GenerationTask }) {
  const attentionNeeded = taskNeedsAttention(task);

  return (
    <li className="task-item">
      <div>
        <strong>{task.id}</strong>
        <span>阶段：{taskStage(task)}</span>
      </div>
      <div className="task-actions">
        {attentionNeeded ? (
          <span className="attention-tag">需要处理</span>
        ) : null}
        {task.result_asset_id ? (
          <span className="muted">结果已归档</span>
        ) : (
          <span className="muted">等待结果</span>
        )}
      </div>
    </li>
  );
}

function EmptyBatchState() {
  return (
    <div className="empty-state">
      <h2>还没有任务记录</h2>
      <p>输入或粘贴 batch id 后，这里会显示生成进度和单任务状态。</p>
    </div>
  );
}

function isTerminalBatch(batch: GenerationBatch) {
  return (
    TERMINAL_BATCH_STATUSES.has(batch.status) ||
    (batch.progress.total_count > 0 &&
      batch.progress.terminal_count === batch.progress.total_count)
  );
}

function statusCountItems(counts: Record<string, number>) {
  return [
    ["成功", counts.succeeded ?? 0],
    ["运行中", counts.running ?? 0],
    ["排队", counts.queued ?? 0],
    ["提交中", counts.submitting ?? 0],
    ["归档中", counts.archiving ?? 0],
    ["失败", counts.failed ?? 0],
    ["取消", counts.cancelled ?? 0],
    ["需要处理", counts.needs_attention ?? 0],
  ] as const;
}

function taskNeedsAttention(task: GenerationTask) {
  return (
    task.status === "SUBMISSION_UNCERTAIN" ||
    task.archive_status === "ARCHIVE_FAILED" ||
    task.quality_issue_codes.includes("AUDIO_QUALITY_FAILED")
  );
}

function taskStage(task: GenerationTask) {
  if (task.archive_status === "ARCHIVE_FAILED") {
    return "归档失败";
  }
  if (task.status === "SUCCEEDED" && task.archive_status === "ARCHIVED") {
    return "已归档";
  }
  return formatStatus(task.status);
}

function formatStatus(status: string) {
  const labels: Record<string, string> = {
    PENDING: "等待中",
    SUBMITTING: "提交中",
    QUEUED: "排队中",
    RUNNING: "生成中",
    ARCHIVING: "归档中",
    SUCCEEDED: "已完成",
    FAILED: "失败",
    CANCELLED: "已取消",
    NEEDS_ATTENTION: "需要处理",
    COMPLETED_WITH_FAILURES: "部分失败",
    SUBMISSION_UNCERTAIN: "提交待确认",
  };

  return labels[status] ?? status;
}
