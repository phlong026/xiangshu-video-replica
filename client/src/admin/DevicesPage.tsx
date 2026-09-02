import { useCallback, useEffect, useState } from "react";

import {
  type DeviceListItem,
  listDevices,
  revokeDeviceCredential,
  unbindDevice,
} from "../api.admin";
import { ConfirmDialog } from "./ui/ConfirmDialog";
import { DataTable } from "./ui/DataTable";
import { PageBanner } from "./ui/PageBanner";
import { Pagination } from "./ui/Pagination";
import { DeviceStatusBadge, PlatformBadge } from "./ui/StatusBadge";
import { formatDateTime } from "./ui/vocabulary";

/**
 * T33 — device list with pagination.
 *
 * The page shows all registered devices with their names, platforms,
 * last seen timestamps, and current status. Mutations retain the server's
 * reason, confirmation, CSRF and idempotency contract.
 */
export function DevicesPage({ readOnly = false }: { readOnly?: boolean }) {
  const [devices, setDevices] = useState<DeviceListItem[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [offset, setOffset] = useState(0);
  const [pageSize] = useState(20);
  const [pendingAction, setPendingAction] = useState<{
    device: DeviceListItem;
    kind: "unbind" | "revoke";
  } | null>(null);
  const [actionError, setActionError] = useState("");
  const [notice, setNotice] = useState("");
  const [submitting, setSubmitting] = useState(false);

  const loadDevices = useCallback(async () => {
    try {
      setLoading(true);
      setError("");
      const response = await listDevices({
        limit: pageSize,
        offset,
      });
      setDevices(response.items);
      setTotal(response.total);
    } catch (err) {
      setError(
        err instanceof Error && err.message
          ? `加载失败：${err.message}`
          : "加载失败：未知错误",
      );
    } finally {
      setLoading(false);
    }
  }, [offset, pageSize]);

  useEffect(() => {
    loadDevices();
  }, [loadDevices]);

  function beginAction(device: DeviceListItem, kind: "unbind" | "revoke") {
    if (readOnly) {
      return;
    }
    setPendingAction({ device, kind });
    setActionError("");
    setNotice("");
  }

  async function submitAction(reason: string) {
    if (!pendingAction || submitting) {
      return;
    }
    setSubmitting(true);
    setActionError("");
    try {
      const result =
        pendingAction.kind === "unbind"
          ? await unbindDevice(pendingAction.device.device_id, reason)
          : await revokeDeviceCredential(
              pendingAction.device.device_id,
              reason,
            );
      setNotice(
        pendingAction.kind === "unbind"
          ? `设备已下线（审计编号：${result.request_id}）`
          : `设备已强制退出，可凭有效激活码重新进入（审计编号：${result.request_id}）`,
      );
      setPendingAction(null);
      await loadDevices();
    } catch (err) {
      setActionError(
        err instanceof Error && err.message.trim()
          ? err.message
          : "设备操作失败，请重新登录管理端后重试",
      );
    } finally {
      setSubmitting(false);
    }
  }

  const activeCount = devices.filter(
    (device) => device.status === "BOUND",
  ).length;
  const revokedCount = devices.filter(
    (device) => device.status === "REVOKED",
  ).length;
  const unboundCount = devices.filter(
    (device) => device.status === "UNBOUND",
  ).length;

  return (
    <div className="devices-page">
      <header className="admin-page-header">
        <h1>设备管理</h1>
        <p>集中处理设备绑定状态、最近活跃情况与强制下线操作。</p>
      </header>

      <section aria-label="设备概览" className="admin-summary-grid">
        <article className="admin-summary-card">
          <span>当前页设备</span>
          <strong>{devices.length}</strong>
          <small>本页加载结果</small>
        </article>
        <article className="admin-summary-card">
          <span>在线设备</span>
          <strong>{activeCount}</strong>
          <small>可执行下线或强退</small>
        </article>
        <article className="admin-summary-card">
          <span>已强制退出</span>
          <strong>{revokedCount}</strong>
          <small>凭激活码可重新进入</small>
        </article>
        <article className="admin-summary-card">
          <span>已解绑</span>
          <strong>{unboundCount}</strong>
          <small>等待重新绑定</small>
        </article>
      </section>

      {loading && <div className="loading">加载中...</div>}

      {error ? <PageBanner tone="error">{error}</PageBanner> : null}
      {notice ? <PageBanner tone="notice">{notice}</PageBanner> : null}

      {!loading && !error && devices.length === 0 && (
        <div className="empty-state">暂无设备数据</div>
      )}

      {!loading && !error && devices.length > 0 && (
        <>
          <DataTable
            ariaLabel="设备列表"
            headers={
              <>
                <th>设备名称</th>
                <th>平台</th>
                <th>绑定时间</th>
                <th>状态</th>
                <th>操作</th>
              </>
            }
          >
            {devices.map((device) => (
              <tr key={device.device_id}>
                <td data-label="设备名称">
                  {device.display_name || `设备 #${device.slot_no}`}
                </td>
                <td data-label="平台">
                  <PlatformBadge platform={device.platform} />
                </td>
                <td data-label="绑定时间">{formatDateTime(device.bound_at)}</td>
                <td data-label="状态">
                  <DeviceStatusBadge status={device.status} />
                </td>
                <td data-label="操作">
                  {readOnly ? (
                    "仅查看"
                  ) : device.status === "BOUND" ? (
                    <div className="admin-actions admin-actions--table">
                      <button
                        aria-label={`下线设备：${device.display_name || device.device_id}`}
                        type="button"
                        onClick={() => beginAction(device, "unbind")}
                      >
                        下线设备
                      </button>
                      <button
                        aria-label={`强制退出：${device.display_name || device.device_id}`}
                        className="admin-action--danger"
                        type="button"
                        onClick={() => beginAction(device, "revoke")}
                      >
                        强制退出
                      </button>
                    </div>
                  ) : (
                    "无需操作"
                  )}
                </td>
              </tr>
            ))}
          </DataTable>

          <Pagination
            disabled={loading}
            limit={pageSize}
            offset={offset}
            total={total}
            onPageChange={setOffset}
          />
        </>
      )}

      <ConfirmDialog
        busy={submitting}
        confirmLabel={
          pendingAction?.kind === "unbind" ? "确认下线" : "确认退出"
        }
        description={
          pendingAction ? (
            <>
              目标设备：
              {pendingAction.device.display_name ||
                pendingAction.device.device_id}
              {pendingAction.kind === "revoke"
                ? "。该操作会立即结束当前登录；用户仍可凭有效激活码重新进入。"
                : "。设备将解除当前绑定，但不会撤销激活码。"}
            </>
          ) : null
        }
        error={actionError}
        level="reason"
        open={pendingAction !== null && !readOnly}
        title={
          pendingAction?.kind === "unbind" ? "确认设备下线" : "确认强制退出设备"
        }
        onClose={() => setPendingAction(null)}
        onConfirm={(reason: string) => void submitAction(reason)}
      />
    </div>
  );
}
