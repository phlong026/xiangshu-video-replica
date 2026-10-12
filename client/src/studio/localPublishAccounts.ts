import { invoke, isTauri } from "@tauri-apps/api/core";
import {
  captureWorkspaceCredentialContext,
  publishBrowserRequest,
} from "../api";
import type { PublishPlatform } from "./PlatformLogo";

export type LocalPublishAccount = {
  id: string;
  platform: PublishPlatform;
  platform_user_id: string;
  username: string;
  verified_at: number;
  /** Desktop: the platform CDN link. Cloud: our re-hosted copy. Absent on older records. */
  avatar_url?: string | null;
};
export type PublishIdentity = {
  platform_user_id: string;
  username: string;
  avatar_url?: string | null;
};
export type CloudPublishAccount = LocalPublishAccount & {
  status: "connected" | "invalid";
  error_message: string | null;
  source: "cloud" | "desktop";
};
export type PublishStorageState = {
  cookies: Record<string, unknown>[];
  origins: Record<string, unknown>[];
};
export type LocalPublishLoginStatus = {
  phase:
    | "loading"
    | "qr_ready"
    | "confirming"
    | "action_required"
    | "expired"
    | "closed"
    | "connected";
  image: string | null;
  account: LocalPublishAccount | null;
  message?: string;
  /** Desktop only: one-shot login export to hand to the server-side worker. */
  storage_state?: PublishStorageState | null;
};
// Native profiles and cookie export are implemented with Windows WebView2.
// Other desktop platforms use the existing encrypted cloud login flow.
export const canUseLocalPublishAccounts = () =>
  isTauri() && navigator.userAgent.includes("Windows");
export const isPublishStorageState = (
  value: unknown,
): value is PublishStorageState =>
  typeof value === "object" &&
  value !== null &&
  Array.isArray((value as PublishStorageState).cookies) &&
  Array.isArray((value as PublishStorageState).origins) &&
  (value as PublishStorageState).cookies.length > 0;
async function command<T>(
  name: string,
  args: Record<string, unknown>,
): Promise<T> {
  if (!canUseLocalPublishAccounts())
    throw new Error("此操作需要 Windows 桌面客户端的本机账号功能。");
  try {
    return await invoke<T>(name, args);
  } catch (error) {
    throw error instanceof Error ? error : new Error(String(error));
  }
}
export const listLocalPublishAccounts = (owner: string) =>
  canUseLocalPublishAccounts()
    ? command<LocalPublishAccount[]>("list_local_publish_accounts", { owner })
    : cloudAccounts();
export const startLocalPublishLogin = (
  owner: string,
  platform: PublishPlatform,
  accountId?: string,
) =>
  canUseLocalPublishAccounts()
    ? command<string>("start_local_publish_login", {
        owner,
        platform,
        accountId: accountId ?? null,
      })
    : startCloudLogin(owner, platform, accountId);
export const checkLocalPublishLogin = (owner: string, loginId: string) =>
  canUseLocalPublishAccounts()
    ? command<LocalPublishLoginStatus>("check_local_publish_login", {
        owner,
        loginId,
      })
    : Promise.resolve(cloudLogin(owner, loginId).status);
export const focusLocalPublishLogin = (owner: string, loginId: string) =>
  command<void>("focus_local_publish_login", { owner, loginId });
export const cancelLocalPublishLogin = (owner: string, loginId: string) =>
  canUseLocalPublishAccounts()
    ? command<void>("cancel_local_publish_login", { owner, loginId })
    : cancelCloudLogin(owner, loginId);
export const removeLocalPublishAccount = (owner: string, accountId: string) =>
  canUseLocalPublishAccounts()
    ? command<void>("remove_local_publish_account", { owner, accountId })
    : cloudDeleteAccount(accountId);
export const openLocalPublishAccount = (owner: string, accountId: string) =>
  command<void>("open_local_publish_account", { owner, accountId });
/** Re-export a connected desktop profile (hidden window) for the server-side worker. */
export const exportLocalPublishAccountState = (
  owner: string,
  accountId: string,
) =>
  command<{
    identity: PublishIdentity;
    storage_state: PublishStorageState;
  }>("export_local_publish_account_state", { owner, accountId });

/** Server-side accounts (cloud QR logins and imported desktop logins) — the delivery source. */
export const listCloudPublishAccounts = (): Promise<CloudPublishAccount[]> =>
  cloudAccounts() as Promise<CloudPublishAccount[]>;
export async function importCloudPublishAccount(
  platform: PublishPlatform,
  identity: PublishIdentity,
  storageState: PublishStorageState,
): Promise<CloudPublishAccount> {
  const response = await publishBrowserRequest(`${cloudBase}/accounts/import`, {
    method: "POST",
    body: JSON.stringify({ platform, identity, storage_state: storageState }),
    signal: AbortSignal.timeout(20000),
  });
  return (await response.json()) as CloudPublishAccount;
}
export async function deleteCloudPublishAccount(
  accountId: string,
): Promise<void> {
  await cloudDeleteAccount(accountId);
}

export const PUBLISH_ACCOUNTS_CHANGED =
  "video-replica:publish-accounts-changed";
const PUBLISH_ACCOUNTS_REVISION = "video-replica:publish-accounts-revision";

export function onPublishAccountsChanged(listener: () => void): () => void {
  const changed = (event: StorageEvent) => {
    if (event.key === PUBLISH_ACCOUNTS_REVISION) listener();
  };
  window.addEventListener(PUBLISH_ACCOUNTS_CHANGED, listener);
  window.addEventListener("storage", changed);
  return () => {
    window.removeEventListener(PUBLISH_ACCOUNTS_CHANGED, listener);
    window.removeEventListener("storage", changed);
  };
}

function publishAccountsChanged() {
  window.dispatchEvent(new Event(PUBLISH_ACCOUNTS_CHANGED));
  try {
    localStorage.setItem(PUBLISH_ACCOUNTS_REVISION, crypto.randomUUID());
  } catch {
    // 浏览器存储不可用不改变服务端解绑结果，同窗口仍收到刷新通知。
  }
}

/** 两处确认后才返回成功；失败时保留本机账号，供原目标重试。 */
export async function disconnectPublishAccount(
  owner: string,
  account: LocalPublishAccount,
): Promise<void> {
  const native = canUseLocalPublishAccounts();
  const requireSession = captureWorkspaceCredentialContext();
  const cloudTargets = async () => {
    requireSession();
    const targets = await listCloudPublishAccounts();
    requireSession();
    return targets;
  };
  const matches = (item: CloudPublishAccount) =>
    native
      ? item.platform === account.platform &&
        item.platform_user_id === account.platform_user_id
      : item.id === account.id;
  try {
    const targets = (await cloudTargets()).filter(matches);
    for (const target of targets) {
      try {
        requireSession();
        await deleteCloudPublishAccount(target.id);
        requireSession();
      } catch (cause) {
        // DELETE 可能已成功但响应丢失；必须重读确认，不能吞错当成功。
        if ((await cloudTargets()).some(matches)) throw cause;
      }
    }
    if (targets.length && (await cloudTargets()).some(matches))
      throw new Error("云端账号仍存在，请重试解绑。");
  } catch (cause) {
    throw new Error(
      `云端解绑尚未确认，${native ? "本机登录状态已保留" : "请重试"}。${cause instanceof Error ? cause.message : "读取云端账号失败。"}`,
    );
  }
  requireSession();
  publishAccountsChanged();
  if (native) {
    try {
      await removeLocalPublishAccount(owner, account.id);
    } catch (cause) {
      // 双窗口可能已清理同一账号，重读本机列表后才能确认完成。
      try {
        if (
          !(await listLocalPublishAccounts(owner)).some(
            (item) => item.id === account.id,
          )
        )
          return;
      } catch {
        // 本机状态不可读仍按未完成处理，保留重试入口。
      }
      throw new Error(
        `云端已解绑，本机清理未完成，请重试。${cause instanceof Error ? cause.message : ""}`,
      );
    }
  }
}

type CloudLogin = {
  owner: string;
  status: LocalPublishLoginStatus;
  controller: AbortController;
  serverId?: string;
};
const cloudLogins = new Map<string, CloudLogin>();
const cloudBase = "/api/studio/publish/browser";
async function cloudAccounts(): Promise<LocalPublishAccount[]> {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 10000);
  try {
    return await (
      await publishBrowserRequest(`${cloudBase}/accounts`, {
        signal: controller.signal,
      })
    ).json();
  } finally {
    clearTimeout(timeout);
  }
}
async function cloudDeleteAccount(accountId: string): Promise<void> {
  await publishBrowserRequest(
    `${cloudBase}/accounts/${encodeURIComponent(accountId)}`,
    { method: "DELETE", signal: AbortSignal.timeout(10000) },
  );
}
function cloudLogin(owner: string, id: string): CloudLogin {
  const login = cloudLogins.get(id);
  if (!login || login.owner !== owner)
    throw new Error("扫码会话不存在，请重新扫码。");
  return login;
}
async function startCloudLogin(
  owner: string,
  platform: PublishPlatform,
  accountId?: string,
): Promise<string> {
  const id = crypto.randomUUID();
  const login: CloudLogin = {
    owner,
    controller: new AbortController(),
    status: { phase: "loading", image: null, account: null },
  };
  cloudLogins.set(id, login);
  void consumeCloudLogin(id, login, platform, accountId);
  return id;
}
async function consumeCloudLogin(
  id: string,
  login: CloudLogin,
  platform: PublishPlatform,
  accountId?: string,
): Promise<void> {
  const timeout = setTimeout(() => login.controller.abort(), 310000);
  try {
    const response = await publishBrowserRequest(`${cloudBase}/logins`, {
      method: "POST",
      body: JSON.stringify({ platform, account_id: accountId ?? null }),
      signal: login.controller.signal,
    });
    if (!response.body)
      throw new Error("浏览器不支持扫码连接，请更换浏览器重试。");
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    try {
      while (cloudLogins.get(id) === login) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        if (buffer.length > 1250000)
          throw new Error("二维码响应过大，请重试。");
        let end = buffer.indexOf("\n");
        while (end >= 0) {
          const event = JSON.parse(
            buffer.slice(0, end),
          ) as LocalPublishLoginStatus & { login_id?: string };
          buffer = buffer.slice(end + 1);
          if (
            ![
              "loading",
              "qr_ready",
              "confirming",
              "action_required",
              "expired",
              "closed",
              "connected",
            ].includes(event.phase)
          )
            throw new Error("扫码响应无效，请重试。");
          if (event.login_id) login.serverId = event.login_id;
          if (cloudLogins.get(id) === login) login.status = event;
          end = buffer.indexOf("\n");
        }
      }
    } finally {
      await reader.cancel();
    }
    if (!["connected", "expired", "closed"].includes(login.status.phase))
      throw new Error("扫码连接已中断，请重新获取二维码。");
  } catch (cause) {
    if (cloudLogins.get(id) === login)
      login.status = {
        phase: "closed",
        image: null,
        account: null,
        message:
          cause instanceof Error && cause.name !== "AbortError"
            ? cause.message
            : "扫码连接超时，请重新获取二维码。",
      };
  } finally {
    clearTimeout(timeout);
    // Terminal account/QR data must not remain in a long-lived module cache.
    setTimeout(() => {
      if (cloudLogins.get(id) === login) cloudLogins.delete(id);
    }, 30000);
  }
}
async function cancelCloudLogin(owner: string, id: string): Promise<void> {
  const login = cloudLogins.get(id);
  if (!login) return;
  if (login.owner !== owner) throw new Error("扫码会话不存在。");
  try {
    if (login.serverId)
      await publishBrowserRequest(
        `${cloudBase}/logins/${encodeURIComponent(login.serverId)}`,
        { method: "DELETE", signal: AbortSignal.timeout(10000) },
      );
  } finally {
    login.controller.abort();
    // Keep a terminal status on failed DELETE so the panel can retry cancellation.
    login.status = { phase: "closed", image: null, account: null };
  }
  cloudLogins.delete(id);
}
