import { useEffect, useRef, useState } from "react";
import { Icon } from "../studio/ui";
import { isTauriRuntime } from "./useCustomerSession";
import "./customer-dialog.css";
import "./desktop-download.css";

const channel = "/downloads/customer-cloud/";

const platforms = [
  { key: "windows-x86_64", label: "Windows", role: "update", suffix: ".exe" },
  {
    key: "darwin-aarch64",
    label: "macOS · Apple 芯片",
    role: "installer",
    suffix: ".dmg",
  },
  {
    key: "darwin-x86_64",
    label: "macOS · Intel 芯片",
    role: "installer",
    suffix: ".dmg",
  },
];

type Download = { label: string; href: string };
type Release = { version: string; downloads: Download[] };

type ReleaseAsset = { path?: unknown; platform?: unknown; role?: unknown };
type ReleaseManifest = {
  version?: unknown;
  assets?: unknown;
  artifact?: unknown;
};

async function getManifest(path: string, signal: AbortSignal) {
  const response = await fetch(path, {
    cache: "no-cache",
    credentials: "omit",
    redirect: "error",
    signal,
  });
  if (!response.ok) throw new Error("下载清单暂不可用");
  return (await response.json()) as ReleaseManifest;
}

async function loadRelease(signal: AbortSignal): Promise<Release> {
  const { version } = await getManifest(`${channel}stable.json`, signal);
  if (typeof version !== "string" || !/^\d+\.\d+\.\d+$/.test(version)) {
    throw new Error("下载版本无法识别");
  }
  const base = `${channel}${version}/`;
  const manifest = await getManifest(`${base}release-manifest.json`, signal);
  if (manifest.version !== version) throw new Error("下载版本不一致");
  const assets = Array.isArray(manifest.assets)
    ? (manifest.assets as (ReleaseAsset | null)[])
    : [{ path: manifest.artifact, platform: "windows-x86_64", role: "update" }];
  const downloads = platforms.flatMap(({ key, label, role, suffix }) => {
    const file = assets.find(
      (asset) => asset?.platform === key && asset?.role === role,
    )?.path;
    if (
      typeof file !== "string" ||
      /[\\/]/.test(file) ||
      !file.endsWith(suffix)
    ) {
      return [];
    }
    return [{ label, href: `${base}${encodeURIComponent(file)}` }];
  });
  if (downloads.length === 0) throw new Error("当前没有可用安装包");
  return { version, downloads };
}

export function DesktopDownload() {
  const dialog = useRef<HTMLDialogElement>(null);
  const [open, setOpen] = useState(false);
  const [release, setRelease] = useState<Release | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    const element = dialog.current;
    if (!open || !element) return;
    if (!element.open) {
      if (typeof element.showModal === "function") element.showModal();
      else element.setAttribute("open", "");
    }
    const request = new AbortController();
    void loadRelease(request.signal)
      .then((result) => {
        if (!request.signal.aborted) setRelease(result);
      })
      .catch(() => {
        if (!request.signal.aborted) setFailed(true);
      });
    return () => request.abort();
  }, [open]);

  if (isTauriRuntime()) return null;

  return (
    <>
      <button
        className="secondary-button desktop-download-trigger"
        type="button"
        onClick={() => {
          setRelease(null);
          setFailed(false);
          setOpen(true);
        }}
      >
        <Icon name="download" />
        下载桌面端
      </button>
      {open && (
        <dialog
          aria-label="下载桌面端"
          className="uc-dialog desktop-download"
          onCancel={(event) => {
            event.preventDefault();
            setOpen(false);
          }}
          ref={dialog}
        >
          <h2>下载桌面端</h2>
          {release ? (
            <>
              <p>最新版 {release.version}</p>
              <ul>
                {release.downloads.map(({ href, label }) => (
                  <li key={href}>
                    <a download href={href}>
                      {label}
                      <Icon name="download" />
                    </a>
                  </li>
                ))}
              </ul>
            </>
          ) : failed ? (
            <p role="alert">暂时无法获取安装包，请关闭后重试。</p>
          ) : (
            <p role="status">正在获取最新版…</p>
          )}
          <div className="uc-dialog-actions">
            <button
              className="secondary-button"
              type="button"
              onClick={() => setOpen(false)}
            >
              关闭
            </button>
          </div>
        </dialog>
      )}
    </>
  );
}
