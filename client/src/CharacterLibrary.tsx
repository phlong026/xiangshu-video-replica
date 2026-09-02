import {
  type FormEvent,
  useCallback,
  useEffect,
  useRef,
  useState,
} from "react";

import {
  type CharacterViewType,
  createCharacterSceneLook,
  deleteSimpleCharacterIdentity,
  downloadCharacterAsset,
  getCachedCharacterAssetUrl,
  getLatestCharacterSheetTask,
  getLatestSceneLookTask,
  listCharacterSceneLooks,
  listSimpleCharacterLibrary,
  regenerateContactSheet,
  renamePersonIdentity,
  type SimpleCharacterView,
  type SimpleLibraryEntry,
  type SimpleSceneLook,
  type UserRole,
  waitForCharacterSheetTask,
} from "./api";
import { SimpleCharacterUpload } from "./SimpleCharacterUpload";

const VIEW_LABELS: Record<CharacterViewType, string> = {
  FRONT_FACE: "正脸近景",
  FRONT_HALF: "正面半身",
  FRONT_FULL: "正面全身",
  LEFT_45: "左 45°",
  RIGHT_45: "右 45°",
  LEFT_SIDE: "左侧面",
  RIGHT_SIDE: "右侧面",
};

function errorMessage(error: unknown, fallback: string): string {
  return error instanceof Error && error.message ? error.message : fallback;
}

function entryAssetIds(entry: SimpleLibraryEntry): string[] {
  return [
    ...(entry.contact_sheet_asset_id ? [entry.contact_sheet_asset_id] : []),
    ...entry.views.map((view) => view.asset_id),
  ];
}

function sceneLookAssetIds(look: SimpleSceneLook): string[] {
  const preview =
    look.views.find((view) => view.view_type === "FRONT_FACE") ?? look.views[0];
  return [look.contact_sheet_asset_id, ...(preview ? [preview.asset_id] : [])];
}

type PreviewStatus = "loading" | "ready" | "error";

export type PendingCharacterState = {
  displayName: string;
  progress: number;
  sourcePreviewUrl?: string;
  stage: string;
  status: "working" | "error";
};

// 卡片封面取正脸近景（辨识度最高）；没有正脸时退回首张视角图，
// 视角全缺（理论上不该出现）才用拼合图裁切兜底。拼合全图在灯箱看。
function coverAsset(
  entry: SimpleLibraryEntry,
): { assetId: string; label: string } | null {
  const view =
    entry.views.find((item) => item.view_type === "FRONT_FACE") ??
    (entry.views.length ? entry.views[0] : null);
  if (view) {
    return { assetId: view.asset_id, label: VIEW_LABELS[view.view_type] };
  }
  return entry.contact_sheet_asset_id
    ? { assetId: entry.contact_sheet_asset_id, label: "五视角拼合图" }
    : null;
}

// 人物库 = 「上传 → 五视角拼合图预览 → 下载」的主动线：上方一键上传，
// 下方 4 列人物卡（正脸近景封面）；点封面开灯箱看拼合大图并下载。
export function CharacterLibrary({
  userRole,
  userId,
}: {
  userRole: UserRole;
  userId: string;
}) {
  const canManage = userRole !== "auditor";
  const [entries, setEntries] = useState<SimpleLibraryEntry[]>([]);
  const [previewUrls, setPreviewUrls] = useState<Record<string, string>>({});
  const [previewStatuses, setPreviewStatuses] = useState<
    Record<string, PreviewStatus>
  >({});
  const [pendingCharacter, setPendingCharacter] =
    useState<PendingCharacterState | null>(null);
  const pendingPreviewUrlRef = useRef("");
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [busyDownloadKey, setBusyDownloadKey] = useState("");
  const [editingId, setEditingId] = useState("");
  const [editingName, setEditingName] = useState("");
  const [busyRenameId, setBusyRenameId] = useState("");
  const [busyDeleteId, setBusyDeleteId] = useState("");
  const [busyRegenerateId, setBusyRegenerateId] = useState("");
  const [lightboxId, setLightboxId] = useState("");

  const loadPreviewUrls = useCallback(
    async (assetIds: string[], retry = false) => {
      const uniqueAssetIds = [...new Set(assetIds)];
      setPreviewStatuses((current) => ({
        ...current,
        ...Object.fromEntries(
          uniqueAssetIds.map((assetId) => [assetId, "loading"]),
        ),
      }));
      const results = await Promise.allSettled(
        uniqueAssetIds.map(async (assetId) => {
          const download = await getCachedCharacterAssetUrl(assetId);
          const retrySeparator = download.url.includes("?") ? "&" : "?";
          return [
            assetId,
            retry
              ? `${download.url}${retrySeparator}preview_retry=${Date.now()}`
              : download.url,
          ] as const;
        }),
      );
      const fulfilled = results.flatMap((result) =>
        result.status === "fulfilled" ? [result.value] : [],
      );
      const failedIds = results.flatMap((result, index) =>
        result.status === "rejected" ? [uniqueAssetIds[index]] : [],
      );
      setPreviewUrls((current) => ({
        ...current,
        ...Object.fromEntries(fulfilled),
      }));
      setPreviewStatuses((current) => ({
        ...current,
        ...Object.fromEntries(fulfilled.map(([assetId]) => [assetId, "ready"])),
        ...Object.fromEntries(failedIds.map((assetId) => [assetId, "error"])),
      }));
    },
    [],
  );

  const markPreviewError = useCallback((assetId: string) => {
    setPreviewStatuses((current) => ({ ...current, [assetId]: "error" }));
  }, []);

  const loadLibrary = useCallback(async () => {
    setIsLoading(true);
    try {
      const result = await listSimpleCharacterLibrary();
      const visibleEntries =
        userRole === "admin" || userRole === "auditor"
          ? result
          : result.filter((entry) => entry.owner_user_id === userId);
      setEntries(visibleEntries);
      setError("");
      void loadPreviewUrls(visibleEntries.flatMap(entryAssetIds));
    } catch (loadError) {
      setError(errorMessage(loadError, "人物库暂不可用，请重试。"));
    } finally {
      setIsLoading(false);
    }
  }, [loadPreviewUrls, userId, userRole]);

  const releasePendingPreview = useCallback(() => {
    if (pendingPreviewUrlRef.current) {
      URL.revokeObjectURL(pendingPreviewUrlRef.current);
      pendingPreviewUrlRef.current = "";
    }
  }, []);

  const handleGenerationFailed = useCallback((generationError: string) => {
    setPendingCharacter((current) =>
      current
        ? {
            ...current,
            stage: generationError,
            status: "error",
          }
        : current,
    );
  }, []);

  const clearPendingGeneration = useCallback(() => {
    releasePendingPreview();
    setPendingCharacter(null);
  }, [releasePendingPreview]);

  useEffect(() => {
    void loadLibrary();
  }, [loadLibrary]);

  useEffect(() => {
    let active = true;
    void (async () => {
      try {
        const task = await getLatestCharacterSheetTask();
        if (!active || !task) {
          return;
        }
        // 恢复接口不区分任务类型：场景造型任务产出的是场景五视图，
        // 文案不能冒充人物基准多视图。
        const taskNoun = task.operation === "SCENE" ? "场景造型" : "人物多视图";
        if (task.status === "SUCCEEDED") {
          clearPendingGeneration();
          setMessage(
            task.operation === "SCENE"
              ? `场景造型“${task.display_name}”已生成，请在人物卡片的“场景造型”页查看。`
              : `人物“${task.display_name}”多视图已生成。`,
          );
          // The first load may race the worker's final commit. Always reload after
          // observing SUCCEEDED so a completed character cannot stay invisible.
          await loadLibrary();
          return;
        }
        if (
          task.status === "FAILED" ||
          task.status === "SUBMISSION_UNCERTAIN"
        ) {
          setPendingCharacter({
            displayName: task.display_name,
            progress: task.status === "FAILED" ? 100 : 72,
            stage:
              task.error_message ??
              (task.status === "SUBMISSION_UNCERTAIN"
                ? "云端提交结果暂时无法确认，请稍后重试。"
                : `${taskNoun}生成失败，请重新提交。`),
            status: "error",
          });
          return;
        }
        setPendingCharacter({
          displayName: task.display_name,
          progress: task.status === "RUNNING" ? 58 : 18,
          stage:
            task.status === "RUNNING"
              ? task.operation === "SCENE"
                ? "正在云端生成场景造型五视图"
                : "正在云端生成多视角拼合图"
              : "已进入云端生成队列",
          status: "working",
        });
        await waitForCharacterSheetTask(task.id);
        if (!active) {
          return;
        }
        clearPendingGeneration();
        setMessage(
          task.operation === "SCENE"
            ? `场景造型“${task.display_name}”已生成，请在人物卡片的“场景造型”页查看。`
            : `人物“${task.display_name}”多视图已生成。`,
        );
        await loadLibrary();
      } catch (recoveryError) {
        if (active) {
          handleGenerationFailed(
            errorMessage(recoveryError, "人物生成失败，请重新提交。"),
          );
        }
      }
    })();
    return () => {
      active = false;
    };
  }, [clearPendingGeneration, handleGenerationFailed, loadLibrary]);

  useEffect(
    () => () => {
      if (pendingPreviewUrlRef.current) {
        URL.revokeObjectURL(pendingPreviewUrlRef.current);
      }
    },
    [],
  );

  function handleGenerationStarted(file: File, displayName: string) {
    releasePendingPreview();
    const sourcePreviewUrl = URL.createObjectURL(file);
    pendingPreviewUrlRef.current = sourcePreviewUrl;
    setPendingCharacter({
      displayName,
      progress: 8,
      sourcePreviewUrl,
      stage: "正在上传授权图片",
      status: "working",
    });
  }

  function handleGenerationProgress(progress: number, stage: string) {
    setPendingCharacter((current) =>
      current ? { ...current, progress, stage, status: "working" } : current,
    );
  }

  // 上传成功后立刻把新人物置顶展示，无需等待整表刷新。
  function handleCreated(newEntry: SimpleLibraryEntry) {
    clearPendingGeneration();
    setEntries((current) => [
      newEntry,
      ...current.filter((item) => item.identity_id !== newEntry.identity_id),
    ]);
    void loadPreviewUrls(entryAssetIds(newEntry));
  }

  function viewFileName(entryName: string, view: SimpleCharacterView): string {
    return `${entryName}-${VIEW_LABELS[view.view_type]}.png`;
  }

  async function handleDownloadSheet(entry: SimpleLibraryEntry) {
    if (!entry.contact_sheet_asset_id) {
      return;
    }
    setBusyDownloadKey(entry.contact_sheet_asset_id);
    setError("");
    try {
      await downloadCharacterAsset(
        entry.contact_sheet_asset_id,
        `${entry.display_name}-五视角拼合图.png`,
      );
      setMessage(`人物“${entry.display_name}”的五视角拼合图已开始下载。`);
    } catch (downloadError) {
      setError(errorMessage(downloadError, "下载拼合图失败，请稍后重试。"));
    } finally {
      setBusyDownloadKey("");
    }
  }

  async function handleDownloadView(
    entry: SimpleLibraryEntry,
    view: SimpleCharacterView,
  ) {
    setBusyDownloadKey(view.asset_id);
    setError("");
    try {
      await downloadCharacterAsset(
        view.asset_id,
        viewFileName(entry.display_name, view),
      );
    } catch (downloadError) {
      setError(errorMessage(downloadError, "下载人物视角图失败，请稍后重试。"));
    } finally {
      setBusyDownloadKey("");
    }
  }

  async function handleDownloadAll(entry: SimpleLibraryEntry) {
    setBusyDownloadKey(entry.identity_id);
    setError("");
    try {
      for (const view of entry.views) {
        await downloadCharacterAsset(
          view.asset_id,
          viewFileName(entry.display_name, view),
        );
      }
      setMessage(
        `人物“${entry.display_name}”的 ${entry.views.length} 张视角图已开始下载。`,
      );
    } catch (downloadError) {
      setError(errorMessage(downloadError, "下载人物视角图失败，请稍后重试。"));
    } finally {
      setBusyDownloadKey("");
    }
  }

  function canRename(entry: SimpleLibraryEntry): boolean {
    if (!canManage || entry.status === "ARCHIVED") {
      return false;
    }
    return userRole === "admin" || entry.owner_user_id === userId;
  }

  function canDelete(entry: SimpleLibraryEntry): boolean {
    if (!canManage) {
      return false;
    }
    return userRole === "admin" || entry.owner_user_id === userId;
  }

  // 重新生成多视图 = 用授权原图重跑单图版 identity-preserve 生成；
  // 新结果作为新版本发布并自动成为人物库预览，已选用旧版本的项目不受影响。
  async function handleRegenerate(entry: SimpleLibraryEntry) {
    setBusyRegenerateId(entry.identity_id);
    setError("");
    setMessage("");
    try {
      const result = await regenerateContactSheet(entry.identity_id);
      setMessage(
        `人物“${entry.display_name}”的多视图已重新生成（V${result.version_number}）。`,
      );
      await loadLibrary();
    } catch (regenerateError) {
      setError(
        errorMessage(regenerateError, "重新生成多视图失败，请稍后重试。"),
      );
    } finally {
      setBusyRegenerateId("");
    }
  }

  async function handleDelete(entry: SimpleLibraryEntry) {
    const confirmed = window.confirm(
      `删除“${entry.display_name}”？该人物的授权图片、五视角拼合图与全部视角图将一并删除，且无法恢复。`,
    );
    if (!confirmed) {
      return;
    }
    setBusyDeleteId(entry.identity_id);
    setError("");
    try {
      await deleteSimpleCharacterIdentity(entry.identity_id);
      setEntries((current) =>
        current.filter((item) => item.identity_id !== entry.identity_id),
      );
      setMessage(`人物“${entry.display_name}”已删除。`);
    } catch (deleteError) {
      setError(errorMessage(deleteError, "删除人物失败，请稍后重试。"));
    } finally {
      setBusyDeleteId("");
    }
  }

  function startRename(entry: SimpleLibraryEntry) {
    setEditingId(entry.identity_id);
    setEditingName(entry.display_name);
    setError("");
    setMessage("");
  }

  function cancelRename() {
    setEditingId("");
    setEditingName("");
    setBusyRenameId("");
  }

  async function saveRename(entry: SimpleLibraryEntry) {
    const name = editingName.trim();
    if (!name) {
      setError("人物名称不能为空。");
      return;
    }
    if (name === entry.display_name) {
      cancelRename();
      return;
    }
    setBusyRenameId(entry.identity_id);
    setError("");
    try {
      const updated = await renamePersonIdentity(entry.identity_id, name);
      setEntries((current) =>
        current.map((item) =>
          item.identity_id === updated.id
            ? { ...item, display_name: updated.display_name }
            : item,
        ),
      );
      setMessage(`人物名称已更新为“${updated.display_name}”。`);
      cancelRename();
    } catch (renameError) {
      setError(errorMessage(renameError, "修改人物名称失败，请重试。"));
    } finally {
      setBusyRenameId("");
    }
  }

  const lightboxEntry =
    entries.find((entry) => entry.identity_id === lightboxId) ?? null;

  return (
    <section aria-label="人物库" className="character-library-simple">
      {canManage ? (
        <SimpleCharacterUpload
          onGenerationFailed={handleGenerationFailed}
          onGenerationProgress={handleGenerationProgress}
          onGenerationStarted={handleGenerationStarted}
          onCreated={(result, displayName) =>
            handleCreated({
              identity_id: result.identity_id,
              display_name: displayName,
              owner_user_id: userId,
              status: "ACTIVE",
              contact_sheet_asset_id: result.contact_sheet_asset_id,
              generation_source: result.generation_source,
              views: result.views,
            })
          }
        />
      ) : (
        <p className="status-note">
          审计身份只读，如需创建或改名请联系管理员。
        </p>
      )}
      {error ? (
        <div className="inline-error-actions">
          <p className="settings-error" role="alert">
            {error}
          </p>
          <button
            className="secondary-button"
            disabled={isLoading}
            onClick={() => void loadLibrary()}
            type="button"
          >
            重新读取人物库
          </button>
        </div>
      ) : null}
      {message ? <p className="setup-success">{message}</p> : null}
      {isLoading && !pendingCharacter ? (
        <p className="status-note">正在读取人物库…</p>
      ) : entries.length === 0 && !pendingCharacter ? (
        <p className="status-note">还没有人物，上传一张图片开始创建。</p>
      ) : (
        <ul className="character-preview-list">
          {pendingCharacter ? (
            <PendingCharacterCard
              character={pendingCharacter}
              onClear={clearPendingGeneration}
            />
          ) : null}
          {entries.map((entry) => {
            const isEditing = editingId === entry.identity_id;
            const isRenaming = busyRenameId === entry.identity_id;
            const cover = coverAsset(entry);
            const coverUrl = cover ? previewUrls[cover.assetId] : undefined;
            const coverStatus = cover
              ? (previewStatuses[cover.assetId] ?? "loading")
              : "error";
            return (
              <li className="character-preview-card" key={entry.identity_id}>
                <button
                  aria-label={`查看人物 ${entry.display_name} 大图`}
                  className="character-preview-card__cover"
                  disabled={coverStatus === "loading"}
                  onClick={() => {
                    if (cover && coverStatus === "error") {
                      void loadPreviewUrls([cover.assetId], true);
                      return;
                    }
                    setLightboxId(entry.identity_id);
                  }}
                  type="button"
                >
                  {coverUrl && cover && coverStatus === "ready" ? (
                    <img
                      alt={`${entry.display_name} ${cover.label}`}
                      loading="lazy"
                      onError={() => markPreviewError(cover.assetId)}
                      src={coverUrl}
                    />
                  ) : coverStatus === "error" ? (
                    <span className="source-frame-placeholder source-frame-placeholder--error">
                      <strong>预览加载失败</strong>
                      <small>点击重新加载</small>
                    </span>
                  ) : (
                    <span className="source-frame-placeholder">
                      预览加载中…
                    </span>
                  )}
                </button>
                <div className="character-preview-card__body">
                  {isEditing ? (
                    <div className="character-preview-card__edit">
                      <input
                        aria-label="修改人物名称"
                        onChange={(event) => setEditingName(event.target.value)}
                        type="text"
                        value={editingName}
                      />
                      <button
                        disabled={isRenaming}
                        onClick={() => void saveRename(entry)}
                        type="button"
                      >
                        {isRenaming ? "正在保存…" : "保存名称"}
                      </button>
                      <button
                        className="secondary-button"
                        disabled={isRenaming}
                        onClick={cancelRename}
                        type="button"
                      >
                        取消
                      </button>
                    </div>
                  ) : (
                    <>
                      <div className="character-preview-card__title">
                        <span className="character-preview-card__name">
                          {entry.display_name}
                        </span>
                        {entry.status === "ARCHIVED" ? (
                          <span className="status-badge">已归档</span>
                        ) : null}
                        {entry.generation_source === "local_placeholder" ? (
                          <span className="status-badge status-badge--warning">
                            本地占位结果
                          </span>
                        ) : null}
                      </div>
                      <div className="character-preview-card__actions">
                        {canRename(entry) ? (
                          <button
                            className="secondary-button"
                            onClick={() => startRename(entry)}
                            type="button"
                          >
                            改名
                          </button>
                        ) : null}
                        {canRename(entry) ? (
                          <button
                            aria-label={`重新生成人物 ${entry.display_name} 的多视图`}
                            className="secondary-button"
                            disabled={busyRegenerateId !== ""}
                            onClick={() => void handleRegenerate(entry)}
                            type="button"
                          >
                            {busyRegenerateId === entry.identity_id
                              ? "正在重新生成…"
                              : "重新生成多视图"}
                          </button>
                        ) : null}
                        {canDelete(entry) ? (
                          <button
                            aria-label={`删除人物 ${entry.display_name}`}
                            className="secondary-button"
                            disabled={busyDeleteId !== ""}
                            onClick={() => void handleDelete(entry)}
                            type="button"
                          >
                            {busyDeleteId === entry.identity_id
                              ? "正在删除…"
                              : "删除"}
                          </button>
                        ) : null}
                      </div>
                    </>
                  )}
                </div>
              </li>
            );
          })}
        </ul>
      )}
      {lightboxEntry ? (
        <CharacterLightbox
          busyDownloadKey={busyDownloadKey}
          canManage={canManage}
          entry={lightboxEntry}
          onClose={() => setLightboxId("")}
          onDownloadSheet={handleDownloadSheet}
          onDownloadView={handleDownloadView}
          onDownloadAll={handleDownloadAll}
          onLoadPreviewUrls={loadPreviewUrls}
          previewUrls={previewUrls}
        />
      ) : null}
    </section>
  );
}

export function PendingCharacterCard({
  character,
  onClear,
}: {
  character: PendingCharacterState;
  onClear: () => void;
}) {
  return (
    <li
      aria-label={`人物 ${character.displayName} 生成进度`}
      className="character-preview-card character-preview-card--generating"
    >
      <div className="character-preview-card__cover character-generation-card__cover">
        {character.sourcePreviewUrl ? (
          <img
            alt={`${character.displayName} 授权原图`}
            src={character.sourcePreviewUrl}
          />
        ) : (
          <span className="source-frame-placeholder">云端任务已恢复</span>
        )}
        <div className="character-generation-card__overlay">
          <strong>
            {character.status === "error"
              ? "生成未完成"
              : `${character.progress}%`}
          </strong>
          <span>{character.stage}</span>
        </div>
      </div>
      <div className="character-preview-card__body">
        <div className="character-preview-card__title">
          <span className="character-preview-card__name">
            {character.displayName}
          </span>
          <span className="status-badge">
            {character.status === "error" ? "失败" : "生成中"}
          </span>
        </div>
        <progress
          aria-label={`${character.displayName} 预计生成进度`}
          max={100}
          value={character.progress}
        />
        <p className="character-generation-card__note">
          {character.status === "error"
            ? "请检查提示后重新提交。"
            : "预计需要 1–3 分钟，可离开当前页面，任务会在后台继续。"}
        </p>
        {character.status === "error" ? (
          <button className="secondary-button" onClick={onClear} type="button">
            移除失败任务
          </button>
        ) : null}
      </div>
    </li>
  );
}

// 人物灯箱：点卡片封面放大查看——有拼合图看拼合全图，无则退回视角
// 网格；下载拼合图/单视角/全部集中在这里，卡片操作行只留管理动作。
// 关闭方式：Esc、点遮罩、右上「关闭」。
function CharacterLightbox({
  busyDownloadKey,
  canManage,
  entry,
  onClose,
  onDownloadSheet,
  onDownloadView,
  onDownloadAll,
  onLoadPreviewUrls,
  previewUrls,
}: {
  busyDownloadKey: string;
  canManage: boolean;
  entry: SimpleLibraryEntry;
  onClose: () => void;
  onDownloadSheet: (entry: SimpleLibraryEntry) => Promise<void>;
  onDownloadView: (
    entry: SimpleLibraryEntry,
    view: SimpleCharacterView,
  ) => Promise<void>;
  onDownloadAll: (entry: SimpleLibraryEntry) => Promise<void>;
  onLoadPreviewUrls: (assetIds: string[]) => Promise<void>;
  previewUrls: Record<string, string>;
}) {
  const [activeTab, setActiveTab] = useState<"base" | "scenes">("base");
  const [sceneLooks, setSceneLooks] = useState<SimpleSceneLook[]>([]);
  const [sceneError, setSceneError] = useState("");
  const [sceneLoading, setSceneLoading] = useState(true);
  const [sceneFormOpen, setSceneFormOpen] = useState(false);
  const [sceneName, setSceneName] = useState("");
  const [sceneDescription, setSceneDescription] = useState("");
  const [costumeDescription, setCostumeDescription] = useState("");
  const [sceneGenerating, setSceneGenerating] = useState(false);
  const [sceneTaskMessage, setSceneTaskMessage] = useState("");
  const [expandedSceneId, setExpandedSceneId] = useState("");

  useEffect(() => {
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        onClose();
      }
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [onClose]);

  useEffect(() => {
    let active = true;

    async function loadLooks(): Promise<void> {
      const looks = await listCharacterSceneLooks(entry.identity_id);
      if (!active) {
        return;
      }
      setSceneLooks(looks);
      setSceneError("");
      await onLoadPreviewUrls(looks.flatMap(sceneLookAssetIds));
    }

    void (async () => {
      setSceneLoading(true);
      try {
        await loadLooks();
        if (active) {
          setSceneLoading(false);
        }
        const task = await getLatestSceneLookTask(entry.identity_id);
        if (!active || !task) {
          return;
        }
        setActiveTab("scenes");
        if (
          task.status === "FAILED" ||
          task.status === "SUBMISSION_UNCERTAIN"
        ) {
          setSceneError(task.error_message ?? "场景造型生成失败，请重新提交。");
          return;
        }
        if (task.status === "PENDING" || task.status === "RUNNING") {
          setSceneGenerating(true);
          setSceneTaskMessage("正在恢复场景造型生成任务…");
          await waitForCharacterSheetTask(task.id);
        }
        if (active) {
          await loadLooks();
        }
      } catch (loadError) {
        if (active) {
          setSceneError(
            errorMessage(loadError, "场景造型暂不可用，请稍后重试。"),
          );
        }
      } finally {
        if (active) {
          setSceneLoading(false);
          setSceneGenerating(false);
          setSceneTaskMessage("");
        }
      }
    })();
    return () => {
      active = false;
    };
  }, [entry.identity_id, onLoadPreviewUrls]);

  async function submitSceneLook(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (sceneGenerating) {
      return;
    }
    const input = {
      scene_name: sceneName.trim(),
      scene_description: sceneDescription.trim(),
      costume_description: costumeDescription.trim(),
    };
    if (
      !input.scene_name ||
      !input.scene_description ||
      !input.costume_description
    ) {
      setSceneError("请完整填写场景名称、场景描述和服装描述。");
      return;
    }
    setSceneGenerating(true);
    setSceneError("");
    try {
      const created = await createCharacterSceneLook(entry.identity_id, input);
      setSceneLooks((current) => [
        created,
        ...current.filter((look) => look.persona_id !== created.persona_id),
      ]);
      await onLoadPreviewUrls(sceneLookAssetIds(created));
      setSceneName("");
      setSceneDescription("");
      setCostumeDescription("");
      setSceneFormOpen(false);
    } catch (createError) {
      setSceneError(errorMessage(createError, "场景造型生成失败，请重试。"));
    } finally {
      setSceneGenerating(false);
    }
  }

  const sheetUrl = entry.contact_sheet_asset_id
    ? previewUrls[entry.contact_sheet_asset_id]
    : undefined;
  const isDownloadingAll = busyDownloadKey === entry.identity_id;

  return (
    // biome-ignore lint/a11y/noStaticElementInteractions: 点遮罩关闭是鼠标增强；键盘用户由下方 Esc 监听兜底。
    // biome-ignore lint/a11y/useKeyWithClickEvents: 键盘关闭走全局 Esc 监听（见上方 useEffect）。
    <div
      className="character-lightbox"
      onClick={(event) => {
        if (event.target === event.currentTarget) {
          onClose();
        }
      }}
    >
      <div
        aria-label={`人物预览 ${entry.display_name}`}
        aria-modal="true"
        className="character-lightbox__dialog"
        role="dialog"
      >
        <div className="character-lightbox__head">
          <div>
            <span className="character-detail__eyebrow">人物形象</span>
            <h3 className="character-lightbox__title">{entry.display_name}</h3>
            <p>人物身份保持不变，不同造型只调整场景、服装与视觉氛围。</p>
          </div>
          <button
            aria-label="关闭人物预览"
            className="secondary-button"
            onClick={onClose}
            type="button"
          >
            关闭
          </button>
        </div>
        <div
          aria-label="人物形象类型"
          className="character-detail__tabs"
          role="tablist"
        >
          <button
            aria-selected={activeTab === "base"}
            className={activeTab === "base" ? "is-active" : undefined}
            onClick={() => setActiveTab("base")}
            role="tab"
            type="button"
          >
            人物基准
          </button>
          <button
            aria-selected={activeTab === "scenes"}
            className={activeTab === "scenes" ? "is-active" : undefined}
            onClick={() => setActiveTab("scenes")}
            role="tab"
            type="button"
          >
            场景造型
          </button>
        </div>
        {activeTab === "scenes" ? (
          <section className="character-scene-panel" role="tabpanel">
            <div className="character-scene-panel__head">
              <div>
                <h4>场景造型</h4>
                <p>基于人物基准生成特定场景下的服装与五视图形象。</p>
              </div>
              {canManage ? (
                <button
                  className="primary-button"
                  onClick={() => setSceneFormOpen((open) => !open)}
                  type="button"
                >
                  {sceneFormOpen ? "收起" : "新增场景造型"}
                </button>
              ) : null}
            </div>
            {sceneTaskMessage ? (
              <p className="status-note">{sceneTaskMessage}</p>
            ) : null}
            {sceneFormOpen ? (
              <form className="character-scene-form" onSubmit={submitSceneLook}>
                <label>
                  场景名称
                  <input
                    maxLength={80}
                    placeholder="例如：工地巡检"
                    value={sceneName}
                    onChange={(event) => setSceneName(event.target.value)}
                  />
                </label>
                <label>
                  场景描述
                  <textarea
                    maxLength={600}
                    placeholder="例如：乡村别墅施工现场，白天自然光"
                    value={sceneDescription}
                    onChange={(event) =>
                      setSceneDescription(event.target.value)
                    }
                  />
                </label>
                <label>
                  服装描述
                  <textarea
                    maxLength={600}
                    placeholder="例如：黄色安全帽、深蓝色工装和反光背心"
                    value={costumeDescription}
                    onChange={(event) =>
                      setCostumeDescription(event.target.value)
                    }
                  />
                </label>
                <p>提交后直接生成并发布五视图，无需管理员审核。</p>
                <button
                  className="primary-button"
                  disabled={sceneGenerating}
                  type="submit"
                >
                  {sceneGenerating
                    ? "正在生成，预计 1–3 分钟…"
                    : "生成场景五视图"}
                </button>
              </form>
            ) : null}
            {sceneError ? (
              <p className="settings-error" role="alert">
                {sceneError}
              </p>
            ) : null}
            {sceneLoading ? (
              <p className="status-note">正在读取场景造型…</p>
            ) : sceneLooks.length ? (
              <div className="character-scene-grid">
                {sceneLooks.map((look) => {
                  const preview =
                    look.views.find(
                      (view) => view.view_type === "FRONT_FACE",
                    ) ?? look.views[0];
                  return (
                    <article
                      className="character-scene-card"
                      key={look.persona_id}
                    >
                      <div className="character-scene-card__image">
                        {preview && previewUrls[preview.asset_id] ? (
                          <img
                            alt={`${entry.display_name} ${look.scene_name}`}
                            src={previewUrls[preview.asset_id]}
                          />
                        ) : (
                          <span className="source-frame-placeholder">
                            场景预览加载中…
                          </span>
                        )}
                      </div>
                      <div className="character-scene-card__body">
                        <strong>{look.scene_name}</strong>
                        <span>{look.scene_description}</span>
                        <small>{look.costume_description}</small>
                        <em>五视图已发布</em>
                        <button
                          aria-label={`查看${look.scene_name}五视图`}
                          className="secondary-button"
                          onClick={() =>
                            setExpandedSceneId((current) =>
                              current === look.persona_id
                                ? ""
                                : look.persona_id,
                            )
                          }
                          type="button"
                        >
                          {expandedSceneId === look.persona_id
                            ? "收起五视图"
                            : "查看五视图"}
                        </button>
                      </div>
                    </article>
                  );
                })}
              </div>
            ) : (
              <p className="character-scene-empty">
                还没有场景造型，可从人物基准创建第一套。
              </p>
            )}
            {sceneLooks
              .filter((look) => look.persona_id === expandedSceneId)
              .map((look) => (
                <section
                  aria-label={`${look.scene_name}五视图`}
                  className="character-scene-views"
                  key={look.persona_id}
                >
                  <div className="character-scene-views__head">
                    <strong>{look.scene_name} · 场景五视图</strong>
                    <button
                      className="secondary-button"
                      onClick={() => setExpandedSceneId("")}
                      type="button"
                    >
                      收起
                    </button>
                  </div>
                  <div className="character-contact-sheet">
                    {previewUrls[look.contact_sheet_asset_id] ? (
                      <img
                        alt={`${entry.display_name} ${look.scene_name} 场景五视图`}
                        src={previewUrls[look.contact_sheet_asset_id]}
                      />
                    ) : (
                      <span className="source-frame-placeholder">
                        五视图加载中…
                      </span>
                    )}
                  </div>
                </section>
              ))}
          </section>
        ) : entry.contact_sheet_asset_id ? (
          <div className="character-contact-sheet">
            {sheetUrl ? (
              <img alt={`${entry.display_name} 五视角拼合图`} src={sheetUrl} />
            ) : (
              <span className="source-frame-placeholder">拼合图加载中…</span>
            )}
            {canManage ? (
              <div className="character-contact-sheet__bar">
                <span className="character-contact-sheet__label">
                  五视角拼合图
                </span>
                <button
                  className="secondary-button"
                  disabled={Boolean(busyDownloadKey) || !sheetUrl}
                  onClick={() => void onDownloadSheet(entry)}
                  type="button"
                >
                  {busyDownloadKey === entry.contact_sheet_asset_id
                    ? "下载中…"
                    : "下载拼合图"}
                </button>
              </div>
            ) : null}
          </div>
        ) : entry.views.length ? (
          <div className="character-lightbox__views">
            <div className="character-contact-sheet__bar">
              <span className="character-contact-sheet__label">
                视角图（{entry.views.length} 张）
              </span>
              {canManage ? (
                <button
                  className="secondary-button"
                  disabled={Boolean(busyDownloadKey)}
                  onClick={() => void onDownloadAll(entry)}
                  type="button"
                >
                  {isDownloadingAll
                    ? "正在下载全部…"
                    : `下载全部（${entry.views.length} 张）`}
                </button>
              ) : null}
            </div>
            <div className="character-preview-grid">
              {entry.views.map((view) => {
                const previewUrl = previewUrls[view.asset_id];
                const isDownloadingView = busyDownloadKey === view.asset_id;
                return (
                  <figure
                    className="character-preview-item"
                    key={view.asset_id}
                  >
                    {previewUrl ? (
                      <img
                        alt={`${entry.display_name} ${VIEW_LABELS[view.view_type]}`}
                        src={previewUrl}
                      />
                    ) : (
                      <span className="source-frame-placeholder">
                        预览加载中…
                      </span>
                    )}
                    <figcaption>
                      <span>{VIEW_LABELS[view.view_type]}</span>
                      {canManage ? (
                        <button
                          className="secondary-button"
                          disabled={Boolean(busyDownloadKey) || !previewUrl}
                          onClick={() => void onDownloadView(entry, view)}
                          type="button"
                        >
                          {isDownloadingView ? "下载中…" : "下载"}
                        </button>
                      ) : null}
                    </figcaption>
                  </figure>
                );
              })}
            </div>
          </div>
        ) : (
          <p className="status-note">该人物暂无已发布的视角图。</p>
        )}
      </div>
    </div>
  );
}
