import { type FormEvent, useCallback, useEffect, useState } from "react";

import {
  AdminDeviceError,
  type DeviceListItem,
  listDevices,
  revokeDeviceCredential,
  unbindDevice,
} from "../api.admin";

/**
 * T33 — device list with pagination.
 *
 * The page shows all registered devices with their names, platforms,
 * last seen timestamps, and current status. Mutations retain the server's
 * reason, confirmation, CSRF and idempotency contract.
 */
export function DevicesPage({ readOnly = false }: { readOnly?: boolean }) {
  const [devices, setDevices] = useState<DeviceListItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [offset, setOffset] = useState(0);
  const [pageSize] = useState(20);
  const [pendingAction, setPendingAction] = useState<{
    device: DeviceListItem;
    kind: "unbind" | "revoke";
  } | null>(null);
  const [reason, setReason] = useState("");
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
    } catch (err) {
      if (err instanceof AdminDeviceError) {
        setError(`加载失败：${err.message}`);
      } else {
        setError("加载失败：未知错误");
      }
    } finally {
      setLoading(false);
    }
  }, [offset, pageSize]);

  useEffect(() => {
    loadDevices();
  }, [loadDevices]);

  const handleNextPage = () => {
    if (devices.length >= pageSize) {
      setOffset(offset + pageSize);
    }
  };

  const handlePrevPage = () => {
    if (offset > 0) {
      setOffset(Math.max(0, offset - pageSize));
    }
  };

  const statusLabels: Record<string, string> = {
    active: "活跃",
    BOUND: "已绑定",
    revoked: "已退出",
    REVOKED: "已退出",
    unbound: "已解绑",
    UNBOUND: "已解绑",
  };

  const platformLabels: Record<string, string> = {
    ios: "iOS",
    android: "Android",
    macos: "macOS",
    windows: "Windows",
  };
  const activeCount = devices.filter((device) =>
    ["BOUND", "active"].includes(device.status),
  ).length;
  const revokedCount = devices.filter(
    (device) => statusLabels[device.status] === "已退出",
  ).length;
  const unboundCount = devices.filter(
    (device) => statusLabels[device.status] === "已解绑",
  ).length;

  function beginAction(device: DeviceListItem, kind: "unbind" | "revoke") {
    if (readOnly) {
      return;
    }
    setPendingAction({ device, kind });
    setReason("");
    setActionError("");
    setNotice("");
  }

  async function submitAction(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!pendingAction || submitting) {
      return;
    }
    const trimmedReason = reason.trim();
    if (!trimmedReason) {
      setActionError("请填写操作原因");
      return;
    }
    setSubmitting(true);
    setActionError("");
    try {
      const result =
        pendingAction.kind === "unbind"
          ? await unbindDevice(pendingAction.device.device_id, trimmedReason)
          : await revokeDeviceCredential(
              pendingAction.device.device_id,
              trimmedReason,
            );
      setNotice(
        pendingAction.kind === "unbind"
          ? `设备已下线（审计编号：${result.request_id}）`
          : `设备已强制退出，可凭有效激活码重新进入（审计编号：${result.request_id}）`,
      );
      setPendingAction(null);
      setReason("");
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

  return (
    <div className="devices-page">
      <header className="admin-page-header">
        <h1>设备管理</h1>
        <p>集中处理设备绑定状态、最近活跃情况与强制下线操作。</p>
      </header>

      <section className="admin-summary-grid" aria-label="设备概览">
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
          <span>已退出</span>
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

      {error && <div className="error">{error}</div>}
      {notice ? <div role="status">{notice}</div> : null}

      {!loading && !error && devices.length === 0 && (
        <div className="empty-state">暂无设备数据</div>
      )}

      {!loading && !error && devices.length > 0 && (
        <>
          <div className="table-scroll admin-table-card">
            <table className="devices-table admin-data-table">
              <thead>
                <tr>
                  <th>设备名称</th>
                  <th>平台</th>
                  <th>最后活跃</th>
                  <th>状态</th>
                  <th>操作</th>
                </tr>
              </thead>
              <tbody>
                {devices.map((device) => (
                  <tr key={device.device_id}>
                    <td data-label="设备名称">
                      {device.display_name || `设备 #${device.slot_no}`}
                    </td>
                    <td data-label="平台">
                      <span
                        className={`platform-badge platform-${device.platform}`}
                      >
                        {platformLabels[device.platform] || device.platform}
                      </span>
                    </td>
                    <td data-label="最后活跃">
                      {device.bound_at
                        ? new Date(device.bound_at).toLocaleString("zh-CN")
                        : "—"}
                    </td>
                    <td data-label="状态">
                      <span className={`status-badge status-${device.status}`}>
                        {statusLabels[device.status] || device.status}
                      </span>
                    </td>
                    <td data-label="操作">
                      {readOnly ? (
                        "仅查看"
                      ) : ["BOUND", "active"].includes(device.status) ? (
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
              </tbody>
            </table>
          </div>

          <div className="pagination">
            <button
              type="button"
              onClick={handlePrevPage}
              disabled={offset === 0}
            >
              上一页
            </button>
            <span>
              偏移 {offset} 起，每页 {pageSize} 条
            </span>
            <button
              type="button"
              onClick={handleNextPage}
              disabled={devices.length < pageSize}
            >
              下一页
            </button>
          </div>
        </>
      )}

      {pendingAction && !readOnly ? (
        <form
          className="admin-form"
          aria-label="确认设备操作"
          onSubmit={submitAction}
        >
          <h2>
            {pendingAction.kind === "unbind"
              ? "确认设备下线"
              : "确认强制退出设备"}
          </h2>
          <p>
            目标设备：
            {pendingAction.device.display_name ||
              pendingAction.device.device_id}
          </p>
          {pendingAction.kind === "revoke" ? (
            <p>该操作会立即结束当前登录；用户仍可凭有效激活码重新进入。</p>
          ) : null}
          <label>
            操作原因
            <textarea
              value={reason}
              onChange={(event) => setReason(event.target.value)}
            />
          </label>
          {actionError ? <p role="alert">{actionError}</p> : null}
          <div className="admin-actions">
            <button disabled={submitting} type="submit">
              {pendingAction.kind === "unbind" ? "确认下线" : "确认退出"}
            </button>
            <button
              disabled={submitting}
              type="button"
              onClick={() => setPendingAction(null)}
            >
              取消
            </button>
          </div>
        </form>
      ) : null}
    </div>
  );
}
