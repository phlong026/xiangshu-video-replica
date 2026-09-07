import { useEffect, useRef, useState } from "react";
import {
  getStudioNotificationPreferences,
  updateStudioNotificationPreferences,
} from "../api";
import { useStudio } from "./context";
import {
  cancelStudioTask,
  downloadStudioTaskResult,
  loadTaskPreview,
  retryStudioTask,
  uploadWorkbenchSourceVideo,
} from "./live";
import { draftFromTask } from "./state";
import type { StudioTask, StudioVideo } from "./types";
import {
  Button,
  Empty,
  Field,
  FilterSelect,
  formatTaskTime,
  Hint,
  Icon,
  Media,
  Panel,
  Tabs,
} from "./ui";

const statusNames: Record<StudioTask["status"], string> = {
  running: "生成中",
  queued: "排队中",
  failed: "待处理",
  completed: "已完成",
  uncertain: "状态待确认",
  cancelled: "已取消",
};

/** 任务中心状态列的图标与子文案（与效果图一致：排队中"等待开始"、
 * 待处理"生成失败"），状态待确认保持独立提示避免误导重试。 */
const statusHints: Partial<Record<StudioTask["status"], string>> = {
  queued: "等待开始",
  failed: "生成失败",
  uncertain: "状态待确认",
};

const statusIcons: Record<StudioTask["status"], string> = {
  running: "clock",
  queued: "clock",
  failed: "warning",
  uncertain: "warning",
  completed: "check",
  cancelled: "close",
};

function formatWorkbenchLikes(video: StudioVideo) {
  if (video.likeDisplay) return video.likeDisplay;
  if (video.likes >= 10000) {
    return `${(video.likes / 10000).toFixed(1).replace(".0", "")}万`;
  }
  return video.likes.toLocaleString("zh-CN");
}

function Status({ task }: { task: StudioTask }) {
  return (
    <span className={`studio-status studio-status--${task.status}`}>
      {statusNames[task.status]}
      {task.progress !== undefined && task.status === "running"
        ? ` ${task.progress}%`
        : ""}
    </span>
  );
}

function StatusCell({ task }: { task: StudioTask }) {
  const hint = statusHints[task.status];
  return (
    <div className="studio-status-cell">
      <span className={`studio-status studio-status--${task.status}`}>
        <Icon name={statusIcons[task.status]} size={16} />
        {statusNames[task.status]}
        {task.progress !== undefined && task.status === "running"
          ? ` ${task.progress}%`
          : ""}
      </span>
      {hint && <small>{hint}</small>}
    </div>
  );
}

/** Per-row overflow menu on the workbench "正在进行" list. Every action is
 * real: jump to the task center, or copy the server batch id for support. */
function RunningRowMenu({ task }: { task: StudioTask }) {
  const { navigate, notify } = useStudio();
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!open) return;
    const close = (event: PointerEvent) => {
      if (rootRef.current && !rootRef.current.contains(event.target as Node)) {
        setOpen(false);
      }
    };
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
    };
    document.addEventListener("pointerdown", close);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("pointerdown", close);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  const copyTaskId = async () => {
    try {
      await navigator.clipboard.writeText(
        task.backendId || task.batchId || task.id,
      );
      notify("任务编号已复制");
    } catch {
      notify("复制失败，请手动复制任务编号。");
    }
  };

  return (
    <div className="studio-row-menu" ref={rootRef}>
      <button
        type="button"
        className="studio-row-menu-trigger"
        aria-label={`更多操作：${task.title}`}
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => setOpen((value) => !value)}
      >
        <Icon name="more" size={20} />
      </button>
      {open && (
        <div className="studio-row-menu-list" role="menu">
          <button
            type="button"
            role="menuitem"
            onClick={() => {
              setOpen(false);
              navigate("tasks");
            }}
          >
            打开任务中心
          </button>
          <button
            type="button"
            role="menuitem"
            onClick={() => {
              setOpen(false);
              void copyTaskId();
            }}
          >
            复制任务编号
          </button>
        </div>
      )}
    </div>
  );
}

export function WorkbenchPage() {
  const {
    data,
    review,
    navigate,
    openLive,
    notify,
    patchDraft,
    updateData,
    state,
    extractScriptFromUpload,
  } = useStudio();
  const [sourceLink, setSourceLink] = useState("");
  const [upload, setUpload] = useState<{
    name: string;
    progress: number;
    error: string;
    projectId: string | null;
    completed: boolean;
  } | null>(null);
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const uploadOperationRef = useRef(0);
  const uploadAbortRef = useRef<AbortController | null>(null);
  useEffect(
    () => () => {
      uploadOperationRef.current += 1;
      uploadAbortRef.current?.abort();
    },
    [],
  );
  const active = data.tasks.filter((task) =>
    ["running", "queued", "uncertain"].includes(task.status),
  );
  const featuredVideos = data.videos.slice(0, 5);
  const begin = (mode: "copy" | "replica") => {
    if (review) {
      navigate(mode);
      return;
    }
    if (sourceLink.trim()) {
      notify("视频链接解析接口尚未接入，可先上传视频进行拆解。");
      return;
    }
    if (upload?.projectId || state.draft.sourceId || state.draft.projectId) {
      // 上传完成的来源已经写进当前草稿：复刻直接进分镜工作区；
      // 文案提取走 script-from-audio 异步管线（抽音轨→转写→回填草稿）。
      if (mode === "replica") {
        navigate("replica");
        return;
      }
      extractScriptFromUpload();
      return;
    }
    openLive("projects");
  };
  const handleUploadFile = (file: File) => {
    if (!/\.(mp4|mov)$/i.test(file.name)) {
      notify("目前仅支持 MP4 / MOV 视频文件。");
      return;
    }
    const operation = ++uploadOperationRef.current;
    uploadAbortRef.current?.abort();
    const abortController = new AbortController();
    uploadAbortRef.current = abortController;
    setUpload({
      name: file.name,
      progress: 0,
      error: "",
      projectId: null,
      completed: false,
    });
    void uploadWorkbenchSourceVideo(
      file,
      (progress) => {
        if (operation !== uploadOperationRef.current) return;
        setUpload((current) => (current ? { ...current, progress } : current));
      },
      abortController.signal,
    )
      .then((uploaded) => {
        if (operation !== uploadOperationRef.current) return;
        const { projectId, assetId } = uploaded;
        setUpload((current) =>
          current
            ? { ...current, progress: 100, projectId, completed: true }
            : current,
        );
        patchDraft({
          projectId,
          sourceId: assetId,
          sourceAssetId: assetId,
        });
        if (uploaded.project || uploaded.asset) {
          updateData((current) => ({
            ...current,
            projects: uploaded.project
              ? [
                  uploaded.project,
                  ...current.projects.filter(
                    (project) => project.id !== uploaded.project?.id,
                  ),
                ]
              : current.projects,
            assets: uploaded.asset
              ? [
                  uploaded.asset,
                  ...current.assets.filter(
                    (asset) => asset.id !== uploaded.asset?.id,
                  ),
                ]
              : current.assets,
          }));
        }
        notify("视频已上传云存储，来源已加入当前创作。");
      })
      .catch((error) => {
        if (operation !== uploadOperationRef.current) return;
        setUpload((current) =>
          current
            ? {
                ...current,
                error:
                  error instanceof Error && error.message.trim()
                    ? error.message.trim()
                    : "上传失败，请重试。",
              }
            : current,
        );
      });
  };
  return (
    <section className="studio-home">
      <h2 className="studio-home-page-title">工作台</h2>
      <header className="studio-hero">
        <h1>
          粘贴一条爆款乡墅视频链接，
          <strong>快速生成它的原创视频</strong>
        </h1>
      </header>
      <div className="studio-start">
        <div className="studio-source-input">
          <button
            type="button"
            className="studio-upload-icon"
            aria-label="上传视频"
            onClick={() => {
              if (review) {
                notify("审核示例不执行真实上传。");
                return;
              }
              fileInputRef.current?.click();
            }}
          >
            <Icon name="upload" />
          </button>
          <input
            aria-label="视频链接"
            placeholder="粘贴视频链接，如抖音、视频号、小红书链接等"
            value={sourceLink}
            onChange={(event) => setSourceLink(event.target.value)}
          />
          <input
            ref={fileInputRef}
            type="file"
            aria-label="选择视频文件"
            accept=".mp4,.mov,video/mp4,video/quicktime"
            hidden
            onChange={(event) => {
              const file = event.target.files?.[0];
              if (file) handleUploadFile(file);
              event.target.value = "";
            }}
          />
          <Button variant="primary" onClick={() => begin("copy")}>
            <Icon name="pen" />
            提取文案
          </Button>
          <Button onClick={() => begin("replica")}>
            <Icon name="play" />
            开始复刻
          </Button>
        </div>
        {upload && (
          <p className="studio-upload-status" role="status">
            {upload.error
              ? `上传失败：${upload.error}`
              : upload.completed
                ? `已上传云存储：${upload.name}`
                : `正在上传 ${upload.name}… ${upload.progress}%`}
          </p>
        )}
        <p className="studio-start-helper">
          提取文案进入文案工坊，开始复刻进入分镜工作区。
        </p>
      </div>
      <div className="studio-home-metrics">
        {[
          {
            label: "今日成片",
            value: data.stats ? String(data.stats.today_completed) : "—",
            hint: review ? "较昨日 +3" : "今日已完成",
            tone: "success",
            icon: "video",
            page: "tasks" as const,
          },
          {
            label: "成片队列",
            value: data.stats
              ? String(data.stats.running + data.stats.queued)
              : data.loading
                ? "—"
                : String(active.length),
            hint: "等待中",
            tone: "muted",
            icon: "tasks",
            page: "tasks" as const,
          },
          {
            label: "累计已发布",
            // C5 发布能力暂缓：没有真实发布数据源，保持 "—" 不伪造。
            value: review ? "156" : "—",
            hint: review ? "本周 +21" : "等待发布统计",
            tone: "success",
            icon: "upload",
            page: "publishing" as const,
          },
          {
            label: "待处理",
            value: data.stats
              ? String(data.stats.needs_attention)
              : data.loading
                ? "—"
                : String(
                    data.tasks.filter((task) =>
                      ["failed", "uncertain"].includes(task.status),
                    ).length,
                  ),
            hint: "需要您处理",
            tone: "danger",
            icon: "warning",
            page: "tasks" as const,
          },
        ].map((metric) => (
          <button
            key={metric.label}
            type="button"
            className="studio-panel"
            onClick={() => navigate(metric.page)}
          >
            <span>
              <Icon name={metric.icon} />
              {metric.label}
            </span>
            <strong>{metric.value}</strong>
            <small className={`studio-metric-hint is-${metric.tone}`}>
              {metric.hint}
            </small>
          </button>
        ))}
      </div>
      <div className="studio-home-grid">
        <div className="studio-home-main">
          <h2>正在进行</h2>
          <Panel className="studio-running-list">
            {active.length ? (
              active.slice(0, 2).map((task) => (
                <div className="studio-running-row" key={task.id}>
                  {task.poster ? (
                    <img src={task.poster} alt={task.title} />
                  ) : (
                    <Icon name="video" size={46} />
                  )}
                  <div>
                    <h3>{task.title}</h3>
                    <p>{task.type}</p>
                  </div>
                  <div className="studio-running-progress">
                    <span
                      className={`studio-status studio-status--${task.status}`}
                    >
                      {statusNames[task.status]}
                    </span>
                    {task.progress !== undefined &&
                      task.status === "running" && (
                        <>
                          <progress value={task.progress} max={100} />
                          <span className="studio-running-percent">
                            {task.progress}%
                          </span>
                        </>
                      )}
                  </div>
                  <Button
                    onClick={() =>
                      navigate("task-detail", { selectedTaskId: task.id })
                    }
                  >
                    查看详情
                  </Button>
                  <RunningRowMenu task={task} />
                </div>
              ))
            ) : (
              <Empty
                title={data.loading ? "正在读取任务" : "还没有进行中的任务"}
                description="从上传视频或选择素材开始创作"
              />
            )}
          </Panel>
          <div className="studio-home-section-heading">
            <h2>爆款视频精选</h2>
            <Button
              variant="quiet"
              aria-label="查看全部爆款"
              onClick={() => navigate("viral")}
            >
              查看全部爆款
              <Icon name="arrow" size={16} />
            </Button>
          </div>
          {featuredVideos.length ? (
            <div className="studio-home-viral-grid">
              {featuredVideos.map((video) => (
                <article className="studio-home-viral-card" key={video.id}>
                  <button
                    type="button"
                    className="studio-home-viral-cover"
                    aria-label={`查看详情：${video.title}`}
                    onClick={() =>
                      navigate("viral-detail", {
                        selectedVideoId: video.id,
                        returnTo: "workbench",
                      })
                    }
                  >
                    <img src={video.poster} alt="" loading="lazy" />
                    <span className="studio-home-viral-platform">
                      {video.platform}
                    </span>
                    <span className="studio-home-viral-duration">
                      {video.duration}
                    </span>
                  </button>
                  <h3 title={video.title}>{video.title}</h3>
                  <div className="studio-home-viral-meta">
                    <span>
                      <Icon name="fire" size={14} />
                      热度 {formatWorkbenchLikes(video)}
                    </span>
                    <Button
                      variant="quiet"
                      aria-label={`用它复刻：${video.title}`}
                      onClick={() => {
                        patchDraft({ sourceId: video.id });
                        navigate("replica", {
                          selectedVideoId: video.id,
                          returnTo: "workbench",
                        });
                      }}
                    >
                      用它复刻
                    </Button>
                  </div>
                </article>
              ))}
            </div>
          ) : (
            <Panel className="studio-home-viral-empty">
              <Empty
                title={data.loading ? "正在读取爆款灵感" : "暂无爆款灵感"}
                description="前往爆款视频页查看更多乡墅参考作品"
              />
            </Panel>
          )}
        </div>
        <aside className="studio-home-side">
          <Panel className="studio-activity">
            <h2>任务动态</h2>
            {data.tasks.slice(0, 5).map((task) => (
              <button
                type="button"
                key={task.id}
                onClick={() =>
                  navigate("task-detail", { selectedTaskId: task.id })
                }
              >
                <i className={`studio-dot studio-dot--${task.status}`} />
                <span>
                  “{task.title}” {statusNames[task.status]}
                  <small>{formatTaskTime(task.submitted)}</small>
                </span>
              </button>
            ))}
            {!data.tasks.length && <p>暂无任务动态</p>}
          </Panel>
          <Panel className="studio-shortcuts">
            <h2>快捷入口</h2>
            <button
              type="button"
              aria-label="快捷入口：文案工坊"
              onClick={() => navigate("copy")}
            >
              <Icon name="pen" size={28} />
              <span>
                文案工坊<small>创作爆款文案</small>
              </span>
              <Icon name="chevron" />
            </button>
            <button
              type="button"
              aria-label="快捷入口：人物库"
              onClick={() => navigate("people")}
            >
              <Icon name="person" size={28} />
              <span>
                人物库<small>管理数字人</small>
              </span>
              <Icon name="chevron" />
            </button>
            <button
              type="button"
              aria-label="快捷入口：素材库"
              onClick={() => navigate("materials")}
            >
              <Icon name="folder" size={28} />
              <span>
                素材库<small>管理我的素材</small>
              </span>
              <Icon name="chevron" />
            </button>
            <button
              type="button"
              aria-label="快捷入口：数据看板"
              onClick={() => navigate("analytics")}
            >
              <Icon name="chart" size={28} />
              <span>
                数据看板<small>查看创作数据</small>
              </span>
              <Icon name="chevron" />
            </button>
          </Panel>
        </aside>
      </div>
    </section>
  );
}

export function TasksPage() {
  const { data, navigate, openLive, review, notify, refresh } = useStudio();
  const [status, setStatus] = useState("all");
  const [kind, setKind] = useState("全部");
  const [cancellingId, setCancellingId] = useState<string | null>(null);
  const matchesStatus = (task: StudioTask) =>
    status === "all" ||
    (status === "active"
      ? ["running", "queued"].includes(task.status)
      : status === "attention"
        ? ["failed", "uncertain"].includes(task.status)
        : task.status === "completed");
  const tasks = data.tasks.filter(
    (task) => matchesStatus(task) && (kind === "全部" || kind === task.type),
  );
  // 类型菜单计数与状态筛选联动：展示"当前状态下各类型还有几个"。
  const kindItems = [
    "全部",
    "视频复刻",
    "视频生成",
    "数字人口播",
    "人物置换",
  ].map((label) => ({
    id: label,
    label: label === "全部" ? "全部类型" : label,
    count: data.tasks.filter(
      (task) =>
        matchesStatus(task) && (label === "全部" || label === task.type),
    ).length,
  }));
  const activeCount = data.tasks.filter((task) =>
    ["running", "queued"].includes(task.status),
  ).length;
  const attentionCount = data.tasks.filter((task) =>
    ["failed", "uncertain"].includes(task.status),
  ).length;
  const cancelTask = async (task: StudioTask) => {
    if (review) {
      notify("审核示例不执行真实取消。");
      return;
    }
    setCancellingId(task.id);
    try {
      const result = await cancelStudioTask(task);
      notify(
        result.billingStatus === "RELEASED" ||
          result.billingStatus === "RELEASE"
          ? "任务已取消，预扣积分已退回。"
          : result.billingStatus
            ? "任务已取消，计费状态处理中，请稍后刷新核对。"
            : "任务已取消，请刷新核对计费状态。",
      );
      refresh();
    } catch (error) {
      notify(
        error instanceof Error && error.message.trim()
          ? error.message.trim()
          : "取消失败，请重试。",
      );
    } finally {
      setCancellingId(null);
    }
  };
  return (
    <section className="studio-tasks-page">
      <h1>任务中心</h1>
      <div className="studio-page-actions">
        {!review && (
          <Button onClick={() => openLive("tasks")}>历史任务与下载</Button>
        )}
        <Button variant="primary" onClick={() => navigate("replica")}>
          <Icon name="play" />
          视频复刻
        </Button>
      </div>
      <div className="studio-filter-bar">
        <Tabs
          value={status}
          onChange={setStatus}
          items={[
            { id: "all", label: "全部" },
            { id: "active", label: `进行中 ${activeCount}` },
            { id: "attention", label: `待处理 ${attentionCount}` },
            { id: "completed", label: "已完成" },
          ]}
        />
        <FilterSelect
          label="类型"
          items={kindItems}
          value={kind}
          onChange={setKind}
        />
      </div>
      <p className="studio-result-count" role="status">
        共 {tasks.length} 条任务
      </p>
      <div className="studio-table-wrap">
        <table className="studio-table">
          <thead>
            <tr>
              <th>作品</th>
              <th>类型</th>
              <th>状态</th>
              <th>提交时间</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            {tasks.map((task) => (
              <tr key={task.id}>
                <td>
                  <div className="studio-task-name">
                    {task.poster ? (
                      <img src={task.poster} alt="" />
                    ) : (
                      <Icon name="video" size={30} />
                    )}
                    <span>{task.title}</span>
                  </div>
                </td>
                <td>{task.type}</td>
                <td>
                  <StatusCell task={task} />
                </td>
                <td>{formatTaskTime(task.submitted)}</td>
                <td>
                  {task.status === "queued" &&
                  (task.backendKind !== "oral_task" ||
                    task.backendStatus === "QUEUED") ? (
                    <Button
                      variant="quiet"
                      disabled={cancellingId === task.id}
                      onClick={() => void cancelTask(task)}
                    >
                      {cancellingId === task.id ? "取消中…" : "取消任务"}
                    </Button>
                  ) : (
                    <Button
                      variant="quiet"
                      onClick={() =>
                        navigate("task-detail", { selectedTaskId: task.id })
                      }
                    >
                      {task.status === "completed" ? "查看结果" : "查看详情"}
                    </Button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {!tasks.length && (
          <Empty
            title={data.loading ? "正在加载任务" : "当前筛选下暂无任务"}
            description="真实生成任务会在这里显示，不会因接口失败填入示例结果。"
            action={
              status !== "all" || kind !== "全部" ? (
                <Button
                  onClick={() => {
                    setStatus("all");
                    setKind("全部");
                  }}
                >
                  清除筛选
                </Button>
              ) : undefined
            }
          />
        )}
      </div>
      {!review && (
        <Hint>
          当前显示最近任务。更多记录及状态核对请进入“历史任务与下载”。状态待确认的任务请先核对，不要直接重复提交。
        </Hint>
      )}
    </section>
  );
}

export function TaskDetailPage() {
  const {
    state,
    data,
    navigate,
    openLive,
    review,
    patchState,
    updateData,
    notify,
    refresh,
  } = useStudio();
  const [actionBusy, setActionBusy] = useState<"download" | "retry">();
  const [previewLoad, setPreviewLoad] = useState<{
    taskId?: string;
    status: "idle" | "loading" | "empty" | "error" | "ready";
  }>({ status: "idle" });
  const selectedTaskIdRef = useRef(state.selectedTaskId);
  const previewRequestRef = useRef(0);
  selectedTaskIdRef.current = state.selectedTaskId;
  const task = data.tasks.find((item) => item.id === state.selectedTaskId);
  if (!task)
    return (
      <Empty
        title="未选择任务"
        action={<Button onClick={() => navigate("tasks")}>返回任务中心</Button>}
      />
    );
  const result = data.assets.find((asset) => asset.id === task.resultId);
  const previewStatus =
    previewLoad.taskId === task.id ? previewLoad.status : "idle";
  const person = data.people.find((item) => item.id === task.ipId);
  const info = [
    ["任务类型", task.type],
    [
      "信息来源",
      task.driverMode === "audio"
        ? "完整口播音频"
        : task.scriptVersion
          ? `终稿 V${task.scriptVersion}`
          : "项目分镜",
    ],
    ["IP", person?.name || "—"],
    ...(task.driverMode !== "audio"
      ? [
          [
            "声音",
            person?.voices.find((voice) => voice.id === task.voiceId)?.name ||
              "—",
          ],
        ]
      : []),
    [
      "口播分身",
      person?.avatars.find((avatar) => avatar.id === task.avatarId)?.name ||
        "—",
    ],
    ["提交时间", task.submitted],
    ["资产状态", result?.saved ? "已保存到素材库" : "以任务返回结果为准"],
  ];
  const recreate = (page: "copy" | "oral" | "replica") => {
    if (!review && !task.draftSnapshot) {
      openLive("tasks");
      notify(
        "请在原任务记录中使用重新生成，以保留服务端确认的镜头和素材参数。",
      );
      return;
    }
    patchState({ draft: draftFromTask(task) });
    navigate(page, { returnTo: "task-detail" });
    if (!review)
      notify("已带入任务关联项目。编辑前请从项目读取已保存的脚本与素材版本。");
  };
  const previewResult = async () => {
    const requestedTask = task;
    const requestId = ++previewRequestRef.current;
    setPreviewLoad({ taskId: requestedTask.id, status: "loading" });
    try {
      const asset = await loadTaskPreview(requestedTask);
      if (
        requestId !== previewRequestRef.current ||
        selectedTaskIdRef.current !== requestedTask.id
      )
        return;
      if (!asset) {
        setPreviewLoad({ taskId: requestedTask.id, status: "empty" });
        return;
      }
      updateData((current) => ({
        ...current,
        assets: current.assets.some((item) => item.id === asset.id)
          ? current.assets.map((item) => (item.id === asset.id ? asset : item))
          : [...current.assets, asset],
        tasks: current.tasks.map((item) =>
          item.id === requestedTask.id ? { ...item, resultId: asset.id } : item,
        ),
      }));
      setPreviewLoad({ taskId: requestedTask.id, status: "ready" });
    } catch {
      if (
        requestId === previewRequestRef.current &&
        selectedTaskIdRef.current === requestedTask.id
      )
        setPreviewLoad({ taskId: requestedTask.id, status: "error" });
    }
  };
  const downloadResult = async () => {
    if (review) {
      notify(
        "这是效果审核示例，未提供可下载成片；真实任务通过原有下载接口获取。",
      );
      return;
    }
    if (task.backendKind !== "oral_task") {
      openLive("tasks");
      return;
    }
    setActionBusy("download");
    try {
      await downloadStudioTaskResult(task);
    } catch (error) {
      notify(
        error instanceof Error && error.message.trim()
          ? error.message.trim()
          : "口播成片下载失败，请重试。",
      );
    } finally {
      setActionBusy(undefined);
    }
  };
  const retryTask = async () => {
    setActionBusy("retry");
    try {
      await retryStudioTask(task);
      notify(
        task.retryAction === "archive-retry"
          ? "已提交成片归档重试。"
          : "已提交口播任务重试。",
      );
      refresh();
    } catch (error) {
      notify(
        error instanceof Error && error.message.trim()
          ? error.message.trim()
          : "重试失败，请刷新后再试。",
      );
    } finally {
      setActionBusy(undefined);
    }
  };
  return (
    <section className="studio-task-detail">
      <h1>任务详情与结果</h1>
      <Button onClick={() => navigate("tasks")}>
        <Icon name="back" />
        返回任务中心
      </Button>
      <header>
        <h2>{task.title}</h2>
        <span className="studio-tag">{task.type}</span>
        <Status task={task} />
      </header>
      <p>
        {task.status === "completed"
          ? "作品已生成完成，可查看与管理生成结果"
          : "任务状态实时以服务端记录为准"}
      </p>
      <div className="studio-result-grid">
        <Media
          asset={
            (task.status === "completed" && result) ||
            (task.status === "completed" && task.poster
              ? {
                  id: task.id,
                  name: task.title,
                  kind: "video",
                  poster: task.poster,
                  group: "任务",
                  source: "任务中心",
                  saved: false,
                }
              : undefined)
          }
          alt={
            task.status === "completed"
              ? task.title
              : "任务处理中或待核对，尚无可预览成片"
          }
          className="studio-result-preview"
        />
        <Panel>
          <h2>任务信息</h2>
          <dl className="studio-details">
            {info.map(([label, value]) => (
              <div key={label}>
                <dt>{label}</dt>
                <dd>{value}</dd>
              </div>
            ))}
          </dl>
        </Panel>
        <Panel>
          <h2>动作</h2>
          <div className="studio-result-actions">
            {!review && task.status === "completed" && !result && (
              <Button
                variant="primary"
                onClick={previewResult}
                disabled={previewStatus === "loading"}
              >
                <Icon name="play" />
                {previewStatus === "loading"
                  ? "正在加载预览…"
                  : previewStatus === "error"
                    ? "重试预览"
                    : previewStatus === "empty"
                      ? "重新尝试"
                      : "预览成片"}
              </Button>
            )}
            <Button
              onClick={() => void downloadResult()}
              disabled={
                task.status !== "completed" || actionBusy === "download"
              }
            >
              <Icon name="download" />
              {actionBusy === "download" ? "正在下载…" : "下载成片"}
            </Button>
            <Button
              disabled={!result || task.status !== "completed"}
              onClick={() =>
                navigate("materials", {
                  selectedAssetId: task.resultId,
                  returnTo: "task-detail",
                })
              }
            >
              <Icon name="folder" />
              查看素材
            </Button>
            <Button
              variant="primary"
              disabled={!result || task.status !== "completed"}
              onClick={() =>
                navigate("publishing", { selectedAssetId: task.resultId })
              }
            >
              <Icon name="upload" />
              去发布管理
            </Button>
          </div>
          {previewStatus === "empty" && (
            <Hint>该批次暂时没有可预览的成功结果。</Hint>
          )}
          {previewStatus === "error" && <Hint>预览加载失败，请重试。</Hint>}
          {!review && task.status === "completed" && (
            <Hint>
              预览仅展示首个可用结果；完整结果与下载请进入“历史任务与下载”。
            </Hint>
          )}
          <Hint>进入发布管理仅创建发布草稿，不会自动发布。</Hint>
          {!review && task.retryAction && (
            <Button
              disabled={actionBusy === "retry"}
              onClick={() => void retryTask()}
            >
              {task.retryAction === "archive-retry" ? "重试归档" : "重试提交"}
            </Button>
          )}
          {task.status === "uncertain" && !task.retryAction && (
            <Button onClick={() => openLive("tasks")}>核对任务状态</Button>
          )}
        </Panel>
        <Panel>
          <h2>基于此任务再创作</h2>
          <div className="studio-recreate">
            <Button onClick={() => recreate("copy")}>
              <Icon name="pen" />
              调整脚本
            </Button>
            <Button
              onClick={() =>
                navigate("materials", {
                  returnTo: "task-detail",
                  selectedTaskId: task.id,
                })
              }
            >
              <Icon name="image" />
              更换素材
            </Button>
            <Button
              onClick={() =>
                recreate(task.type === "数字人口播" ? "oral" : "replica")
              }
            >
              <Icon name="refresh" />
              新建草稿
            </Button>
          </div>
        </Panel>
      </div>
    </section>
  );
}

export function ProfilePage() {
  const { user, review, openLive, notify } = useStudio();
  const [name, setName] = useState(user.display_name || user.username);
  const [profileTab, setProfileTab] = useState("overview");
  // C10b 通知偏好：进页拉取，乐观保存、失败回退；审核模式只演示不落库。
  const [notificationsEnabled, setNotificationsEnabled] = useState<
    boolean | null
  >(null);
  const [savingNotifications, setSavingNotifications] = useState(false);

  useEffect(() => {
    if (review) {
      setNotificationsEnabled(true);
      return;
    }
    let cancelled = false;
    getStudioNotificationPreferences()
      .then((prefs) => {
        if (!cancelled) setNotificationsEnabled(prefs.enabled);
      })
      .catch(() => {
        if (!cancelled) setNotificationsEnabled(null);
      });
    return () => {
      cancelled = true;
    };
  }, [review]);

  async function toggleNotifications() {
    if (review) {
      notify("审核模式下为演示开关，不保存设置。");
      return;
    }
    if (notificationsEnabled === null || savingNotifications) return;
    const previous = notificationsEnabled;
    const next = !previous;
    setNotificationsEnabled(next);
    setSavingNotifications(true);
    try {
      await updateStudioNotificationPreferences(next);
      notify(next ? "已开启通知。" : "已关闭通知。");
    } catch (cause) {
      setNotificationsEnabled(previous);
      notify(cause instanceof Error ? cause.message : "保存通知偏好失败");
    } finally {
      setSavingNotifications(false);
    }
  }
  return (
    <section className="studio-profile">
      <h1>用户档案</h1>
      <div className="studio-profile-identity">
        {review ? (
          <img src="/studio/li.png" alt="登录账户" />
        ) : (
          <span className="studio-user-initial">
            {user.display_name?.slice(0, 1) || "我"}
          </span>
        )}
        <div>
          <h2>
            {user.display_name || user.username}
            <span className="studio-tag">登录账户</span>
          </h2>
          <p>{review ? "众墅之家" : "账户资料"}</p>
          <small>与创作用的人物 IP 分开管理</small>
        </div>
      </div>
      <Tabs
        value={profileTab}
        onChange={(id) =>
          id === "billing"
            ? openLive("wallet")
            : id === "devices"
              ? openLive("profile")
              : setProfileTab(id)
        }
        items={[
          { id: "overview", label: "账户资料" },
          { id: "publishing", label: "发布账号" },
          { id: "billing", label: "使用记录" },
          { id: "devices", label: "设备管理" },
        ]}
      />
      {profileTab === "publishing" ? (
        <Panel>
          <h2>发布账号管理</h2>
          <Hint>这里管理平台账号，人物 IP 与作品发布在各自模块中管理。</Hint>
          {review ? (
            <>
              <div className="studio-publish-account">
                <Icon name="video" size={32} />
                <span>抖音 · 张工说乡墅</span>
                <small>已连接 · 示例</small>
              </div>
              <div className="studio-publish-account">
                <Icon name="link" size={32} />
                <span>视频号 · 众墅乡建</span>
                <small>已连接 · 示例</small>
              </div>
            </>
          ) : (
            <Empty
              title="尚未连接发布账号"
              description="平台账号授权接口尚未接入，暂不可添加账号。"
            />
          )}
          <Button disabled>连接发布账号</Button>
          <Hint>
            {review
              ? "这里只展示账号管理样式，不代表已连接真实账号。"
              : "接入后，已授权账号将同步提供给发布管理选择。"}
          </Hint>
        </Panel>
      ) : (
        <>
          <div className="studio-profile-grid">
            <Panel>
              <h2>账户资料</h2>
              <Field label="用户名">
                <input
                  value={name}
                  onChange={(event) => setName(event.target.value)}
                  readOnly={!review}
                />
              </Field>
              <dl className="studio-details">
                <div>
                  <dt>团队</dt>
                  <dd>{review ? "众墅之家" : "未提供"}</dd>
                </div>
                <div>
                  <dt>手机号</dt>
                  <dd>{review ? "138****6688" : "以账户资料为准"}</dd>
                </div>
                <div>
                  <dt>
                    通知偏好<small>接收平台公告、任务提醒等通知</small>
                  </dt>
                  <dd>
                    <button
                      type="button"
                      className="studio-switch"
                      aria-label="通知偏好"
                      aria-pressed={notificationsEnabled === true}
                      disabled={
                        notificationsEnabled === null || savingNotifications
                      }
                      onClick={() => void toggleNotifications()}
                    >
                      {notificationsEnabled === false
                        ? "关闭"
                        : notificationsEnabled === null
                          ? "—"
                          : "开启"}
                    </button>
                  </dd>
                </div>
              </dl>
            </Panel>
            <Panel>
              <h2>发布账号概览</h2>
              {review ? (
                <>
                  <div className="studio-publish-account">
                    <Icon name="video" size={32} />
                    <span>抖音 · 张工说乡墅</span>
                    <small>已连接 · 示例</small>
                  </div>
                  <div className="studio-publish-account">
                    <Icon name="link" size={32} />
                    <span>视频号 · 众墅乡建</span>
                    <small>已连接 · 示例</small>
                  </div>
                </>
              ) : (
                <p>发布账号服务尚未接入</p>
              )}
              <Button onClick={() => setProfileTab("publishing")}>
                <Icon name="person" />
                管理发布账号
              </Button>
            </Panel>
            <Panel>
              <h2>账户使用概览</h2>
              <dl className="studio-details">
                <div>
                  <dt>账户余额</dt>
                  <dd>按实际账户显示</dd>
                </div>
                <div>
                  <dt>
                    使用记录<small>查看详细的使用记录</small>
                  </dt>
                  <dd>
                    <Button onClick={() => openLive("wallet")}>
                      查看使用记录
                    </Button>
                  </dd>
                </div>
              </dl>
            </Panel>
            <Panel>
              <h2>设备摘要</h2>
              <div className="studio-device-summary">
                <span>
                  {review
                    ? "已绑定 2 台 · 同时 1 台在线"
                    : "进入设备管理查看当前绑定记录"}
                </span>
                <Button onClick={() => openLive("profile")}>管理设备</Button>
              </div>
            </Panel>
          </div>
          <div className="studio-profile-save">
            <Button
              variant="primary"
              onClick={() =>
                review
                  ? notify("示例账户，不保存真实资料。")
                  : openLive("profile")
              }
            >
              {review ? "保存资料" : "编辑账户资料"}
            </Button>
          </div>
        </>
      )}
    </section>
  );
}
