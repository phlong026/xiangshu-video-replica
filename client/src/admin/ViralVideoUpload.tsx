import { useRef, useState } from "react";
import {
  adminActivationErrorMessage,
  adminUploadViralVideo,
} from "../api.admin";
import { PageBanner } from "./ui/PageBanner";

export function ViralVideoUpload({ onUploaded }: { onUploaded: () => void }) {
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  const key = useRef(crypto.randomUUID());
  return (
    <section aria-label="上传爆款视频">
      <h3>上传视频</h3>
      <form
        onChange={() => {
          key.current = crypto.randomUUID();
        }}
        onSubmit={async (event) => {
          event.preventDefault();
          if (busy) return;
          const form = event.currentTarget;
          const data = new FormData(form);
          const file =
            form.querySelector<HTMLInputElement>('input[name="file"]')
              ?.files?.[0];
          if (
            !(file instanceof File) ||
            !file.size ||
            file.size > 200 * 1024 * 1024
          ) {
            setError("请选择不超过 200 MB 的 MP4/MOV 视频。");
            return;
          }
          data.set("file", file);
          setBusy(true);
          setError("");
          setNotice("");
          try {
            await adminUploadViralVideo(data, key.current);
            form.reset();
            key.current = crypto.randomUUID();
            setNotice("视频已入库并准备完成；请在视频库中推送到前端。");
            onUploaded();
          } catch (cause) {
            setError(adminActivationErrorMessage(cause, "视频上传失败"));
          } finally {
            setBusy(false);
          }
        }}
      >
        <fieldset disabled={busy} className="admin-toolbar admin-viral-toolbar">
          <label>
            来源平台
            <select name="platform" defaultValue="douyin">
              <option value="douyin">抖音</option>
              <option value="wechat_channels">视频号</option>
            </select>
          </label>
          <label>
            视频标题
            <input name="title" required maxLength={200} />
          </label>
          <label>
            作者
            <input name="author" maxLength={120} />
          </label>
          <label>
            视频文件
            <input
              name="file"
              type="file"
              accept=".mp4,.mov,video/mp4,video/quicktime"
              required
            />
          </label>
          <button type="submit">{busy ? "上传处理中…" : "上传入库"}</button>
        </fieldset>
      </form>
      {error && <PageBanner tone="error">{error}</PageBanner>}
      {notice && <PageBanner tone="notice">{notice}</PageBanner>}
    </section>
  );
}
