import { useRef, useState } from "react";

import { type SimpleCharacterResult, uploadSimpleCharacter } from "./api";

const MAX_UPLOAD_BYTES = 10 * 1024 * 1024;
const ALLOWED_TYPES = ["image/png", "image/jpeg"];

export function SimpleCharacterUpload({
  onCreated,
  projectId,
}: {
  onCreated: (result: SimpleCharacterResult) => void;
  projectId: string;
}) {
  const [busy, setBusy] = useState(false);
  const [displayName, setDisplayName] = useState("");
  const [error, setError] = useState("");
  const [fileName, setFileName] = useState("");
  const [message, setMessage] = useState("");
  const [personaName, setPersonaName] = useState("");
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
      const result = await uploadSimpleCharacter(
        projectId,
        file,
        name,
        personaName,
      );
      setMessage(`人物“${name}”已生成并发布，可在下方列表中选择。`);
      setDisplayName("");
      setPersonaName("");
      setFileName("");
      if (fileInputRef.current) {
        fileInputRef.current.value = "";
      }
      onCreated(result);
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
      <p className="status-note">
        上传一张已获授权的人物图片，系统会自动生成七个标准视角并直接发布为可选角色版本。
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
        <label>
          角色名（可选）
          <input
            onChange={(event) => setPersonaName(event.target.value)}
            placeholder="默认与人物名称相同"
            type="text"
            value={personaName}
          />
        </label>
        <label>
          授权图片
          <input
            accept={ALLOWED_TYPES.join(",")}
            onChange={(event) => pickFile(event.target.files?.[0])}
            ref={fileInputRef}
            type="file"
          />
        </label>
        {fileName ? <p className="status-note">已选择：{fileName}</p> : null}
        <button disabled={busy} onClick={submit} type="button">
          {busy ? "正在生成七视角" : "一键生成人物"}
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
