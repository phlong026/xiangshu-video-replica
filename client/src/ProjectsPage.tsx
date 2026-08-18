import {
  type ChangeEvent,
  useCallback,
  useEffect,
  useRef,
  useState,
} from "react";

import {
  completeVideoUpload,
  createProject,
  createVideoUploadIntent,
  deleteProject,
  type GenerationBatch,
  listProjects,
  type Project,
  type PromptPreviewResult,
  previewGenerationPrompt,
  startVideoAnalysis,
  uploadReferenceVideo,
} from "./api";
import { QuickGenerateDialog } from "./QuickGenerateDialog";

const PROJECT_POLL_INTERVAL_MS = 5_000;

type UploadStage =
  | "creating_project"
  | "creating_upload"
  | "uploading"
  | "verifying"
  | "analyzing";

type UploadItem = {
  key: string;
  name: string;
  projectId: string | null;
  progress: number | null;
  stage: UploadStage | null;
  error: string;
};

type ProjectsPageProps = {
  canWrite: boolean;
  onBatchCreated: (batch: GenerationBatch) => void;
  onOpenAnalysis: (project: Project) => void;
};

const UPLOAD_STAGE_LABELS: Record<UploadStage, string> = {
  creating_project: "正在创建项目",
  creating_upload: "正在准备上传",
  uploading: "正在上传视频",
  verifying: "正在校验视频",
  analyzing: "正在启动拆解",
};

const SUPPORTED_VIDEO_PATTERN = /\.(mp4|mov)$/i;
const UNSUPPORTED_FILE_ERROR = "只支持 MP4 或 MOV 格式的视频。";

function makeUploadKey() {
  return `${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function rejectUploadEntries(files: File[]): UploadItem[] {
  return files
    .filter((file) => !SUPPORTED_VIDEO_PATTERN.test(file.name))
    .map((file) => ({
      key: makeUploadKey(),
      name: file.name,
      projectId: null,
      progress: null,
      stage: null,
      error: UNSUPPORTED_FILE_ERROR,
    }));
}

// 项目页 = 「上传 → 拆解 → 提示词 + 首帧 → 生成」的主动线：
// 顶部上传区支持一次选择多个视频（每个视频一个项目，串行上传并自动
// 拆解）；列表行展示拆解状态、可展开查看系统编译的提示词；已拆解的
// 项目可就地打开快速生成弹层完成付费生成，或进入工作区做高级编辑。
export function ProjectsPage({
  canWrite,
  onBatchCreated,
  onOpenAnalysis,
}: ProjectsPageProps) {
  const [projects, setProjects] = useState<Project[]>([]);
  const [projectsError, setProjectsError] = useState("");
  const [isProjectsLoading, setIsProjectsLoading] = useState(false);
  const [uploads, setUploads] = useState<UploadItem[]>([]);
  const [deletingProjectId, setDeletingProjectId] = useState("");
  const [deleteMessage, setDeleteMessage] = useState("");
  const [deleteError, setDeleteError] = useState("");
  const [expandedProjectId, setExpandedProjectId] = useState("");
  const [promptPreviews, setPromptPreviews] = useState<
    Record<string, PromptPreviewResult | "loading" | "error">
  >({});
  const [quickGenerateProject, setQuickGenerateProject] =
    useState<Project | null>(null);
  const [rebindProject, setRebindProject] = useState<Project | null>(null);
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const rebindFileInputRef = useRef<HTMLInputElement | null>(null);
  const uploadChainRef = useRef<Promise<void>>(Promise.resolve());
  const abortControllersRef = useRef(new Map<string, AbortController>());
  const isUploading = uploads.some(
    (item) => !item.error && item.stage !== null,
  );

  // 桌面端关闭窗口时中止仍在传输的上传，避免留下半途请求。
  useEffect(() => {
    const abortAllUploads = () => {
      for (const controller of abortControllersRef.current.values()) {
        controller.abort();
      }
    };
    window.addEventListener("pagehide", abortAllUploads);
    return () => window.removeEventListener("pagehide", abortAllUploads);
  }, []);

  const loadProjects = useCallback(
    async (options: { silent?: boolean } = {}) => {
      if (!options.silent) {
        setIsProjectsLoading(true);
      }
      try {
        const nextProjects = await listProjects();
        setProjects(nextProjects);
        setProjectsError("");
      } catch {
        setProjectsError("项目列表暂不可用，请检查本地服务连接。");
      } finally {
        if (!options.silent) {
          setIsProjectsLoading(false);
        }
      }
    },
    [],
  );

  useEffect(() => {
    void loadProjects();
  }, [loadProjects]);

  // 存在拆解中或上传中的项目时轮询刷新，让状态自动前进。
  const hasPendingWork =
    uploads.some((item) => !item.error && item.stage !== null) ||
    projects.some((project) => project.analysis_status === "PENDING");
  useEffect(() => {
    if (!hasPendingWork) {
      return;
    }
    const timer = window.setInterval(() => {
      void loadProjects({ silent: true });
    }, PROJECT_POLL_INTERVAL_MS);
    return () => window.clearInterval(timer);
  }, [hasPendingWork, loadProjects]);

  const updateUpload = useCallback(
    (key: string, patch: Partial<UploadItem>) => {
      setUploads((current) =>
        current.map((item) =>
          item.key === key ? { ...item, ...patch } : item,
        ),
      );
    },
    [],
  );

  const uploadOne = useCallback(
    async (key: string, file: File, existingProject: Project | null) => {
      const controller = new AbortController();
      abortControllersRef.current.set(key, controller);
      try {
        updateUpload(key, {
          stage: existingProject ? "creating_upload" : "creating_project",
          progress: 0,
          error: "",
        });
        const project =
          existingProject ??
          (await createProject(defaultProjectName(file.name)));
        updateUpload(key, { projectId: project.id, stage: "creating_upload" });
        const intent = await createVideoUploadIntent(project.id, file);
        updateUpload(key, { stage: "uploading" });
        await uploadReferenceVideo(
          intent,
          file,
          (progress) => {
            updateUpload(key, { progress });
          },
          controller.signal,
        );
        updateUpload(key, { stage: "verifying" });
        const completed = await completeVideoUpload(intent.asset_id);
        updateUpload(key, { stage: "analyzing" });
        await startVideoAnalysis(project.id, completed.asset_id);
        updateUpload(key, { stage: null, progress: null });
        await loadProjects({ silent: true });
      } catch (error) {
        updateUpload(key, {
          stage: null,
          progress: null,
          error: error instanceof Error ? error.message : "上传或拆解失败。",
        });
        await loadProjects({ silent: true });
      } finally {
        abortControllersRef.current.delete(key);
      }
    },
    [loadProjects, updateUpload],
  );

  const enqueueUpload = useCallback(
    (file: File, existingProject: Project | null) => {
      const key = makeUploadKey();
      setUploads((current) => [
        {
          key,
          name: file.name,
          projectId: existingProject?.id ?? null,
          progress: null,
          stage: "creating_project",
          error: "",
        },
        ...current,
      ]);
      uploadChainRef.current = uploadChainRef.current.then(() =>
        uploadOne(key, file, existingProject),
      );
    },
    [uploadOne],
  );

  const handleFileChange = useCallback(
    (event: ChangeEvent<HTMLInputElement>) => {
      const files = Array.from(event.target.files ?? []);
      const rejected = rejectUploadEntries(files);
      if (rejected.length) {
        setUploads((current) => [...rejected, ...current]);
      }
      for (const file of files.filter((item) =>
        SUPPORTED_VIDEO_PATTERN.test(item.name),
      )) {
        enqueueUpload(file, null);
      }
      event.target.value = "";
    },
    [enqueueUpload],
  );

  // 续传走独立的单文件选择器，与主上传互不干扰：用户取消
  // 续传对话框不会影响之后的主上传语义。
  const handleRebindFileChange = useCallback(
    (event: ChangeEvent<HTMLInputElement>) => {
      const files = Array.from(event.target.files ?? []);
      const target = rebindProject;
      setRebindProject(null);
      const rejected = rejectUploadEntries(files);
      if (rejected.length) {
        setUploads((current) => [...rejected, ...current]);
      }
      const accepted = files.filter((item) =>
        SUPPORTED_VIDEO_PATTERN.test(item.name),
      );
      if (target && accepted[0]) {
        enqueueUpload(accepted[0], target);
      } else {
        for (const file of accepted) {
          enqueueUpload(file, null);
        }
      }
      event.target.value = "";
    },
    [enqueueUpload, rebindProject],
  );

  function requestRebind(project: Project) {
    setRebindProject(project);
    rebindFileInputRef.current?.click();
  }

  async function handleDeleteProject(project: Project) {
    if (!canWrite) {
      return;
    }
    const confirmed = window.confirm(
      `删除“${project.name}”？该项目的参考视频、分析结果、脚本、生成记录与生成产物将一并删除，且无法恢复。云端文件若暂时无法清理，将在审计日志中记录。`,
    );
    if (!confirmed) {
      return;
    }
    setDeletingProjectId(project.id);
    setDeleteError("");
    setDeleteMessage("");
    try {
      await deleteProject(project.id);
      setProjects((current) =>
        current.filter((item) => item.id !== project.id),
      );
      setDeleteMessage(`项目“${project.name}”已删除。`);
    } catch (error) {
      setDeleteError(
        error instanceof Error ? error.message : "删除项目失败，请稍后重试。",
      );
    } finally {
      setDeletingProjectId("");
    }
  }

  const loadPromptPreview = useCallback(async (project: Project) => {
    setPromptPreviews((current) => ({ ...current, [project.id]: "loading" }));
    try {
      const preview = await previewGenerationPrompt(project.id);
      setPromptPreviews((current) => ({ ...current, [project.id]: preview }));
    } catch {
      setPromptPreviews((current) => ({ ...current, [project.id]: "error" }));
    }
  }, []);

  function togglePromptPreview(project: Project) {
    if (expandedProjectId === project.id) {
      setExpandedProjectId("");
      return;
    }
    setExpandedProjectId(project.id);
    const state = promptPreviews[project.id];
    if (!state || state === "error") {
      void loadPromptPreview(project);
    }
  }

  function renderStatusBadge(project: Project) {
    if (project.reference_upload_status !== "READY") {
      return (
        <span className="project-badge project-badge--pending">待上传</span>
      );
    }
    if (project.analysis_status === "PENDING") {
      return (
        <span className="project-badge project-badge--running">拆解中</span>
      );
    }
    if (project.analysis_status === "READY") {
      return <span className="project-badge project-badge--ready">已拆解</span>;
    }
    return <span className="project-badge project-badge--pending">待拆解</span>;
  }

  return (
    <section className="projects-page" aria-labelledby="projects-title">
      <div className="section-heading">
        <div>
          <h2 id="projects-title">项目</h2>
          <p>上传参考视频，拆解提示词，配首帧生成新视频。</p>
        </div>
        <span className="project-count">{projects.length} 个项目</span>
      </div>

      {canWrite ? (
        <div className="projects-upload-zone">
          <input
            accept=".mp4,.mov,video/mp4,video/quicktime"
            aria-label="选择一个或多个参考视频"
            hidden
            multiple
            onChange={handleFileChange}
            ref={fileInputRef}
            type="file"
          />
          <input
            accept=".mp4,.mov,video/mp4,video/quicktime"
            aria-label="重新上传参考视频"
            hidden
            onChange={handleRebindFileChange}
            ref={rebindFileInputRef}
            type="file"
          />
          <p className="projects-upload-zone__title">
            拖入或选择参考视频，自动拆解
          </p>
          <p className="projects-upload-zone__note">
            支持一次选择多个 MP4 / MOV（4–15 秒，不超过 50MB）；每个视频
            创建一个项目并自动拆解出提示词。
          </p>
          <button
            disabled={isUploading}
            onClick={() => fileInputRef.current?.click()}
            type="button"
          >
            {isUploading ? "正在上传" : "选择视频"}
          </button>
        </div>
      ) : (
        <p className="status-note">只读身份：仅可查看项目。</p>
      )}

      {deleteError ? (
        <p className="settings-error" role="alert">
          {deleteError}
        </p>
      ) : null}
      {deleteMessage ? (
        <p className="setup-success" role="status">
          {deleteMessage}
        </p>
      ) : null}

      {uploads.length ? (
        <ul className="projects-upload-list">
          {uploads.map((item) => (
            <li className="projects-upload-item" key={item.key}>
              <div>
                <strong>{item.name}</strong>
                {item.stage ? (
                  <p className="projects-upload-item__status">
                    {UPLOAD_STAGE_LABELS[item.stage]}
                    {item.stage === "uploading" && item.progress != null
                      ? ` · ${item.progress}%`
                      : ""}
                  </p>
                ) : null}
                {item.error ? (
                  <p className="settings-error" role="alert">
                    {item.error}
                  </p>
                ) : null}
              </div>
              {item.stage ? (
                <div className="projects-upload-item__aside">
                  <div
                    aria-label="上传进度"
                    aria-valuemax={100}
                    aria-valuemin={0}
                    aria-valuenow={
                      item.stage === "uploading" && item.progress != null
                        ? item.progress
                        : undefined
                    }
                    className={`projects-upload-item__track ${
                      item.stage === "uploading" ? "" : "is-indeterminate"
                    }`}
                    role="progressbar"
                  >
                    <span
                      style={{
                        width: `${
                          item.stage === "uploading"
                            ? (item.progress ?? 0)
                            : 100
                        }%`,
                      }}
                    />
                  </div>
                  <button
                    className="secondary-button"
                    onClick={() =>
                      abortControllersRef.current.get(item.key)?.abort()
                    }
                    type="button"
                  >
                    取消上传
                  </button>
                </div>
              ) : item.error ? (
                <button
                  className="secondary-button"
                  onClick={() =>
                    setUploads((current) =>
                      current.filter((entry) => entry.key !== item.key),
                    )
                  }
                  type="button"
                >
                  知道了
                </button>
              ) : (
                <span className="setup-success">已提交拆解</span>
              )}
            </li>
          ))}
        </ul>
      ) : null}

      {projectsError ? <p className="settings-error">{projectsError}</p> : null}
      {isProjectsLoading ? (
        <p className="status-note">正在加载项目列表</p>
      ) : null}

      {!isProjectsLoading && !projectsError && projects.length ? (
        <ul className="projects-list">
          {projects.map((project) => {
            const isAnalyzed = project.analysis_status === "READY";
            const previewState = promptPreviews[project.id];
            const preview =
              previewState &&
              previewState !== "loading" &&
              previewState !== "error"
                ? previewState
                : null;
            return (
              <li className="projects-list__item" key={project.id}>
                <div className="projects-list__row">
                  <button
                    aria-label={`打开项目 ${project.name}`}
                    className="projects-list__main"
                    onClick={() => onOpenAnalysis(project)}
                    type="button"
                  >
                    <strong>{project.name}</strong>
                    <span className="projects-list__meta">
                      {renderStatusBadge(project)}
                    </span>
                  </button>
                  <div className="projects-list__actions">
                    {isAnalyzed && canWrite ? (
                      <button
                        className="projects-generate-button"
                        onClick={() => setQuickGenerateProject(project)}
                        type="button"
                      >
                        生成视频
                      </button>
                    ) : null}
                    {isAnalyzed ? (
                      <button
                        className="secondary-button"
                        onClick={() => togglePromptPreview(project)}
                        type="button"
                      >
                        {expandedProjectId === project.id
                          ? "收起提示词"
                          : "提示词"}
                      </button>
                    ) : null}
                    {project.reference_upload_status !== "READY" && canWrite ? (
                      <button
                        className="secondary-button"
                        disabled={isUploading || Boolean(deletingProjectId)}
                        onClick={() => requestRebind(project)}
                        type="button"
                      >
                        重新上传
                      </button>
                    ) : null}
                    {canWrite ? (
                      <button
                        aria-label={`删除项目 ${project.name}`}
                        className="secondary-button project-delete-button"
                        disabled={isUploading || Boolean(deletingProjectId)}
                        onClick={() => void handleDeleteProject(project)}
                        type="button"
                      >
                        {deletingProjectId === project.id ? "正在删除" : "删除"}
                      </button>
                    ) : null}
                  </div>
                </div>
                {expandedProjectId === project.id ? (
                  <div className="projects-prompt-preview">
                    {previewState === "loading" ? (
                      <p className="status-note">正在编译提示词…</p>
                    ) : null}
                    {previewState === "error" ? (
                      <p className="settings-error">
                        提示词预览暂不可用，请稍后重试。
                      </p>
                    ) : null}
                    {preview ? (
                      <>
                        <pre className="quickgen-prompt-text">
                          {preview.prompt_text}
                        </pre>
                        <p className="status-note">
                          成片 {preview.output_duration_seconds} 秒 ·{" "}
                          {preview.resolution} ·
                          {preview.script_source === "script_version"
                            ? " 口播来源：已保存口播稿"
                            : " 口播来源：拆解原稿"}
                        </p>
                      </>
                    ) : null}
                  </div>
                ) : null}
              </li>
            );
          })}
        </ul>
      ) : null}

      {!isProjectsLoading && !projectsError && !projects.length ? (
        <p className="status-note">
          还没有项目。上传第一个参考视频即可开始复刻。
        </p>
      ) : null}

      {quickGenerateProject ? (
        <QuickGenerateDialog
          onBatchCreated={onBatchCreated}
          onClose={() => setQuickGenerateProject(null)}
          project={quickGenerateProject}
          readOnly={!canWrite}
        />
      ) : null}
    </section>
  );
}

function defaultProjectName(filename: string) {
  const base = filename.replace(/\.(mp4|mov)$/i, "").trim();
  return (base || filename).slice(0, 120);
}
