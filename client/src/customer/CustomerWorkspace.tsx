import { useCallback, useEffect, useState } from "react";
import { WorkspaceShell } from "../App";
import {
  CustomerApiError,
  type CustomerDeviceListResponse,
  customerApproveDevicePairing,
  customerListDevices,
  customerUnbindDevice,
} from "../api";
import { customerToCurrentUser } from "../RootApp";
import { DeviceManagementPage } from "./DeviceManagementPage";
import { PairingApprovalCard } from "./PairingApprovalCard";
import type {
  CustomerCredentialStore,
  CustomerWorkspaceUser,
} from "./useCustomerSession";

/**
 * T31 / FE-04 — the customer workspace: the shared generation shell plus the
 * device-management view that M4 shipped as an orphan component.
 *
 * The view toggles between the generation workspace and the two-slot device
 * page; the device page loads the T16 contract through the customer API
 * adapter (device credential from the vault, never a second master code).
 * A 401 on any device call falls back to the session-expired screen.
 */
export function CustomerWorkspace({
  user,
  store,
  onSessionExpired,
}: {
  user: CustomerWorkspaceUser;
  store: CustomerCredentialStore;
  onSessionExpired: () => void;
}) {
  // The generation workspace is the landing view: a freshly activated or
  // logged-in customer is here to work, not to manage devices; the header
  // button flips to the device page when device management is needed.
  const [view, setView] = useState<"workspace" | "devices">("workspace");
  const [devices, setDevices] = useState<CustomerDeviceListResponse | null>(
    null,
  );
  const [deviceError, setDeviceError] = useState("");

  const loadDevices = useCallback(async () => {
    const token = await store.loadDeviceCredentialToken();
    if (token === null) {
      onSessionExpired();
      return;
    }
    try {
      const response = await customerListDevices({ kind: "device", token });
      setDevices(response);
      setDeviceError("");
    } catch (cause) {
      if (cause instanceof CustomerApiError && cause.status === 401) {
        onSessionExpired();
        return;
      }
      setDeviceError(
        cause instanceof Error && cause.message
          ? cause.message
          : "设备列表加载失败",
      );
    }
  }, [store, onSessionExpired]);

  useEffect(() => {
    void loadDevices();
  }, [loadDevices]);

  // Entering the device view re-fetches: a second device's enroll may have
  // landed a PENDING pairing after the workspace first loaded, and the
  // approval list must show it (T30 / FE-03 approval reachability).
  useEffect(() => {
    if (view === "devices") {
      void loadDevices();
    }
  }, [view, loadDevices]);

  async function handleUnbind(deviceId: string) {
    const token = await store.loadDeviceCredentialToken();
    if (token === null) {
      onSessionExpired();
      return;
    }
    try {
      await customerUnbindDevice({ kind: "device", token }, deviceId, {
        idempotencyKey: crypto.randomUUID(),
      });
      await loadDevices();
    } catch (cause) {
      if (cause instanceof CustomerApiError && cause.status === 401) {
        onSessionExpired();
        return;
      }
      setDeviceError(
        cause instanceof Error && cause.message
          ? cause.message
          : "解绑设备失败",
      );
    }
  }

  async function handleApprovePairing(pairingId: string) {
    const token = await store.loadDeviceCredentialToken();
    if (token === null) {
      onSessionExpired();
      return;
    }
    try {
      await customerApproveDevicePairing({ kind: "device", token }, pairingId);
      await loadDevices();
    } catch (cause) {
      if (cause instanceof CustomerApiError && cause.status === 401) {
        onSessionExpired();
        return;
      }
      setDeviceError(
        cause instanceof Error && cause.message
          ? cause.message
          : "审批配对失败",
      );
    }
  }

  if (view === "devices") {
    const pendingPairings = devices?.pending_pairings ?? [];
    return (
      <div className="customer-workspace">
        <header className="customer-workspace-header">
          <span className="eyebrow">JINGXU STUDIO</span>
          <div>
            <button
              type="button"
              className="btn-secondary"
              onClick={() => setView("workspace")}
            >
              进入工作区
            </button>
          </div>
        </header>
        {deviceError ? (
          <p className="settings-error" role="alert">
            {deviceError}
          </p>
        ) : null}
        {devices !== null ? (
          <>
            {pendingPairings.length > 0 ? (
              <section
                className="pending-pairings"
                aria-label="待审批的配对请求"
              >
                <h2>待审批配对</h2>
                {pendingPairings.map((pending) => (
                  <PairingApprovalCard
                    key={pending.pairing_request_id}
                    pairing={{
                      id: pending.pairing_request_id,
                      deviceFingerprint: `${pending.display_name} · ${pending.platform}`,
                      createdAt: pending.created_at,
                    }}
                    onApprove={(id) => void handleApprovePairing(id)}
                    onReject={() =>
                      setDeviceError(
                        "暂不支持拒绝配对,请联系客服处理未授权的配对请求",
                      )
                    }
                  />
                ))}
              </section>
            ) : null}
            <DeviceManagementPage
              devices={devices}
              isOnline
              leaseExpiresAt={null}
              onUnbind={(deviceId) => void handleUnbind(deviceId)}
              onError={(error) => setDeviceError(error.message)}
              onRecharge={() =>
                setDeviceError("续充请联系客服或在工作区使用钱包功能")
              }
            />
          </>
        ) : null}
      </div>
    );
  }

  return (
    <div className="customer-workspace">
      <header className="customer-workspace-header">
        <span className="eyebrow">JINGXU STUDIO</span>
        <div>
          <button
            type="button"
            className="btn-secondary"
            onClick={() => setView("devices")}
          >
            设备管理
          </button>
        </div>
      </header>
      <WorkspaceShell currentUser={customerToCurrentUser(user)} />
    </div>
  );
}
