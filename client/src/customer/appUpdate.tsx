import { invoke } from "@tauri-apps/api/core";
import { check, type Update } from "@tauri-apps/plugin-updater";
import { type ReactNode, useEffect, useSyncExternalStore } from "react";
import { UpdateDialog } from "./UpdateDialog";
import { isTauriRuntime } from "./useCustomerSession";

/**
 * 桌面端自动更新（仅客户 lane；浏览器版没有自动更新能力）。
 *
 * 状态机：idle → checking → available → downloading → installed → 重启。
 * 检查/下载/安装全部走 Tauri 官方 updater 插件：清单里的安装包带 minisign
 * 签名，插件下载后会先验签再安装，前端不自己拼升级逻辑。
 *
 * 两条路径：
 * - 自动路径（autoInstall，由客户壳开启）：界面打开后立即安排检查，
 *   未登录也生效；发现新版本后显示进度，免确认下载、验签安装并重启。
 *   启动时检查失败会重试一次，避免短暂的网络初始化失败错过更新。
 * - 手动路径（用户中心「检查更新」）：弹 UpdateDialog，确认→下载→重启，
 *   全程有界面反馈，作为自动路径失败时的兜底入口。
 */

export type AppUpdatePhase =
  | "idle"
  | "checking"
  | "up-to-date"
  | "available"
  | "downloading"
  | "installed"
  | "failed";

export interface AvailableAppUpdate {
  version: string;
  notes: string;
}

export interface AppUpdateController {
  supported: boolean;
  phase: AppUpdatePhase;
  currentVersion: string;
  update: AvailableAppUpdate | null;
  progress: { received: number; total: number | null } | null;
  error: string;
  dialog: ReactNode;
  openManualCheck: () => void;
  beginDownload: () => void;
  restart: () => void;
  retry: () => void;
  close: () => void;
}

/** UpdateDialog 只消费状态与动作，不消费 dialog 自身（避免自引用）。 */
export type AppUpdateView = Omit<AppUpdateController, "dialog">;

const AUTO_CHECK_DELAY_MS = 250;
const AUTO_CHECK_RETRY_DELAY_MS = 1500;
// 原生请求负责真正停止超时网络流；仅在前端结束等待会让后台安装继续占用更新对象。
const CHECK_TIMEOUT_MS = 20_000;
// 安装包远大于更新清单，给慢速网络保留分钟级下载窗口，同时避免永久占用互斥锁。
const DOWNLOAD_TIMEOUT_MS = 10 * 60_000;

type AppUpdateState = Pick<
  AppUpdateController,
  "phase" | "update" | "progress" | "error"
> & { dialogOpen: boolean };

// 更新属于客户端生命周期，不属于账号或页面。模块内共享状态和锁可跨过
// StrictMode 的 effect 重放、登录切换及个人中心卸载，避免争用原生 Update。
let state: AppUpdateState = {
  phase: "idle",
  update: null,
  progress: null,
  error: "",
  dialogOpen: false,
};
const listeners = new Set<() => void>();
let pendingUpdate: Update | null = null;
let operationBusy = false;
let hasChecked = false;
let installed = false;
let autoCheckStarted = false;
let autoInstallRequested = false;
let autoCheckTimer: number | null = null;
let autoCheckSubscribers = 0;

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

function getSnapshot(): AppUpdateState {
  return state;
}

function setState(next: Partial<AppUpdateState>): void {
  state = { ...state, ...next };
  for (const listener of listeners) listener();
}

async function releasePendingUpdate(): Promise<void> {
  const pending = pendingUpdate;
  pendingUpdate = null;
  try {
    await pending?.close();
  } catch {
    // 原生资源清理失败不改变更新结果，也不能卡住后续手动重试。
  }
}

/** 共享的下载+验签+安装流程：手动路径与自动路径唯一共用点，进度事件
 * 归一成 {received,total} 回调，total 在 Started 帧确定后保持不变。 */
function downloadPendingWithProgress(
  pending: Update,
  onProgress: (progress: { received: number; total: number | null }) => void,
): Promise<void> {
  let receivedBytes = 0;
  let total: number | null = null;
  return pending.downloadAndInstall(
    (event) => {
      switch (event.event) {
        case "Started":
          total = event.data.contentLength ?? null;
          onProgress({ received: 0, total });
          break;
        case "Progress":
          receivedBytes += event.data.chunkLength;
          onProgress({ received: receivedBytes, total });
          break;
        default:
          break;
      }
    },
    { timeout: DOWNLOAD_TIMEOUT_MS },
  );
}

async function restartInstalled(): Promise<void> {
  setState({ phase: "installed", error: "" });
  try {
    await invoke("restart_app");
    setState({ dialogOpen: false });
  } catch {
    setState({
      phase: "failed",
      error: "自动重启失败，请手动退出客户端后重新打开完成升级。",
    });
  }
}

// 调用方持有同一把锁，检查转下载之间也不释放，避免手动请求覆盖原生对象。
async function installPending(): Promise<void> {
  const pending = pendingUpdate;
  if (!pending) return;
  setState({
    phase: "downloading",
    progress: { received: 0, total: null },
    error: "",
    dialogOpen: state.dialogOpen || autoInstallRequested,
  });
  try {
    await downloadPendingWithProgress(pending, (progress) =>
      setState({ progress }),
    );
    installed = true;
    setState({ phase: "installed" });
  } catch {
    setState({
      phase: "failed",
      error: "下载或安装更新失败，请检查网络后重试。",
    });
  } finally {
    await releasePendingUpdate();
  }
  if (installed && autoInstallRequested) {
    // Windows NSIS 启动安装器时插件会退出进程；macOS 等返回后仍需主动重启。
    await restartInstalled();
  }
}

async function checkForUpdate(): Promise<Update | null> {
  try {
    return await check({ timeout: CHECK_TIMEOUT_MS });
  } catch (error) {
    if (!autoInstallRequested) throw error;
    await new Promise<void>((resolve) =>
      window.setTimeout(resolve, AUTO_CHECK_RETRY_DELAY_MS),
    );
    return check({ timeout: CHECK_TIMEOUT_MS });
  }
}

async function runCheck(
  autoInstall = false,
  showAvailable = false,
): Promise<void> {
  if (operationBusy || installed) return;
  operationBusy = true;
  hasChecked = true;
  autoInstallRequested = autoInstall;
  setState({ phase: "checking", update: null, progress: null, error: "" });
  try {
    await releasePendingUpdate();
    const found = await checkForUpdate();
    pendingUpdate = found;
    if (!found) {
      setState({ phase: "up-to-date" });
      return;
    }
    setState({
      phase: "available",
      update: { version: found.version, notes: (found.body ?? "").trim() },
    });
    if (autoInstallRequested) {
      await installPending();
    } else if (showAvailable) {
      setState({ dialogOpen: true });
    }
  } catch {
    setState({ phase: "failed", error: "检查更新失败，请稍后重试。" });
  } finally {
    autoInstallRequested = false;
    operationBusy = false;
  }
}

async function runDownload(autoInstall = false): Promise<void> {
  if (operationBusy || installed || state.phase !== "available") return;
  operationBusy = true;
  autoInstallRequested = autoInstall;
  try {
    await installPending();
  } finally {
    autoInstallRequested = false;
    operationBusy = false;
  }
}

function startAutomaticCheck(autoInstall: boolean): void {
  if (autoCheckStarted) return;
  autoCheckStarted = true;
  if (installed) return;
  if (operationBusy) {
    // 手动检查或下载已在途时接续同一流程，不另建 Update，也不漏掉自动重启。
    autoInstallRequested = autoInstall;
    return;
  }
  if (hasChecked && state.phase !== "failed") {
    // 延迟窗口内的手动检查已覆盖本次启动，复用结果即可。
    if (state.phase === "available") {
      if (autoInstall) void runDownload(true);
      else setState({ dialogOpen: true });
    }
    return;
  }
  void runCheck(autoInstall, !autoInstall);
}

function openManualCheck(): void {
  if (!isTauriRuntime()) return;
  setState({ dialogOpen: true });
  if (state.phase !== "available") void runCheck();
}

function beginDownload(): void {
  if (isTauriRuntime()) void runDownload();
}

async function runRestart(): Promise<void> {
  if (!isTauriRuntime() || operationBusy || !installed) return;
  operationBusy = true;
  try {
    await restartInstalled();
  } finally {
    operationBusy = false;
  }
}

function restart(): void {
  void runRestart();
}

function retry(): void {
  if (!isTauriRuntime()) return;
  setState({ dialogOpen: true });
  if (installed) restart();
  else void runCheck();
}

function close(): void {
  if (state.phase !== "downloading") setState({ dialogOpen: false });
}

export function useAppUpdate({
  autoCheck = false,
  autoInstall = false,
}: {
  autoCheck?: boolean;
  autoInstall?: boolean;
} = {}): AppUpdateController {
  const supported = isTauriRuntime();
  const snapshot = useSyncExternalStore(subscribe, getSnapshot);

  useEffect(() => {
    if (!autoCheck || !supported || autoCheckStarted) return;
    autoCheckSubscribers += 1;
    if (autoCheckTimer === null) {
      autoCheckTimer = window.setTimeout(() => {
        autoCheckTimer = null;
        startAutomaticCheck(autoInstall);
      }, AUTO_CHECK_DELAY_MS);
    }
    return () => {
      autoCheckSubscribers -= 1;
      if (autoCheckSubscribers === 0 && autoCheckTimer !== null) {
        window.clearTimeout(autoCheckTimer);
        autoCheckTimer = null;
      }
    };
  }, [autoCheck, autoInstall, supported]);

  const controller: AppUpdateView = {
    supported,
    phase: snapshot.phase,
    currentVersion: __APP_VERSION__,
    update: snapshot.update,
    progress: snapshot.progress,
    error: snapshot.error,
    openManualCheck,
    beginDownload,
    restart,
    retry,
    close,
  };

  return {
    ...controller,
    dialog:
      snapshot.dialogOpen && supported ? (
        <UpdateDialog update={controller} />
      ) : null,
  };
}
