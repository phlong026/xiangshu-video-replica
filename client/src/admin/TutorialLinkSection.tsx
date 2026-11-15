import { useCallback, useEffect, useState } from "react";
import { getDisplaySetting, updateDisplaySetting } from "../api.admin";

/**
 * 系统设置 · 展示配置：参考生视频页「提示词教程」外链。
 *
 * 链接此前写死在前端常量（PROMPT_TUTORIAL_URL 占位值），每换一次要发一次
 * 版；搬进数据库后运营在这里改，客户端即时生效。留空 = 客户端按钮点击弹
 * 「敬请期待」占位弹窗（正式链接就绪前的默认行为，用户拍板 2026-11-15）。
 */
export function TutorialLinkSection({
  readOnly = false,
}: {
  readOnly?: boolean;
}) {
  const [value, setValue] = useState("");
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  const load = useCallback(async () => {
    try {
      setLoading(true);
      setError("");
      const response = await getDisplaySetting("prompt_tutorial_url");
      setValue(response.value);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "读取教程链接失败");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  async function save() {
    const trimmed = value.trim();
    if (trimmed && !/^https?:\/\//i.test(trimmed)) {
      setError("链接需以 http(s):// 开头。");
      return;
    }
    setSaving(true);
    setError("");
    setNotice("");
    try {
      const response = await updateDisplaySetting({
        key: "prompt_tutorial_url",
        value: trimmed,
        reason: "更新提示词教程链接",
      });
      setValue(response.value);
      setNotice(
        response.value
          ? "教程链接已保存，客户端即时生效。"
          : "已清空：客户端按钮将显示占位弹窗。",
      );
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "保存教程链接失败");
    } finally {
      setSaving(false);
    }
  }

  return (
    <section aria-label="展示配置" className="admin-tutorial-link">
      <header className="admin-tutorial-link__header">
        <h2>展示配置</h2>
        <p className="admin-hint">
          参考生视频页「提示词教程」按钮的外链地址；留空时客户点击按钮会看到
          「敬请期待」占位弹窗。
        </p>
      </header>
      {error ? <p className="admin-hint admin-hint--error">{error}</p> : null}
      {notice ? <p className="admin-hint">{notice}</p> : null}
      {loading ? (
        <p className="admin-hint">正在读取配置…</p>
      ) : (
        <div
          className="admin-tutorial-link__row"
          style={{ alignItems: "center", display: "flex", gap: 8 }}
        >
          <input
            aria-label="提示词教程链接"
            disabled={readOnly || saving}
            maxLength={2000}
            onChange={(event) => setValue(event.target.value)}
            placeholder="https://飞书文档分享链接"
            style={{ flex: 1 }}
            type="url"
            value={value}
          />
          <button
            disabled={readOnly || saving}
            type="button"
            onClick={() => void save()}
          >
            保存
          </button>
        </div>
      )}
    </section>
  );
}
