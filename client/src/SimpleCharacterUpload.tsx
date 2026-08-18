import { useRef, useState } from "react";

import { type SimpleCharacterResult, uploadSimpleCharacter } from "./api";

const MAX_UPLOAD_BYTES = 10 * 1024 * 1024;
const ALLOWED_TYPES = ["image/png", "image/jpeg"];

export function SimpleCharacterUpload({
  onCreated,
  projectId = null,
}: {
  onCreated: (result: SimpleCharacterResult, displayName: string) => void;
  projectId?: string | null;
}) {
  const [busy, setBusy] = useState(false);
  const [displayName, setDisplayName] = useState("");
  const [error, setError] = useState("");
  const [fileName, setFileName] = useState("");
  const [message, setMessage] = useState("");
  const fileInputRef = useRef<HTMLInputElement>(null);

  function pickFile(file: File | undefined) {
    setError("");
    setMessage("");
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
    const name = displayName.trim();
    if (!file || !name) {
      setError("请选择图片并填写人物名称。");
      return;
    }
    setBusy(true);
    setError("");
    setMessage("");
    try {
      const result = await uploadSimpleCharacter(projectId, file, name);
      setMessage(`人物“${name}”五视角拼合图已生成，可在下方预览与下载。`);
      setDisplayName("");
      setFileName("");
      if (fileInputRef.current) {
        fileInputRef.current.value = "";
      }
      onCreated(result, name);
    } catch (requestError) {
      setError(
        requestError instanceof Error
          ? requestError.message
          : "一键创建人物失败，请重试。",
      );
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="simple-character-upload" aria-label="一键上传人物">
      <p className="simple-character-upload__title">
        上传人物图片，生成五视角拼合图
      </p>
      <p className="simple-character-upload__note">
        上传一张已获授权的人物图片（PNG / JPEG，不超过 10MB），系统会生成
        一张包含五个视角的拼合图，并发布为可选角色。AI 绘制约需 1~3
        分钟，请耐心等待。
      </p>
      <div className="simple-character-upload-form">
        <label>
          人物名称
          <input
            onChange={(event) => setDisplayName(event.target.value)}
            placeholder="例如：荣哥"
            type="text"
            value={displayName}
          />
        </label>
        <div className="simple-character-upload__picker">
          <input
            accept={ALLOWED_TYPES.join(",")}
            aria-label="授权图片"
            hidden
            onChange={(event) => pickFile(event.target.files?.[0])}
            ref={fileInputRef}
            type="file"
          />
          <button
            className="secondary-button"
            disabled={busy}
            onClick={() => fileInputRef.current?.click()}
            type="button"
          >
            选择图片
          </button>
          <span className="simple-character-upload__file">
            {fileName || "未选择图片"}
          </span>
        </div>
        <button
          className="simple-character-upload__submit"
          disabled={busy}
          onClick={submit}
          type="button"
        >
          {busy ? "正在生成拼合图（约 1~3 分钟）…" : "一键生成五视角拼合图"}
        </button>
      </div>
      {error ? (
        <p className="settings-error" role="alert">
          {error}
        </p>
      ) : null}
      {message ? <p className="setup-success">{message}</p> : null}
    </section>
  );
}
