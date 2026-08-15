import { type ChangeEvent, type FormEvent, useEffect, useState } from "react";
import { AnalysisWorkspace } from "./AnalysisWorkspace";
import {
  completeVideoUpload,
  createProject,
  createVideoUploadIntent,
  deleteProject,
  type GenerationBatch,
  type GenerationTask,
  getGenerationBatch,
  getHealth,
  listProjects,
  type Project,
  startVideoAnalysis,
  uploadReferenceVideo,
} from "./api";
import { SettingsPanel } from "./SettingsPanel";
import "./styles.css";

type Page = "login" | "projects" | "settings";
type ServiceState = "checking" | "connected" | "disconnected";
type UploadStage =
  | "creating_project"
  | "creating_upload"
  | "uploading"
  | "verifying"
  | "analyzing";

const BATCH_STORAGE_KEY = "generation.batchId";
const POLL_INTERVAL_MS = 2_000;
const MAX_RETRY_DELAY_MS = 16_000;
const MAX_BATCH_POLL_RETRIES = 5;
const HEALTH_RETRY_INTERVAL_MS = 5_000;
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
  const [projects, setProjects] = useState<Project[]>([]);
  const [projectsError, setProjectsError] = useState("");
  const [isProjectsLoading, setIsProjectsLoading] = useState(false);
  const [projectName, setProjectName] = useState("");
  const [referenceVideo, setReferenceVideo] = useState<File | null>(null);
  const [pendingProject, setPendingProject] = useState<Project | null>(null);
  const [setupError, setSetupError] = useState("");
  const [setupMessage, setSetupMessage] = useState("");
  const [uploadProgress, setUploadProgress] = useState<number | null>(null);
  const [uploadStage, setUploadStage] = useState<UploadStage | null>(null);
  const [isUploading, setIsUploading] = useState(false);
  const [deletingProjectId, setDeletingProjectId] = useState("");
  const [activeAnalysisProject, setActiveAnalysisProject] =
    useState<Project | null>(null);

  useEffect(() => {
    if (page === "login") {
      return;
    }

    let isActive = true;
    let timeoutId: number | undefined;
    setServiceState("checking");

    async function checkHealth() {
      try {
        await getHealth();
        if (isActive) {
          setServiceState("connected");
        }
      } catch {
        if (isActive) {
          setServiceState("disconnected");
          // Keep retrying so the badge recovers once the local backend is up.
          timeoutId = window.setTimeout(checkHealth, HEALTH_RETRY_INTERVAL_MS);
        }
      }
    }
    void checkHealth();

    return () => {
      isActive = false;
      if (timeoutId !== undefined) {
        window.clearTimeout(timeoutId);
      }
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
    if (page !== "projects") {
      return;
    }

    let isActive = true;
    setIsProjectsLoading(true);
    listProjects()
      .then((nextProjects) => {
        if (isActive) {
          setProjects(nextProjects);
          setProjectsError("");
        }
      })
      .catch(() => {
        if (isActive) {
          setProjectsError("项目列表暂不可用，请检查本地服务连接。");
        }
      })
      .finally(() => {
        if (isActive) {
          setIsProjectsLoading(false);
        }
      });

    return () => {
      isActive = false;
    };
  }, [page]);

  useEffect(() => {
    if (page !== "projects" || !activeBatchId) {
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
        window.localStorage.setItem(BATCH_STORAGE_KEY, activeBatchId);

        if (!isTerminalBatch(nextBatch)) {
          timeoutId = window.setTimeout(loadBatch, POLL_INTERVAL_MS);
        }
      } catch (error) {
        if (!isActive) {
          return;
        }
        const status = (error as { status?: number }).status;
        if (status === 404) {
          // Hard error: the batch no longer exists. Stop polling and clear it.
          setBatchError("该任务记录不存在，已停止自动刷新。");
          setRetryDelaySeconds(null);
          window.localStorage.removeItem(BATCH_STORAGE_KEY);
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

  function handleReferenceVideoChange(event: ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0] ?? null;
    setReferenceVideo(file);
    setSetupError(file ? validateReferenceVideo(file) : "");
    setSetupMessage("");
  }

  async function handleProjectSetup(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!referenceVideo) {
      setSetupError("请选择 4–15 秒的 MP4 或 MOV 参考视频。");
      return;
    }

    const validationError = validateReferenceVideo(referenceVideo);
    if (validationError) {
      setSetupError(validationError);
      return;
    }

    setIsUploading(true);
    setSetupError("");
    setSetupMessage("");
    setUploadProgress(0);
    setUploadStage(pendingProject ? "creating_upload" : "creating_project");
    try {
      const project =
        pendingProject ?? (await createProject(projectName.trim()));
      if (!pendingProject) {
        setPendingProject(project);
        setProjects((current) => [project, ...current]);
      }
      setUploadStage("creating_upload");
      const intent = await createVideoUploadIntent(project.id, referenceVideo);
      setUploadStage("uploading");
      await uploadReferenceVideo(intent, referenceVideo, setUploadProgress);
      setUploadStage("verifying");
      const completed = await completeVideoUpload(intent.asset_id);
      setUploadStage("analyzing");
      await startVideoAnalysis(
        project.id,
        completed.asset_id,
        completed.metadata.duration_seconds,
      );
      setProjects((current) =>
        current.map((item) =>
          item.id === project.id
            ? {
                ...item,
                status: "REFERENCE_READY",
                reference_asset_id: completed.asset_id,
                reference_upload_status: "READY",
              }
            : item,
        ),
      );
      setSetupMessage(
        `“${project.name}”已完成上传和预检（${completed.metadata.duration_seconds.toFixed(1)} 秒），已自动进入视频拆解。`,
      );
      setPendingProject(null);
      setProjectName("");
      setReferenceVideo(null);
      setUploadProgress(null);
      setUploadStage(null);
    } catch (error) {
      setSetupError(
        error instanceof Error
          ? `${error.message}。项目已保留，可修正设置后重新上传。`
          : "创建或上传失败。项目已保留，可修正设置后重新上传。",
      );
      setUploadProgress(null);
      setUploadStage(null);
    } finally {
      setIsUploading(false);
    }
  }

  function handleContinueUpload(project: Project) {
    setPendingProject(project);
    setProjectName(project.name);
    setReferenceVideo(null);
    setSetupError("");
    setSetupMessage("");
    setUploadProgress(null);
    setUploadStage(null);
  }

  async function handleDeleteProject(project: Project) {
    const confirmed = window.confirm(
      `删除“${project.name}”？未完成的上传文件和项目记录将一并删除，且无法恢复。`,
    );
    if (!confirmed) {
      return;
    }

    setDeletingProjectId(project.id);
    setSetupError("");
    setSetupMessage("");
    try {
      await deleteProject(project.id);
      setProjects((current) =>
        current.filter((item) => item.id !== project.id),
      );
      if (pendingProject?.id === project.id) {
        setPendingProject(null);
        setProjectName("");
        setReferenceVideo(null);
      }
      setSetupMessage(`项目“${project.name}”已删除。`);
    } catch (error) {
      setSetupError(
        error instanceof Error ? error.message : "删除项目失败，请稍后重试。",
      );
    } finally {
      setDeletingProjectId("");
    }
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
        <>
          <ProjectSetupPanel
            isLoading={isProjectsLoading}
            isUploading={isUploading}
            deletingProjectId={deletingProjectId}
            onContinueUpload={handleContinueUpload}
            onDeleteProject={handleDeleteProject}
            onOpenAnalysis={setActiveAnalysisProject}
            onFileChange={handleReferenceVideoChange}
            onProjectNameChange={setProjectName}
            onSubmit={handleProjectSetup}
            pendingProject={pendingProject}
            projectName={projectName}
            projects={projects}
            projectsError={projectsError}
            referenceVideo={referenceVideo}
            setupError={setupError}
            setupMessage={setupMessage}
            uploadProgress={uploadProgress}
            uploadStage={uploadStage}
          />
          {activeAnalysisProject ? (
            <AnalysisWorkspace
              onClose={() => setActiveAnalysisProject(null)}
              project={activeAnalysisProject}
            />
          ) : (
            <section
              className="task-records"
              aria-labelledby="task-records-title"
            >
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
          )}
        </>
      ) : null}
    </main>
  );
}

function ProjectSetupPanel({
  isLoading,
  isUploading,
  deletingProjectId,
  onContinueUpload,
  onDeleteProject,
  onOpenAnalysis,
  onFileChange,
  onProjectNameChange,
  onSubmit,
  pendingProject,
  projectName,
  projects,
  projectsError,
  referenceVideo,
  setupError,
  setupMessage,
  uploadProgress,
  uploadStage,
}: {
  isLoading: boolean;
  isUploading: boolean;
  deletingProjectId: string;
  onContinueUpload: (project: Project) => void;
  onDeleteProject: (project: Project) => void;
  onOpenAnalysis: (project: Project) => void;
  onFileChange: (event: ChangeEvent<HTMLInputElement>) => void;
  onProjectNameChange: (value: string) => void;
  onSubmit: (event: FormEvent<HTMLFormElement>) => void;
  pendingProject: Project | null;
  projectName: string;
  projects: Project[];
  projectsError: string;
  referenceVideo: File | null;
  setupError: string;
  setupMessage: string;
  uploadProgress: number | null;
  uploadStage: UploadStage | null;
}) {
  return (
    <section className="project-setup" aria-labelledby="project-setup-title">
      <div className="section-heading">
        <div>
          <span className="eyebrow">START HERE</span>
          <h2 id="project-setup-title">新建复刻项目</h2>
          <p>上传 4–15 秒的参考视频，系统会先完成格式与时长预检。</p>
        </div>
        <span className="project-count">{projects.length} 个项目</span>
      </div>
      <form className="project-setup-form" onSubmit={onSubmit}>
        <label htmlFor="project-name">项目名称</label>
        <input
          id="project-name"
          disabled={Boolean(pendingProject) || isUploading}
          maxLength={120}
          onChange={(event) => onProjectNameChange(event.target.value)}
          placeholder="例如：夏日咖啡口播复刻"
          value={projectName}
        />
        <label htmlFor="reference-video">参考视频</label>
        <input
          id="reference-video"
          accept=".mp4,.mov,video/mp4,video/quicktime"
          disabled={isUploading}
          onChange={onFileChange}
          type="file"
        />
        {referenceVideo ? (
          <p className="file-note">
            已选择：{referenceVideo.name}（{formatFileSize(referenceVideo.size)}
            ）
          </p>
        ) : null}
        {pendingProject ? (
          <p className="status-note">
            正在为“{pendingProject.name}”重新上传参考视频。
          </p>
        ) : null}
        {pendingProject && !isUploading ? (
          <button
            className="secondary-button project-delete-button"
            disabled={Boolean(deletingProjectId)}
            onClick={() => onDeleteProject(pendingProject)}
            type="button"
          >
            删除此项目
          </button>
        ) : null}
        {isUploading && uploadStage ? (
          <UploadProgress stage={uploadStage} progress={uploadProgress} />
        ) : null}
        {setupError ? <p className="settings-error">{setupError}</p> : null}
        {setupMessage ? <p className="setup-success">{setupMessage}</p> : null}
        <button
          disabled={
            isUploading ||
            !referenceVideo ||
            (!pendingProject && !projectName.trim()) ||
            Boolean(referenceVideo && validateReferenceVideo(referenceVideo))
          }
          type="submit"
        >
          {isUploading
            ? "正在上传"
            : pendingProject
              ? "重新上传"
              : "创建并上传"}
        </button>
      </form>
      {projectsError ? <p className="settings-error">{projectsError}</p> : null}
      {isLoading ? <p className="status-note">正在加载项目列表</p> : null}
      {!isUploading && !isLoading && !projectsError && projects.length ? (
        <ul className="project-list">
          {projects.map((project) => (
            <li key={project.id}>
              <div>
                <strong>{project.name}</strong>
                <span>
                  {formatReferenceStatus(project.reference_upload_status)}
                </span>
              </div>
              {project.reference_upload_status !== "READY" ? (
                <div className="project-actions">
                  <button
                    className="secondary-button"
                    disabled={isUploading || Boolean(deletingProjectId)}
                    onClick={() => onContinueUpload(project)}
                    type="button"
                  >
                    继续上传
                  </button>
                  <button
                    className="secondary-button project-delete-button"
                    disabled={isUploading || Boolean(deletingProjectId)}
                    onClick={() => onDeleteProject(project)}
                    type="button"
                  >
                    {deletingProjectId === project.id ? "正在删除" : "删除项目"}
                  </button>
                </div>
              ) : (
                <button
                  className="secondary-button"
                  onClick={() => onOpenAnalysis(project)}
                  type="button"
                >
                  编辑拆解
                </button>
              )}
            </li>
          ))}
        </ul>
      ) : null}
      {!isUploading && !isLoading && !projectsError && !projects.length ? (
        <p className="status-note">还没有项目，从第一个参考视频开始。</p>
      ) : null}
    </section>
  );
}

function UploadProgress({
  progress,
  stage,
}: {
  progress: number | null;
  stage: UploadStage;
}) {
  const details: Record<
    UploadStage,
    { step: number; title: string; message: string }
  > = {
    creating_project: {
      step: 1,
      title: "正在创建项目",
      message: "正在保存本次复刻任务。",
    },
    creating_upload: {
      step: 2,
      title: "正在准备上传",
      message: "正在获取安全上传地址。",
    },
    uploading: {
      step: 3,
      title: "正在上传参考视频",
      message:
        progress && progress > 0 ? `已传输 ${progress}%` : "等待传输进度",
    },
    verifying: {
      step: 4,
      title: "正在验证参考视频",
      message: "正在检查文件、格式和视频时长。",
    },
    analyzing: {
      step: 5,
      title: "正在启动视频拆解",
      message: "视频已通过预检，正在创建拆解任务。",
    },
  };
  const detail = details[stage];
  const hasTransferProgress = stage === "uploading";
  const percentage = progress ?? 0;
  return (
    <div className="upload-status" role="status" aria-live="polite">
      <p className="upload-status__step">
        步骤 {detail.step}/5 · {detail.title}
      </p>
      <div
        aria-label="参考视频上传进度"
        aria-valuemax={100}
        aria-valuemin={0}
        aria-valuenow={hasTransferProgress ? percentage : undefined}
        aria-valuetext={`${detail.title}，${detail.message}`}
        className={`upload-progress ${
          hasTransferProgress ? "" : "upload-progress--indeterminate"
        }`}
        role="progressbar"
      >
        <span style={{ width: `${hasTransferProgress ? percentage : 100}%` }} />
        <strong>{hasTransferProgress ? `${percentage}%` : "处理中"}</strong>
      </div>
      <p className="status-note">{detail.message}</p>
    </div>
  );
}

function validateReferenceVideo(file: File): string {
  const filename = file.name.toLowerCase();
  const isSupportedType =
    file.type === "video/mp4" ||
    file.type === "video/quicktime" ||
    filename.endsWith(".mp4") ||
    filename.endsWith(".mov");
  if (!isSupportedType) {
    return "只支持 MP4 或 MOV 格式的视频。";
  }
  if (file.size > 50 * 1024 * 1024) {
    return "参考视频不能超过 50MB。";
  }
  return "";
}

function formatFileSize(sizeBytes: number): string {
  return `${(sizeBytes / 1024 / 1024).toFixed(1)} MB`;
}

function formatReferenceStatus(
  status: Project["reference_upload_status"],
): string {
  const labels: Record<Project["reference_upload_status"], string> = {
    NOT_STARTED: "未上传参考视频",
    UPLOAD_PENDING: "上传未完成",
    READY: "参考视频已就绪",
  };
  return labels[status];
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
