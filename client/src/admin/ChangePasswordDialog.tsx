import { useState } from "react";
import {
  adminActivationErrorMessage,
  changeOwnAdminPassword,
} from "../api.admin";
import { ConfirmDialog } from "./ui/ConfirmDialog";

// 管理员自助改密（整改 T17）：此前管理员登录后只能找回或找超管重置。
// 与服务端同一口径：新密码 12–128 个字符且不能与原密码相同；成功后本人其它
// 会话会被吊销，当前会话保留。
const MIN_LENGTH = 12;
const MAX_LENGTH = 128;

export function ChangePasswordDialog({
  open,
  onClose,
  onChanged,
}: {
  open: boolean;
  onClose: () => void;
  onChanged: () => void;
}) {
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [confirm, setConfirm] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  function close() {
    if (busy) return;
    setCurrent("");
    setNext("");
    setConfirm("");
    setError("");
    onClose();
  }

  async function submit() {
    if (next.length < MIN_LENGTH || next.length > MAX_LENGTH) {
      setError(`新密码需为 ${MIN_LENGTH} 至 ${MAX_LENGTH} 个字符。`);
      return;
    }
    if (next !== confirm) {
      setError("两次输入的新密码不一致。");
      return;
    }
    if (next === current) {
      setError("新密码不能与原密码相同。");
      return;
    }
    setBusy(true);
    setError("");
    try {
      await changeOwnAdminPassword(current, next);
      setCurrent("");
      setNext("");
      setConfirm("");
      onChanged();
      onClose();
    } catch (cause) {
      setError(adminActivationErrorMessage(cause, "修改密码失败"));
    } finally {
      setBusy(false);
    }
  }

  return (
    <ConfirmDialog
      open={open}
      title="修改我的密码"
      description="需要原密码；修改成功后，你在其它设备上的管理登录会被退出，当前登录保留。"
      confirmLabel="确认修改"
      level="standard"
      busy={busy}
      error={error}
      onClose={close}
      onConfirm={() => void submit()}
    >
      <label>
        原密码
        <input
          autoComplete="current-password"
          type="password"
          value={current}
          onChange={(event) => setCurrent(event.target.value)}
        />
      </label>
      <label>
        新密码（{MIN_LENGTH}–{MAX_LENGTH} 个字符）
        <input
          autoComplete="new-password"
          type="password"
          value={next}
          onChange={(event) => setNext(event.target.value)}
        />
      </label>
      <label>
        再次输入新密码
        <input
          autoComplete="new-password"
          type="password"
          value={confirm}
          onChange={(event) => setConfirm(event.target.value)}
        />
      </label>
    </ConfirmDialog>
  );
}
