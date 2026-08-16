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
import { CharacterLibrary } from "./CharacterLibrary";
import { ProjectWorkflowSteps } from "./ProjectWorkflowSteps";
import { SettingsPanel } from "./SettingsPanel";
import "./styles.css";

type WorkspacePage = "characters" | "new" | "projects" | "settings" | "tasks";
type Page = "login" | WorkspacePage;
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
    function abortActiveUpload() {
      uploadAbortRef.current?.abort();
    }

    window.addEventListener("pagehide", abortActiveUpload);
    return () => {
      window.removeEventListener("pagehide", abortActiveUpload);
      abortActiveUpload();
    };
  }, []);

  useEffect(() => {
    if (!currentUser) {
      return;
    }
    const authenticatedUser = currentUser;
    function syncDeepLink() {
      const nextPage = workspacePageFromHash(authenticatedUser);
      ensureWorkspaceHash(nextPage);
      setActiveAnalysisProject(null);
      setPage(nextPage);
    }
    window.addEventListener("hashchange", syncDeepLink);
    window.addEventListener("popstate", syncDeepLink);
    return () => {
      window.removeEventListener("hashchange", syncDeepLink);
      window.removeEventListener("popstate", syncDeepLink);
    };
  }, [currentUser]);

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
    if (page !== "tasks" || activeBatchId) {
      return;
    }

    const restoredBatchId = readStoredBatchId();
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
    if (page !== "tasks" || !activeBatchId) {
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
          // Hard error: the batch no longer exists. Stop polling and clear it.
          setBatchError("该任务记录不存在，已停止自动刷新。");
          setRetryDelaySeconds(null);
          clearStoredBatchId();
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
      setSetupError("请选择 4 至 15 秒的 MP4 或 MOV 参考视频。");
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
    let readyProject: Project | null = null;
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
      const uploadedProject: Project = {
        ...project,
        status: "REFERENCE_READY",
        reference_asset_id: completed.asset_id,
        reference_upload_status: "READY",
        analysis_status: "PENDING",
      };
      readyProject = uploadedProject;
      setPendingProject(uploadedProject);
      setProjects((current) =>
        current.map((item) =>
          item.id === project.id ? uploadedProject : item,
        ),
      );
      setSetupMessage(
        `“${project.name}”已完成上传和预检（${completed.metadata.duration_seconds.toFixed(1)} 秒），已自动进入视频拆解。`,
      );
      setUploadStage("analyzing");
      await startVideoAnalysis(
        project.id,
        completed.asset_id,
        completed.metadata.duration_seconds,
      );
      const analyzedProject: Project = {
        ...uploadedProject,
        analysis_status: "READY",
      };
      setProjects((current) =>
        current.map((item) =>
          item.id === project.id ? analyzedProject : item,
        ),
      );
      setPendingProject(null);
      setProjectName("");
      setReferenceVideo(null);
      setUploadProgress(null);
      setUploadStage(null);
      openAnalysis(analyzedProject);
    } catch (error) {
      const message =
        error instanceof Error ? error.message : "创建或上传失败。";
      if (readyProject) {
        setSetupError(
          `${message}。参考视频与项目已保留，可在视频拆解步骤继续。`,
        );
        setPendingProject(null);
        setProjectName("");
        setReferenceVideo(null);
        openAnalysis(readyProject);
      } else {
        setSetupError(`${message}。项目已保留，可修正设置后重新上传。`);
      }
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
    window.history.pushState(null, "", "#new");
    setActiveAnalysisProject(null);
    setPage("new");
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
        const nextPage = workspacePageFromHash(user);
        ensureWorkspaceHash(nextPage);
        setCurrentUser(user);
        setPage(nextPage);
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

  const workspacePage = page as WorkspacePage;
  const currentRole = currentUser.role;
  const workspaceTitle =
    activeAnalysisProject?.name ?? pageTitle(workspacePage);
  const breadcrumb = activeAnalysisProject
    ? `项目 / ${activeAnalysisProject.name}`
    : `工作台 / ${pageTitle(workspacePage)}`;

  function navigateTo(nextPage: WorkspacePage) {
    if (!workspacePageAllowed(nextPage, currentRole)) {
      return;
    }
    window.history.pushState(null, "", `#${nextPage}`);
    setActiveAnalysisProject(null);
    setPage(nextPage);
  }

  function openAnalysis(project: Project) {
    window.history.pushState(null, "", "#projects");
    setPage("projects");
    setActiveAnalysisProject(project);
  }

  function openCreatedBatch(nextBatch: GenerationBatch) {
    setBatch(nextBatch);
    setBatchError("");
    setRetryDelaySeconds(null);
    setBatchIdInput(nextBatch.id);
    setActiveBatchId(nextBatch.id);
    storeBatchId(nextBatch.id);
    navigateTo("tasks");
  }

  function markAnalysisReady(projectId: string) {
    setSetupError("");
    setProjects((current) =>
      current.map((project) =>
        project.id === projectId
          ? { ...project, analysis_status: "READY" }
          : project,
      ),
    );
    setActiveAnalysisProject((current) =>
      current?.id === projectId
        ? { ...current, analysis_status: "READY" }
        : current,
    );
  }

  return (
    <main className="app-shell">
      <AppSidebar
        activePage={page}
        currentUser={currentUser}
        onNavigate={navigateTo}
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
          {page === "characters" ? (
            <CharacterLibrary userRole={currentUser.role} />
          ) : null}
          {page === "projects" && activeAnalysisProject ? (
            <>
              {setupError ? (
                <p className="settings-error">{setupError}</p>
              ) : null}
              {setupMessage ? (
                <p className="setup-success">{setupMessage}</p>
              ) : null}
              <AnalysisWorkspace
                onClose={() => setActiveAnalysisProject(null)}
                onAnalysisReady={markAnalysisReady}
                onBatchCreated={openCreatedBatch}
                project={activeAnalysisProject}
                readOnly={!canWrite}
              />
            </>
          ) : null}
          {(page === "projects" || page === "new") && !activeAnalysisProject ? (
            <ProjectSetupPanel
              mode={page === "new" ? "create" : "list"}
              isLoading={isProjectsLoading}
              isUploading={isUploading}
              deletingProjectId={deletingProjectId}
              canWrite={canWrite}
              onCancelUpload={handleCancelUpload}
              onContinueUpload={handleContinueUpload}
              onDeleteProject={handleDeleteProject}
              onOpenAnalysis={openAnalysis}
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
          ) : null}
          {page === "tasks" ? (
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
          ) : null}
        </div>
      </section>
    </main>
  );
}

function AppSidebar({
  activePage,
  currentUser,
  onNavigate,
}: {
  activePage: Page;
  currentUser: CurrentUser;
  onNavigate: (page: WorkspacePage) => void;
}) {
  const items: Array<{
    icon: "characters" | "new" | "projects" | "settings" | "tasks";
    label: string;
    page: WorkspacePage;
  }> = [
    { icon: "projects", label: "项目", page: "projects" },
    ...(currentUser.role === "auditor"
      ? []
      : [{ icon: "new" as const, label: "新建复刻", page: "new" as const }]),
    { icon: "characters", label: "人物库", page: "characters" },
    { icon: "tasks", label: "任务记录", page: "tasks" },
    ...(currentUser.role === "admin"
      ? [
          {
            icon: "settings" as const,
            label: "设置",
            page: "settings" as const,
          },
        ]
      : []),
  ];
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
        {items.map((item) => (
          <button
            aria-current={activePage === item.page ? "page" : undefined}
            className={
              activePage === item.page
                ? "nav-button nav-button--active"
                : "nav-button"
            }
            key={item.page}
            onClick={() => onNavigate(item.page)}
            type="button"
          >
            <SidebarIcon name={item.icon} />
            {item.label}
          </button>
        ))}
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
  name: "characters" | "new" | "projects" | "settings" | "tasks" | "user";
}) {
  if (name === "new") {
    return (
      <svg aria-hidden="true" className="sidebar-icon" viewBox="0 0 24 24">
        <path d="m4 11 8-7 8 7v8a1 1 0 0 1-1 1h-5v-6h-4v6H5a1 1 0 0 1-1-1v-8Z" />
      </svg>
    );
  }
  if (name === "projects" || name === "tasks") {
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
  mode,
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
  mode: "create" | "list";
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
  const isCreateMode = mode === "create";
  const titleId = isCreateMode ? "project-create-title" : "project-list-title";
  return (
    <section className="project-setup" aria-labelledby={titleId}>
      <div className="section-heading">
        <div>
          <span className="eyebrow">
            {isCreateMode ? "START HERE" : "PROJECTS"}
          </span>
          <h2 id={titleId}>{isCreateMode ? "新建复刻项目" : "项目列表"}</h2>
          <p>
            {isCreateMode
              ? "上传 4 至 15 秒的参考视频，系统会先完成格式与时长预检。"
              : "查看项目状态，并继续上传或进入视频拆解。"}
          </p>
        </div>
        {!isCreateMode ? (
          <span className="project-count">{projects.length} 个项目</span>
        ) : null}
      </div>
      {isCreateMode ? <ProjectWorkflowSteps currentStep={1} /> : null}
      {setupError ? <p className="settings-error">{setupError}</p> : null}
      {setupMessage ? <p className="setup-success">{setupMessage}</p> : null}
      {isCreateMode && canWrite ? (
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
      ) : null}
      {!isCreateMode && !canWrite ? (
        <p className="status-note">
          当前为只读身份，可查看项目和任务记录，不能创建、上传、编辑、删除或重试。
        </p>
      ) : null}
      {!isCreateMode && projectsError ? (
        <p className="settings-error">{projectsError}</p>
      ) : null}
      {!isCreateMode && isLoading ? (
        <p className="status-note">正在加载项目列表</p>
      ) : null}
      {!isCreateMode &&
      !isUploading &&
      !isLoading &&
      !projectsError &&
      projects.length ? (
        <ul className="project-list">
          {projects.map((project) => (
            <li key={project.id}>
              <div>
                <strong>{project.name}</strong>
                <div className="project-statuses">
                  <span>
                    {formatReferenceStatus(project.reference_upload_status)}
                  </span>
                  <span>{formatAnalysisStatus(project.analysis_status)}</span>
                </div>
              </div>
              {project.reference_upload_status !== "READY" && canWrite ? (
                <div className="project-actions">
                  <button
                    className="secondary-button"
                    disabled={isUploading || Boolean(deletingProjectId)}
                    onClick={() => onContinueUpload(project)}
                    type="button"
                  >
                    继续编辑
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
                  {canWrite ? "继续编辑" : "查看项目"}
                </button>
              ) : null}
            </li>
          ))}
        </ul>
      ) : null}
      {!isCreateMode &&
      !isUploading &&
      !isLoading &&
      !projectsError &&
      !projects.length ? (
        <p className="status-note">
          还没有项目，请从“新建复刻”创建第一个项目。
        </p>
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

function formatAnalysisStatus(
  status: Project["analysis_status"] | undefined,
): string {
  const labels: Record<Project["analysis_status"], string> = {
    NOT_READY: "等待参考视频",
    PENDING: "视频拆解待开始",
    READY: "视频拆解已完成",
  };
  return status ? labels[status] : "拆解状态待确认";
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

function workspacePageFromHash(user: CurrentUser): WorkspacePage {
  const requested = window.location.hash.replace(/^#/, "") as WorkspacePage;
  return workspacePageAllowed(requested, user.role) ? requested : "projects";
}

function workspacePageAllowed(
  page: WorkspacePage,
  role: CurrentUser["role"],
): boolean {
  if (
    !(
      ["characters", "new", "projects", "settings", "tasks"] as string[]
    ).includes(page)
  ) {
    return false;
  }
  if (page === "settings") {
    return role === "admin";
  }
  if (page === "new") {
    return role !== "auditor";
  }
  return true;
}

function ensureWorkspaceHash(page: WorkspacePage) {
  if (window.location.hash !== `#${page}`) {
    window.history.replaceState(null, "", `#${page}`);
  }
}

function pageTitle(page: WorkspacePage): string {
  return {
    characters: "人物库",
    new: "新建复刻",
    projects: "项目工作台",
    settings: "设置",
    tasks: "任务记录",
  }[page];
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
