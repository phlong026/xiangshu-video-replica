import { useCallback, useEffect, useRef, useState } from "react";
import {
  deleteTrainingMaterial,
  listTrainingMaterials,
  type TrainingMaterialItem,
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
 * IA 改版第 5 项：平台培训素材库的管理面。
 *
 * 工作台素材库已整体替换为本库的只读视图（产品拍板彻底替换，2026-11-01），
 * 这里是唯一上传口：视频（mp4/mov）与培训文档（pdf/docx/pptx/xlsx）。
 * kind 由文件类型推导，上传侧不提供手填，避免栏目错位。
 */
const PAGE_SIZE = 20;
const MAX_UPLOAD_BYTES = 200 * 1024 * 1024;

/** 后端白名单的 (后缀, content-type) 对；与 TRAINING_UPLOADS 一一对应。 */
const ALLOWED_UPLOAD_TYPES = new Set([
  "video/mp4",
  "video/quicktime",
  "application/pdf",
  "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
  "application/vnd.openxmlformats-officedocument.presentationml.presentation",
  "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
]);
const ALLOWED_SUFFIXES = new Set([
  ".mp4",
  ".mov",
  ".pdf",
  ".docx",
  ".pptx",
  ".xlsx",
]);

function formatSizeMb(sizeBytes: number): string {
  const mb = sizeBytes / (1024 * 1024);
  return mb >= 1
    ? `${mb.toFixed(2)} MB`
    : `${(sizeBytes / 1024).toFixed(1)} KB`;
}

function fileKindLabel(file: File): string {
  return file.type.startsWith("video/") ? "视频" : "培训文档";
}

function uploadFileError(file: File): string {
  const suffix = file.name.slice(file.name.lastIndexOf(".")).toLowerCase();
  if (!ALLOWED_SUFFIXES.has(suffix) || !ALLOWED_UPLOAD_TYPES.has(file.type)) {
    return "仅支持 mp4/mov 视频与 pdf/docx/pptx/xlsx 文档（后缀与类型需匹配）。";
  }
  if (file.size > MAX_UPLOAD_BYTES) {
    return "文件超过 200MB 上限。";
  }
  return "";
}

type UploadDraft = {
  file: File | null;
  title: string;
  description: string;
  sortOrder: number;
  visible: boolean;
};

type EditDraft = {
  item: TrainingMaterialItem;
  title: string;
  description: string;
  sortOrder: number;
};

const EMPTY_UPLOAD: UploadDraft = {
  file: null,
  title: "",
  description: "",
  sortOrder: 0,
  visible: true,
};

export function TrainingMaterialsPage({
  readOnly = false,
}: {
  readOnly?: boolean;
}) {
  const [items, setItems] = useState<TrainingMaterialItem[]>([]);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
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
      });
      setItems(response.items);
      setTotal(response.total ?? response.items.length);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "读取培训素材失败");
    } finally {
      setLoading(false);
    }
  }, [offset]);

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
    setSaving(true);
    setError("");
    setUploadPercent(0);
    try {
      await uploadTrainingMaterial({
        file,
        title: uploadDraft.title.trim(),
        description: uploadDraft.description.trim(),
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
      sortOrder: item.sort_order,
    });
    setEditKey(`tm-${Date.now()}-${Math.random().toString(36).slice(2)}`);
  }

  async function submitEdit(reason: string) {
    if (!editing) return;
    setSaving(true);
    setError("");
    try {
      await updateTrainingMaterial({
        id: editing.item.id,
        title: editing.title.trim(),
        description: editing.description.trim(),
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
                工作台「素材库」页面已替换为本库的只读视图：这里统一上传培训文档与
                示例视频，客户只能浏览与下载。数字越大排在越前（同序按新→旧）。
              </p>
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
          文件（mp4/mov/pdf/docx/pptx/xlsx，≤200MB）
          <input
            ref={fileInputRef}
            type="file"
            accept=".mp4,.mov,.pdf,.docx,.pptx,.xlsx"
            disabled={saving}
            onChange={(event) => {
              const file = event.target.files?.[0] ?? null;
              setUploadDraft({ ...uploadDraft, file });
              if (file && !uploadDraft.title.trim()) {
                setUploadDraft((draft) => ({
                  ...draft,
                  file,
                  title:
                    draft.title.trim() || file.name.replace(/\.[^.]+$/, ""),
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
          description="修改标题、说明与排序；上下架请用列表上的开关。"
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
            ? `「${pendingDelete.title}」将立即对客户不可见；文件保留在存储中，误删可联系管理员恢复。`
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
