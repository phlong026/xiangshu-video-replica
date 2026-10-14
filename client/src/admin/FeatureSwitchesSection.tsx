import { useCallback, useEffect, useState } from "react";
import { readPublishStatsEnabled, setPublishStatsEnabled } from "../api.admin";
import { ConfirmDialog } from "./ui/ConfirmDialog";
import { PageBanner } from "./ui/PageBanner";
import { StatusBadge } from "./ui/StatusBadge";

/**
 * 功能开关（整改 T28）：默认关闭、会改变菜单结构的功能统一在这里开关。
 * 开关此前挂在「发布成效」页内，而页面本身只有开启后才有意义，
 * 导致关闭时菜单里躺着一个打开只能看到「未开启」的入口。
 */
export function FeatureSwitchesSection({
  readOnly = false,
  onChanged,
}: {
  readOnly?: boolean;
  /** 切换成功后通知外壳刷新菜单显隐。 */
  onChanged?: (enabled: boolean) => void;
}) {
  const [enabled, setEnabled] = useState<boolean | null>(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [dialogOpen, setDialogOpen] = useState(false);
  const [saving, setSaving] = useState(false);

  const load = useCallback(async () => {
    try {
      setError("");
      setEnabled((await readPublishStatsEnabled()).enabled);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "读取功能开关失败");
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  async function toggle(reason: string) {
    const next = !enabled;
    setSaving(true);
    setError("");
    try {
      await setPublishStatsEnabled({ enabled: next, reason });
      setEnabled(next);
      setNotice(next ? "发布效果回流已开启。" : "发布效果回流已关闭。");
      setDialogOpen(false);
      onChanged?.(next);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "切换失败");
    } finally {
      setSaving(false);
    }
  }

  return (
    <section className="admin-panel" aria-label="功能开关">
      <header>
        <h3>功能开关</h3>
        <p className="admin-hint">
          默认关闭的功能在此开启后才会出现在侧边栏菜单里。
        </p>
      </header>
      {error ? <PageBanner tone="error">{error}</PageBanner> : null}
      {notice ? <PageBanner tone="notice">{notice}</PageBanner> : null}
      <div className="admin-feature-switch">
        <div>
          <strong>发布效果回流</strong>
          <p className="admin-hint">
            同步客户已发布内容的公开互动数据。开启前需确认对客户发布内容的采集授权口径；
            开启后菜单出现「发布成效」。
          </p>
        </div>
        <StatusBadge tone={enabled ? "good" : "warn"}>
          {enabled === null ? "读取中" : enabled ? "已开启" : "未开启（默认）"}
        </StatusBadge>
        {!readOnly && enabled !== null ? (
          <button type="button" onClick={() => setDialogOpen(true)}>
            {enabled ? "关闭回流" : "开启回流"}
          </button>
        ) : null}
      </div>
      <ConfirmDialog
        open={dialogOpen}
        title={enabled ? "关闭发布效果回流" : "开启发布效果回流"}
        confirmLabel="确认"
        description={
          enabled
            ? "关闭后停止同步，菜单不再显示「发布成效」；已采集的数据保留。"
            : "开启后将可采集客户发布内容的公开互动数据。请确认已具备对客户发布内容的采集授权口径。"
        }
        busy={saving}
        error={error}
        level="reason"
        onClose={() => !saving && setDialogOpen(false)}
        onConfirm={(reason) => void toggle(reason)}
      />
    </section>
  );
}
