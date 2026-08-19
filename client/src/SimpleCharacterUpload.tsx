import { useRef, useState } from "react";

import { uploadSimpleCharacter } from "./api";

const MAX_UPLOAD_BYTES = 10 * 1024 * 1024;
const ALLOWED_TYPES = ["image/png", "image/jpeg"];

export function SimpleCharacterUpload({
  projectId,
  onComplete,
  onCancel,
}: {
  projectId: string;
  onComplete: (
    result: Awaited<ReturnType<typeof uploadSimpleCharacter>>,
  ) => void;
  onCancel: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [fileName, setFileName] = useState("");
  const [progress, setProgress] = useState(0);
  const fileInputRef = useRef<HTMLInputElement>(null);

  function pickFile(file: File | undefined) {
    setError("");
    setProgress(0);
    if (!file) {
      setFileName("");
      return;
    }
    if (!ALLOWED_TYPES.includes(file.type)) {
      setFileName("");
      setError("仅支持 PNG 或 JPEG 图片。");
      return;
    }
    if (file.size > MAX_UPLOAD_BYTES) {
      setFileName("");
      setError("图片超过 10MB 限制。");
      return;
    }
    setFileName(file.name);
  }

  async function submit() {
    const file = fileInputRef.current?.files?.[0];
    if (!file) {
      setError("请选择人物图片。");
      return;
    }
    setBusy(true);
    setError("");
    try {
      const result = await uploadSimpleCharacter(projectId, file, setProgress);
      onComplete(result);
    } catch (uploadError) {
      setError(
        uploadError instanceof Error
          ? uploadError.message
          : "极简上传失败，请重试。",
      );
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="character-upload-panel" aria-label="极简人物上传">
      <div>
        <h3>极简上传</h3>
        <p className="status-note">上传单张授权图，生成多视角人物资产。</p>
      </div>
      <input
        accept={ALLOWED_TYPES.join(",")}
        aria-label="人物授权图片"
        onChange={(event) => pickFile(event.target.files?.[0])}
        ref={fileInputRef}
        type="file"
      />
      {fileName ? <p className="status-note">已选择：{fileName}</p> : null}
      {busy ? <p className="status-note">上传进度：{progress}%</p> : null}
      {error ? (
        <p className="settings-error" role="alert">
          {error}
        </p>
      ) : null}
      <div className="toolbar-actions">
        <button disabled={busy} onClick={submit} type="button">
          {busy ? "正在上传…" : "开始生成"}
        </button>
        <button
          className="secondary-button"
          disabled={busy}
          onClick={onCancel}
          type="button"
        >
          取消
        </button>
      </div>
    </section>
  );
}
