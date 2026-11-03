//! 客户端本地抽音轨（P2 · 决策 #15/C）。
//!
//! 已缓存的视频 → 单声道 16kHz 32k AAC。参数与服务端
//! `media_tools.extract_audio` **逐项一致**（`-vn -ac 1 -ar 16000 -c:a aac
//! -b:a 32k`），否则同一段音频在本地链路与服务端链路会得到不同的转写结果。
//!
//! ffmpeg/ffprobe 随 NSIS 分发（`bundle.resources` 打进 `resources/ffmpeg/`），
//! 因此这里**不依赖 PATH**；找不到就 fail-closed 报明确错误，不静默降级——
//! 与 `media_tools.resolve_media_binary` 是同一姿态。

use std::path::{Path, PathBuf};
use std::process::Command;

use tauri::Manager;

use crate::viral_cache::safe_segment;

/// 在资源目录下按候选相对路径找可执行文件。
///
/// Tauri 按 `bundle.resources` 里写的相对路径原样复制，所以
/// `resources/ffmpeg/ffmpeg.exe` 会落在 `<resource_dir>/resources/ffmpeg/`；
/// 另外两个候选覆盖不同打包口径，免得升级 Tauri 后这里悄悄失效。
pub fn find_within(resource_dir: &Path, file: &str) -> Option<PathBuf> {
    [
        format!("resources/ffmpeg/{file}"),
        format!("ffmpeg/{file}"),
        file.to_string(),
    ]
    .into_iter()
    .map(|relative| resource_dir.join(relative))
    .find(|candidate| candidate.is_file())
}

fn missing_tool_message(resource_dir: &Path, file: &str) -> String {
    format!(
        "安装包缺少 {file}（已在 {} 下查找）。请重新安装完整安装包，不要手工删除安装目录内的文件。",
        resource_dir.display()
    )
}

/// 定位随包分发的 ffmpeg 与 ffprobe。
pub fn locate_tools<R: tauri::Runtime>(
    app: &tauri::AppHandle<R>,
) -> Result<(PathBuf, PathBuf), String> {
    let resource_dir = app
        .path()
        .resource_dir()
        .map_err(|error| format!("无法定位应用资源目录：{error}"))?;
    let ffmpeg = find_within(&resource_dir, "ffmpeg.exe")
        .ok_or_else(|| missing_tool_message(&resource_dir, "ffmpeg.exe"))?;
    let ffprobe = find_within(&resource_dir, "ffprobe.exe")
        .ok_or_else(|| missing_tool_message(&resource_dir, "ffprobe.exe"))?;
    Ok((ffmpeg, ffprobe))
}

/// 只保留 stderr 的尾部：ffmpeg 失败时前面几十行都是无关的流信息，
/// 真正的原因在最后几行。
fn failure_tail(stderr: &str) -> String {
    let trimmed = stderr.trim();
    let lines: Vec<&str> = trimmed.lines().collect();
    let start = lines.len().saturating_sub(4);
    lines[start..].join("\n")
}

/// 抽音轨。参数顺序与服务端一致，便于对照排查。
pub fn extract_audio(ffmpeg: &Path, video: &Path, audio: &Path) -> Result<(), String> {
    let output = Command::new(ffmpeg)
        .arg("-y")
        .arg("-i")
        .arg(video)
        .args([
            "-vn", "-ac", "1", "-ar", "16000", "-c:a", "aac", "-b:a", "32k",
        ])
        .arg(audio)
        .output()
        .map_err(|error| format!("无法启动 ffmpeg：{error}"))?;
    if !output.status.success() {
        return Err(format!(
            "抽取音轨失败：{}",
            failure_tail(&String::from_utf8_lossy(&output.stderr))
        ));
    }
    Ok(())
}

/// 派生音轨的存放位置：与视频同根，单独一个 audio/ 子树，便于整体清理。
/// `pub(crate)`：删除缓存时要连派生的音轨一起清掉（见 viral_cache 的淘汰逻辑）。
pub(crate) fn audio_path(root: &Path, platform: &str, video_id: &str) -> PathBuf {
    root.join("audio")
        .join(safe_segment(platform))
        .join(format!("{}.m4a", safe_segment(video_id)))
}

/// 把用户上传的视频字节经 ffmpeg 抽成音轨字节：写临时文件 → 抽轨 → 读回，
/// 两个临时文件无论成败都清理（清理失败不影响主流程）。
///
/// 与 `extract_audio` 拆开是为了可测：这里只依赖 ffmpeg 路径与字节，不依赖
/// AppHandle / State。
fn extract_audio_from_bytes(ffmpeg: &Path, video_bytes: &[u8]) -> Result<Vec<u8>, String> {
    let workdir = std::env::temp_dir().join(format!("local-audio-{}", uuid::Uuid::new_v4()));
    std::fs::create_dir_all(&workdir).map_err(|error| format!("无法创建临时目录：{error}"))?;
    let video = workdir.join("source.mp4");
    let audio = workdir.join("audio.m4a");
    let cleanup = || {
        let _ = std::fs::remove_file(&video);
        let _ = std::fs::remove_file(&audio);
        let _ = std::fs::remove_dir(&workdir);
    };
    let result = (|| {
        std::fs::write(&video, video_bytes)
            .map_err(|error| format!("无法写入临时视频：{error}"))?;
        extract_audio(ffmpeg, &video, &audio)?;
        std::fs::read(&audio).map_err(|error| format!("读取音轨失败：{error}"))
    })();
    cleanup();
    result
}

/// 用户上传的 >15s 视频走「本地抽音轨 → 只传音频」的文案提取链路：
/// WebView 的文件选择拿不到本地路径，所以前端把视频字节经 raw IPC 原样送来，
/// 抽出的音轨字节原样送回（与 `viral_extract_audio` 同一条原始字节通道，
/// 避免 JSON 数组膨胀）。
#[tauri::command]
pub async fn local_video_extract_audio(
    app: tauri::AppHandle,
    request: tauri::ipc::Request<'_>,
) -> Result<tauri::ipc::Response, String> {
    // 只接受 raw body。JSON 数组形态的 50MB 视频会解析成 GB 级内存，宁可
    // fail-closed 让前后端版本对齐，也不给错误通道留静默降级。
    let video_bytes = match request.body() {
        tauri::ipc::InvokeBody::Raw(raw) => raw.to_vec(),
        tauri::ipc::InvokeBody::Json(_) => {
            return Err("音轨抽取请求格式不正确，请升级客户端后重试。".to_string())
        }
    };
    if video_bytes.is_empty() {
        return Err("视频内容为空，请重新选择文件。".to_string());
    }
    let (ffmpeg, _ffprobe) = locate_tools(&app)?;
    // ffmpeg 是阻塞进程，放到阻塞线程池，别占住异步运行时的工作线程。
    let audio_bytes = tauri::async_runtime::spawn_blocking(move || {
        extract_audio_from_bytes(&ffmpeg, &video_bytes)
    })
    .await
    .map_err(|error| format!("抽取音轨任务异常：{error}"))??;
    Ok(tauri::ipc::Response::new(audio_bytes))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;

    fn scratch(name: &str) -> PathBuf {
        let dir = std::env::temp_dir().join(format!("viral-audio-test-{name}"));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).expect("创建临时目录");
        dir
    }

    #[test]
    fn extract_from_bytes_cleans_up_and_reports_ffmpeg_failure() {
        // 不依赖真实 ffmpeg：随便给一个不存在的可执行文件路径，断言
        // 「命令启动失败」的报错路径，且临时目录已被清理干净。
        let absent = scratch("absent-ffmpeg").join("no-ffmpeg.exe");
        let error = extract_audio_from_bytes(&absent, b"fake video bytes")
            .expect_err("ffmpeg 缺失必须报错");
        assert!(error.contains("无法启动 ffmpeg"), "实际报错：{error}");
        let leftovers: Vec<_> = fs::read_dir(std::env::temp_dir())
            .expect("读临时目录")
            .filter_map(Result::ok)
            .filter(|entry| {
                entry
                    .file_name()
                    .to_string_lossy()
                    .starts_with("local-audio-")
            })
            .collect();
        assert!(leftovers.is_empty(), "临时目录未清理：{:?}", leftovers);
    }

    #[test]
    fn find_within_prefers_the_bundled_resource_layout() {
        let dir = scratch("prefer");
        fs::create_dir_all(dir.join("resources/ffmpeg")).expect("建目录");
        fs::write(dir.join("resources/ffmpeg/ffmpeg.exe"), b"x").expect("写文件");
        assert_eq!(
            find_within(&dir, "ffmpeg.exe"),
            Some(dir.join("resources/ffmpeg/ffmpeg.exe"))
        );
    }

    #[test]
    fn find_within_falls_back_to_flatter_layouts() {
        let dir = scratch("fallback");
        fs::create_dir_all(dir.join("ffmpeg")).expect("建目录");
        fs::write(dir.join("ffmpeg/ffmpeg.exe"), b"x").expect("写文件");
        assert_eq!(
            find_within(&dir, "ffmpeg.exe"),
            Some(dir.join("ffmpeg/ffmpeg.exe"))
        );
    }

    #[test]
    fn find_within_returns_none_when_the_installer_did_not_ship_it() {
        // 缺 ffmpeg 必须能被察觉（fail-closed），绝不能回退到 PATH 上的任意
        // ffmpeg——那会让「随包分发」的保证名存实亡。
        let dir = scratch("absent");
        assert_eq!(find_within(&dir, "ffmpeg.exe"), None);
    }

    #[test]
    fn audio_path_is_scoped_and_path_safe() {
        let root = Path::new("C:/cache");
        let path = audio_path(root, "wechat_channels", "abc/def");
        assert!(path.starts_with(root.join("audio")));
        // 视频号 id 里的 `/` 不能逃出 audio 子树。
        assert!(path.to_string_lossy().contains("abc%2Fdef"));
        assert!(!path.to_string_lossy().contains("abc/def"));
    }

    #[test]
    fn failure_tail_keeps_only_the_last_lines() {
        let stderr = (1..=20)
            .map(|index| format!("line-{index}"))
            .collect::<Vec<_>>()
            .join("\n");
        let tail = failure_tail(&stderr);
        assert!(tail.contains("line-20"));
        assert!(!tail.contains("line-1\n"), "前面几十行流信息不该带出来");
    }
}
