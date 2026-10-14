import { useEffect, useRef, useState } from "react";
import {
  batchAnnotateCustomers,
  type CustomerOwnerCandidate,
  listCustomerOwnerCandidates,
  listCustomerTags,
} from "../api.admin";
import type { BatchTarget } from "./BatchGrantDialog";
import { ConfirmDialog } from "./ui/ConfirmDialog";

const splitTags = (value: string) =>
  value
    .split(/[,，、\s]+/)
    .map((tag) => tag.trim())
    .filter(Boolean);

/**
 * 批量标注（整改 T30）：对勾选的客户增量加标签 / 去标签 / 改负责人。
 *
 * 只叠加不替换——客户已有的其他标签和备注原样保留；服务端逐户落审计并逐户
 * 返回结果，个别客户失败（如标签超 10 个）不影响其余客户。
 */
export function BatchAnnotateDialog({
  open,
  targets,
  onClose,
  onFinished,
}: {
  open: boolean;
  targets: BatchTarget[];
  onClose: () => void;
  onFinished: () => void;
}) {
  const [addTags, setAddTags] = useState("");
  const [removeTags, setRemoveTags] = useState("");
  // keep = 不改；clear = 清空负责人；其余为管理员 user_id。
  const [owner, setOwner] = useState("keep");
  const [tagOptions, setTagOptions] = useState<string[]>([]);
  const [owners, setOwners] = useState<CustomerOwnerCandidate[]>([]);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [failures, setFailures] = useState<string[]>([]);
  const retryKey = useRef<{ fingerprint: string; key: string } | null>(null);

  useEffect(() => {
    if (!open) return;
    let active = true;
    void listCustomerTags()
      .then((result) => {
        if (active) setTagOptions(result.items.map((item) => item.tag));
      })
      .catch(() => undefined);
    void listCustomerOwnerCandidates()
      .then((result) => {
        if (active) setOwners(result.items);
      })
      .catch(() => undefined);
    return () => {
      active = false;
    };
  }, [open]);

  function close() {
    if (saving) return;
    setError("");
    setFailures([]);
    onClose();
  }

  async function run(reason: string) {
    const add = splitTags(addTags);
    const remove = splitTags(removeTags);
    if (add.length === 0 && remove.length === 0 && owner === "keep") {
      setError("请至少填写一项：加标签、去标签或负责人。");
      return;
    }
    const input = {
      userIds: targets.map((item) => item.user_id),
      addTags: add,
      removeTags: remove,
      setOwner: owner !== "keep",
      ownerUserId: owner === "keep" || owner === "clear" ? null : owner,
    };
    const fingerprint = JSON.stringify([input, reason]);
    if (retryKey.current?.fingerprint !== fingerprint)
      retryKey.current = { fingerprint, key: crypto.randomUUID() };
    setSaving(true);
    setError("");
    try {
      const result = await batchAnnotateCustomers(
        input,
        reason,
        retryKey.current.key,
      );
      retryKey.current = null;
      onFinished();
      if (result.failed === 0) {
        setFailures([]);
        onClose();
        return;
      }
      const names = new Map(targets.map((t) => [t.user_id, t.username]));
      setFailures(
        result.results
          .filter((item) => !item.ok)
          .map(
            (item) =>
              `${names.get(item.user_id) ?? item.user_id}：${item.message}`,
          ),
      );
      setError(`成功 ${result.succeeded} / 失败 ${result.failed}`);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "批量标注失败");
    } finally {
      setSaving(false);
    }
  }

  return (
    <ConfirmDialog
      open={open}
      title="批量标注"
      confirmLabel="确认应用"
      description={`对 ${targets.length} 名客户叠加标签 / 调整负责人；已有标签与备注保留，逐户留痕。`}
      busy={saving}
      error={error}
      level="reason"
      onClose={close}
      onConfirm={(reason) => void run(reason)}
    >
      <label>
        加标签（逗号分隔）
        <input
          list="batch-tag-options"
          value={addTags}
          onChange={(event) => setAddTags(event.target.value)}
          placeholder="如：续费关注, 重点"
        />
        <datalist id="batch-tag-options">
          {tagOptions.map((tag) => (
            <option key={tag} value={tag} />
          ))}
        </datalist>
      </label>
      <label>
        去标签（逗号分隔）
        <input
          value={removeTags}
          onChange={(event) => setRemoveTags(event.target.value)}
        />
      </label>
      <label>
        负责人
        <select
          value={owner}
          onChange={(event) => setOwner(event.target.value)}
        >
          <option value="keep">不修改</option>
          <option value="clear">清空负责人</option>
          {owners.map((item) => (
            <option key={item.user_id} value={item.user_id}>
              {item.display_name || item.username}
            </option>
          ))}
        </select>
      </label>
      {failures.length > 0 ? (
        <ul aria-label="失败客户">
          {failures.map((line) => (
            <li key={line}>{line}</li>
          ))}
        </ul>
      ) : null}
    </ConfirmDialog>
  );
}
