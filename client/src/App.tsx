import { useCallback, useEffect, useRef, useState } from "react";
import { AnalysisWorkspace } from "./AnalysisWorkspace";
import {
  type CurrentUser,
  type GenerationBatch,
  getCurrentUser,
  getHealth,
  type Project,
  SESSION_EXPIRED_EVENT,
} from "./api";
import { CharacterLibrary } from "./CharacterLibrary";
import { ProjectDetailFlow } from "./ProjectDetailFlow";
import { ProjectsPage } from "./ProjectsPage";
import { SettingsPanel } from "./SettingsPanel";
import { TaskRecordsPanel } from "./TaskRecordsPanel";
import "./styles.css";

type WorkspacePage = "characters" | "projects" | "settings" | "tasks";
type Page = "login" | WorkspacePage;
type ServiceState = "checking" | "connected" | "disconnected";

const HEALTH_RETRY_INTERVAL_MS = 5_000;

export function App() {
  const [page, setPage] = useState<Page>("login");
  const [currentUser, setCurrentUser] = useState<CurrentUser | null>(null);
  const [loginError, setLoginError] = useState("");
  const [sessionMessage, setSessionMessage] = useState("");
  const [isLoginLoading, setIsLoginLoading] = useState(false);
  const [serviceState, setServiceState] = useState<ServiceState>("checking");
  const [pendingBatchHandoff, setPendingBatchHandoff] =
    useState<GenerationBatch | null>(null);
  const [activeAnalysisProject, setActiveAnalysisProject] =
    useState<Project | null>(null);
  const [activeDetailProject, setActiveDetailProject] =
    useState<Project | null>(null);
  const [isAnalysisWorkspaceBusy, setIsAnalysisWorkspaceBusy] = useState(false);
  const activeAnalysisBusyRef = useRef(false);
  const activeAnalysisSessionRef = useRef(0);
  const canWrite = currentUser?.role !== "auditor";

  const handleAnalysisWorkspaceBusyChange = useCallback(
    (session: number, busy: boolean) => {
      if (session !== activeAnalysisSessionRef.current) {
        return;
      }
      activeAnalysisBusyRef.current = busy;
      setIsAnalysisWorkspaceBusy(busy);
    },
    [],
  );
  const consumeBatchHandoff = useCallback(() => {
    setPendingBatchHandoff(null);
  }, []);

  const handleLogin = useCallback(async () => {
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
  }, []);

  // 启动即自动验证身份并直接进入工作台首页，无需手动点击“进入”；
  // 仅当验证失败（如本地服务未就绪）时停留在登录卡片，可点击重试。
  const hasAutoLoginRef = useRef(false);
  useEffect(() => {
    if (hasAutoLoginRef.current) {
      return;
    }
    hasAutoLoginRef.current = true;
    void handleLogin();
  }, [handleLogin]);

  useEffect(() => {
    function handleSessionExpired() {
      setCurrentUser(null);
      setPage("login");
      activeAnalysisSessionRef.current += 1;
      activeAnalysisBusyRef.current = false;
      setIsAnalysisWorkspaceBusy(false);
      setActiveAnalysisProject(null);
      setActiveDetailProject(null);
      setPendingBatchHandoff(null);
      setSessionMessage("登录已失效，请重新进入工作台。");
      setLoginError("");
    }

    window.addEventListener(SESSION_EXPIRED_EVENT, handleSessionExpired);
    return () => {
      window.removeEventListener(SESSION_EXPIRED_EVENT, handleSessionExpired);
    };
  }, []);

  useEffect(() => {
    if (!currentUser) {
      return;
    }
    const authenticatedUser = currentUser;
    function syncDeepLink() {
      if (activeAnalysisBusyRef.current) {
        ensureWorkspaceHash("projects");
        return;
      }
      const nextPage = workspacePageFromHash(authenticatedUser);
      ensureWorkspaceHash(nextPage);
      activeAnalysisSessionRef.current += 1;
      activeAnalysisBusyRef.current = false;
      setIsAnalysisWorkspaceBusy(false);
      setActiveAnalysisProject(null);
      setActiveDetailProject(null);
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

  if (page === "login") {
    return (
      <main className="centered-shell">
        <section className="login-card" aria-labelledby="app-title">
          <span className="eyebrow">JINGXU STUDIO</span>
          <h1 id="app-title">镜序 Studio</h1>
          <p>短视频复刻工作台</p>
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
            {isLoginLoading ? "正在验证身份" : "进入"}
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
  const analysisWorkspaceSession = activeAnalysisSessionRef.current;
  const workspaceTitle =
    activeAnalysisProject?.name ?? pageTitle(workspacePage);
  const breadcrumb = activeAnalysisProject
    ? `项目 / ${activeAnalysisProject.name}`
    : `工作台 / ${pageTitle(workspacePage)}`;

  function navigateTo(nextPage: WorkspacePage) {
    if (!workspacePageAllowed(nextPage, currentRole)) {
      return;
    }
    if (activeAnalysisProject && activeAnalysisBusyRef.current) {
      return;
    }
    transitionToPage(nextPage);
  }

  function transitionToPage(nextPage: WorkspacePage) {
    window.history.pushState(null, "", `#${nextPage}`);
    activeAnalysisSessionRef.current += 1;
    activeAnalysisBusyRef.current = false;
    setIsAnalysisWorkspaceBusy(false);
    setActiveAnalysisProject(null);
    setActiveDetailProject(null);
    setPage(nextPage);
  }

  function openAnalysis(project: Project) {
    window.history.pushState(null, "", "#projects");
    activeAnalysisSessionRef.current += 1;
    activeAnalysisBusyRef.current = false;
    setIsAnalysisWorkspaceBusy(false);
    setPage("projects");
    setActiveDetailProject(null);
    setActiveAnalysisProject(project);
  }

  function closeAnalysis() {
    activeAnalysisSessionRef.current += 1;
    activeAnalysisBusyRef.current = false;
    setIsAnalysisWorkspaceBusy(false);
    setActiveAnalysisProject(null);
  }

  // 生成流程详情页与工作区共用同一 busy 拦截链路：流程进行中禁止
  // 导航切换，批次创建后同样交接给任务记录页。
  function openDetail(project: Project) {
    window.history.pushState(null, "", "#projects");
    activeAnalysisSessionRef.current += 1;
    activeAnalysisBusyRef.current = false;
    setIsAnalysisWorkspaceBusy(false);
    setPage("projects");
    setActiveAnalysisProject(null);
    setActiveDetailProject(project);
  }

  function closeDetail() {
    activeAnalysisSessionRef.current += 1;
    activeAnalysisBusyRef.current = false;
    setIsAnalysisWorkspaceBusy(false);
    setActiveDetailProject(null);
  }

  function openCreatedBatch(nextBatch: GenerationBatch) {
    setPendingBatchHandoff(nextBatch);
    transitionToPage("tasks");
  }

  function markAnalysisReady(projectId: string) {
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
        navigationDisabled={isAnalysisWorkspaceBusy}
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
            <CharacterLibrary
              userId={currentUser.id}
              userRole={currentUser.role}
            />
          ) : null}
          {page === "projects" && activeDetailProject ? (
            <ProjectDetailFlow
              onBack={closeDetail}
              onBatchCreated={openCreatedBatch}
              onBusyChange={(busy) =>
                handleAnalysisWorkspaceBusyChange(
                  analysisWorkspaceSession,
                  busy,
                )
              }
              project={activeDetailProject}
              readOnly={!canWrite}
            />
          ) : null}
          {page === "projects" &&
          !activeDetailProject &&
          activeAnalysisProject ? (
            <AnalysisWorkspace
              currentUserId={currentUser.id}
              onClose={closeAnalysis}
              onAnalysisReady={markAnalysisReady}
              onBatchCreated={openCreatedBatch}
              onWorkspaceBusyChange={(busy) =>
                handleAnalysisWorkspaceBusyChange(
                  analysisWorkspaceSession,
                  busy,
                )
              }
              project={activeAnalysisProject}
              readOnly={!canWrite}
            />
          ) : null}
          {page === "projects" &&
          !activeDetailProject &&
          !activeAnalysisProject ? (
            <ProjectsPage
              canWrite={canWrite}
              onOpenAnalysis={openAnalysis}
              onOpenDetail={openDetail}
            />
          ) : null}
          {page === "tasks" ? (
            <TaskRecordsPanel
              handoffBatch={pendingBatchHandoff}
              onHandoffConsumed={consumeBatchHandoff}
              userRole={currentUser.role}
            />
          ) : null}
        </div>
      </section>
    </main>
  );
}

function AppSidebar({
  activePage,
  currentUser,
  navigationDisabled,
  onNavigate,
}: {
  activePage: Page;
  currentUser: CurrentUser;
  navigationDisabled: boolean;
  onNavigate: (page: WorkspacePage) => void;
}) {
  const items: Array<{
    icon: "characters" | "projects" | "settings" | "tasks";
    label: string;
    page: WorkspacePage;
  }> = [
    { icon: "projects", label: "项目", page: "projects" },
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
          <strong>镜序 Studio</strong>
          <small className="app-brand__subtitle">AI 视频复刻</small>
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
            disabled={navigationDisabled}
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
  name: "characters" | "projects" | "settings" | "tasks" | "user";
}) {
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

function workspacePageFromHash(user: CurrentUser): WorkspacePage {
  const requested = window.location.hash.replace(/^#/, "") as WorkspacePage;
  return workspacePageAllowed(requested, user.role) ? requested : "projects";
}

function workspacePageAllowed(
  page: WorkspacePage,
  role: CurrentUser["role"],
): boolean {
  if (
    !(["characters", "projects", "settings", "tasks"] as string[]).includes(
      page,
    )
  ) {
    return false;
  }
  if (page === "settings") {
    return role === "admin";
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
    projects: "项目",
    settings: "设置",
    tasks: "任务记录",
  }[page];
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
