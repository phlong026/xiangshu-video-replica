//! 用户选择的文件只在本机登记路径；跨 IPC 传递有界元数据和进度。
use std::collections::HashMap;
use std::fs::{self, File};
use std::io::{Read, Write};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::{
    atomic::{AtomicBool, Ordering},
    Arc, Mutex,
};
use std::time::{Duration, Instant, UNIX_EPOCH};

use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use tauri::{Emitter, Manager, WebviewWindow};

#[derive(Clone, Serialize, Deserialize)]
pub struct NativeSource {
    pub native_source_id: String,
    pub account_id: String,
    pub name: String,
    pub size: u64,
    #[serde(rename = "type")]
    pub content_type: String,
    pub duration_seconds: f64,
    pub sha256: String,
}

#[derive(Serialize, Deserialize)]
struct Entry {
    source: NativeSource,
    path: PathBuf,
    modified_ns: u128,
    #[serde(default)]
    generated: bool,
}

#[derive(Clone, Default)]
pub struct SourceJobs(Arc<Mutex<JobRegistry>>);

#[derive(Default)]
struct JobRegistry {
    active: HashMap<String, Arc<AtomicBool>>,
    cancelled: HashMap<String, Instant>,
}

struct Job {
    jobs: SourceJobs,
    key: String,
    cancelled: Arc<AtomicBool>,
}
impl Drop for Job {
    fn drop(&mut self) {
        self.cancelled.store(true, Ordering::Relaxed);
        if let Ok(mut jobs) = self.jobs.0.lock() {
            jobs.active.remove(&self.key);
        }
    }
}
fn job_key(account: &str, id: &str) -> Result<String, String> {
    if account.is_empty()
        || account.len() > 128
        || account.contains('\0')
        || uuid::Uuid::parse_str(id).is_err()
    {
        return Err("来源操作编号无效".into());
    }
    Ok(format!("{account}\0{id}"))
}
fn begin_job(window: &WebviewWindow, account: &str, id: &str) -> Result<Job, String> {
    let jobs = window.state::<SourceJobs>().inner().clone();
    let key = job_key(account, id)?;
    let cancelled = Arc::new(AtomicBool::new(false));
    {
        let mut active = jobs.0.lock().map_err(|_| "来源状态不可用")?;
        active.cancelled.retain(|_, at| at.elapsed() < Duration::from_secs(300));
        // 取消命令可能比后台注册先到，短暂保留取消标记消除这个窗口。
        if active.cancelled.remove(&key).is_some() {
            return Err("来源操作已取消".into());
        }
        if active.active.contains_key(&key) || active.active.len() >= 4 {
            return Err("已有来源操作正在进行，请稍后重试".into());
        }
        active.active.insert(key.clone(), cancelled.clone());
    }
    Ok(Job {
        jobs,
        key,
        cancelled,
    })
}
struct Temporary(PathBuf);
impl Drop for Temporary {
    fn drop(&mut self) {
        let _ = fs::remove_file(&self.0);
    }
}
struct Process(std::process::Child);
impl Drop for Process {
    fn drop(&mut self) {
        let _ = self.0.kill();
        let _ = self.0.wait();
    }
}

fn root(window: &WebviewWindow) -> Result<PathBuf, String> {
    let dir = window
        .app_handle()
        .path()
        .app_local_data_dir()
        .map_err(|_| "无法读取来源目录")?
        .join("source-files");
    fs::create_dir_all(&dir).map_err(|_| "无法创建来源目录")?;
    fs::canonicalize(dir).map_err(|_| "无法定位来源目录".into())
}
fn entry_path(dir: &Path, id: &str) -> Result<PathBuf, String> {
    let parsed = uuid::Uuid::parse_str(id).map_err(|_| "来源编号无效")?;
    Ok(dir.join(format!("{parsed}.json")))
}
fn modified(path: &Path) -> Result<(u64, u128), String> {
    let metadata = fs::metadata(path).map_err(|_| "原视频不存在，请重新定位文件")?;
    if !metadata.is_file() {
        return Err("请选择有效文件".into());
    }
    let time = metadata
        .modified()
        .map_err(|_| "无法核对来源文件")?
        .duration_since(UNIX_EPOCH)
        .map_err(|_| "来源时间无效")?
        .as_nanos();
    Ok((metadata.len(), time))
}
fn load(dir: &Path, id: &str, account: &str) -> Result<Entry, String> {
    let path = entry_path(dir, id)?;
    let mut data = Vec::new();
    File::open(&path)
        .map_err(|_| "未找到本机原片登记，请重新选择视频")?
        .take(16_385)
        .read_to_end(&mut data)
        .map_err(|_| "来源登记读取失败")?;
    if data.len() > 16_384 {
        return Err("来源登记无效，请重新选择视频".into());
    }
    let entry: Entry = serde_json::from_slice(&data).map_err(|_| "来源登记无效")?;
    if entry.source.account_id != account || entry.source.native_source_id != id {
        return Err("来源不属于当前账号".into());
    }
    Ok(entry)
}
fn checked(dir: &Path, id: &str, account: &str) -> Result<Entry, String> {
    let entry = load(dir, id, account)?;
    let (size, time) = modified(&entry.path)?;
    if size != entry.source.size || time != entry.modified_ns {
        return Err("原视频已变化，请重新定位并校验文件".into());
    }
    Ok(entry)
}
fn digest(path: &Path) -> Result<String, String> {
    let mut file = File::open(path).map_err(|_| "无法打开来源文件")?;
    let mut hash = Sha256::new();
    let mut buffer = vec![0u8; 1024 * 1024];
    loop {
        let n = file.read(&mut buffer).map_err(|_| "读取来源文件失败")?;
        if n == 0 {
            break;
        }
        hash.update(&buffer[..n]);
    }
    Ok(format!("{:x}", hash.finalize()))
}
fn hidden(command: &mut Command) {
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        command.creation_flags(0x08000000);
    }
    #[cfg(not(windows))]
    {
        let _ = command;
    }
}
fn probe(dir: &Path, ffprobe: &Path, path: &Path) -> Result<f64, String> {
    let output = Temporary(dir.join(format!("{}.probe", uuid::Uuid::new_v4())));
    let file = File::create(&output.0).map_err(|_| "无法创建探测文件")?;
    let mut command = Command::new(ffprobe);
    command
        .args([
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "json",
        ])
        .arg(path)
        .stdin(Stdio::null())
        .stderr(Stdio::null())
        .stdout(file);
    hidden(&mut command);
    let mut child = Process(command.spawn().map_err(|_| "无法启动媒体探测")?);
    let deadline = Instant::now() + Duration::from_secs(15);
    loop {
        match child.0.try_wait().map_err(|_| "媒体探测异常")? {
            Some(status) if status.success() => break,
            Some(_) => return Err("无法识别视频，请选择有效 MP4 或 MOV".into()),
            None if Instant::now() > deadline => return Err("媒体探测超时，请重试".into()),
            None => std::thread::sleep(Duration::from_millis(100)),
        }
    }
    let mut bytes = Vec::new();
    File::open(&output.0)
        .map_err(|_| "无法读取探测结果")?
        .take(65_536)
        .read_to_end(&mut bytes)
        .map_err(|_| "无法读取探测结果")?;
    let json: serde_json::Value = serde_json::from_slice(&bytes).map_err(|_| "媒体信息无效")?;
    let duration = json["format"]["duration"]
        .as_str()
        .and_then(|v| v.parse::<f64>().ok())
        .filter(|v| v.is_finite() && *v >= 3.9 && *v <= 43_200.0)
        .ok_or("视频需为 4 秒至 12 小时")?;
    Ok(duration)
}
fn register(
    dir: &Path,
    account: &str,
    path: PathBuf,
    duration: f64,
    existing: Option<&str>,
) -> Result<NativeSource, String> {
    register_impl(dir, account, path, duration, existing, false)
}
fn register_generated(dir: &Path, account: &str, path: PathBuf, duration: f64) -> Result<NativeSource, String> {
    register_impl(dir, account, path, duration, None, true)
}
fn register_impl(
    dir: &Path, account: &str, path: PathBuf, duration: f64,
    existing: Option<&str>, generated: bool,
) -> Result<NativeSource, String> {
    let path = fs::canonicalize(path).map_err(|_| "无法定位来源文件")?;
    let (size, modified_ns) = modified(&path)?;
    if size == 0 || size > 20 * 1024u64.pow(3) {
        return Err("来源文件需大于零且不超过 20 GiB".into());
    }
    let extension = path
        .extension()
        .and_then(|e| e.to_str())
        .unwrap_or("")
        .to_ascii_lowercase();
    let content_type = match extension.as_str() {
        "mp4" => "video/mp4",
        "mov" => "video/quicktime",
        "m4a" => "audio/mp4",
        _ => return Err("来源格式不支持".into()),
    };
    let hash = digest(&path)?;
    if modified(&path)? != (size, modified_ns) {
        return Err("读取期间文件已变化，请重试".into());
    }
    let id = if let Some(id) = existing {
        let old = load(dir, id, account)?;
        if old.source.sha256 != hash {
            return Err("所选文件与原视频内容不一致".into());
        }
        id.to_string()
    } else {
        uuid::Uuid::new_v4().to_string()
    };
    let source = NativeSource {
        native_source_id: id.clone(),
        account_id: account.into(),
        name: path
            .file_name()
            .ok_or("来源名称无效")?
            .to_string_lossy()
            .into(),
        size,
        content_type: content_type.into(),
        duration_seconds: duration,
        sha256: hash,
    };
    let entry = Entry {
        source: source.clone(),
        path,
        modified_ns,
        generated,
    };
    let temp = Temporary(dir.join(format!("{}.tmp", uuid::Uuid::new_v4())));
    {
        let mut f = File::create(&temp.0).map_err(|_| "无法保存来源")?;
        f.write_all(&serde_json::to_vec(&entry).map_err(|_| "来源序列化失败")?)
            .map_err(|_| "保存来源失败")?;
        f.sync_all().map_err(|_| "保存来源失败")?;
    }
    fs::rename(&temp.0, entry_path(dir, &id)?).map_err(|_| "保存来源失败")?;
    Ok(source)
}

#[tauri::command]
pub async fn choose_local_source(
    window: WebviewWindow,
    account_id: String,
    replace_id: Option<String>,
) -> Result<Option<NativeSource>, String> {
    crate::video_downloads::local_origin(window.as_ref())?;
    if account_id.is_empty() || account_id.len() > 128 {
        return Err("账号信息无效".into());
    }
    let dir = root(&window)?;
    if let Some(id) = &replace_id {
        load(&dir, id, &account_id)?;
    }
    let Some(path) = crate::video_downloads::choose_source_path(&window).await? else {
        return Ok(None);
    };
    if !path
        .extension()
        .is_some_and(|e| e.eq_ignore_ascii_case("mp4") || e.eq_ignore_ascii_case("mov"))
    {
        return Err("请选择 MP4 或 MOV 视频".into());
    }
    let (_, ffprobe) = crate::viral_audio::locate_tools(window.app_handle())?;
    tauri::async_runtime::spawn_blocking(move || {
        let duration = probe(&dir, &ffprobe, &path)?;
        register(&dir, &account_id, path, duration, replace_id.as_deref()).map(Some)
    })
    .await
    .map_err(|_| "读取来源失败")?
}

#[tauri::command]
pub fn get_local_source(
    window: WebviewWindow,
    account_id: String,
    source_id: String,
) -> Result<NativeSource, String> {
    crate::video_downloads::local_origin(window.as_ref())?;
    Ok(checked(&root(&window)?, &source_id, &account_id)?.source)
}

#[tauri::command]
pub fn cancel_source_operation(
    window: WebviewWindow,
    account_id: String,
    operation_id: String,
) -> Result<(), String> {
    crate::video_downloads::local_origin(window.as_ref())?;
    let state = window.state::<SourceJobs>();
    let mut active = state.0.lock().map_err(|_| "来源状态不可用")?;
    let key = job_key(&account_id, &operation_id)?;
    if let Some(flag) = active.active.get(&key) {
        flag.store(true, Ordering::Relaxed);
    } else {
        active.cancelled.retain(|_, at| at.elapsed() < Duration::from_secs(300));
        if active.cancelled.len() >= 128 {
            if let Some(oldest) = active.cancelled.iter().min_by_key(|(_, at)| **at).map(|(key, _)| key.clone()) {
                active.cancelled.remove(&oldest);
            }
        }
        active.cancelled.insert(key, Instant::now());
    }
    Ok(())
}

#[tauri::command]
pub async fn extract_source_audio(
    window: WebviewWindow,
    account_id: String,
    source_id: String,
    operation_id: String,
) -> Result<NativeSource, String> {
    crate::video_downloads::local_origin(window.as_ref())?;
    let dir = root(&window)?;
    let entry = checked(&dir, &source_id, &account_id)?;
    let (ffmpeg, _) = crate::viral_audio::locate_tools(window.app_handle())?;
    let job = begin_job(&window, &account_id, &operation_id)?;
    tauri::async_runtime::spawn_blocking(move || {
        let mut audio = Temporary(dir.join(format!("{}.m4a", uuid::Uuid::new_v4())));
        let mut command = Command::new(ffmpeg);
        command
            .args(["-y", "-nostdin", "-v", "error", "-i"])
            .arg(&entry.path)
            .args([
                "-vn", "-ac", "1", "-ar", "16000", "-c:a", "aac", "-b:a", "32k",
            ])
            .arg(&audio.0)
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null());
        hidden(&mut command);
        let mut child = Process(command.spawn().map_err(|_| "无法启动音轨提取")?);
        let deadline = Instant::now()
            + Duration::from_secs(
                (entry.source.duration_seconds * 0.5 + 120.0).clamp(180.0, 7200.0) as u64,
            );
        let mut last_progress = Instant::now();
        let mut bytes = 0;
        loop {
            if job.cancelled.load(Ordering::Relaxed) {
                return Err("音轨提取已取消".into());
            }
            match child.0.try_wait().map_err(|_| "音轨提取异常")? {
                Some(status) if status.success() => break,
                Some(_) => return Err("音轨提取失败，请检查视频是否含有效语音".into()),
                None => {}
            }
            let size = fs::metadata(&audio.0).map(|m| m.len()).unwrap_or(0);
            if size != bytes {
                bytes = size;
                last_progress = Instant::now();
            }
            if Instant::now() > deadline || last_progress.elapsed() > Duration::from_secs(90) {
                return Err("音轨提取超时，可稍后恢复或选择片段".into());
            }
            std::thread::sleep(Duration::from_millis(200));
        }
        checked(&dir, &source_id, &account_id)?;
        let result = register_generated(
            &dir,
            &account_id,
            audio.0.clone(),
            entry.source.duration_seconds,
        )?;
        // 成功音轨属于受控缓存，保留供上传；其余路径均由 RAII 清理。
        audio.0.clear();
        Ok(result)
    })
    .await
    .map_err(|_| "音轨任务异常退出")?
}

fn upload_url(value: &str) -> Result<reqwest::Url, String> {
    let url = reqwest::Url::parse(value).map_err(|_| "上传地址无效")?;
    let host = url.host_str().unwrap_or("");
    if url.scheme() != "https"
        || !host.ends_with(".myqcloud.com")
        || !url.username().is_empty()
        || url.password().is_some()
        || url.port().is_some_and(|p| p != 443)
    {
        return Err("原生上传需要有效的云存储签名地址".into());
    }
    Ok(url)
}

#[tauri::command]
pub async fn trim_source_video(
    window: WebviewWindow, account_id: String, source_id: String, operation_id: String,
    start_seconds: f64, end_seconds: f64,
) -> Result<NativeSource, String> {
    crate::video_downloads::local_origin(window.as_ref())?;
    let dir = root(&window)?;
    let entry = checked(&dir, &source_id, &account_id)?;
    if !start_seconds.is_finite() || !end_seconds.is_finite() || start_seconds < 0.0
        || !(4.0..=180.0).contains(&(end_seconds - start_seconds))
        || end_seconds > entry.source.duration_seconds + 0.1 {
        return Err("请选择原视频内 4–180 秒的连续片段".into());
    }
    let job = begin_job(&window, &account_id, &operation_id)?;
    let (ffmpeg, ffprobe) = crate::viral_audio::locate_tools(window.app_handle())?;
    tauri::async_runtime::spawn_blocking(move || {
        let mut output = Temporary(dir.join(format!("{}.mp4", uuid::Uuid::new_v4())));
        let mut command = Command::new(ffmpeg);
        command.args(["-y", "-nostdin", "-v", "error", "-threads", "2", "-max_alloc", "268435456", "-ss"])
            .arg(start_seconds.to_string()).arg("-i").arg(&entry.path)
            .arg("-t").arg((end_seconds - start_seconds).to_string())
            .args(["-vf", "scale=w=min(1920\\,iw):h=min(1920\\,ih):force_original_aspect_ratio=decrease:force_divisible_by=2",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "22", "-maxrate", "8M", "-bufsize", "16M",
                "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart"])
            .arg(&output.0).stdin(Stdio::null()).stdout(Stdio::null()).stderr(Stdio::null());
        hidden(&mut command);
        let mut process = Process(command.spawn().map_err(|_| "无法启动视频选段")?);
        let started = Instant::now();
        let mut last_growth = started;
        let mut bytes = 0;
        loop {
            if job.cancelled.load(Ordering::Relaxed) { return Err("视频选段已取消".into()); }
            match process.0.try_wait().map_err(|_| "视频选段异常")? {
                Some(status) if status.success() => break,
                Some(_) => return Err("视频选段处理失败，请检查原视频".into()),
                None => {}
            }
            let size = fs::metadata(&output.0).map(|m| m.len()).unwrap_or(0);
            if size > bytes { bytes = size; last_growth = Instant::now(); }
            if size > 600 * 1024 * 1024 || started.elapsed() > Duration::from_secs(600)
                || last_growth.elapsed() > Duration::from_secs(90) { return Err("视频选段超时或文件过大".into()); }
            std::thread::sleep(Duration::from_millis(200));
        }
        checked(&dir, &source_id, &account_id)?;
        let duration = probe(&dir, &ffprobe, &output.0)?;
        if (duration - (end_seconds - start_seconds)).abs() > 0.15 { return Err("选段时长校验失败".into()); }
        let result = register_generated(&dir, &account_id, output.0.clone(), duration)?;
        output.0.clear();
        Ok(result)
    }).await.map_err(|_| "视频选段任务异常退出")?
}

fn generated_audio(dir: &Path, entry: &Entry) -> bool {
    entry.generated
        && matches!(entry.source.content_type.as_str(), "audio/mp4" | "video/mp4")
        && entry.path.parent() == Some(dir)
        && entry.path.extension().is_some_and(|v| v == "m4a" || v == "mp4")
        && entry
            .path
            .file_stem()
            .and_then(|v| v.to_str())
            .is_some_and(|v| uuid::Uuid::parse_str(v).is_ok())
}

#[tauri::command]
pub fn release_source_audio(
    window: WebviewWindow,
    account_id: String,
    source_id: String,
) -> Result<(), String> {
    crate::video_downloads::local_origin(window.as_ref())?;
    let dir = root(&window)?;
    let entry = load(&dir, &source_id, &account_id)?;
    if !generated_audio(&dir, &entry) {
        return Err("用户原文件不能作为缓存删除".into());
    }
    if entry.path.exists() {
        fs::remove_file(&entry.path).map_err(|_| "音频缓存清理失败")?;
    }
    fs::remove_file(entry_path(&dir, &source_id)?).map_err(|_| "音频缓存登记清理失败")?;
    Ok(())
}

pub(crate) fn prune_cached_audio(app: &tauri::AppHandle) {
    let Ok(dir) = app
        .path()
        .app_local_data_dir()
        .map(|v| v.join("source-files"))
    else {
        return;
    };
    let Ok(dir) = fs::canonicalize(dir) else {
        return;
    };
    let Ok(files) = fs::read_dir(&dir) else {
        return;
    };
    for file in files.flatten() {
        let path = file.path();
        if path.extension().is_none_or(|v| v != "json") {
            continue;
        }
        let Ok(metadata) = file.metadata() else {
            continue;
        };
        if metadata.len() > 16_384
            || metadata
                .modified()
                .ok()
                .and_then(|v| v.elapsed().ok())
                .is_none_or(|age| age < Duration::from_secs(7 * 86400))
        {
            continue;
        }
        let Some(id) = path.file_stem().and_then(|v| v.to_str()) else {
            continue;
        };
        if uuid::Uuid::parse_str(id).is_err() {
            continue;
        }
        let Ok(bytes) = fs::read(&path) else {
            continue;
        };
        let Ok(entry) = serde_json::from_slice::<Entry>(&bytes) else {
            continue;
        };
        if entry.source.native_source_id != id || !generated_audio(&dir, &entry) {
            continue;
        }
        if fs::remove_file(&entry.path).is_ok() || !entry.path.exists() {
            let _ = fs::remove_file(&path);
        }
    }
}
struct UploadReader {
    file: File,
    job: Job,
    window: WebviewWindow,
    operation: String,
    size: u64,
    sent: u64,
    percent: u64,
}
impl Read for UploadReader {
    fn read(&mut self, buffer: &mut [u8]) -> std::io::Result<usize> {
        if self.job.cancelled.load(Ordering::Relaxed) {
            return Err(std::io::Error::new(
                std::io::ErrorKind::Interrupted,
                "上传已取消",
            ));
        }
        let n = self.file.read(buffer)?;
        self.sent += n as u64;
        let percent = (self.sent * 100 / self.size).min(100);
        if percent > self.percent {
            self.percent = percent;
            let _ = self.window.emit(
                "source-upload-progress",
                serde_json::json!({"operation_id":self.operation,"percent":percent}),
            );
        }
        Ok(n)
    }
}
#[tauri::command]
pub async fn upload_local_source(
    window: WebviewWindow,
    account_id: String,
    source_id: String,
    operation_id: String,
    url: String,
    headers: HashMap<String, String>,
) -> Result<(), String> {
    crate::video_downloads::local_origin(window.as_ref())?;
    let url = upload_url(&url)?;
    let entry = checked(&root(&window)?, &source_id, &account_id)?;
    let job = begin_job(&window, &account_id, &operation_id)?;
    tauri::async_runtime::spawn_blocking(move || {
        let client = reqwest::blocking::Client::builder()
            .redirect(reqwest::redirect::Policy::none())
            .connect_timeout(Duration::from_secs(30))
            .timeout(Duration::from_secs(
                (entry.source.size / 200_000 + 60).clamp(60, 7200),
            ))
            .build()
            .map_err(|_| "无法初始化上传")?;
        let file = File::open(&entry.path).map_err(|_| "无法读取原片")?;
        let reader = UploadReader {
            file,
            job,
            window,
            operation: operation_id,
            size: entry.source.size,
            sent: 0,
            percent: 0,
        };
        let mut request = client
            .put(url)
            .header("Content-Length", entry.source.size)
            .body(reqwest::blocking::Body::new(reader));
        for (name, value) in headers {
            if name.eq_ignore_ascii_case("content-type")
                || name.to_ascii_lowercase().starts_with("x-cos-")
            {
                request = request.header(name, value);
            } else {
                return Err("云上传请求包含不支持的头字段".into());
            }
        }
        let response = request.send().map_err(|_| "原片上传中断，可稍后重试")?;
        if !response.status().is_success() {
            return Err(format!("云上传未完成（{}）", response.status().as_u16()));
        }
        Ok(())
    })
    .await
    .map_err(|_| "上传任务异常退出")?
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn restart_relocation_and_account_scope() {
        let dir = std::env::temp_dir().join(format!("source-test-{}", uuid::Uuid::new_v4()));
        fs::create_dir_all(&dir).unwrap();
        let video = dir.join("原片.mp4");
        fs::write(&video, b"video").unwrap();
        let source = register(&dir, "one", video.clone(), 60.0, None).unwrap();
        assert_eq!(
            checked(&dir, &source.native_source_id, "one")
                .unwrap()
                .source
                .sha256,
            source.sha256
        );
        assert!(checked(&dir, &source.native_source_id, "two").is_err());
        let moved = dir.join("移动后.mp4");
        fs::rename(&video, &moved).unwrap();
        assert!(checked(&dir, &source.native_source_id, "one").is_err());
        register(
            &dir,
            "one",
            moved.clone(),
            60.0,
            Some(&source.native_source_id),
        )
        .unwrap();
        assert!(checked(&dir, &source.native_source_id, "one").is_ok());
        fs::write(&moved, b"different").unwrap();
        assert!(checked(&dir, &source.native_source_id, "one").is_err());
        assert!(dir
            .canonicalize()
            .unwrap()
            .starts_with(std::env::temp_dir().canonicalize().unwrap()));
        fs::remove_dir_all(&dir).unwrap();
    }
    #[test]
    fn rejects_paths_and_upload_redirections() {
        assert!(entry_path(Path::new("cache"), "../../secret").is_err());
        for url in [
            "http://a.myqcloud.com/x",
            "https://a.myqcloud.com.evil/x",
            "https://127.0.0.1/x",
            "https://user@a.myqcloud.com/x",
        ] {
            assert!(upload_url(url).is_err());
        }
        assert!(upload_url("https://bucket.cos.ap-shanghai.myqcloud.com/source?sign=test").is_ok());
    }
}
