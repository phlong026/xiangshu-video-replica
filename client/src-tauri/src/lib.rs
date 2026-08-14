use std::net::TcpStream;
use std::process::{Child, Command};
use std::sync::Mutex;
use std::time::Duration;

use tauri::Manager;

const LOCAL_API_ADDR: &str = "127.0.0.1:8000";
const BOOT_COMMAND_ENV: &str = "VIDEO_REPLICA_BOOT_COMMAND";

/// Holds the spawned local-backend process so it can be terminated on app exit.
#[derive(Default)]
struct BackendProcess(Mutex<Option<Child>>);

fn local_api_ready() -> bool {
    match LOCAL_API_ADDR.parse() {
        Ok(socket_addr) => {
            TcpStream::connect_timeout(&socket_addr, Duration::from_millis(300)).is_ok()
        }
        Err(_) => false,
    }
}

fn default_boot_command() -> Option<Command> {
    let exe_dir = std::env::current_exe().ok()?.parent()?.to_path_buf();
    if cfg!(windows) {
        let script = exe_dir.join("start-backend.bat");
        if script.exists() {
            let mut command = Command::new("cmd");
            command.args(["/c", script.to_str()?]);
            return Some(command);
        }
    } else {
        let script = exe_dir.join("start-backend.sh");
        if script.exists() {
            let mut command = Command::new("sh");
            command.args([script.to_str()?]);
            return Some(command);
        }
    }
    None
}

fn boot_command() -> Option<Command> {
    // An explicit env override takes precedence (used in dev and for custom installs).
    if let Ok(raw) = std::env::var(BOOT_COMMAND_ENV) {
        let mut parts = raw.split_whitespace();
        let program = parts.next()?.to_string();
        let args: Vec<String> = parts.map(str::to_string).collect();
        let mut command = Command::new(program);
        command.args(&args);
        return Some(command);
    }
    default_boot_command()
}

fn start_local_services() -> Option<Child> {
    boot_command()?.spawn().ok()
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .setup(|app| {
            // The frontend talks to the local FastAPI on 127.0.0.1:8000. If the
            // backend is not already running, start it via the boot command so
            // the packaged desktop app works end to end without manual setup.
            if !local_api_ready() {
                if let Some(child) = start_local_services() {
                    app.manage(BackendProcess(Mutex::new(Some(child))));
                }
            }
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("failed to build desktop application")
        .run(|app_handle, event| {
            if let tauri::RunEvent::Exit = event {
                if let Some(state) = app_handle.try_state::<BackendProcess>() {
                    if let Ok(mut guard) = state.0.lock() {
                        if let Some(mut child) = guard.take() {
                            let _ = child.kill();
                        }
                    }
                }
            }
        });
}
