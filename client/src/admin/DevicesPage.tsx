import { useCallback, useEffect, useRef, useState } from "react";

import {
  type DeviceListItem,
  type DeviceSummary,
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
export function DevicesPage({
  readOnly = false,
  userId,
}: {
  readOnly?: boolean;
  userId?: string;
}) {
  const [devices, setDevices] = useState<DeviceListItem[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [offset, setOffset] = useState(0);
  const [pageSize] = useState(20);
  const [summary, setSummary] = useState<DeviceSummary>({
    bound: 0,
    online: 0,
    revoked_today: 0,
    unbound: 0,
  });
  const [statusFilter, setStatusFilter] = useState("");
  const [platformFilter, setPlatformFilter] = useState("");
  const [pendingAction, setPendingAction] = useState<{
    device: DeviceListItem;
    kind: "unbind" | "revoke";
  } | null>(null);
  const [actionError, setActionError] = useState("");
  const [notice, setNotice] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const requestIdRef = useRef(0);
  const contextRef = useRef({ id: 0, userId });

  const loadDevices = useCallback(async () => {
    const requestId = requestIdRef.current + 1;
    requestIdRef.current = requestId;
    try {
      setLoading(true);
      setError("");
      const response = await listDevices({
        limit: pageSize,
        offset,
        status: statusFilter || undefined,
        platform: platformFilter || undefined,
        userId,
      });
      if (requestId !== requestIdRef.current) {
        return;
      }
      setDevices(response.items);
      setTotal(response.total);
      setSummary(
        response.summary ?? {
          bound: response.total,
          online: 0,
          revoked_today: 0,
          unbound: 0,
        },
      );
    } catch (err) {
      if (requestId !== requestIdRef.current) {
        return;
      }
      setError(
        err instanceof Error && err.message
          ? `加载失败：${err.message}`
          : "加载失败：未知错误",
      );
    } finally {
      if (requestId === requestIdRef.current) {
        setLoading(false);
      }
    }
  }, [offset, pageSize, platformFilter, statusFilter, userId]);

  useEffect(() => {
    contextRef.current = { id: contextRef.current.id + 1, userId };
    requestIdRef.current += 1;
    setDevices([]);
    setTotal(0);
    setOffset(0);
    setPendingAction(null);
    setActionError("");
    setNotice("");
    setSubmitting(false);
  }, [userId]);

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
    const actionContext = contextRef.current;
    try {
      const result =
        pendingAction.kind === "unbind"
          ? await unbindDevice(pendingAction.device.device_id, reason)
          : await revokeDeviceCredential(
              pendingAction.device.device_id,
              reason,
            );
      if (contextRef.current !== actionContext) {
        return;
      }
      setNotice(
        pendingAction.kind === "unbind"
          ? `设备已下线（审计编号：${result.request_id}）`
          : `设备已强制退出，可凭有效激活码重新进入（审计编号：${result.request_id}）`,
      );
      setPendingAction(null);
      await loadDevices();
    } catch (err) {
      if (contextRef.current !== actionContext) {
        return;
      }
      setActionError(
        err instanceof Error && err.message.trim()
          ? err.message
          : "设备操作失败，请重新登录管理端后重试",
      );
    } finally {
      if (contextRef.current === actionContext) {
        setSubmitting(false);
      }
    }
  }

  return (
    <div className="devices-page">
      <header className="admin-page-header">
        <h2>设备管理</h2>
        <p>集中处理设备绑定状态、最近活跃情况与强制下线操作。</p>
      </header>

      {userId ? <p className="admin-hint">当前客户：{userId}</p> : null}
      {!userId ? (
        <section aria-label="设备概览" className="admin-summary-grid">
          <article className="admin-summary-card">
            <span>绑定设备</span>
            <strong>{summary.bound}</strong>
            <small>全量口径</small>
          </article>
          <article className="admin-summary-card">
            <span>在线设备</span>
            <strong>{summary.online}</strong>
            <small>租约有效</small>
          </article>
          <article className="admin-summary-card">
            <span>已强制退出</span>
            <strong>{summary.revoked_today}</strong>
            <small>今日全量</small>
          </article>
          <article className="admin-summary-card">
            <span>已解绑</span>
            <strong>{summary.unbound}</strong>
            <small>累计全量</small>
          </article>
        </section>
      ) : null}
      {!userId ? (
        <form
          className="admin-toolbar"
          onSubmit={(event) => {
            event.preventDefault();
            setOffset(0);
            void loadDevices();
          }}
        >
          <label className="admin-toolbar__field admin-toolbar__field--select">
            <span>设备状态</span>
            <select
              aria-label="设备状态"
              value={statusFilter}
              onChange={(event) => setStatusFilter(event.target.value)}
            >
              <option value="">全部状态</option>
              <option value="BOUND">已绑定</option>
              <option value="UNBOUND">已解绑</option>
              <option value="REVOKED">已强制退出</option>
            </select>
          </label>
          <label className="admin-toolbar__field admin-toolbar__field--select">
            <span>平台</span>
            <select
              aria-label="设备平台"
              value={platformFilter}
              onChange={(event) => setPlatformFilter(event.target.value)}
            >
              <option value="">全部平台</option>
              <option value="windows">Windows</option>
              <option value="macos">macOS</option>
              <option value="linux">Linux</option>
            </select>
          </label>
          <button type="submit">查询</button>
        </form>
      ) : null}

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
                <th>所属客户</th>
                <th>绑定激活码</th>
                <th>槽位</th>
                <th>平台</th>
                <th>绑定时间</th>
                <th>最近心跳</th>
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
                <td data-label="所属客户" title={device.username}>
                  {device.username}
                </td>
                <td
                  data-label="绑定激活码"
                  title={device.activation_code ?? undefined}
                >
                  <code>{device.activation_code}</code>
                </td>
                <td data-label="槽位">{device.slot_no}/2</td>
                <td data-label="平台">
                  <PlatformBadge platform={device.platform} />
                </td>
                <td data-label="绑定时间">{formatDateTime(device.bound_at)}</td>
                <td data-label="最近心跳">
                  {device.online ? "● " : "○ "}
                  {formatDateTime(device.last_heartbeat_at)}
                </td>
                <td data-label="状态">
                  <DeviceStatusBadge
                    status={
                      device.status === "BOUND"
                        ? device.online
                          ? "ONLINE"
                          : "OFFLINE"
                        : device.status
                    }
                  />
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
