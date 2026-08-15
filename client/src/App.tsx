import {
  type ChangeEvent,
  type FormEvent,
  useEffect,
  useRef,
  useState,
} from "react";
import { AnalysisWorkspace } from "./AnalysisWorkspace";
import {
  type CurrentUser,
  completeVideoUpload,
  createProject,
  createVideoUploadIntent,
  deleteProject,
  type GenerationBatch,
  type GenerationTask,
  getCurrentUser,
  getGenerationBatch,
  getHealth,
  listProjects,
  type Project,
  reconcileUncertainTask,
  SESSION_EXPIRED_EVENT,
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
  const [currentUser, setCurrentUser] = useState<CurrentUser | null>(null);
  const [loginError, setLoginError] = useState("");
  const [sessionMessage, setSessionMessage] = useState("");
  const [isLoginLoading, setIsLoginLoading] = useState(false);
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
  const uploadAbortRef = useRef<AbortController | null>(null);
  const [deletingProjectId, setDeletingProjectId] = useState("");
  const [activeAnalysisProject, setActiveAnalysisProject] =
    useState<Project | null>(null);
  const canWrite = currentUser?.role !== "auditor";
  const isAdmin = currentUser?.role === "admin";

  useEffect(() => {
    function handleSessionExpired() {
      setCurrentUser(null);
      setPage("login");
      setActiveAnalysisProject(null);
      setPendingProject(null);
      setSessionMessage("登录已失效，请重新进入工作台。");
      setLoginError("");
    }

    window.addEventListener(SESSION_EXPIRED_EVENT, handleSessionExpired);
    return () => {
      window.removeEventListener(SESSION_EXPIRED_EVENT, handleSessionExpired);
    };
  }, []);

  useEffect(() => {
    if (page === "login" || !currentUser) {
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
  }, [page, currentUser]);

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

  async function handleReconcile(taskId: string) {
    if (!canWrite) {
      return;
    }
    try {
      await reconcileUncertainTask(taskId);
      if (activeBatchId) {
        const nextBatch = await getGenerationBatch(activeBatchId);
        setBatch(nextBatch);
        setBatchError("");
      }
    } catch {
      setBatchError("任务对账失败，请重试。");
    }
  }

  function handleReferenceVideoChange(event: ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0] ?? null;
    setReferenceVideo(file);
    setSetupError(file ? validateReferenceVideo(file) : "");
    setSetupMessage("");
  }

  async function handleProjectSetup(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!canWrite) {
      return;
    }
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
    const controller = new AbortController();
    uploadAbortRef.current = controller;
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
      await uploadReferenceVideo(
        intent,
        referenceVideo,
        setUploadProgress,
        controller.signal,
      );
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
      uploadAbortRef.current = null;
      setIsUploading(false);
    }
  }

  function handleCancelUpload() {
    uploadAbortRef.current?.abort();
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
    if (!canWrite) {
      return;
    }
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
    async function handleLogin() {
      setIsLoginLoading(true);
      setLoginError("");
      setSessionMessage("");
      try {
        const user = await getCurrentUser();
        setCurrentUser(user);
        setPage("projects");
      } catch (error) {
        setCurrentUser(null);
        setLoginError(loginErrorMessage(error));
      } finally {
        setIsLoginLoading(false);
      }
    }

    return (
      <main className="centered-shell">
        <section className="login-card" aria-labelledby="app-title">
          <span className="eyebrow">INTERNAL PREVIEW</span>
          <h1 id="app-title">短视频复刻工作台</h1>
          <p>面向内部员工的 P0 工程骨架</p>
          {sessionMessage ? (
            <p className="settings-error" role="alert">
              {sessionMessage}
            </p>
          ) : null}
          {loginError ? (
            <p className="settings-error" role="alert">
              {loginError}
            </p>
          ) : null}
          <button type="button" disabled={isLoginLoading} onClick={handleLogin}>
            {isLoginLoading ? "正在验证身份" : "进入工作台"}
          </button>
        </section>
      </main>
    );
  }

  if (!currentUser) {
    return null;
  }

  const workspaceTitle =
    page === "settings"
      ? "设置"
      : (activeAnalysisProject?.name ?? "项目工作台");
  const breadcrumb =
    page === "settings"
      ? "工作台 / 设置"
      : activeAnalysisProject
        ? `项目 / ${activeAnalysisProject.name}`
        : "工作台 / 项目";

  function showWorkspace() {
    setPage("projects");
  }

  function showProjects() {
    setActiveAnalysisProject(null);
    setPage("projects");
  }

  function showSettings() {
    if (!isAdmin) {
      return;
    }
    setActiveAnalysisProject(null);
    setPage("settings");
  }

  return (
    <main className="app-shell">
      <AppSidebar
        activePage={page}
        currentUser={currentUser}
        isAdmin={isAdmin}
        onProjects={showProjects}
        onSettings={showSettings}
        onWorkspace={showWorkspace}
      />
      <section className="workspace-stage">
        <header className="workspace-header">
          <div>
            <p className="workspace-breadcrumb">{breadcrumb}</p>
            <div className="workspace-title-row">
              <h1>{workspaceTitle}</h1>
              <ServiceBadge state={serviceState} />
            </div>
          </div>
        </header>
        <div className="workspace-body">
          {page === "settings" ? <SettingsPanel /> : null}
          {page === "projects" && activeAnalysisProject ? (
            <AnalysisWorkspace
              onClose={() => setActiveAnalysisProject(null)}
              project={activeAnalysisProject}
              readOnly={!canWrite}
            />
          ) : null}
          {page === "projects" && !activeAnalysisProject ? (
            <>
              <ProjectSetupPanel
                isLoading={isProjectsLoading}
                isUploading={isUploading}
                deletingProjectId={deletingProjectId}
                canWrite={canWrite}
                onCancelUpload={handleCancelUpload}
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

                {batch ? (
                  <BatchPanel
                    batch={batch}
                    canWrite={canWrite}
                    onReconcile={handleReconcile}
                  />
                ) : (
                  <EmptyBatchState />
                )}
              </section>
            </>
          ) : null}
        </div>
      </section>
    </main>
  );
}

function AppSidebar({
  activePage,
  currentUser,
  isAdmin,
  onProjects,
  onSettings,
  onWorkspace,
}: {
  activePage: Page;
  currentUser: CurrentUser;
  isAdmin: boolean;
  onProjects: () => void;
  onSettings: () => void;
  onWorkspace: () => void;
}) {
  return (
    <aside className="app-sidebar">
      <div className="app-brand">
        <span className="brand-mark" aria-hidden="true">
          <svg
            aria-hidden="true"
            className="brand-mark__icon"
            viewBox="0 0 24 24"
          >
            <path d="M8 6.8v10.4L17 12 8 6.8Z" />
          </svg>
        </span>
        <span>
          <strong>短视频复刻</strong>
          <small className="app-brand__subtitle">内部创作工作台</small>
        </span>
      </div>
      <nav className="sidebar-nav" aria-label="主导航">
        <button
          className={
            activePage === "projects"
              ? "nav-button nav-button--active"
              : "nav-button"
          }
          onClick={onWorkspace}
          type="button"
        >
          <SidebarIcon name="workspace" />
          工作台
        </button>
        <button className="nav-button" onClick={onProjects} type="button">
          <SidebarIcon name="projects" />
          项目
        </button>
        <button
          aria-label="人物库（开发中）"
          className="nav-button nav-button--planned"
          disabled
          type="button"
        >
          <SidebarIcon name="characters" />
          人物库
          <small className="nav-planned-badge">开发中</small>
        </button>
        {isAdmin ? (
          <button
            className={
              activePage === "settings"
                ? "nav-button nav-button--active"
                : "nav-button"
            }
            onClick={onSettings}
            type="button"
          >
            <SidebarIcon name="settings" />
            设置
          </button>
        ) : null}
      </nav>
      <div className="sidebar-user">
        <span className="sidebar-user__avatar" aria-hidden="true">
          <SidebarIcon name="user" />
        </span>
        <span>
          <strong>{currentUser.display_name}</strong>
          <small className="sidebar-user__subtitle">
            {formatRole(currentUser.role)}
          </small>
        </span>
      </div>
    </aside>
  );
}

function SidebarIcon({
  name,
}: {
  name: "characters" | "projects" | "settings" | "user" | "workspace";
}) {
  if (name === "workspace") {
    return (
      <svg aria-hidden="true" className="sidebar-icon" viewBox="0 0 24 24">
        <path d="m4 11 8-7 8 7v8a1 1 0 0 1-1 1h-5v-6h-4v6H5a1 1 0 0 1-1-1v-8Z" />
      </svg>
    );
  }
  if (name === "projects") {
    return (
      <svg aria-hidden="true" className="sidebar-icon" viewBox="0 0 24 24">
        <path d="M3.5 7.5h17v12h-17v-12Zm0 0 2-3h5l2 3" />
      </svg>
    );
  }
  if (name === "characters" || name === "user") {
    return (
      <svg aria-hidden="true" className="sidebar-icon" viewBox="0 0 24 24">
        <circle cx="12" cy="8" r="3.5" />
        <path d="M5 20c.5-4 2.8-6 7-6s6.5 2 7 6" />
      </svg>
    );
  }
  return (
    <svg aria-hidden="true" className="sidebar-icon" viewBox="0 0 24 24">
      <circle cx="12" cy="12" r="3" />
      <path d="M12 2v3m0 14v3M2 12h3m14 0h3M4.9 4.9 7 7m10 10 2.1 2.1M19.1 4.9 17 7M7 17l-2.1 2.1" />
    </svg>
  );
}

function ProjectSetupPanel({
  canWrite,
  isLoading,
  isUploading,
  deletingProjectId,
  onCancelUpload,
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
  canWrite: boolean;
  isLoading: boolean;
  isUploading: boolean;
  deletingProjectId: string;
  onCancelUpload: () => void;
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
      {canWrite ? (
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
              已选择：{referenceVideo.name}（
              {formatFileSize(referenceVideo.size)}）
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
            <div className="upload-status-actions">
              <UploadProgress stage={uploadStage} progress={uploadProgress} />
              {uploadStage === "verifying" ||
              uploadStage === "analyzing" ? null : (
                <button
                  className="secondary-button"
                  onClick={onCancelUpload}
                  type="button"
                >
                  取消上传
                </button>
              )}
            </div>
          ) : null}
          {setupError ? <p className="settings-error">{setupError}</p> : null}
          {setupMessage ? (
            <p className="setup-success">{setupMessage}</p>
          ) : null}
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
      ) : (
        <p className="status-note">
          当前为只读身份，可查看项目和任务记录，不能创建、上传、编辑、删除或重试。
        </p>
      )}
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
              {project.reference_upload_status !== "READY" && canWrite ? (
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
              ) : project.reference_upload_status === "READY" ? (
                <button
                  className="secondary-button"
                  onClick={() => onOpenAnalysis(project)}
                  type="button"
                >
                  {canWrite ? "编辑拆解" : "查看拆解"}
                </button>
              ) : null}
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
  return labels[status] ?? status;
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

function BatchPanel({
  batch,
  canWrite,
  onReconcile,
}: {
  batch: GenerationBatch;
  canWrite: boolean;
  onReconcile: (taskId: string) => void;
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
          <TaskItem
            key={task.id}
            canWrite={canWrite}
            task={task}
            onReconcile={onReconcile}
          />
        ))}
      </ul>
    </div>
  );
}

function TaskItem({
  canWrite,
  task,
  onReconcile,
}: {
  canWrite: boolean;
  task: GenerationTask;
  onReconcile?: (taskId: string) => void;
}) {
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
        {canWrite && task.status === "SUBMISSION_UNCERTAIN" ? (
          <button type="button" onClick={() => onReconcile?.(task.id)}>
            对账
          </button>
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
    (task.quality_issue_codes ?? []).includes("AUDIO_QUALITY_FAILED")
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

function formatRole(role: CurrentUser["role"]) {
  const labels: Record<CurrentUser["role"], string> = {
    admin: "管理员",
    auditor: "审计员",
    employee: "普通员工",
  };
  return labels[role];
}

function loginErrorMessage(error: unknown) {
  if (error instanceof Error && error.message.trim()) {
    if (error.message === "Failed to fetch") {
      return "本地服务未连接，请启动本地服务后重试。";
    }
    return error.message;
  }
  return "身份验证失败，请检查本地服务后重试。";
}
