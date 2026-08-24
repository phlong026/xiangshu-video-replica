mod customer_credentials;

#[cfg(feature = "local-sidecar")]
use std::net::TcpStream;
#[cfg(feature = "local-sidecar")]
use std::process::{Child, Command};
#[cfg(feature = "local-sidecar")]
use std::sync::Mutex;
#[cfg(feature = "local-sidecar")]
use std::time::Duration;
#[cfg(feature = "local-sidecar")]
use tauri::Manager;

// DESK-02 (T29): everything below guards the internal local-sidecar boot.
// The internal build keeps the historical behaviour (default feature set);
// a customer-cloud build compiles `--no-default-features` and ships without
// any of it, so the customer app never auto-boots a local business source
// of truth (dev doc §14).
#[cfg(feature = "local-sidecar")]
const LOCAL_API_ADDR: &str = "127.0.0.1:8000";
#[cfg(feature = "local-sidecar")]
const BOOT_COMMAND_ENV: &str = "VIDEO_REPLICA_BOOT_COMMAND";

/// Holds the spawned local-backend process so it can be terminated on app exit.
#[cfg(feature = "local-sidecar")]
#[derive(Default)]
struct BackendProcess(Mutex<Option<Child>>);

#[cfg(feature = "local-sidecar")]
fn local_api_ready() -> bool {
    match LOCAL_API_ADDR.parse() {
        Ok(socket_addr) => {
            TcpStream::connect_timeout(&socket_addr, Duration::from_millis(300)).is_ok()
        }
        Err(_) => false,
    }
}

#[cfg(feature = "local-sidecar")]
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

#[cfg(feature = "local-sidecar")]
fn boot_command() -> Option<Command> {
    // An explicit env override takes precedence (used in dev and for custom
    // installs). It is a full command line, so run it through the platform
    // shell to tolerate paths containing spaces (e.g. `C:\Program Files\...`).
    if let Ok(raw) = std::env::var(BOOT_COMMAND_ENV) {
        let trimmed = raw.trim();
        if trimmed.is_empty() {
            return default_boot_command();
        }
        if cfg!(windows) {
            let mut command = Command::new("cmd");
            command.args(["/c", trimmed]);
            return Some(command);
        }
        let mut command = Command::new("sh");
        command.args(["-c", trimmed]);
        return Some(command);
    }
    default_boot_command()
}

#[cfg(feature = "local-sidecar")]
fn start_local_services() -> Option<Child> {
    boot_command()?.spawn().ok()
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .invoke_handler(tauri::generate_handler![
            customer_credentials::customer_device_instance_id,
            customer_credentials::customer_save_credentials,
            customer_credentials::customer_load_credentials,
            customer_credentials::customer_clear_session_token,
            customer_credentials::customer_clear_all_credentials,
        ])
        .setup(|_app| {
            // The frontend talks to the local FastAPI on 127.0.0.1:8000. If the
            // backend is not already running, start it via the boot command so
            // the packaged desktop app works end to end without manual setup.
            // (Internal build only — see the DESK-02 note above.)
            #[cfg(feature = "local-sidecar")]
            if !local_api_ready() {
                if let Some(child) = start_local_services() {
                    _app.manage(BackendProcess(Mutex::new(Some(child))));
                }
            }
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("failed to build desktop application")
        .run(|_app_handle, _event| {
            #[cfg(feature = "local-sidecar")]
            if let tauri::RunEvent::Exit = _event {
                if let Some(state) = _app_handle.try_state::<BackendProcess>() {
                    if let Ok(mut guard) = state.0.lock() {
                        if let Some(mut child) = guard.take() {
                            let _ = child.kill();
                        }
                    }
                }
            }
        });
}
