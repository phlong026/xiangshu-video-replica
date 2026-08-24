import { useCallback, useEffect, useState } from "react";

import {
  type DeviceListItem,
  AdminDeviceError,
  listDevices,
} from "../api.admin";

/**
 * T33 — device list with pagination.
 *
 * The page shows all registered devices with their names, platforms,
 * last seen timestamps, and current status. All roles (admin/auditor)
 * see the same read-only view; device management operations (unbind,
 * revoke) are left for future enhancement.
 */
export function DevicesPage() {
  const [devices, setDevices] = useState<DeviceListItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [offset, setOffset] = useState(0);
  const [pageSize] = useState(20);

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
    revoked: "已作废",
    unbound: "已解绑",
  };

  const platformLabels: Record<string, string> = {
    ios: "iOS",
    android: "Android",
    macos: "macOS",
    windows: "Windows",
  };

  return (
    <div className="devices-page">
      <header>
        <h1>设备管理</h1>
      </header>

      {loading && <div className="loading">加载中...</div>}

      {error && <div className="error">{error}</div>}

      {!loading && !error && devices.length === 0 && (
        <div className="empty-state">暂无设备数据</div>
      )}

      {!loading && !error && devices.length > 0 && (
        <>
          <table className="devices-table">
            <thead>
              <tr>
                <th>设备名称</th>
                <th>平台</th>
                <th>最后活跃</th>
                <th>状态</th>
              </tr>
            </thead>
            <tbody>
              {devices.map((device) => (
                <tr key={device.device_id}>
                  <td>{device.display_name || `设备 #${device.slot_no}`}</td>
                  <td>
                    <span className={`platform-badge platform-${device.platform}`}>
                      {platformLabels[device.platform] || device.platform}
                    </span>
                  </td>
                  <td>
                    {device.bound_at
                      ? new Date(device.bound_at).toLocaleString("zh-CN")
                      : "—"}
                  </td>
                  <td>
                    <span className={`status-badge status-${device.status}`}>
                      {statusLabels[device.status] || device.status}
                    </span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>

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
    </div>
  );
}
