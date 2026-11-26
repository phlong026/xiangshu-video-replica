import { invoke } from "@tauri-apps/api/core";
import { check, type Update } from "@tauri-apps/plugin-updater";
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
} from "@testing-library/react";
import { StrictMode } from "react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import type { AppUpdateController } from "./appUpdate";

vi.mock("@tauri-apps/plugin-updater", () => ({ check: vi.fn() }));
vi.mock("@tauri-apps/api/core", () => ({ invoke: vi.fn() }));

const invokeMock = vi.mocked(invoke);
const checkMock = vi.mocked(check);
const OLD_AUTO_CHECK_DAY_KEY = "uc:update-auto-check-day";

function localDayStamp(): string {
  const now = new Date();
  const month = `${now.getMonth() + 1}`.padStart(2, "0");
  const day = `${now.getDate()}`.padStart(2, "0");
  return `${now.getFullYear()}-${month}-${day}`;
}

function fakeUpdate(overrides: Partial<Update> = {}): Update {
  return {
    version: "2.5.0",
    body: "  更新日志内容  ",
    date: null,
    currentVersion: "2.4.0",
    downloadAndInstall: vi.fn(() => Promise.resolve()),
    close: vi.fn(() => Promise.resolve()),
    ...overrides,
  } as unknown as Update;
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

let useAppUpdate: typeof import("./appUpdate")["useAppUpdate"];
let controller: AppUpdateController;
let manualController: AppUpdateController;

// 模块随 WebView 的新启动重新加载；组件卸载/重挂本身不代表软件重启。
async function freshStartup(): Promise<void> {
  vi.resetModules();
  ({ useAppUpdate } = await import("./appUpdate"));
}

function Harness({
  autoCheck = false,
  autoInstall = false,
}: {
  autoCheck?: boolean;
  autoInstall?: boolean;
}) {
  controller = useAppUpdate({ autoCheck, autoInstall });
  return (
    <>
      {controller.dialog}
      <button onClick={controller.openManualCheck} type="button">
        手动检查
      </button>
    </>
  );
}

function AutomaticShell() {
  controller = useAppUpdate({ autoCheck: true, autoInstall: true });
  return null;
}

function ManualPage() {
  manualController = useAppUpdate();
  return <>{manualController.dialog}</>;
}

async function advance(ms = 250): Promise<void> {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

beforeEach(async () => {
  vi.resetAllMocks();
  vi.useFakeTimers();
  invokeMock.mockResolvedValue(undefined);
  checkMock.mockResolvedValue(null);
  window.localStorage.clear();
  (window as unknown as Record<string, unknown>).__TAURI_INTERNALS__ = {};
  await freshStartup();
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  delete (window as unknown as Record<string, unknown>).__TAURI_INTERNALS__;
});

test("浏览器禁用自动和手动更新，不发请求、不弹窗或重启", async () => {
  delete (window as unknown as Record<string, unknown>).__TAURI_INTERNALS__;
  render(<Harness autoCheck autoInstall />);
  expect(controller.supported).toBe(false);
  await act(async () => {
    controller.openManualCheck();
    controller.retry();
    controller.beginDownload();
    controller.restart();
  });
  await advance(30000);
  expect(checkMock).not.toHaveBeenCalled();
  expect(invokeMock).not.toHaveBeenCalled();
  expect(controller.dialog).toBeNull();
});

test("手动检查发现新版本：确认后下载安装，最后可一键重启", async () => {
  const pending = fakeUpdate();
  checkMock.mockResolvedValue(pending);
  render(<Harness />);

  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: "手动检查" }));
  });
  expect(screen.getByText("发现新版本 V2.5.0")).toBeVisible();
  expect(screen.getByText("更新日志内容")).toBeVisible();
  expect(pending.downloadAndInstall).not.toHaveBeenCalled();

  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: "立即更新" }));
    controller.beginDownload();
  });
  expect(screen.getByText("新版本已就绪")).toBeVisible();
  expect(pending.downloadAndInstall).toHaveBeenCalledTimes(1);
  expect(pending.close).toHaveBeenCalledTimes(1);
  expect(invokeMock).not.toHaveBeenCalled();

  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: "立即重启" }));
    controller.restart();
  });
  expect(invokeMock).toHaveBeenCalledExactlyOnceWith("restart_app");
});

test("手动检查已是最新：提示当前版本，不再写每日节流标记", async () => {
  render(<Harness />);
  await act(async () => {
    controller.openManualCheck();
  });
  expect(screen.getByText("已是最新版本")).toBeVisible();
  expect(window.localStorage.getItem(OLD_AUTO_CHECK_DAY_KEY)).toBeNull();
});

test("手动检查失败后释放互斥锁，重试可重新检查", async () => {
  checkMock.mockRejectedValueOnce(new Error("network"));
  render(<Harness />);
  await act(async () => {
    controller.openManualCheck();
  });
  expect(screen.getByText("检查更新失败，请稍后重试。")).toBeVisible();
  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: "重试" }));
  });
  expect(screen.getByText("已是最新版本")).toBeVisible();
  expect(checkMock).toHaveBeenCalledTimes(2);
});

test.each(["自动检查", "手动检查", "自动下载", "手动下载"])(
  "%s 使用原生有限超时，失败后释放互斥并允许手动重新检查安装",
  async (path) => {
    const automatic = path.startsWith("自动");
    const downloading = path.endsWith("下载");
    const timeout = downloading ? 600_000 : 20_000;
    const failed = fakeUpdate({
      downloadAndInstall: vi.fn<Update["downloadAndInstall"]>(
        (_onEvent, options) =>
          new Promise<void>((_resolve, reject) => {
            window.setTimeout(
              () => reject(new Error("native download timed out")),
              options?.timeout,
            );
          }),
      ),
    });
    const next = fakeUpdate();
    if (downloading) {
      checkMock.mockResolvedValueOnce(failed);
    } else {
      checkMock.mockImplementationOnce(
        (options) =>
          new Promise<Update | null>((_resolve, reject) => {
            window.setTimeout(
              () => reject(new Error("native check timed out")),
              options?.timeout,
            );
          }),
      );
      if (automatic) checkMock.mockRejectedValueOnce(new Error("network"));
    }
    checkMock.mockResolvedValue(next);
    render(<Harness autoCheck={automatic} autoInstall />);
    if (automatic) {
      await advance();
    } else {
      await act(async () => {
        controller.openManualCheck();
      });
      if (downloading) {
        await act(async () => {
          controller.beginDownload();
        });
      }
    }
    expect(checkMock).toHaveBeenNthCalledWith(1, { timeout: 20_000 });
    if (downloading) {
      expect(failed.downloadAndInstall).toHaveBeenCalledExactlyOnceWith(
        expect.any(Function),
        { timeout: 600_000 },
      );
    }
    await advance(timeout - 1);
    expect(controller.phase).toBe(downloading ? "downloading" : "checking");
    expect(invokeMock).not.toHaveBeenCalled();
    await advance(1);
    if (automatic && !downloading) {
      expect(controller.phase).toBe("checking");
      await advance(1500);
    }
    expect(controller.phase).toBe("failed");
    if (automatic && !downloading) expect(controller.dialog).toBeNull();
    if (automatic && downloading) {
      expect(screen.getByRole("dialog", { name: "软件更新" })).toBeVisible();
    }
    if (downloading) expect(failed.close).toHaveBeenCalledTimes(1);
    expect(invokeMock).not.toHaveBeenCalled();

    await act(async () => {
      controller.retry();
    });
    expect(checkMock).toHaveBeenNthCalledWith(
      automatic && !downloading ? 3 : 2,
      {
        timeout: 20_000,
      },
    );
    expect(controller.phase).toBe("available");
    expect(next.downloadAndInstall).not.toHaveBeenCalled();
    await act(async () => {
      controller.beginDownload();
    });
    expect(next.downloadAndInstall).toHaveBeenCalledExactlyOnceWith(
      expect.any(Function),
      { timeout: 600_000 },
    );
    expect(controller.phase).toBe("installed");
    expect(invokeMock).not.toHaveBeenCalled();
  },
);

test("取消手动检查仅关闭弹窗，再次打开复用在途检查", async () => {
  const result = deferred<Update | null>();
  checkMock.mockReturnValue(result.promise);
  render(<Harness />);
  await act(async () => {
    controller.openManualCheck();
    controller.close();
    controller.openManualCheck();
    controller.retry();
  });
  expect(checkMock).toHaveBeenCalledTimes(1);
  await act(async () => {
    result.resolve(null);
  });
  expect(screen.getByText("已是最新版本")).toBeVisible();
});

test("界面打开后尽快检查，旧的当日 localStorage 标记不影响两次新启动", async () => {
  window.localStorage.setItem(OLD_AUTO_CHECK_DAY_KEY, localDayStamp());
  const first = render(<Harness autoCheck autoInstall />);
  await advance(249);
  expect(checkMock).not.toHaveBeenCalled();
  await advance(1);
  expect(checkMock).toHaveBeenCalledTimes(1);
  expect(controller.phase).toBe("up-to-date");
  expect(controller.dialog).toBeNull();
  first.unmount();

  await freshStartup();
  render(<Harness autoCheck autoInstall />);
  await advance();
  expect(checkMock).toHaveBeenCalledTimes(2);
  expect(window.localStorage.getItem(OLD_AUTO_CHECK_DAY_KEY)).toBe(
    localDayStamp(),
  );
});

test("StrictMode、重渲染与组件重挂在同一生命周期内只检查一次", async () => {
  const view = render(
    <StrictMode>
      <Harness autoCheck autoInstall />
    </StrictMode>,
  );
  await advance(125);
  view.rerender(
    <StrictMode>
      <Harness autoCheck autoInstall />
    </StrictMode>,
  );
  await advance(125);
  expect(checkMock).toHaveBeenCalledTimes(1);
  view.unmount();
  render(
    <StrictMode>
      <Harness autoCheck autoInstall />
    </StrictMode>,
  );
  await advance(30000);
  expect(checkMock).toHaveBeenCalledTimes(1);
});

test("纯自动检查发现新版本时仍可展示更新日志", async () => {
  checkMock.mockResolvedValue(fakeUpdate());
  render(<Harness autoCheck />);
  await advance();
  expect(screen.getByText("发现新版本 V2.5.0")).toBeVisible();
  expect(screen.getByText("更新日志内容")).toBeVisible();
});

test("自动更新免确认下载安装，沿用累计进度，安装成功后自动重启", async () => {
  const pending = fakeUpdate({
    downloadAndInstall: vi.fn(async (onEvent) => {
      onEvent?.({ event: "Started", data: { contentLength: 100 } });
      onEvent?.({ event: "Progress", data: { chunkLength: 30 } });
      onEvent?.({ event: "Progress", data: { chunkLength: 70 } });
      onEvent?.({ event: "Finished" });
    }),
  });
  checkMock.mockResolvedValue(pending);
  render(<Harness autoCheck autoInstall />);
  await advance();
  expect(controller.dialog).toBeNull();
  expect(pending.downloadAndInstall).toHaveBeenCalledTimes(1);
  expect(controller.progress).toEqual({ received: 100, total: 100 });
  expect(controller.phase).toBe("installed");
  expect(invokeMock).toHaveBeenCalledExactlyOnceWith("restart_app");
});

test.each(["检查", "下载安装"])(
  "自动%s最终失败后重挂不重复更新，新启动可重新尝试",
  async (failure) => {
    const pending = fakeUpdate();
    if (failure === "检查") {
      checkMock
        .mockRejectedValueOnce(new Error("network"))
        .mockRejectedValueOnce(new Error("network"));
    } else {
      checkMock.mockResolvedValueOnce(
        fakeUpdate({
          downloadAndInstall: vi.fn(async () => {
            throw new Error("signature or download");
          }),
        }),
      );
    }
    checkMock.mockResolvedValue(pending);
    const first = render(<Harness autoCheck autoInstall />);
    await advance();
    if (failure === "检查") await advance(1500);
    expect(controller.phase).toBe("failed");
    if (failure === "检查") expect(controller.dialog).toBeNull();
    else expect(screen.getByText("更新失败")).toBeVisible();
    expect(invokeMock).not.toHaveBeenCalled();
    first.unmount();

    const remount = render(<Harness autoCheck autoInstall />);
    await advance(30000);
    const checks = failure === "检查" ? 2 : 1;
    expect(checkMock).toHaveBeenCalledTimes(checks);
    remount.unmount();

    await freshStartup();
    render(<Harness autoCheck autoInstall />);
    await advance();
    expect(checkMock).toHaveBeenCalledTimes(checks + 1);
    expect(pending.downloadAndInstall).toHaveBeenCalledTimes(1);
    expect(controller.phase).toBe("installed");
    expect(invokeMock).toHaveBeenCalledExactlyOnceWith("restart_app");
  },
);

test("启动时网络暂不可用，重试发现新版后仍自动安装重启", async () => {
  const pending = fakeUpdate();
  checkMock
    .mockRejectedValueOnce(new Error("network"))
    .mockResolvedValueOnce(pending);
  render(<Harness autoCheck autoInstall />);
  await advance();
  expect(controller.phase).toBe("checking");
  expect(controller.dialog).toBeNull();
  await advance(1500);
  expect(checkMock).toHaveBeenCalledTimes(2);
  expect(pending.downloadAndInstall).toHaveBeenCalledTimes(1);
  expect(invokeMock).toHaveBeenCalledExactlyOnceWith("restart_app");
});

test("自动检查在途时切页和手动重试复用检查，下载中手动操作不创建另一个 Update", async () => {
  const result = deferred<Update | null>();
  const download = deferred<void>();
  const pending = fakeUpdate({
    downloadAndInstall: vi.fn(() => download.promise),
  });
  checkMock.mockReturnValue(result.promise);
  const view = render(<AutomaticShell />);
  await advance();
  expect(checkMock).toHaveBeenCalledTimes(1);
  view.rerender(
    <>
      <AutomaticShell />
      <ManualPage />
    </>,
  );
  await act(async () => {
    manualController.openManualCheck();
    manualController.retry();
    manualController.beginDownload();
  });
  expect(manualController.phase).toBe("checking");
  await act(async () => {
    result.resolve(pending);
  });
  expect(manualController.phase).toBe("downloading");
  expect(screen.getByText("正在下载新版本 V2.5.0")).toBeVisible();
  await act(async () => {
    manualController.openManualCheck();
    manualController.retry();
    manualController.beginDownload();
    manualController.restart();
  });
  view.rerender(<AutomaticShell />);
  view.rerender(
    <>
      <AutomaticShell />
      <ManualPage />
    </>,
  );
  expect(manualController.phase).toBe("downloading");
  await act(async () => {
    download.resolve();
  });
  expect(checkMock).toHaveBeenCalledTimes(1);
  expect(pending.downloadAndInstall).toHaveBeenCalledTimes(1);
  expect(invokeMock).toHaveBeenCalledExactlyOnceWith("restart_app");
});

test.each(["checking", "available", "downloading", "up-to-date"])(
  "启动检查时手动流程处于 %s：复用请求或结果，自动更新不并发",
  async (phase) => {
    const result = deferred<Update | null>();
    const download = deferred<void>();
    const pending = fakeUpdate({
      downloadAndInstall: vi.fn(() => download.promise),
    });
    checkMock.mockReturnValue(result.promise);
    render(
      <>
        <AutomaticShell />
        <ManualPage />
      </>,
    );
    await act(async () => {
      manualController.openManualCheck();
    });
    if (phase !== "checking") {
      await act(async () => {
        result.resolve(phase === "up-to-date" ? null : pending);
      });
    }
    if (phase === "downloading") {
      await act(async () => {
        manualController.beginDownload();
      });
    }
    await advance();
    expect(checkMock).toHaveBeenCalledTimes(1);
    if (phase === "checking") {
      await act(async () => {
        result.resolve(pending);
      });
    }
    await act(async () => {
      download.resolve();
    });
    if (phase === "up-to-date") {
      expect(pending.downloadAndInstall).not.toHaveBeenCalled();
      expect(invokeMock).not.toHaveBeenCalled();
    } else {
      expect(pending.downloadAndInstall).toHaveBeenCalledTimes(1);
      expect(invokeMock).toHaveBeenCalledExactlyOnceWith("restart_app");
    }
  },
);

test("检查中所有视图卸载再重挂也会完成原自动流程并只安装一次", async () => {
  const result = deferred<Update | null>();
  const pending = fakeUpdate();
  checkMock.mockReturnValue(result.promise);
  const first = render(<Harness autoCheck autoInstall />);
  await advance();
  first.unmount();
  await act(async () => {
    result.resolve(pending);
  });
  render(<Harness autoCheck autoInstall />);
  await advance(30000);
  expect(controller.phase).toBe("installed");
  expect(checkMock).toHaveBeenCalledTimes(1);
  expect(pending.downloadAndInstall).toHaveBeenCalledTimes(1);
  expect(invokeMock).toHaveBeenCalledExactlyOnceWith("restart_app");
});

test("自动下载安装失败后，手动兜底重新检查使用新对象，仍需确认安装", async () => {
  const failed = fakeUpdate({
    downloadAndInstall: vi.fn(async () => {
      throw new Error("signature");
    }),
  });
  const next = fakeUpdate();
  checkMock.mockResolvedValueOnce(failed).mockResolvedValueOnce(next);
  render(<Harness autoCheck autoInstall />);
  await advance();
  expect(failed.close).toHaveBeenCalledTimes(1);
  await act(async () => {
    controller.openManualCheck();
  });
  expect(controller.phase).toBe("available");
  expect(next.downloadAndInstall).not.toHaveBeenCalled();
  await act(async () => {
    controller.beginDownload();
  });
  expect(next.downloadAndInstall).toHaveBeenCalledTimes(1);
  expect(invokeMock).not.toHaveBeenCalled();
});

test("自动重启失败显示提示，重试只重启，不重复检查或安装", async () => {
  const pending = fakeUpdate();
  checkMock.mockResolvedValue(pending);
  invokeMock.mockRejectedValueOnce(new Error("restart"));
  render(<Harness autoCheck autoInstall />);
  await advance();
  expect(controller.phase).toBe("failed");
  expect(screen.getByText("更新失败")).toBeVisible();
  await act(async () => {
    controller.openManualCheck();
    controller.beginDownload();
  });
  expect(
    screen.getByText("自动重启失败，请手动退出客户端后重新打开完成升级。"),
  ).toBeVisible();
  await act(async () => {
    controller.retry();
  });
  expect(checkMock).toHaveBeenCalledTimes(1);
  expect(pending.downloadAndInstall).toHaveBeenCalledTimes(1);
  expect(invokeMock).toHaveBeenCalledTimes(2);
});
