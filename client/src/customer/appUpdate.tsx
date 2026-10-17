import { invoke } from "@tauri-apps/api/core";
import { check, type Update } from "@tauri-apps/plugin-updater";
import {
  type ReactNode,
  useCallback,
  useEffect,
  useRef,
  useState,
} from "react";
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
 * - 自动路径（autoInstall，默认会话壳开启）：启动后每天最多检查一次、延迟
 *   避开登录期接口高峰；发现新版本**免确认**——自动下载、验签安装并重启，
 *   全程不弹窗不打扰（2026-10-16 产品口径：打开软件即自动更新，用户不再去
 *   个人中心手动升级）。下载/安装失败静默，清掉当日节流标记，下次启动重试。
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

const AUTO_CHECK_DAY_KEY = "uc:update-auto-check-day";
const AUTO_CHECK_DELAY_MS = 8000;

function localDayStamp(): string {
  const now = new Date();
  const month = `${now.getMonth() + 1}`.padStart(2, "0");
  const day = `${now.getDate()}`.padStart(2, "0");
  return `${now.getFullYear()}-${month}-${day}`;
}

function autoCheckAlreadyRanToday(): boolean {
  try {
    return window.localStorage.getItem(AUTO_CHECK_DAY_KEY) === localDayStamp();
  } catch {
    // 读不到（隐私模式等）就当作今天已经检查过：少打扰比多打扰好。
    return true;
  }
}

function markAutoCheckDone(): void {
  try {
    window.localStorage.setItem(AUTO_CHECK_DAY_KEY, localDayStamp());
  } catch {
    // 记不住就明天再说，不影响其它功能。
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
  return pending.downloadAndInstall((event) => {
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
  });
}

export function useAppUpdate({
  autoCheck = false,
  autoInstall = false,
}: {
  autoCheck?: boolean;
  autoInstall?: boolean;
} = {}): AppUpdateController {
  const supported = isTauriRuntime();
  const [phase, setPhase] = useState<AppUpdatePhase>("idle");
  const [update, setUpdate] = useState<AvailableAppUpdate | null>(null);
  const [progress, setProgress] = useState<{
    received: number;
    total: number | null;
  } | null>(null);
  const [error, setError] = useState("");
  const [dialogOpen, setDialogOpen] = useState(false);
  const pendingRef = useRef<Update | null>(null);
  const mountedRef = useRef(true);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  const runCheck = useCallback(async (): Promise<
    "available" | "up-to-date" | "failed"
  > => {
    setPhase("checking");
    setUpdate(null);
    setError("");
    pendingRef.current = null;
    try {
      const found = await check();
      if (!mountedRef.current) {
        return "failed";
      }
      markAutoCheckDone();
      if (!found) {
        setPhase("up-to-date");
        return "up-to-date";
      }
      pendingRef.current = found;
      setUpdate({
        version: found.version,
        notes: (found.body ?? "").trim(),
      });
      setPhase("available");
      return "available";
    } catch {
      if (mountedRef.current) {
        setPhase("failed");
        setError("检查更新失败，请稍后重试。");
      }
      return "failed";
    }
  }, []);

  // 自动路径的下载安装：不弹窗、装完自动重启；失败清当日节流，下次启动重试。
  const downloadAndAutoRestart = useCallback((pending: Update) => {
    setPhase("downloading");
    setProgress({ received: 0, total: null });
    setError("");
    void downloadPendingWithProgress(pending, setProgress)
      .then(() => {
        if (!mountedRef.current) {
          return;
        }
        setPhase("installed");
        // 新包已装好；重启失败不弹窗，下次启动自然运行新版本。
        void invoke("restart_app").catch(() => {});
      })
      .catch(() => {
        if (!mountedRef.current) {
          return;
        }
        setPhase("failed");
        try {
          window.localStorage.removeItem(AUTO_CHECK_DAY_KEY);
        } catch {
          // 记不住节流就算了，不影响其它功能。
        }
      });
  }, []);

  useEffect(() => {
    if (!autoCheck || !supported || autoCheckAlreadyRanToday()) {
      return;
    }
    const timer = window.setTimeout(() => {
      void runCheck().then((outcome) => {
        if (!mountedRef.current || outcome !== "available") {
          // 已是最新/检查失败都不出声。
          return;
        }
        const pending = pendingRef.current;
        if (autoInstall && pending) {
          // 免确认自动升级：下载→验签安装→自动重启，全程无界面。
          downloadAndAutoRestart(pending);
          return;
        }
        setDialogOpen(true);
      });
    }, AUTO_CHECK_DELAY_MS);
    return () => window.clearTimeout(timer);
  }, [autoCheck, autoInstall, supported, runCheck, downloadAndAutoRestart]);

  const openManualCheck = useCallback(() => {
    if (!supported) {
      return;
    }
    setDialogOpen(true);
    void runCheck();
  }, [supported, runCheck]);

  const beginDownload = useCallback(() => {
    const pending = pendingRef.current;
    if (!pending) {
      setPhase("failed");
      setError("更新信息已失效，请重新检查更新。");
      return;
    }
    setPhase("downloading");
    setProgress({ received: 0, total: null });
    setError("");
    void downloadPendingWithProgress(pending, setProgress)
      .then(() => {
        // 走到这里安装包已验签并安装完成，重启后即运行新版本。
        if (mountedRef.current) {
          setPhase("installed");
        }
      })
      .catch(() => {
        if (mountedRef.current) {
          setPhase("failed");
          setError("下载或安装更新失败，请检查网络后重试。");
        }
      });
  }, []);

  const restart = useCallback(() => {
    void invoke("restart_app").catch(() => {
      if (mountedRef.current) {
        setPhase("failed");
        setError("自动重启失败，请手动退出客户端后重新打开完成升级。");
      }
    });
  }, []);

  const retry = useCallback(() => {
    setDialogOpen(true);
    void runCheck();
  }, [runCheck]);

  const close = useCallback(() => {
    // 下载中不允许关：半途关掉最容易留下「我以为已经更新了」的错觉。
    if (phase === "downloading") {
      return;
    }
    setDialogOpen(false);
    setPhase("idle");
  }, [phase]);

  const controller: Omit<AppUpdateController, "dialog"> = {
    supported,
    phase,
    currentVersion: __APP_VERSION__,
    update,
    progress,
    error,
    openManualCheck,
    beginDownload,
    restart,
    retry,
    close,
  };

  return {
    ...controller,
    dialog:
      dialogOpen && supported ? <UpdateDialog update={controller} /> : null,
  };
}
