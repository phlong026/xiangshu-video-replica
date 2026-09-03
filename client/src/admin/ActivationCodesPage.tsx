import { Fragment, useCallback, useEffect, useState } from "react";

import {
  type ActivationCodeDevice,
  type ActivationCodeListItem,
  type ActivationCodePendingPairing,
  AdminActivationError,
  adminActivationErrorMessage,
  approveDevicePairing,
  archiveActivationCode,
  createIdempotencyKey,
  listActivationCodes,
  replaceDeviceForPairing,
  resumeActivationCode,
  revealActivationCode,
  revokeActivationCode,
  revokeDeviceCredential,
  suspendActivationCode,
  unbindDevice,
} from "../api.admin";
import { ConfirmDialog, type ConfirmLevel } from "./ui/ConfirmDialog";
import { PageBanner } from "./ui/PageBanner";
import { Pagination } from "./ui/Pagination";
import {
  ActivationCodeStatusBadge,
  DeviceStatusBadge,
  PlatformBadge,
} from "./ui/StatusBadge";
import {
  activationCodeStatusLabel,
  formatDateTime,
  platformLabel,
} from "./ui/vocabulary";

type CodeAction = "revoke" | "archive" | "suspend" | "resume";

const PAGE_SIZE = 50;

type PendingAction =
  | { kind: "code"; action: CodeAction; codeId: string; status: string }
  | {
      kind: "device";
      action: "unbind" | "revoke";
      codeId: string;
      deviceId: string;
      deviceName: string;
    }
  | {
      kind: "pairing";
      codeId: string;
      pairingId: string;
      candidateName: string;
      replaceDeviceId?: string;
      replaceDeviceName?: string;
    };

export function ActivationCodesPage({
  readOnly = false,
  refreshToken = 0,
  onSessionExpired,
}: {
  readOnly?: boolean;
  refreshToken?: number;
  onSessionExpired?: () => void;
}) {
  const [items, setItems] = useState<ActivationCodeListItem[]>([]);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [searchDraft, setSearchDraft] = useState("");
  const [search, setSearch] = useState("");
  const [filterStatus, setFilterStatus] = useState("");
  const [includeArchived, setIncludeArchived] = useState(false);
  const [expandedCodeId, setExpandedCodeId] = useState<string | null>(null);
  const [pending, setPending] = useState<PendingAction | null>(null);
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState("");
  const [copyingCodeId, setCopyingCodeId] = useState<string | null>(null);
  const [actionKey, setActionKey] = useState<string | null>(null);

  const handleFailure = useCallback(
    (cause: unknown, fallback: string) => {
      if (cause instanceof AdminActivationError && cause.status === 401) {
        setError("会话已失效，请重新登录");
        onSessionExpired?.();
        return;
      }
      setError(adminActivationErrorMessage(cause, fallback));
    },
    [onSessionExpired],
  );

  useEffect(() => {
    void refreshToken;
    let cancelled = false;
    void (async () => {
      setError("");
      setLoading(true);
      try {
        const page = await listActivationCodes({
          status: filterStatus || undefined,
          search: search || undefined,
          include_archived: includeArchived || undefined,
          limit: PAGE_SIZE,
          offset,
        });
        if (!cancelled) {
          setItems(page.items);
          setTotal(page.total);
        }
      } catch (cause) {
        if (!cancelled) {
          handleFailure(cause, "读取激活码与设备失败");
        }
      } finally {
        if (!cancelled) {
          setLoading(false);
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [
    filterStatus,
    search,
    includeArchived,
    offset,
    handleFailure,
    refreshToken,
  ]);

  // 搜索（A9）已下沉到服务端：掩码码或绑定用户名匹配，跨全部页生效。
  const visibleItems = items;

  async function copyActivationCode(codeId: string) {
    setError("");
    setNotice("");
    setCopyingCodeId(codeId);
    try {
      const result = await revealActivationCode(
        codeId,
        "后台复制激活码",
        createIdempotencyKey(),
      );
      await navigator.clipboard.writeText(result.activation_code);
      setNotice(`激活码已复制（request id: ${result.request_id}）`);
    } catch (cause) {
      handleFailure(cause, "复制激活码失败");
    } finally {
      setCopyingCodeId(null);
    }
  }

  function openAction(action: PendingAction) {
    setPending(action);
    setActionKey(null);
    setError("");
    setNotice("");
    setActionError("");
  }

  async function submitAction(reason: string) {
    if (!pending || busy) {
      return;
    }

    setBusy(true);
    setActionError("");
    try {
      if (pending.kind === "code") {
        const key = actionKey ?? createIdempotencyKey();
        setActionKey(key);
        if (pending.action === "archive") {
          const result = await archiveActivationCode(
            pending.codeId,
            reason,
            key,
          );
          setItems((current) =>
            current.filter((item) => item.code_id !== result.code_id),
          );
          setNotice(
            `激活码已删除并保留审计记录（request id: ${result.request_id}）`,
          );
        } else {
          const mutate =
            pending.action === "revoke"
              ? revokeActivationCode
              : pending.action === "suspend"
                ? suspendActivationCode
                : resumeActivationCode;
          const result = await mutate(pending.codeId, reason, key);
          setItems((current) =>
            current.map((item) =>
              item.code_id === result.code_id
                ? { ...item, status: result.status }
                : item,
            ),
          );
          setNotice(
            `${activationCodeStatusLabel(result.status)}（request id: ${result.request_id}）`,
          );
        }
      } else if (pending.kind === "device") {
        const result =
          pending.action === "unbind"
            ? await unbindDevice(pending.deviceId, reason)
            : await revokeDeviceCredential(pending.deviceId, reason);
        setItems((current) =>
          updateDeviceStatus(
            current,
            pending.codeId,
            pending.deviceId,
            result.status,
          ),
        );
        setNotice(
          `${pending.action === "unbind" ? "设备已解绑" : "设备已强制退出"}（request id: ${result.request_id}）`,
        );
      } else {
        const key = actionKey ?? createIdempotencyKey();
        setActionKey(key);
        const result = pending.replaceDeviceId
          ? await replaceDeviceForPairing(
              pending.pairingId,
              pending.replaceDeviceId,
              reason,
              key,
            )
          : await approveDevicePairing(pending.pairingId, reason, key);
        setItems((current) =>
          current.map((item) =>
            item.code_id !== pending.codeId
              ? item
              : {
                  ...item,
                  devices: pending.replaceDeviceId
                    ? item.devices.map((device) =>
                        device.device_id === pending.replaceDeviceId
                          ? { ...device, status: "UNBOUND" }
                          : device,
                      )
                    : item.devices,
                  pending_pairings: item.pending_pairings.filter(
                    (pairing) =>
                      pairing.pairing_request_id !== pending.pairingId,
                  ),
                },
          ),
        );
        setNotice(`设备申请已批准（request id: ${result.request_id}）`);
      }
      setPending(null);
      setActionKey(null);
    } catch (cause) {
      const fallback =
        pending.kind === "code"
          ? CODE_ACTION_FALLBACK[pending.action]
          : pending.kind === "pairing"
            ? "处理设备申请失败"
            : "设备操作失败";
      if (cause instanceof AdminActivationError && cause.status === 401) {
        setError("会话已失效，请重新登录");
        onSessionExpired?.();
      } else if (cause instanceof AdminActivationError) {
        setActionError(adminActivationErrorMessage(cause, fallback));
      } else {
        setError(adminActivationErrorMessage(cause, fallback));
      }
      if (cause instanceof AdminActivationError && cause.status !== undefined) {
        // 明确失败释放幂等键；超时等模糊失败保留键以便重试重放。
        setActionKey(null);
      }
    } finally {
      setBusy(false);
    }
  }

  return (
    <section
      className="admin-panel activation-device-page"
      aria-label="激活码与设备"
    >
      <header className="admin-page-header">
        <div>
          <h2>激活码与设备</h2>
          <p>
            在同一处查看激活码的使用账号、关联设备，并执行复制、暂停恢复、撤销或设备解绑。
          </p>
        </div>
      </header>

      {readOnly ? (
        <PageBanner tone="notice">当前为只读模式，写操作不可用。</PageBanner>
      ) : null}
      {loading ? <p className="admin-hint">正在读取激活码…</p> : null}
      {error ? <PageBanner tone="error">{error}</PageBanner> : null}
      {notice ? <PageBanner tone="notice">{notice}</PageBanner> : null}

      <form
        className="admin-filters activation-device-filters"
        onSubmit={(event) => {
          event.preventDefault();
          setOffset(0);
          setSearch(searchDraft.trim());
        }}
      >
        <label>
          搜索
          <input
            placeholder="激活码或绑定账号（服务端检索）"
            value={searchDraft}
            onChange={(event) => setSearchDraft(event.target.value)}
          />
        </label>
        <label>
          状态
          <select
            value={filterStatus}
            onChange={(event) => {
              setOffset(0);
              setFilterStatus(event.target.value);
            }}
          >
            <option value="">全部</option>
            <option value="GENERATED">待启用</option>
            <option value="ISSUED">可使用</option>
            <option value="ACTIVE">使用中</option>
            <option value="SUSPENDED">已暂停</option>
            <option value="REVOKED">已撤销</option>
          </select>
        </label>
        <label>
          <input
            checked={includeArchived}
            type="checkbox"
            onChange={(event) => {
              setOffset(0);
              setIncludeArchived(event.target.checked);
            }}
          />
          显示已归档
        </label>
        <button type="submit">搜索</button>
      </form>

      <div className="table-scroll admin-table-card">
        <table
          aria-label="激活码列表"
          className="internal-table admin-data-table"
        >
          <thead>
            <tr>
              <th>激活码</th>
              <th>状态</th>
              <th>绑定账号</th>
              <th>关联设备</th>
              <th>最近活跃</th>
              {readOnly ? null : <th>操作</th>}
            </tr>
          </thead>
          <tbody>
            {visibleItems.map((item) => {
              const expanded = expandedCodeId === item.code_id;
              return (
                <Fragment key={item.code_id}>
                  <tr>
                    <td>
                      <code>{item.masked_code}</code>
                      {readOnly ? null : (
                        <button
                          type="button"
                          disabled={copyingCodeId !== null}
                          onClick={() => void copyActivationCode(item.code_id)}
                        >
                          {copyingCodeId === item.code_id ? "复制中…" : "复制"}
                        </button>
                      )}
                    </td>
                    <td>
                      <ActivationCodeStatusBadge status={item.status} />
                      {item.archived_at ? (
                        <span className="status-badge status-badge--neutral">
                          已归档
                        </span>
                      ) : null}
                    </td>
                    <td>{item.bound_username ?? "未绑定"}</td>
                    <td>
                      <button
                        className="activation-device-toggle"
                        type="button"
                        aria-expanded={expanded}
                        onClick={() =>
                          setExpandedCodeId(expanded ? null : item.code_id)
                        }
                      >
                        {item.devices.length} 台设备
                      </button>
                    </td>
                    <td>{formatDateTime(latestActivity(item.devices))}</td>
                    {readOnly ? null : (
                      <td>
                        <div className="admin-actions admin-actions--table">
                          {item.status === "ACTIVE" ? (
                            <button
                              type="button"
                              onClick={() =>
                                openAction({
                                  kind: "code",
                                  action: "suspend",
                                  codeId: item.code_id,
                                  status: item.status,
                                })
                              }
                            >
                              暂停
                            </button>
                          ) : null}
                          {item.status === "SUSPENDED" ? (
                            <button
                              type="button"
                              onClick={() =>
                                openAction({
                                  kind: "code",
                                  action: "resume",
                                  codeId: item.code_id,
                                  status: item.status,
                                })
                              }
                            >
                              恢复
                            </button>
                          ) : null}
                          {item.status === "REVOKED" ? (
                            <button
                              className="admin-action--danger"
                              type="button"
                              onClick={() =>
                                openAction({
                                  kind: "code",
                                  action: "archive",
                                  codeId: item.code_id,
                                  status: item.status,
                                })
                              }
                            >
                              删除激活码
                            </button>
                          ) : (
                            <button
                              className="admin-action--danger"
                              type="button"
                              onClick={() =>
                                openAction({
                                  kind: "code",
                                  action: "revoke",
                                  codeId: item.code_id,
                                  status: item.status,
                                })
                              }
                            >
                              撤销激活码
                            </button>
                          )}
                        </div>
                      </td>
                    )}
                  </tr>
                  {expanded ? (
                    <tr className="admin-detail-row activation-device-detail-row">
                      <td colSpan={readOnly ? 5 : 6}>
                        <DeviceList
                          codeId={item.code_id}
                          devices={item.devices}
                          readOnly={readOnly}
                          onAction={openAction}
                        />
                        <PairingList
                          codeId={item.code_id}
                          devices={item.devices}
                          pairings={item.pending_pairings}
                          readOnly={readOnly}
                          onAction={openAction}
                        />
                      </td>
                    </tr>
                  ) : null}
                </Fragment>
              );
            })}
          </tbody>
        </table>
      </div>
      {visibleItems.length === 0 ? (
        <p className="activation-device-empty">没有符合条件的激活码。</p>
      ) : null}

      <Pagination
        limit={PAGE_SIZE}
        offset={offset}
        total={total}
        onPageChange={setOffset}
      />

      <ConfirmDialog
        busy={busy}
        confirmLabel="确认执行"
        description={pending ? actionWarning(pending) : null}
        error={actionError}
        level={confirmLevel(pending)}
        open={pending !== null}
        title={pending ? actionTitle(pending) : ""}
        onClose={() => {
          setPending(null);
          setActionError("");
        }}
        onConfirm={(reason: string) => void submitAction(reason)}
      />
    </section>
  );
}

const CODE_ACTION_FALLBACK: Record<CodeAction, string> = {
  archive: "删除激活码失败",
  revoke: "撤销激活码失败",
  suspend: "暂停激活码失败",
  resume: "恢复激活码失败",
};

/** 吊销/删除/吊销凭据是不可逆终态：要求原因 + 勾选；其余要求原因。 */
function confirmLevel(pending: PendingAction | null): ConfirmLevel {
  if (pending === null) {
    return "reason";
  }
  if (pending.kind === "code") {
    return pending.action === "revoke" || pending.action === "archive"
      ? "reasonAndAck"
      : "reason";
  }
  if (pending.kind === "device") {
    return pending.action === "revoke" ? "reasonAndAck" : "reason";
  }
  return pending.replaceDeviceId ? "reasonAndAck" : "reason";
}

function PairingList({
  codeId,
  devices,
  pairings,
  readOnly,
  onAction,
}: {
  codeId: string;
  devices: ActivationCodeDevice[];
  pairings: ActivationCodePendingPairing[];
  readOnly: boolean;
  onAction: (action: PendingAction) => void;
}) {
  if (pairings.length === 0) {
    return null;
  }
  const boundDevices = devices.filter((device) => device.status === "BOUND");
  // PR #85 评审 P2：普通批准通道的开关是"首个设备（槽位 1）是否仍 BOUND"
  // （服务端 §12.2 step 3 语义），不是"是否还有任何绑定设备"——槽位 1 已
  // 解绑/撤销而槽位 2 仍绑定时，直接批准可用，不必先替换掉健康的槽位 2。
  const firstDeviceBound = devices.some(
    (device) => device.slot_no === 1 && device.status === "BOUND",
  );
  return (
    <section aria-label="待处理设备申请" className="pairing-pending">
      <h3>待处理设备申请</h3>
      {pairings.map((pairing) => (
        <article key={pairing.pairing_request_id}>
          <strong>{pairing.display_name}</strong>
          <p>
            {platformLabel(pairing.platform)} · 申请于{" "}
            {formatDateTime(pairing.created_at)}
          </p>
          <small>配对编号：{pairing.pairing_request_id}</small>
          {pairing.status === "APPROVED" ? (
            <p>已批准，等待新设备完成绑定。</p>
          ) : readOnly ? null : !firstDeviceBound ? (
            <button
              type="button"
              onClick={() =>
                onAction({
                  kind: "pairing",
                  codeId,
                  pairingId: pairing.pairing_request_id,
                  candidateName: pairing.display_name,
                })
              }
            >
              批准设备
            </button>
          ) : null}
          {pairing.status !== "APPROVED" && !readOnly && boundDevices.length > 0
            ? boundDevices.map((device) => (
                <button
                  type="button"
                  key={device.device_id}
                  onClick={() =>
                    onAction({
                      kind: "pairing",
                      codeId,
                      pairingId: pairing.pairing_request_id,
                      candidateName: pairing.display_name,
                      replaceDeviceId: device.device_id,
                      replaceDeviceName:
                        device.display_name || `设备 #${device.slot_no}`,
                    })
                  }
                >
                  {boundDevices.length === 1
                    ? `替换为${pairing.display_name}`
                    : `用${pairing.display_name}替换${device.display_name || `设备 #${device.slot_no}`}`}
                </button>
              ))
            : null}
        </article>
      ))}
    </section>
  );
}

function DeviceList({
  codeId,
  devices,
  readOnly,
  onAction,
}: {
  codeId: string;
  devices: ActivationCodeDevice[];
  readOnly: boolean;
  onAction: (action: PendingAction) => void;
}) {
  if (devices.length === 0) {
    return <p className="activation-device-empty">该激活码尚未关联设备。</p>;
  }
  return (
    <table aria-label="关联设备列表" className="activation-device-subtable">
      <thead>
        <tr>
          <th>设备</th>
          <th>平台</th>
          <th>状态</th>
          <th>绑定时间</th>
          <th>最近活跃</th>
          {readOnly ? null : <th>设备操作</th>}
        </tr>
      </thead>
      <tbody>
        {devices.map((device) => {
          const active = device.status === "BOUND";
          const deviceName = device.display_name || `设备 #${device.slot_no}`;
          return (
            <tr key={device.device_id}>
              <td>{deviceName}</td>
              <td>
                <PlatformBadge platform={device.platform} />
              </td>
              <td>
                <DeviceStatusBadge status={device.status} />
              </td>
              <td>{formatDateTime(device.bound_at)}</td>
              <td>{formatDateTime(device.last_active_at)}</td>
              {readOnly ? null : (
                <td>
                  {active ? (
                    <div className="admin-actions admin-actions--table">
                      <button
                        type="button"
                        onClick={() =>
                          onAction({
                            kind: "device",
                            action: "unbind",
                            codeId,
                            deviceId: device.device_id,
                            deviceName,
                          })
                        }
                      >
                        解绑设备
                      </button>
                      <button
                        className="admin-action--danger"
                        type="button"
                        onClick={() =>
                          onAction({
                            kind: "device",
                            action: "revoke",
                            codeId,
                            deviceId: device.device_id,
                            deviceName,
                          })
                        }
                      >
                        强制退出
                      </button>
                    </div>
                  ) : (
                    "无需操作"
                  )}
                </td>
              )}
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

function updateDeviceStatus(
  items: ActivationCodeListItem[],
  codeId: string,
  deviceId: string,
  status: string,
): ActivationCodeListItem[] {
  return items.map((item) =>
    item.code_id === codeId
      ? {
          ...item,
          devices: item.devices.map((device) =>
            device.device_id === deviceId ? { ...device, status } : device,
          ),
        }
      : item,
  );
}

function latestActivity(devices: ActivationCodeDevice[]): string | null {
  return devices.reduce<string | null>((latest, device) => {
    if (!device.last_active_at) {
      return latest;
    }
    return !latest || device.last_active_at > latest
      ? device.last_active_at
      : latest;
  }, null);
}

function actionTitle(action: PendingAction): string {
  if (action.kind === "code") {
    if (action.action === "archive") {
      return `删除激活码 ${action.codeId}`;
    }
    if (action.action === "revoke") {
      return `撤销激活码 ${action.codeId}`;
    }
    return `${action.action === "suspend" ? "暂停" : "恢复"}激活码 ${action.codeId}`;
  }
  if (action.kind === "pairing") {
    return action.replaceDeviceId
      ? `用${action.candidateName}替换${action.replaceDeviceName}`
      : `批准设备 ${action.candidateName}`;
  }
  return `${action.action === "unbind" ? "解绑" : "强制退出"} ${action.deviceName}`;
}

function actionWarning(action: PendingAction): string {
  if (action.kind === "code") {
    if (action.action === "archive") {
      return "删除后将从日常列表隐藏，但设备、充值和审计记录会继续保留。";
    }
    if (action.action === "revoke") {
      return "撤销后该激活码不可恢复，已绑定设备也不能再用它重新登录。";
    }
    return action.action === "suspend"
      ? "暂停后该激活码下的账号无法登录生成，恢复后立即生效。"
      : "恢复后该激活码下的账号可以重新登录生成。";
  }
  if (action.kind === "pairing") {
    return action.replaceDeviceId
      ? "旧设备将被解绑，新设备获批后会自动完成绑定。"
      : "该申请获批后，新设备会自动完成绑定。";
  }
  return action.action === "unbind"
    ? "设备将解除当前绑定，但不会撤销激活码。"
    : "当前设备凭据将立即失效，激活码本身保持原状态。";
}
