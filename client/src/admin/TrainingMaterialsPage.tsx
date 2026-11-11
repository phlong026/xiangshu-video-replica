import { useCallback, useEffect, useRef, useState } from "react";
import {
  deleteTrainingMaterial,
  fetchTrainingMaterialDirectLink,
  listTrainingMaterials,
  type TrainingMaterialItem,
  type TrainingMaterialKind,
  updateTrainingMaterial,
  uploadTrainingMaterial,
} from "../api.admin";
import { ConfirmDialog } from "./ui/ConfirmDialog";
import { DataTable } from "./ui/DataTable";
import { ListPage } from "./ui/ListPage";
import { Pagination } from "./ui/Pagination";
import { StatusBadge } from "./ui/StatusBadge";
import { formatDateTime } from "./ui/vocabulary";

/**
 * IA 改版第 5 项：平台培训素材库的管理面（2026-11-12 接入网盘存储）。
 *
 * 工作台素材库已整体替换为本库的只读视图（产品拍板彻底替换，2026-11-01），
 * 这里是唯一上传口：视频 / 音频 / 图片 / 文档四组白名单格式，标签必填
 * （1-5 个，驱动客户侧的分类筛选），支持格式分组与标签筛选，可复制分享用
 * 直链（30 分钟时效）。kind 由文件类型推导，上传侧不提供手填，避免栏目错位。
 */
const PAGE_SIZE = 20;
const MAX_UPLOAD_BYTES = 200 * 1024 * 1024;

const KIND_TABS: Array<{ id: "" | TrainingMaterialKind; label: string }> = [
  { id: "", label: "全部" },
  { id: "video", label: "视频" },
  { id: "audio", label: "音频" },
  { id: "image", label: "图片" },
  { id: "doc", label: "文档" },
];

/** 后端白名单的 (后缀, content-type) 对；与 TRAINING_UPLOADS 一一对应。 */
const ALLOWED_UPLOAD_TYPES = new Set([
  "video/mp4",
  "video/quicktime",
  "video/webm",
  "audio/mpeg",
  "audio/mp4",
  "audio/wav",
  "image/jpeg",
  "image/png",
  "image/gif",
  "image/webp",
  "application/pdf",
  "application/msword",
  "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
  "application/vnd.ms-excel",
  "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
  "application/vnd.ms-powerpoint",
  "application/vnd.openxmlformats-officedocument.presentationml.presentation",
]);
const ALLOWED_SUFFIXES = new Set([
  ".mp4",
  ".mov",
  ".webm",
  ".mp3",
  ".m4a",
  ".wav",
  ".jpg",
  ".jpeg",
  ".png",
  ".gif",
  ".webp",
  ".pdf",
  ".docx",
  ".doc",
  ".xlsx",
  ".xls",
  ".pptx",
  ".ppt",
]);

function formatSizeMb(sizeBytes: number): string {
  const mb = sizeBytes / (1024 * 1024);
  return mb >= 1
    ? `${mb.toFixed(2)} MB`
    : `${(sizeBytes / 1024).toFixed(1)} KB`;
}

function fileKindLabel(file: File): string {
  const type = file.type;
  if (type.startsWith("video/")) return "视频";
  if (type.startsWith("audio/")) return "音频";
  if (type.startsWith("image/")) return "图片";
  return "文档";
}

function uploadFileError(file: File): string {
  const suffix = file.name.slice(file.name.lastIndexOf(".")).toLowerCase();
  if (!ALLOWED_SUFFIXES.has(suffix) || !ALLOWED_UPLOAD_TYPES.has(file.type)) {
    return "仅支持视频（mp4/mov/webm）、音频（mp3/m4a/wav）、图片（jpg/png/gif/webp）与文档（pdf/docx/doc/xlsx/xls/pptx/ppt），后缀与类型需匹配。";
  }
  if (file.size > MAX_UPLOAD_BYTES) {
    return "文件超过 200MB 上限。";
  }
  return "";
}

/** 表单输入的标签串 → 数组：逗号/顿号/换行分隔，strip、去空、保序去重。 */
function parseTagInput(raw: string): string[] {
  const seen: string[] = [];
  for (const piece of raw.split(/[,，、\n]/)) {
    const tag = piece.trim().replace(/\s+/g, " ");
    if (tag && !seen.includes(tag)) seen.push(tag);
  }
  return seen;
}

type UploadDraft = {
  file: File | null;
  title: string;
  description: string;
  tagsInput: string;
  sortOrder: number;
  visible: boolean;
};

type EditDraft = {
  item: TrainingMaterialItem;
  title: string;
  description: string;
  tagsInput: string;
  sortOrder: number;
};

const EMPTY_UPLOAD: UploadDraft = {
  file: null,
  title: "",
  description: "",
  tagsInput: "",
  sortOrder: 0,
  visible: true,
};

export function TrainingMaterialsPage({
  readOnly = false,
}: {
  readOnly?: boolean;
}) {
  const [items, setItems] = useState<TrainingMaterialItem[]>([]);
  const [tagFacet, setTagFacet] = useState<string[]>([]);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [kindTab, setKindTab] = useState<"" | TrainingMaterialKind>("");
  const [tagFilter, setTagFilter] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [uploadOpen, setUploadOpen] = useState(false);
  const [uploadDraft, setUploadDraft] = useState<UploadDraft>(EMPTY_UPLOAD);
  const [uploadPercent, setUploadPercent] = useState(-1);
  const [uploadKey, setUploadKey] = useState("");
  const [editing, setEditing] = useState<EditDraft | null>(null);
  const [editKey, setEditKey] = useState("");
  const [pendingDelete, setPendingDelete] =
    useState<TrainingMaterialItem | null>(null);
  const [saving, setSaving] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);

  const load = useCallback(async () => {
    try {
      setLoading(true);
      setError("");
      const response = await listTrainingMaterials({
        limit: PAGE_SIZE,
        offset,
        kind: kindTab || undefined,
        tag: tagFilter || undefined,
      });
      setItems(response.items);
      setTagFacet(response.tag_facet ?? []);
      setTotal(response.total ?? response.items.length);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "读取培训素材失败");
    } finally {
      setLoading(false);
    }
  }, [offset, kindTab, tagFilter]);

  useEffect(() => {
    void load();
  }, [load]);

  function openUpload() {
    setUploadDraft(EMPTY_UPLOAD);
    setUploadPercent(-1);
    // 每次打开都换幂等键：上传失败后的「重试」必须沿用同一键才能安全去重，
    // 而重新开始的上传（重新选文件/改标题）用新键。
    setUploadKey(`tm-${Date.now()}-${Math.random().toString(36).slice(2)}`);
    setUploadOpen(true);
  }

  async function submitUpload(reason: string) {
    const file = uploadDraft.file;
    if (!file) {
      setError("请先选择文件。");
      return;
    }
    const fileProblem = uploadFileError(file);
    if (fileProblem) {
      setError(fileProblem);
      return;
    }
    const tags = parseTagInput(uploadDraft.tagsInput);
    if (tags.length < 1) {
      setError("请至少填写 1 个标签（逗号分隔，用于客户侧分类筛选）。");
      return;
    }
    setSaving(true);
    setError("");
    setUploadPercent(0);
    try {
      await uploadTrainingMaterial({
        file,
        title: uploadDraft.title.trim(),
        description: uploadDraft.description.trim(),
        tags,
        sortOrder: uploadDraft.sortOrder,
        visible: uploadDraft.visible,
        reason,
        idempotencyKey: uploadKey,
        onProgress: setUploadPercent,
      });
      setNotice(`「${uploadDraft.title.trim() || file.name}」已上传。`);
      setUploadOpen(false);
      setUploadDraft(EMPTY_UPLOAD);
      if (fileInputRef.current) fileInputRef.current.value = "";
      await load();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "上传培训素材失败");
    } finally {
      setSaving(false);
      setUploadPercent(-1);
    }
  }

  function openEdit(item: TrainingMaterialItem) {
    setEditing({
      item,
      title: item.title,
      description: item.description,
      tagsInput: item.tags.join("、"),
      sortOrder: item.sort_order,
    });
    setEditKey(`tm-${Date.now()}-${Math.random().toString(36).slice(2)}`);
  }

  async function submitEdit(reason: string) {
    if (!editing) return;
    const tags = parseTagInput(editing.tagsInput);
    if (tags.length < 1) {
      setError("请至少保留 1 个标签。");
      return;
    }
    setSaving(true);
    setError("");
    try {
      await updateTrainingMaterial({
        id: editing.item.id,
        title: editing.title.trim(),
        description: editing.description.trim(),
        tags,
        sortOrder: editing.sortOrder,
        reason,
        idempotencyKey: editKey,
      });
      setNotice("培训素材已更新。");
      setEditing(null);
      await load();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "修改培训素材失败");
    } finally {
      setSaving(false);
    }
  }

  async function toggleVisible(item: TrainingMaterialItem, reason: string) {
    setSaving(true);
    setError("");
    try {
      await updateTrainingMaterial({
        id: item.id,
        visible: !item.visible,
        reason,
        idempotencyKey: `tm-${Date.now()}-${Math.random().toString(36).slice(2)}`,
      });
      setNotice(
        item.visible
          ? `「${item.title}」已下架。`
          : `「${item.title}」已上架。`,
      );
      await load();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "修改培训素材失败");
    } finally {
      setSaving(false);
    }
  }

  async function copyDirectLink(item: TrainingMaterialItem) {
    setError("");
    try {
      const link = await fetchTrainingMaterialDirectLink(item.id);
      const minutes = Math.max(
        1,
        Math.round((Number(link.expires_at) - Date.now() / 1000) / 60),
      );
      try {
        await navigator.clipboard.writeText(link.url);
        setNotice(`「${item.title}」直链已复制，约 ${minutes} 分钟后失效。`);
      } catch {
        // 剪贴板不可用（桌面壳限制等）时展示链接让运营手动复制。
        setNotice(`直链（约 ${minutes} 分钟后失效）：${link.url}`);
      }
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "获取直链失败");
    }
  }

  async function confirmDelete(reason: string) {
    if (!pendingDelete) return;
    setSaving(true);
    setError("");
    try {
      await deleteTrainingMaterial({
        id: pendingDelete.id,
        reason,
        idempotencyKey: `tm-${Date.now()}-${Math.random().toString(36).slice(2)}`,
      });
      setNotice(`「${pendingDelete.title}」已删除。`);
      setPendingDelete(null);
      await load();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "删除培训素材失败");
    } finally {
      setSaving(false);
    }
  }

  return (
    <>
      <ListPage
        ariaLabel="培训素材库"
        className="admin-training-materials-page"
        error={error}
        notice={notice}
        loading={loading}
        hasItems={items.length > 0}
        loadingText="正在读取培训素材…"
        emptyText="还没有培训素材；点「上传素材」发布第一条。"
        actions={
          <>
            <header>
              <p className="admin-hint">
                工作台「素材库」页面展示本库内容：这里统一上传培训视频、音频、
                图片与文档，客户只能浏览与下载。数字越大排在越前（同序按新→旧）；
                标签必填，客户按标签筛选内容。
              </p>
              <div role="tablist" aria-label="格式分组">
                {KIND_TABS.map((entry) => (
                  <button
                    key={entry.id || "all"}
                    type="button"
                    role="tab"
                    aria-selected={kindTab === entry.id}
                    onClick={() => {
                      setKindTab(entry.id);
                      setOffset(0);
                    }}
                  >
                    {entry.label}
                  </button>
                ))}
              </div>
              {tagFacet.length > 0 ? (
                <div aria-label="按标签筛选">
                  {tagFacet.map((tag) => (
                    <button
                      key={tag}
                      type="button"
                      aria-pressed={tagFilter === tag}
                      onClick={() => {
                        setTagFilter(tagFilter === tag ? "" : tag);
                        setOffset(0);
                      }}
                    >
                      #{tag}
                    </button>
                  ))}
                </div>
              ) : null}
            </header>
            {!readOnly && !uploadOpen && editing === null ? (
              <button type="button" onClick={openUpload}>
                上传素材
              </button>
            ) : null}
          </>
        }
        pagination={
          <Pagination
            disabled={loading}
            limit={PAGE_SIZE}
            offset={offset}
            total={total}
            onPageChange={setOffset}
          />
        }
      >
        <DataTable
          ariaLabel="培训素材清单"
          headers={
            <>
              <th>标题 / 说明</th>
              <th>类型</th>
              <th>大小</th>
              <th>标签</th>
              <th>排序 / 状态</th>
              <th>下载次数</th>
              <th>上传时间 / 人</th>
              <th>操作</th>
            </>
          }
        >
          {items.map((item) => (
            <tr key={item.id}>
              <td>
                <strong>{item.title}</strong>
                {item.description ? (
                  <p className="admin-hint">{item.description.slice(0, 80)}</p>
                ) : null}
              </td>
              <td>
                {item.kind_label}
                <small>{item.file_suffix}</small>
              </td>
              <td>{formatSizeMb(item.size_bytes)}</td>
              <td>{item.tags.map((tag) => `#${tag}`).join(" ")}</td>
              <td>
                <strong>{item.sort_order}</strong>
                <br />
                <StatusBadge tone={item.visible ? "good" : "neutral"}>
                  {item.visible ? "上架" : "已下架"}
                </StatusBadge>
              </td>
              <td>{item.download_count}</td>
              <td>
                {formatDateTime(item.created_at)} · {item.created_by}
              </td>
              <td>
                {readOnly ? null : (
                  <>
                    <button
                      type="button"
                      disabled={saving}
                      onClick={() => void copyDirectLink(item)}
                    >
                      复制直链
                    </button>{" "}
                    <button
                      type="button"
                      disabled={saving}
                      onClick={() => openEdit(item)}
                    >
                      编辑
                    </button>{" "}
                    <button
                      type="button"
                      disabled={saving}
                      onClick={() =>
                        void toggleVisible(
                          item,
                          item.visible ? "临时下架" : "重新上架",
                        )
                      }
                    >
                      {item.visible ? "下架" : "上架"}
                    </button>{" "}
                    <button
                      type="button"
                      disabled={saving}
                      onClick={() => setPendingDelete(item)}
                    >
                      删除
                    </button>
                  </>
                )}
              </td>
            </tr>
          ))}
        </DataTable>
      </ListPage>
      <ConfirmDialog
        open={uploadOpen}
        title="上传培训素材"
        confirmLabel="确认上传"
        description="上传后立即对客户可见（可先取消勾选「立即上架」转为草稿）。
          重试沿用同一幂等键，不会重复入库。"
        busy={saving}
        error={error}
        level="reason"
        onClose={() => !saving && setUploadOpen(false)}
        onConfirm={(reason) => void submitUpload(reason)}
      >
        <label>
          文件（视频/音频/图片/文档，≤200MB）
          <input
            ref={fileInputRef}
            type="file"
            accept=".mp4,.mov,.webm,.mp3,.m4a,.wav,.jpg,.jpeg,.png,.gif,.webp,.pdf,.docx,.doc,.xlsx,.xls,.pptx,.ppt"
            disabled={saving}
            onChange={(event) => {
              const file = event.target.files?.[0] ?? null;
              if (file && !uploadDraft.title.trim()) {
                setUploadDraft((draft) => ({
                  ...draft,
                  file,
                  title: draft.title.trim() || file.name.replace(/\.[^.]+$/, ""),
                }));
                return;
              }
              setUploadDraft({ ...uploadDraft, file });
            }}
          />
          {uploadDraft.file ? (
            <p className="admin-hint">
              {uploadDraft.file.name} · {fileKindLabel(uploadDraft.file)} ·{" "}
              {uploadFileError(uploadDraft.file) || "文件校验通过"}
            </p>
          ) : null}
        </label>
        <label>
          标题（≤120 字）
          <input
            value={uploadDraft.title}
            onChange={(event) =>
              setUploadDraft({ ...uploadDraft, title: event.target.value })
            }
            maxLength={120}
            placeholder="如：三步完成爆款复刻"
          />
        </label>
        <label>
          标签（必填 1-5 个，逗号/顿号分隔；客户按标签筛选）
          <input
            value={uploadDraft.tagsInput}
            onChange={(event) =>
              setUploadDraft({ ...uploadDraft, tagsInput: event.target.value })
            }
            list="training-tag-options"
            placeholder="如：入门教程,剪映技巧"
          />
          <datalist id="training-tag-options">
            {tagFacet.map((tag) => (
              <option key={tag} value={tag} />
            ))}
          </datalist>
        </label>
        <label>
          说明（≤1000 字，客户在素材卡片上可见）
          <textarea
            value={uploadDraft.description}
            onChange={(event) =>
              setUploadDraft({
                ...uploadDraft,
                description: event.target.value,
              })
            }
            rows={3}
            maxLength={1000}
          />
        </label>
        <label>
          排序权重（数字越大越靠前）
          <input
            type="number"
            value={uploadDraft.sortOrder}
            onChange={(event) =>
              setUploadDraft({
                ...uploadDraft,
                sortOrder: Number(event.target.value) || 0,
              })
            }
          />
        </label>
        <label className="admin-dialog__ack">
          <input
            type="checkbox"
            checked={uploadDraft.visible}
            onChange={(event) =>
              setUploadDraft({ ...uploadDraft, visible: event.target.checked })
            }
          />
          立即上架（客户可见）
        </label>
        {uploadPercent >= 0 ? (
          <progress
            aria-label="上传进度"
            max={100}
            value={uploadPercent}
            style={{ width: "100%" }}
          />
        ) : null}
      </ConfirmDialog>
      {editing !== null ? (
        <ConfirmDialog
          open
          title="编辑培训素材"
          confirmLabel="保存修改"
          description="修改标题、说明、标签与排序；上下架请用列表上的开关。"
          busy={saving}
          error={error}
          level="reason"
          onClose={() => !saving && setEditing(null)}
          onConfirm={(reason) => void submitEdit(reason)}
        >
          <label>
            标题
            <input
              value={editing.title}
              onChange={(event) =>
                setEditing({ ...editing, title: event.target.value })
              }
              maxLength={120}
            />
          </label>
          <label>
            标签（必填 1-5 个，逗号/顿号分隔）
            <input
              value={editing.tagsInput}
              onChange={(event) =>
                setEditing({ ...editing, tagsInput: event.target.value })
              }
              list="training-tag-options"
            />
          </label>
          <label>
            说明
            <textarea
              value={editing.description}
              onChange={(event) =>
                setEditing({ ...editing, description: event.target.value })
              }
              rows={3}
              maxLength={1000}
            />
          </label>
          <label>
            排序权重
            <input
              type="number"
              value={editing.sortOrder}
              onChange={(event) =>
                setEditing({
                  ...editing,
                  sortOrder: Number(event.target.value) || 0,
                })
              }
            />
          </label>
        </ConfirmDialog>
      ) : null}
      <ConfirmDialog
        open={pendingDelete !== null}
        title="删除培训素材"
        confirmLabel="确认删除"
        description={
          pendingDelete
            ? `「${pendingDelete.title}」将立即对客户不可见且无法再签发链接；文件保留在存储中，误删可联系管理员恢复。`
            : ""
        }
        busy={saving}
        error={error}
        level="reason"
        onClose={() => !saving && setPendingDelete(null)}
        onConfirm={(reason) => void confirmDelete(reason)}
      />
    </>
  );
}
