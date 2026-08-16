import {
  type FormEvent,
  useCallback,
  useEffect,
  useRef,
  useState,
} from "react";

import {
  type GenerationBatch,
  type GenerationBatchListItem,
  type GenerationTask,
  getGenerationBatch,
  getGenerationResultDownloadUrl,
  listGenerationBatches,
  reconcileUncertainTask,
  type UserRole,
} from "./api";

const BATCH_STORAGE_KEY = "generation.batchId";
const POLL_INTERVAL_MS = 2_000;
const MAX_RETRY_DELAY_MS = 16_000;
const MAX_BATCH_POLL_RETRIES = 5;
const TERMINAL_BATCH_STATUSES = new Set([
  "SUCCEEDED",
  "COMPLETED_WITH_FAILURES",
  "FAILED",
  "CANCELLED",
  "NEEDS_ATTENTION",
]);

type TaskRecordsPanelProps = {
  handoffBatch: GenerationBatch | null;
  onHandoffConsumed: () => void;
  userRole: UserRole;
};

export function TaskRecordsPanel({
  handoffBatch,
  onHandoffConsumed,
  userRole,
}: TaskRecordsPanelProps) {
  const restoredBatchId = handoffBatch?.id ?? readStoredBatchId() ?? "";
  const [batchIdInput, setBatchIdInput] = useState(restoredBatchId);
  const [activeBatchId, setActiveBatchId] = useState(restoredBatchId);
  const activeBatchIdRef = useRef(restoredBatchId);
  const [batch, setBatch] = useState<GenerationBatch | null>(handoffBatch);
  const [batchError, setBatchError] = useState("");
  const [isBatchLoading, setIsBatchLoading] = useState(false);
  const [retryDelaySeconds, setRetryDelaySeconds] = useState<number | null>(
    null,
  );
  const [batchHistory, setBatchHistory] = useState<GenerationBatchListItem[]>(
    [],
  );
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [historyError, setHistoryError] = useState("");
  const [isHistoryLoading, setIsHistoryLoading] = useState(false);
  const historyRequestRef = useRef(0);
  const [previewUrls, setPreviewUrls] = useState<Record<string, string>>({});
  const [resultErrors, setResultErrors] = useState<Record<string, string>>({});
  const [activeResultAction, setActiveResultAction] = useState("");
  const canOperate = userRole !== "auditor";

  const selectBatch = useCallback(
    (batchId: string, knownBatch?: GenerationBatch) => {
      activeBatchIdRef.current = batchId;
      setActiveBatchId(batchId);
      setBatchIdInput(batchId);
      setBatch(knownBatch ?? null);
      setBatchError("");
      setRetryDelaySeconds(null);
      storeBatchId(batchId);
    },
    [],
  );

  const loadHistory = useCallback(
    async (cursor?: string, append = false) => {
      const requestId = historyRequestRef.current + 1;
      historyRequestRef.current = requestId;
      setIsHistoryLoading(true);
      try {
        const page = await listGenerationBatches({
          limit: 20,
          ...(cursor ? { cursor } : {}),
        });
        if (historyRequestRef.current !== requestId) {
          return;
        }
        const items = Array.isArray(page.items) ? page.items : [];
        const pageCursor =
          typeof page.next_cursor === "string" ? page.next_cursor : null;
        setBatchHistory((current) =>
          append ? appendUniqueBatches(current, items) : items,
        );
        setNextCursor(pageCursor);
        setHistoryError("");
        if (!activeBatchIdRef.current && items[0]) {
          selectBatch(items[0].id);
        }
      } catch {
        if (historyRequestRef.current === requestId) {
          setHistoryError("任务记录列表暂不可用，请检查本地服务后重试。");
        }
      } finally {
        if (historyRequestRef.current === requestId) {
          setIsHistoryLoading(false);
        }
      }
    },
    [selectBatch],
  );

  useEffect(() => {
    void loadHistory();
    return () => {
      historyRequestRef.current += 1;
    };
  }, [loadHistory]);

  useEffect(() => {
    if (!handoffBatch) {
      return;
    }
    selectBatch(handoffBatch.id, handoffBatch);
    onHandoffConsumed();
  }, [handoffBatch, onHandoffConsumed, selectBatch]);

  useEffect(() => {
    if (!activeBatchId) {
      return;
    }

    let isActive = true;
    let timeoutId: number | undefined;
    let nextRetryDelayMs = POLL_INTERVAL_MS;
    let retryCount = 0;

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
        retryCount = 0;
        nextRetryDelayMs = POLL_INTERVAL_MS;
        storeBatchId(activeBatchId);
        if (!isTerminalBatch(nextBatch)) {
          timeoutId = window.setTimeout(loadBatch, POLL_INTERVAL_MS);
        }
      } catch (error) {
        if (!isActive) {
          return;
        }
        const status = (error as { status?: number }).status;
        if (status === 404) {
          setBatchError("该任务记录不存在，已停止自动刷新。");
          setRetryDelaySeconds(null);
          clearStoredBatchId();
          activeBatchIdRef.current = "";
          setActiveBatchId("");
          setBatch(null);
          return;
        }
        retryCount += 1;
        if (retryCount >= MAX_BATCH_POLL_RETRIES) {
          setBatchError(
            "网络连接失败，已停止自动刷新，请检查本地服务后手动刷新。",
          );
          setRetryDelaySeconds(null);
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

    void loadBatch();
    return () => {
      isActive = false;
      if (timeoutId !== undefined) {
        window.clearTimeout(timeoutId);
      }
    };
  }, [activeBatchId]);

  function handleBatchSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const nextBatchId = batchIdInput.trim();
    if (nextBatchId) {
      selectBatch(nextBatchId);
    }
  }

  async function handleReconcile(taskId: string) {
    if (!canOperate || !activeBatchId) {
      return;
    }
    const batchIdAtStart = activeBatchId;
    try {
      await reconcileUncertainTask(taskId);
      if (activeBatchIdRef.current !== batchIdAtStart) {
        return;
      }
      const nextBatch = await getGenerationBatch(batchIdAtStart);
      if (activeBatchIdRef.current !== batchIdAtStart) {
        return;
      }
      setBatch(nextBatch);
      setBatchError("");
    } catch {
      if (activeBatchIdRef.current === batchIdAtStart) {
        setBatchError("任务对账失败，请重试。");
      }
    }
  }

  async function handlePreview(task: GenerationTask) {
    if (!canOperate || !task.result_asset_id) {
      return;
    }
    const actionKey = `${task.id}:preview`;
    setActiveResultAction(actionKey);
    setResultErrors((current) => ({ ...current, [task.id]: "" }));
    try {
      const result = await getGenerationResultDownloadUrl(task.result_asset_id);
      setPreviewUrls((current) => ({ ...current, [task.id]: result.url }));
    } catch {
      setResultErrors((current) => ({
        ...current,
        [task.id]: "预览链接获取失败，请重试。",
      }));
    } finally {
      setActiveResultAction((current) =>
        current === actionKey ? "" : current,
      );
    }
  }

  async function handleDownload(task: GenerationTask) {
    if (!canOperate || !task.result_asset_id) {
      return;
    }
    const actionKey = `${task.id}:download`;
    setActiveResultAction(actionKey);
    setResultErrors((current) => ({ ...current, [task.id]: "" }));
    try {
      const result = await getGenerationResultDownloadUrl(task.result_asset_id);
      const anchor = document.createElement("a");
      anchor.href = result.url;
      anchor.download = `${task.id}.mp4`;
      anchor.rel = "noopener noreferrer";
      document.body.append(anchor);
      anchor.click();
      anchor.remove();
    } catch {
      setResultErrors((current) => ({
        ...current,
        [task.id]: "下载链接获取失败，请重试。",
      }));
    } finally {
      setActiveResultAction((current) =>
        current === actionKey ? "" : current,
      );
    }
  }

  return (
    <section className="task-records" aria-labelledby="task-records-title">
      <div className="section-heading">
        <div>
          <span className="eyebrow">TASK RECORDS</span>
          <h2 id="task-records-title">任务记录</h2>
          <p className="section-description">
            按项目查看批次、生成结果与需处理事项。
          </p>
        </div>
        {activeBatchId ? (
          <span className="batch-id">{activeBatchId}</span>
        ) : null}
      </div>

      <div className="task-records-layout">
        <aside className="batch-history-panel" aria-label="批次历史">
          <div className="batch-history-heading">
            <div>
              <span className="eyebrow">BATCH HISTORY</span>
              <h3>项目批次</h3>
            </div>
            <button
              className="secondary-button"
              disabled={isHistoryLoading}
              onClick={() => void loadHistory()}
              type="button"
            >
              刷新
            </button>
          </div>
          {historyError ? (
            <p className="status-note" role="status">
              {historyError}
            </p>
          ) : null}
          {isHistoryLoading && batchHistory.length === 0 ? (
            <p className="muted">正在加载项目任务记录…</p>
          ) : null}
          <ul className="batch-history-list">
            {batchHistory.map((item) => (
              <li key={item.id}>
                <button
                  aria-label={`打开批次 ${item.id}`}
                  aria-pressed={item.id === activeBatchId}
                  className={
                    item.id === activeBatchId
                      ? "batch-history-card batch-history-card--active"
                      : "batch-history-card"
                  }
                  onClick={() => selectBatch(item.id)}
                  type="button"
                >
                  <span className="batch-history-card__title">
                    <strong>{item.project_name}</strong>
                    <span
                      className={`batch-status batch-status--${item.status.toLowerCase()}`}
                    >
                      {formatStatus(item.status)}
                    </span>
                  </span>
                  <span>{item.created_by_display_name}</span>
                  <span>
                    {item.progress.progress_percent}% · {item.quantity} 个任务
                  </span>
                  <span>{formatTimestamp(item.created_at)}</span>
                  {item.needs_attention_count ? (
                    <span className="attention-tag">
                      需处理 {item.needs_attention_count}
                    </span>
                  ) : null}
                </button>
              </li>
            ))}
          </ul>
          {nextCursor ? (
            <button
              className="load-more-button"
              disabled={isHistoryLoading}
              onClick={() => void loadHistory(nextCursor, true)}
              type="button"
            >
              加载更多任务记录
            </button>
          ) : null}
        </aside>

        <div className="batch-detail-column">
          <BatchStatusMessage
            error={batchError}
            isLoading={isBatchLoading}
            retryDelaySeconds={retryDelaySeconds}
          />
          {batch ? (
            <BatchPanel
              activeResultAction={activeResultAction}
              batch={batch}
              canOperate={canOperate}
              onDownload={handleDownload}
              onPreview={handlePreview}
              onReconcile={handleReconcile}
              previewUrls={previewUrls}
              resultErrors={resultErrors}
            />
          ) : (
            <EmptyBatchState hasHistory={batchHistory.length > 0} />
          )}

          <details className="batch-compatibility-query">
            <summary>兼容查询：通过 Batch ID 查找历史记录</summary>
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
          </details>
        </div>
      </div>
    </section>
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
  return isLoading ? (
    <p className="status-note" role="status">
      正在刷新任务记录
    </p>
  ) : null;
}

function BatchPanel({
  activeResultAction,
  batch,
  canOperate,
  onDownload,
  onPreview,
  onReconcile,
  previewUrls,
  resultErrors,
}: {
  activeResultAction: string;
  batch: GenerationBatch;
  canOperate: boolean;
  onDownload: (task: GenerationTask) => void;
  onPreview: (task: GenerationTask) => void;
  onReconcile: (taskId: string) => void;
  previewUrls: Record<string, string>;
  resultErrors: Record<string, string>;
}) {
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
        aria-label="批次进度"
        aria-valuemax={100}
        aria-valuemin={0}
        aria-valuenow={batch.progress.progress_percent}
        className="progress-track"
        role="progressbar"
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
      {batch.stale ? (
        <p className="stale-banner" role="status">
          该批次的上游版本已更新；结果仍可查看，但不能作为当前版本的交付依据。
        </p>
      ) : null}
      {counts.needs_attention ? (
        <p className="attention-banner">需要处理 {counts.needs_attention}</p>
      ) : null}
      <ul className="task-list">
        {batch.tasks.map((task) => (
          <TaskItem
            activeResultAction={activeResultAction}
            canOperate={canOperate}
            key={task.id}
            onDownload={onDownload}
            onPreview={onPreview}
            onReconcile={onReconcile}
            previewUrl={previewUrls[task.id]}
            resultError={resultErrors[task.id]}
            task={task}
          />
        ))}
      </ul>
    </div>
  );
}

function TaskItem({
  activeResultAction,
  canOperate,
  onDownload,
  onPreview,
  onReconcile,
  previewUrl,
  resultError,
  task,
}: {
  activeResultAction: string;
  canOperate: boolean;
  onDownload: (task: GenerationTask) => void;
  onPreview: (task: GenerationTask) => void;
  onReconcile: (taskId: string) => void;
  previewUrl?: string;
  resultError?: string;
  task: GenerationTask;
}) {
  const attentionNeeded = taskNeedsAttention(task);
  const audioFailed =
    task.quality_status === "AUDIO_QUALITY_FAILED" ||
    task.quality_issue_codes.includes("AUDIO_QUALITY_FAILED");
  const qualityPassed = task.quality_status === "AUDIO_OK";
  const previewAction = `${task.id}:preview`;
  const downloadAction = `${task.id}:download`;

  return (
    <li className="task-item task-result-card">
      <div className="task-result-heading">
        <div>
          <strong>{task.id}</strong>
          <span>阶段：{taskStage(task)}</span>
        </div>
        <div className="task-actions">
          {attentionNeeded ? (
            <span className="attention-tag">需要处理</span>
          ) : null}
          {canOperate && task.status === "SUBMISSION_UNCERTAIN" ? (
            <button type="button" onClick={() => onReconcile(task.id)}>
              对账
            </button>
          ) : null}
        </div>
      </div>

      <div className="task-metadata-grid">
        <span>耗时 {formatDuration(task.duration_seconds)}</span>
        <span>
          {task.provider_task_id_tail
            ? `Provider 尾号 ${task.provider_task_id_tail}`
            : "Provider 尾号未公开"}
        </span>
        <span>
          尝试 {task.attempt ?? 0} 次 · 归档重试 {task.archive_retry_count ?? 0}{" "}
          次
        </span>
        <span>费用 {formatCost(task.actual_cost ?? task.estimated_cost)}</span>
      </div>

      <div
        className={
          audioFailed
            ? "quality-summary quality-summary--failed"
            : qualityPassed
              ? "quality-summary"
              : "quality-summary quality-summary--pending"
        }
      >
        <strong>
          {audioFailed ? "音频质检失败" : qualityLabel(task.quality_status)}
        </strong>
        <p>
          {audioFailed
            ? "该结果不能作为合格交付，后续只能重新生成视频。"
            : qualityPassed
              ? "音频正常，结果可进入人工确认。"
              : "结果归档后将自动执行音频质检。"}
        </p>
        {task.quality_issue_codes.length > 0 ? (
          <span>{task.quality_issue_codes.join(" · ")}</span>
        ) : null}
      </div>

      {task.error_message_redacted ? (
        <p className="task-error-summary">{task.error_message_redacted}</p>
      ) : null}

      {task.result_asset_id ? (
        <div className="task-result-actions">
          <span className="muted">结果已归档</span>
          {canOperate ? (
            <>
              <button
                disabled={activeResultAction === previewAction}
                onClick={() => void onPreview(task)}
                type="button"
              >
                {previewUrl ? `刷新预览 ${task.id}` : `加载预览 ${task.id}`}
              </button>
              <button
                disabled={activeResultAction === downloadAction}
                onClick={() => void onDownload(task)}
                type="button"
              >
                下载 MP4 {task.id}
              </button>
            </>
          ) : (
            <span className="muted">审计只读，不可预览或下载结果</span>
          )}
        </div>
      ) : (
        <span className="muted">等待结果归档</span>
      )}
      {task.result_asset_id && canOperate ? (
        <section
          aria-label={`结果播放器 ${task.id}`}
          className="task-result-preview"
        >
          {previewUrl ? (
            // biome-ignore lint/a11y/useMediaCaption: Generated Provider videos do not include a separate caption asset.
            <video
              aria-label={`结果预览 ${task.id}`}
              className="task-result-video"
              controls
              preload="metadata"
              src={previewUrl}
            />
          ) : (
            <span className="muted">
              点击加载预览，系统将签发新的短期链接。
            </span>
          )}
        </section>
      ) : null}
      {resultError ? (
        <p className="task-error-summary" role="status">
          {resultError}
        </p>
      ) : null}
    </li>
  );
}

function EmptyBatchState({ hasHistory }: { hasHistory: boolean }) {
  return (
    <div className="empty-state">
      <h2>{hasHistory ? "请选择一个任务批次" : "还没有任务记录"}</h2>
      <p>
        {hasHistory
          ? "从左侧项目批次中选择记录查看详情。"
          : "项目生成批次会自动出现在这里，也可使用下方 Batch ID 兼容查询。"}
      </p>
    </div>
  );
}

function appendUniqueBatches(
  current: GenerationBatchListItem[],
  incoming: GenerationBatchListItem[],
): GenerationBatchListItem[] {
  const existingIds = new Set(current.map((item) => item.id));
  return [...current, ...incoming.filter((item) => !existingIds.has(item.id))];
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
    task.quality_status === "AUDIO_QUALITY_FAILED" ||
    task.quality_issue_codes.includes("AUDIO_QUALITY_FAILED")
  );
}

function taskStage(task: GenerationTask) {
  if (task.stage) {
    return formatStatus(task.stage);
  }
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
    ARCHIVED: "已归档",
    ARCHIVING: "归档中",
    ARCHIVE_FAILED: "归档失败",
    CANCELLED: "已取消",
    COMPLETED: "已归档",
    COMPLETED_WITH_FAILURES: "部分失败",
    FAILED: "失败",
    NEEDS_ATTENTION: "需要处理",
    PENDING: "等待中",
    QUALITY_FAILED: "质检失败",
    QUEUED: "排队中",
    RUNNING: "生成中",
    SUBMISSION_UNCERTAIN: "提交结果待确认",
    SUBMITTING: "提交中",
    SUCCEEDED: "已完成",
  };
  return labels[status] ?? status;
}

function qualityLabel(status: string) {
  return status === "AUDIO_OK" ? "音频质检通过" : "质检待完成";
}

function formatDuration(value: number | null | undefined) {
  return value === null || value === undefined ? "—" : `${value.toFixed(1)} 秒`;
}

function formatCost(value: number | null | undefined) {
  return value === null || value === undefined
    ? "待回填"
    : `¥${value.toFixed(2)}`;
}

function formatTimestamp(value: string) {
  return value.replace("T", " ").replace("Z", "").slice(0, 19);
}

function readStoredBatchId(): string | null {
  try {
    return window.localStorage.getItem(BATCH_STORAGE_KEY);
  } catch {
    return null;
  }
}

function storeBatchId(batchId: string): void {
  try {
    window.localStorage.setItem(BATCH_STORAGE_KEY, batchId);
  } catch {
    // The active batch remains available in memory when browser storage is blocked.
  }
}

function clearStoredBatchId(): void {
  try {
    window.localStorage.removeItem(BATCH_STORAGE_KEY);
  } catch {
    // A blocked storage backend must not interrupt task navigation or polling.
  }
}
