//! User-approved destinations for WebView downloads; video bytes stay in the existing fetch path.
use std::collections::VecDeque;
use std::io::Write;
use std::path::PathBuf;
use std::sync::Mutex;
use std::time::{Duration, Instant};

use serde::Serialize;
use tauri::{webview::DownloadEvent, Emitter, Manager, Webview, WebviewWindow};

#[derive(Clone, Serialize)]
pub struct ChosenDownload {
    download_id: String,
    path: String,
}

#[derive(Clone, Serialize)]
pub struct FinishedDownload {
    download_id: String,
    path: String,
    success: bool,
    error: Option<String>,
}

struct Reservation {
    id: String,
    path: PathBuf,
    url: Option<String>,
    started: bool,
    completed: bool,
    finished: Option<FinishedDownload>,
    created: Instant,
}

#[derive(Default)]
struct Registry(VecDeque<Reservation>);

impl Registry {
    #[cfg(any(windows, test))]
    fn prepare(&mut self, path: PathBuf) -> Result<ChosenDownload, String> {
        // Bound retained history and abandoned pre-download reservations without removing files.
        self.0
            .retain(|item| item.started || item.created.elapsed() < Duration::from_secs(600));
        while self.0.len() >= 64 {
            let Some(index) = self.0.iter().position(|item| item.completed) else {
                return Err("请等待当前下载完成后重试".into());
            };
            self.0.remove(index);
        }
        // Windows path aliases can name the same file; serialize writes instead of comparing paths.
        if self.0.iter().any(|item| !item.completed) {
            return Err("请等待当前下载完成后重试".into());
        }
        let chosen = ChosenDownload {
            download_id: uuid::Uuid::new_v4().to_string(),
            path: path.to_string_lossy().into_owned(),
        };
        self.0.push_back(Reservation {
            id: chosen.download_id.clone(),
            path,
            url: None,
            started: false,
            completed: false,
            finished: None,
            created: Instant::now(),
        });
        Ok(chosen)
    }

    fn bind(&mut self, id: &str, url: &str) -> Result<(), String> {
        if self.0.iter().any(|item| item.url.as_deref() == Some(url)) {
            return Err("下载链接已使用".into());
        }
        let item = self
            .0
            .iter_mut()
            .find(|item| item.id == id && item.url.is_none() && !item.completed)
            .ok_or("下载授权不存在或已使用")?;
        if item.created.elapsed() >= Duration::from_secs(600) {
            return Err("保存位置授权已过期，请重新下载".into());
        }
        item.url = Some(url.into());
        Ok(())
    }

    fn request(&mut self, url: &str) -> Option<PathBuf> {
        let item = self
            .0
            .iter_mut()
            .find(|item| item.url.as_deref() == Some(url))?;
        if item.started || item.completed || item.created.elapsed() >= Duration::from_secs(600) {
            return None;
        }
        item.started = true;
        Some(item.path.clone())
    }

    fn finish(
        &mut self,
        url: &str,
        path: Option<PathBuf>,
        success: bool,
    ) -> Option<FinishedDownload> {
        let item = self
            .0
            .iter_mut()
            .find(|item| item.url.as_deref() == Some(url) && item.started && !item.completed)?;
        let success = success && path.as_deref() == Some(item.path.as_path());
        item.completed = true;
        let result = FinishedDownload {
            download_id: item.id.clone(),
            path: item.path.to_string_lossy().into_owned(),
            success,
            error: (!success).then(|| "DOWNLOAD_FAILED".into()),
        };
        // Failed records remain URL tombstones, but cannot be used to reveal a path.
        if !success {
            item.started = false;
        }
        item.finished = Some(result.clone());
        Some(result)
    }

    fn finished_status(&self, id: &str) -> Option<FinishedDownload> {
        self.0.iter().find(|item| item.id == id)?.finished.clone()
    }

    fn cancel(&mut self, id: &str) -> Result<(), String> {
        let Some(item) = self.0.iter_mut().find(|item| item.id == id) else {
            return Ok(());
        };
        if item.completed {
            return Ok(());
        }
        if item.started {
            return Err("下载已开始，请等待下载完成".into());
        }
        item.completed = true;
        Ok(())
    }

    fn completed_path(&self, id: &str) -> Result<PathBuf, String> {
        self.0
            .iter()
            .find(|item| item.id == id && item.started && item.completed)
            .map(|item| item.path.clone())
            .ok_or("找不到已完成的下载，请在保存位置查看文件".into())
    }
}

#[derive(Default)]
pub struct VideoDownloads(Mutex<Registry>);

#[cfg(any(windows, test))]
fn safe_filename(filename: &str) -> String {
    let cleaned: String = filename
        .chars()
        .take(120)
        .map(|c| {
            if c.is_control() || "<>:\"/\\|?*".contains(c) {
                '_'
            } else {
                c
            }
        })
        .collect();
    let stem = cleaned
        .rsplit_once('.')
        .map_or(cleaned.as_str(), |(stem, _)| stem)
        .trim_matches([' ', '.']);
    let upper = stem.to_ascii_uppercase();
    let reserved = matches!(upper.as_str(), "CON" | "PRN" | "AUX" | "NUL")
        || ((upper.starts_with("COM") || upper.starts_with("LPT"))
            && upper.len() == 4
            && upper.as_bytes()[3].is_ascii_digit());
    format!(
        "{}.mp4",
        if stem.is_empty() || reserved {
            "video"
        } else {
            stem
        }
    )
}

fn local_origin(webview: &Webview) -> Result<String, String> {
    let url = webview.url().map_err(|_| "无法确认窗口来源")?;
    let origin = url.origin().ascii_serialization();
    validate_origin(webview.label(), &origin)?;
    Ok(origin)
}

fn validate_origin(label: &str, origin: &str) -> Result<(), String> {
    let trusted = matches!(origin, "http://tauri.localhost" | "https://tauri.localhost")
        || (cfg!(debug_assertions) && origin == "http://127.0.0.1:5173");
    if label != "main" || !trusted {
        return Err("仅允许桌面主窗口下载".into());
    }
    Ok(())
}

fn valid_blob(url: &str, origin: &str) -> bool {
    url.strip_prefix(&format!("blob:{origin}/"))
        .is_some_and(|id| !id.is_empty() && !id.contains(['/', '?', '#']))
}

#[tauri::command]
pub async fn choose_video_download(
    window: WebviewWindow,
    filename: String,
) -> Result<Option<ChosenDownload>, String> {
    local_origin(window.as_ref())?;
    #[cfg(not(windows))]
    {
        let _ = filename;
        Err("此保存功能目前仅支持 Windows 桌面端".into())
    }
    #[cfg(windows)]
    {
        let owner = window.hwnd().map_err(|_| "无法打开保存窗口")?.0 as usize;
        let suggested = safe_filename(&filename);
        let path =
            tauri::async_runtime::spawn_blocking(move || windows_dialog::choose(owner, &suggested))
                .await
                .map_err(|_| "保存窗口异常退出")??;
        // Navigation while the modal was open must not grant a remote page a destination.
        local_origin(window.as_ref())?;
        path.map(|path| {
            window
                .state::<VideoDownloads>()
                .0
                .lock()
                .map_err(|_| "下载状态不可用".to_string())?
                .prepare(path)
        })
        .transpose()
    }
}

#[tauri::command]
pub fn start_video_download(
    window: WebviewWindow,
    download_id: String,
    url: String,
) -> Result<(), String> {
    let origin = local_origin(window.as_ref())?;
    if !valid_blob(&url, &origin) {
        return Err("无效的本地视频下载链接".into());
    }
    window
        .state::<VideoDownloads>()
        .0
        .lock()
        .map_err(|_| "下载状态不可用")?
        .bind(&download_id, &url)
}

#[tauri::command]
pub fn cancel_video_download(window: WebviewWindow, download_id: String) -> Result<(), String> {
    local_origin(window.as_ref())?;
    window
        .state::<VideoDownloads>()
        .0
        .lock()
        .map_err(|_| "下载状态不可用")?
        .cancel(&download_id)
}

#[tauri::command]
pub fn get_video_download_status(
    window: WebviewWindow,
    download_id: String,
) -> Result<Option<FinishedDownload>, String> {
    local_origin(window.as_ref())?;
    Ok(window
        .state::<VideoDownloads>()
        .0
        .lock()
        .map_err(|_| "下载状态不可用")?
        .finished_status(&download_id))
}

#[tauri::command]
pub fn open_video_download_folder(
    window: WebviewWindow,
    download_id: String,
) -> Result<(), String> {
    local_origin(window.as_ref())?;
    let path = window
        .state::<VideoDownloads>()
        .0
        .lock()
        .map_err(|_| "下载状态不可用")?
        .completed_path(&download_id)?;
    if !path.is_file() {
        return Err("文件已移动或删除，请检查保存位置".into());
    }
    #[cfg(windows)]
    {
        // No shell parsing and no frontend-supplied path. Explorer receives the recorded file as one argument.
        let mut argument = std::ffi::OsString::from("/select,");
        argument.push(path.as_os_str());
        std::process::Command::new("explorer.exe")
            .arg(argument)
            .spawn()
            .map_err(|_| "无法打开保存文件夹")?;
        Ok(())
    }
    #[cfg(not(windows))]
    {
        Err("此功能目前仅支持 Windows 桌面端".into())
    }
}

pub fn on_download(webview: Webview, event: DownloadEvent<'_>) -> bool {
    let state = webview.state::<VideoDownloads>();
    let Ok(mut registry) = state.0.lock() else {
        return false;
    };
    match event {
        DownloadEvent::Requested { url, destination } => {
            let known = registry
                .0
                .iter()
                .any(|item| item.url.as_deref() == Some(url.as_str()));
            if !known {
                return true;
            } // Preserve unrelated diagnostic/CSV downloads.
            if local_origin(&webview).is_err() {
                return false;
            }
            if let Some(path) = registry.request(url.as_str()) {
                *destination = path;
                true
            } else {
                false
            }
        }
        DownloadEvent::Finished { url, path, success } => {
            let success = success
                && path.as_ref().is_some_and(|path| {
                    std::fs::metadata(path).is_ok_and(|meta| meta.is_file() && meta.len() > 0)
                });
            let result = registry.finish(url.as_str(), path, success);
            drop(registry);
            if let Some(result) = result {
                if local_origin(&webview).is_ok() {
                    // Finished ignores the callback return value; retain the result for status queries.
                    if webview
                        .emit_to(
                            tauri::EventTarget::webview_window("main"),
                            "video-download-finished",
                            &result,
                        )
                        .is_err()
                    {
                        // Best-effort diagnostic: no logging backend is installed, and errors may contain URLs.
                        let _ = writeln!(
                            std::io::stderr(),
                            "ERROR VIDEO_DOWNLOAD_FINISHED_EMIT_FAILED download_id={}",
                            result.download_id
                        );
                    }
                }
            }
            true
        }
        _ => false,
    }
}

#[cfg(windows)]
mod windows_dialog {
    use super::*;
    use std::ffi::{c_void, OsString};
    use std::os::windows::ffi::OsStringExt;

    #[repr(C)]
    #[cfg_attr(target_arch = "x86", repr(packed(1)))]
    struct OpenFileName {
        size: u32,
        owner: *mut c_void,
        instance: *mut c_void,
        filter: *const u16,
        custom_filter: *mut u16,
        max_custom_filter: u32,
        filter_index: u32,
        file: *mut u16,
        max_file: u32,
        file_title: *mut u16,
        max_file_title: u32,
        initial_dir: *const u16,
        title: *const u16,
        flags: u32,
        file_offset: u16,
        file_extension: u16,
        default_extension: *const u16,
        custom_data: isize,
        hook: *mut c_void,
        template: *const u16,
        reserved: *mut c_void,
        reserved_word: u32,
        flags_ex: u32,
    }

    #[link(name = "comdlg32")]
    extern "system" {
        fn GetSaveFileNameW(value: *mut OpenFileName) -> i32;
        fn CommDlgExtendedError() -> u32;
    }

    pub fn choose(owner: usize, filename: &str) -> Result<Option<PathBuf>, String> {
        let mut buffer = vec![0_u16; 32768];
        let suggested: Vec<u16> = filename.encode_utf16().collect();
        buffer[..suggested.len()].copy_from_slice(&suggested);
        let filter: Vec<u16> = "MP4 视频\0*.mp4\0\0".encode_utf16().collect();
        let extension: Vec<u16> = "mp4\0".encode_utf16().collect();
        let title: Vec<u16> = "保存视频\0".encode_utf16().collect();
        // All unused pointer fields are nullable, and every passed UTF-16 buffer lives through the modal call.
        let mut value: OpenFileName = unsafe { std::mem::zeroed() };
        value.size = std::mem::size_of::<OpenFileName>() as u32;
        value.owner = owner as *mut c_void;
        value.filter = filter.as_ptr();
        value.filter_index = 1;
        value.file = buffer.as_mut_ptr();
        value.max_file = buffer.len() as u32;
        value.default_extension = extension.as_ptr();
        value.title = title.as_ptr();
        // OVERWRITEPROMPT | NOCHANGEDIR | PATHMUSTEXIST | EXPLORER | NOREADONLYRETURN.
        value.flags = 0x2 | 0x8 | 0x800 | 0x80000 | 0x8000;
        if unsafe { GetSaveFileNameW(&mut value) } == 0 {
            let code = unsafe { CommDlgExtendedError() };
            return if code == 0 {
                Ok(None)
            } else {
                Err(format!("无法选择保存位置（{code}）"))
            };
        }
        let end = buffer
            .iter()
            .position(|unit| *unit == 0)
            .ok_or("保存路径无效")?;
        let path = PathBuf::from(OsString::from_wide(&buffer[..end]));
        if !path.is_absolute()
            || !path
                .extension()
                .is_some_and(|ext| ext.eq_ignore_ascii_case("mp4"))
        {
            return Err("请选择完整的 MP4 文件保存路径".into());
        }
        Ok(Some(path))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn suggested_filename_cannot_select_a_directory_or_executable() {
        assert_eq!(safe_filename("../a\\b:影片.exe"), "_a_b_影片.mp4");
        assert_eq!(safe_filename(""), "video.mp4");
        assert_eq!(safe_filename("CON.mp4"), "video.mp4");
    }

    #[test]
    fn blob_must_belong_to_the_local_main_webview() {
        assert!(valid_blob(
            "blob:http://tauri.localhost/abc",
            "http://tauri.localhost"
        ));
        assert!(!valid_blob(
            "https://provider.test/video.mp4",
            "http://tauri.localhost"
        ));
        assert!(!valid_blob(
            "blob:https://evil.test/abc",
            "http://tauri.localhost"
        ));
        assert!(!valid_blob(
            "blob:http://tauri.localhost.evil/abc",
            "http://tauri.localhost"
        ));
    }

    #[test]
    fn download_commands_only_accept_the_local_main_window() {
        assert!(validate_origin("main", "http://tauri.localhost").is_ok());
        assert!(validate_origin("main", "https://tauri.localhost").is_ok());
        assert!(validate_origin("other", "http://tauri.localhost").is_err());
        assert!(validate_origin("main", "https://provider.test").is_err());
        assert!(validate_origin("main", "http://tauri.localhost.evil").is_err());
        assert!(validate_origin("main", "null").is_err());
    }

    #[test]
    fn approved_download_is_one_use_and_only_completed_paths_can_be_revealed() {
        let mut registry = Registry::default();
        let chosen = registry
            .prepare(PathBuf::from("C:/Videos/one.mp4"))
            .unwrap();
        let url = "blob:http://tauri.localhost/abc";
        assert!(registry.completed_path(&chosen.download_id).is_err());
        registry.bind(&chosen.download_id, url).unwrap();
        assert!(registry.bind(&chosen.download_id, url).is_err());
        assert_eq!(registry.request(url), Some(PathBuf::from(&chosen.path)));
        assert!(registry.request(url).is_none());
        assert!(registry.cancel(&chosen.download_id).is_err());
        let result = registry
            .finish(url, Some(PathBuf::from(&chosen.path)), true)
            .unwrap();
        assert!(result.success);
        assert_eq!(
            registry.completed_path(&chosen.download_id).unwrap(),
            PathBuf::from(chosen.path)
        );
        assert!(registry.finish(url, None, false).is_none());
    }

    #[test]
    fn cancellation_and_failed_destination_do_not_authorize_reveal() {
        let mut registry = Registry::default();
        let chosen = registry
            .prepare(PathBuf::from("C:/Videos/one.mp4"))
            .unwrap();
        registry.cancel(&chosen.download_id).unwrap();
        assert!(registry
            .bind(&chosen.download_id, "blob:http://tauri.localhost/a")
            .is_err());
        let chosen = registry
            .prepare(PathBuf::from("C:/Videos/one.mp4"))
            .unwrap();
        registry
            .bind(&chosen.download_id, "blob:http://tauri.localhost/b")
            .unwrap();
        registry.request("blob:http://tauri.localhost/b");
        let result = registry
            .finish(
                "blob:http://tauri.localhost/b",
                Some(PathBuf::from("C:/wrong.mp4")),
                true,
            )
            .unwrap();
        assert!(!result.success);
        assert!(registry.completed_path(&chosen.download_id).is_err());
    }

    #[test]
    fn abandoned_reservations_expire_and_completed_history_is_bounded() {
        let mut registry = Registry::default();
        let chosen = registry
            .prepare(PathBuf::from("C:/Videos/one.mp4"))
            .unwrap();
        registry.0[0].created = Instant::now() - Duration::from_secs(601);
        assert!(registry
            .bind(&chosen.download_id, "blob:http://tauri.localhost/old")
            .is_err());
        for index in 0..70 {
            let chosen = registry
                .prepare(PathBuf::from(format!("C:/Videos/{index}.mp4")))
                .unwrap();
            registry.cancel(&chosen.download_id).unwrap();
            registry.cancel(&chosen.download_id).unwrap();
        }
        assert_eq!(registry.0.len(), 64);
    }

    #[test]
    fn two_active_downloads_cannot_overwrite_the_same_destination() {
        let mut registry = Registry::default();
        let path = PathBuf::from("C:/Videos/one.mp4");
        let chosen = registry.prepare(path.clone()).unwrap();
        assert!(registry.prepare(path.clone()).is_err());
        registry.cancel(&chosen.download_id).unwrap();
        assert!(registry.prepare(path).is_ok());
    }

    #[test]
    fn only_one_unfinished_download_is_allowed_even_with_windows_path_aliases() {
        let mut registry = Registry::default();
        let chosen = registry
            .prepare(PathBuf::from("C:/Videos/one.mp4"))
            .unwrap();
        assert!(registry
            .prepare(PathBuf::from("C:/Videos/ONE.mp4"))
            .is_err());
        assert!(registry
            .prepare(PathBuf::from("C:/Videos/two.mp4"))
            .is_err());
        registry.cancel(&chosen.download_id).unwrap();
        let chosen = registry
            .prepare(PathBuf::from("C:/Videos/ONE.mp4"))
            .unwrap();
        let url = "blob:http://tauri.localhost/alias";
        registry.bind(&chosen.download_id, url).unwrap();
        registry.request(url).unwrap();
        assert!(registry
            .prepare(PathBuf::from("C:/Videos/two.mp4"))
            .is_err());
        registry
            .finish(url, Some(PathBuf::from(chosen.path)), true)
            .unwrap();
        assert!(registry.prepare(PathBuf::from("C:/Videos/two.mp4")).is_ok());
    }

    #[test]
    fn finished_status_survives_missing_event_delivery() {
        let mut registry = Registry::default();
        assert!(registry.finished_status("unknown").is_none());
        for success in [true, false] {
            let chosen = registry
                .prepare(PathBuf::from("C:/Videos/one.mp4"))
                .unwrap();
            let url = format!("blob:http://tauri.localhost/{success}");
            assert!(registry.finished_status(&chosen.download_id).is_none());
            registry.bind(&chosen.download_id, &url).unwrap();
            registry.request(&url).unwrap();
            assert!(registry.finished_status(&chosen.download_id).is_none());
            registry
                .finish(&url, Some(PathBuf::from(&chosen.path)), success)
                .unwrap();
            let status = registry.finished_status(&chosen.download_id).unwrap();
            assert_eq!(status.download_id, chosen.download_id);
            assert_eq!(status.path, chosen.path);
            assert_eq!(status.success, success);
            assert_eq!(
                status.error.as_deref(),
                (!success).then_some("DOWNLOAD_FAILED")
            );
            assert!(registry.finished_status(&chosen.download_id).is_some());
        }
        let chosen = registry
            .prepare(PathBuf::from("C:/Videos/cancelled.mp4"))
            .unwrap();
        registry.cancel(&chosen.download_id).unwrap();
        assert!(registry.finished_status(&chosen.download_id).is_none());
    }
}
