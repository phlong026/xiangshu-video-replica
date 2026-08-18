import { useCallback, useEffect, useState } from "react";

import {
  type CharacterViewType,
  deleteSimpleCharacterIdentity,
  downloadCharacterAsset,
  getAssetDownloadUrl,
  listSimpleCharacterLibrary,
  regenerateContactSheet,
  renamePersonIdentity,
  type SimpleCharacterView,
  type SimpleLibraryEntry,
  type UserRole,
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

// 人物库 = 「上传 → 五视角拼合图预览 → 下载」的主动线：上方一键上传，
// 下方按人物展示单张五视角拼合大图；旧人物（无拼合图）降级为七格预览。
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
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [busyDownloadKey, setBusyDownloadKey] = useState("");
  const [editingId, setEditingId] = useState("");
  const [editingName, setEditingName] = useState("");
  const [busyRenameId, setBusyRenameId] = useState("");
  const [busyDeleteId, setBusyDeleteId] = useState("");
  const [busyRegenerateId, setBusyRegenerateId] = useState("");

  const loadPreviewUrls = useCallback(async (assetIds: string[]) => {
    const results = await Promise.allSettled(
      assetIds.map(async (assetId) => {
        const download = await getAssetDownloadUrl(assetId);
        return [assetId, download.url] as const;
      }),
    );
    setPreviewUrls((current) => ({
      ...current,
      ...Object.fromEntries(
        results.flatMap((result) =>
          result.status === "fulfilled" ? [result.value] : [],
        ),
      ),
    }));
  }, []);

  const loadLibrary = useCallback(async () => {
    setIsLoading(true);
    try {
      const result = await listSimpleCharacterLibrary();
      setEntries(result);
      setError("");
      void loadPreviewUrls(result.flatMap(entryAssetIds));
    } catch (loadError) {
      setError(errorMessage(loadError, "人物库暂不可用，请重试。"));
    } finally {
      setIsLoading(false);
    }
  }, [loadPreviewUrls]);

  useEffect(() => {
    void loadLibrary();
  }, [loadLibrary]);

  // 上传成功后立刻把新人物置顶展示，无需等待整表刷新。
  function handleCreated(newEntry: SimpleLibraryEntry) {
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

  return (
    <section aria-label="人物库" className="character-library-simple">
      <div className="character-library-simple__head">
        <h2>人物库</h2>
        <p className="status-note">
          上传一张图片即可一键生成单张五视角拼合图；生成后可预览、下载，并在项目中选用。
        </p>
      </div>
      {canManage ? (
        <SimpleCharacterUpload
          onCreated={(result, displayName) =>
            handleCreated({
              identity_id: result.identity_id,
              display_name: displayName,
              owner_user_id: userId,
              status: "ACTIVE",
              contact_sheet_asset_id: result.contact_sheet_asset_id,
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
        <p className="settings-error" role="alert">
          {error}
        </p>
      ) : null}
      {message ? <p className="setup-success">{message}</p> : null}
      {isLoading ? (
        <p className="status-note">正在读取人物库…</p>
      ) : entries.length === 0 ? (
        <p className="status-note">还没有人物，上传一张图片开始创建。</p>
      ) : (
        <ul className="character-preview-list">
          {entries.map((entry) => {
            const isEditing = editingId === entry.identity_id;
            const isRenaming = busyRenameId === entry.identity_id;
            const isDownloadingAll = busyDownloadKey === entry.identity_id;
            return (
              <li className="character-preview-card" key={entry.identity_id}>
                <div className="character-preview-card__head">
                  {isEditing ? (
                    <>
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
                    </>
                  ) : (
                    <>
                      <span className="character-preview-card__name">
                        {entry.display_name}
                      </span>
                      {entry.status === "ARCHIVED" ? (
                        <span className="status-badge">已归档</span>
                      ) : null}
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
                      {canManage &&
                      !entry.contact_sheet_asset_id &&
                      entry.views.length ? (
                        <button
                          className="secondary-button"
                          disabled={Boolean(busyDownloadKey)}
                          onClick={() => void handleDownloadAll(entry)}
                          type="button"
                        >
                          {isDownloadingAll
                            ? "正在下载全部…"
                            : `下载全部（${entry.views.length} 张）`}
                        </button>
                      ) : null}
                    </>
                  )}
                </div>
                {entry.contact_sheet_asset_id ? (
                  <div className="character-contact-sheet">
                    {previewUrls[entry.contact_sheet_asset_id] ? (
                      <img
                        alt={`${entry.display_name} 五视角拼合图`}
                        loading="lazy"
                        src={previewUrls[entry.contact_sheet_asset_id]}
                      />
                    ) : (
                      <span className="source-frame-placeholder">
                        拼合图加载中…
                      </span>
                    )}
                    <div className="character-contact-sheet__bar">
                      <span className="character-contact-sheet__label">
                        五视角拼合图
                      </span>
                      {canManage ? (
                        <button
                          className="secondary-button"
                          disabled={
                            Boolean(busyDownloadKey) ||
                            !previewUrls[entry.contact_sheet_asset_id]
                          }
                          onClick={() => void handleDownloadSheet(entry)}
                          type="button"
                        >
                          {busyDownloadKey === entry.contact_sheet_asset_id
                            ? "下载中…"
                            : "下载拼合图"}
                        </button>
                      ) : null}
                    </div>
                  </div>
                ) : entry.views.length ? (
                  <div className="character-preview-grid">
                    {entry.views.map((view) => {
                      const previewUrl = previewUrls[view.asset_id];
                      const isDownloadingView =
                        busyDownloadKey === view.asset_id;
                      return (
                        <figure
                          className="character-preview-item"
                          key={view.asset_id}
                        >
                          {previewUrl ? (
                            <img
                              alt={`${entry.display_name} ${VIEW_LABELS[view.view_type]}`}
                              loading="lazy"
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
                                disabled={
                                  Boolean(busyDownloadKey) || !previewUrl
                                }
                                onClick={() =>
                                  void handleDownloadView(entry, view)
                                }
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
                ) : (
                  <p className="status-note">该人物暂无已发布的视角图。</p>
                )}
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}
