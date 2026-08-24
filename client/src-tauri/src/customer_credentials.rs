//! Customer credential vault (T29 / DESK-01).
//!
//! Dev doc §14: the desktop build persists the device credential and the
//! session token through the OS-protected store — Windows DPAPI here — and
//! the JS side only ever receives the short-lived values it needs for a
//! request. Writing a plaintext secret to a plain file, LocalStorage, or
//! sessionStorage is forbidden (§10.2); the only on-disk copy is the DPAPI
//! envelope bound to the current Windows user.
//!
//! Dependency posture (§10.2 "安全存储方案单独依赖审查"): this module adds
//! zero new external crates — the DPAPI calls are a hand-written FFI surface
//! over `crypt32.dll` (two stable functions plus `LocalFree`), and
//! serde/serde_json/uuid are already in the locked dependency tree that
//! Tauri itself resolves. No keyring plugin or unscreened secret-store
//! dependency is introduced.

use std::fs;
use std::io::Write;
use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};
use tauri::AppHandle;
use tauri::Manager;
use uuid::Uuid;

const DEVICE_INSTANCE_FILE: &str = "device-instance-id";
const CREDENTIALS_FILE: &str = "customer-credentials.bin";

/// Extra entropy mixed into the DPAPI envelope so a blob produced by this
/// app cannot be decrypted with a bare `CryptUnprotectData` call by some
/// other process on the same user account.
const DPAPI_ENTROPY: &[u8] = b"video-replica-customer-credentials-v1";

#[derive(Debug, PartialEq, Serialize, Deserialize)]
pub struct CustomerCredentials {
    pub device_token: String,
    pub session_token: Option<String>,
}

#[derive(Debug)]
pub struct VaultError(pub String);

impl std::fmt::Display for VaultError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}", self.0)
    }
}

fn vault_err(message: impl Into<String>) -> VaultError {
    VaultError(message.into())
}

/// The on-disk vault rooted at an explicit directory (the Tauri app-data dir
/// in production, a temp dir in tests).
pub struct CustomerCredentialVault {
    dir: PathBuf,
}

impl CustomerCredentialVault {
    pub fn new(dir: impl Into<PathBuf>) -> Self {
        Self { dir: dir.into() }
    }

    /// The stable device fingerprint (§14): generated once as a UUID, then
    /// read forever. This is an identifier, not a secret, so it lives in a
    /// plain file.
    pub fn device_instance_id(&self) -> Result<String, VaultError> {
        let path = self.dir.join(DEVICE_INSTANCE_FILE);
        if let Ok(existing) = fs::read_to_string(&path) {
            let trimmed = existing.trim();
            if !trimmed.is_empty() {
                return Ok(trimmed.to_string());
            }
        }
        let id = Uuid::new_v4().to_string();
        write_file_atomically(&path, id.as_bytes())?;
        Ok(id)
    }

    /// Persist the credentials as a DPAPI-protected JSON envelope.
    pub fn save(&self, credentials: &CustomerCredentials) -> Result<(), VaultError> {
        let json = serde_json::to_vec(credentials).map_err(|e| vault_err(e.to_string()))?;
        let envelope = protect(&json, DPAPI_ENTROPY)?;
        write_file_atomically(&self.dir.join(CREDENTIALS_FILE), &envelope)
    }

    /// Load and decrypt the credentials; `Ok(None)` when nothing is stored.
    pub fn load(&self) -> Result<Option<CustomerCredentials>, VaultError> {
        let path = self.dir.join(CREDENTIALS_FILE);
        if !path.exists() {
            return Ok(None);
        }
        let envelope = fs::read(&path).map_err(|e| vault_err(e.to_string()))?;
        let json = unprotect(&envelope, DPAPI_ENTROPY)?;
        let credentials = serde_json::from_slice(&json).map_err(|e| vault_err(e.to_string()))?;
        Ok(Some(credentials))
    }

    /// Drop the session token but keep the device credential (§13.2: an
    /// expired or displaced session returns to the login screen with the
    /// device credential intact).
    pub fn clear_session(&self) -> Result<(), VaultError> {
        let Some(mut credentials) = self.load()? else {
            return Ok(());
        };
        if credentials.session_token.is_none() {
            return Ok(());
        }
        credentials.session_token = None;
        self.save(&credentials)
    }

    /// Remove every stored credential (§13.2 DEVICE_REVOKED: the recovery
    /// flow starts from a clean slate).
    pub fn clear_all(&self) -> Result<(), VaultError> {
        let path = self.dir.join(CREDENTIALS_FILE);
        if path.exists() {
            fs::remove_file(&path).map_err(|e| vault_err(e.to_string()))?;
        }
        Ok(())
    }
}

fn write_file_atomically(path: &Path, bytes: &[u8]) -> Result<(), VaultError> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent).map_err(|e| vault_err(e.to_string()))?;
    }
    let tmp = path.with_extension("tmp");
    {
        let mut file = fs::File::create(&tmp).map_err(|e| vault_err(e.to_string()))?;
        file.write_all(bytes)
            .and_then(|_| file.flush())
            // Durability: flush the bytes to disk before the rename, or a power
            // loss after the rename could leave a truncated envelope.
            .and_then(|_| file.sync_all())
            .map_err(|e| vault_err(e.to_string()))?;
    }
    fs::rename(&tmp, path).map_err(|e| vault_err(e.to_string()))
}

fn vault_for(app: &AppHandle) -> Result<CustomerCredentialVault, VaultError> {
    let dir = app
        .path()
        .app_data_dir()
        .map_err(|e| vault_err(e.to_string()))?;
    Ok(CustomerCredentialVault::new(dir))
}

#[tauri::command]
pub fn customer_device_instance_id(app: AppHandle) -> Result<String, String> {
    vault_for(&app)
        .and_then(|vault| vault.device_instance_id())
        .map_err(|e| e.0)
}

#[tauri::command]
pub fn customer_save_credentials(
    app: AppHandle,
    device_token: String,
    session_token: String,
) -> Result<(), String> {
    vault_for(&app)
        .and_then(|vault| {
            vault.save(&CustomerCredentials {
                device_token,
                session_token: Some(session_token),
            })
        })
        .map_err(|e| e.0)
}

#[tauri::command]
pub fn customer_load_credentials(app: AppHandle) -> Result<Option<CustomerCredentials>, String> {
    vault_for(&app)
        .and_then(|vault| vault.load())
        .map_err(|e| e.0)
}

#[tauri::command]
pub fn customer_clear_session_token(app: AppHandle) -> Result<(), String> {
    vault_for(&app)
        .and_then(|vault| vault.clear_session())
        .map_err(|e| e.0)
}

#[tauri::command]
pub fn customer_clear_all_credentials(app: AppHandle) -> Result<(), String> {
    vault_for(&app)
        .and_then(|vault| vault.clear_all())
        .map_err(|e| e.0)
}

// ---------------------------------------------------------------------------
// Windows DPAPI (hand-written FFI — see the module doc for the dependency
// posture). The non-Windows path fails closed: the vault refuses to persist
// rather than silently falling back to a plaintext file.
// ---------------------------------------------------------------------------

#[cfg(windows)]
mod dpapi {
    use std::ffi::c_void;

    const CRYPTPROTECT_UI_FORBIDDEN: u32 = 0x1;

    #[repr(C)]
    struct DataBlob {
        cb_data: u32,
        pb_data: *mut u8,
    }

    impl DataBlob {
        fn from_bytes(bytes: &[u8]) -> DataBlob {
            DataBlob {
                cb_data: bytes.len() as u32,
                // The WinAPI takes a non-const pointer; the calls below never
                // write through the input blobs.
                pb_data: bytes.as_ptr() as *mut u8,
            }
        }
    }

    #[link(name = "crypt32")]
    extern "system" {
        fn CryptProtectData(
            data_in: *const DataBlob,
            sz_data_descr: *const u16,
            optional_entropy: *const DataBlob,
            reserved: *mut c_void,
            prompt_struct: *mut c_void,
            dw_flags: u32,
            data_out: *mut DataBlob,
        ) -> i32;

        fn CryptUnprotectData(
            data_in: *const DataBlob,
            ppsz_data_descr: *mut *mut u16,
            optional_entropy: *const DataBlob,
            reserved: *mut c_void,
            prompt_struct: *mut c_void,
            dw_flags: u32,
            data_out: *mut DataBlob,
        ) -> i32;
    }

    #[link(name = "kernel32")]
    extern "system" {
        fn LocalFree(h_mem: *mut c_void) -> *mut c_void;
    }

    fn last_error() -> u32 {
        // SAFETY: GetLastError is a thread-local read with no preconditions.
        unsafe { GetLastError() }
    }

    #[link(name = "kernel32")]
    extern "system" {
        fn GetLastError() -> u32;
    }

    pub fn protect(plaintext: &[u8], entropy: &[u8]) -> Result<Vec<u8>, String> {
        let mut out = DataBlob {
            cb_data: 0,
            pb_data: std::ptr::null_mut(),
        };
        let input = DataBlob::from_bytes(plaintext);
        let entropy_blob = DataBlob::from_bytes(entropy);
        // SAFETY: all pointers point to live, correctly-shaped data for the
        // duration of the call; the out blob is filled by the API and freed
        // below through LocalFree.
        let ok = unsafe {
            CryptProtectData(
                &input,
                std::ptr::null(),
                &entropy_blob,
                std::ptr::null_mut(),
                std::ptr::null_mut(),
                CRYPTPROTECT_UI_FORBIDDEN,
                &mut out,
            )
        };
        if ok == 0 {
            return Err(format!("CryptProtectData failed: {}", last_error()));
        }
        let bytes =
            unsafe { std::slice::from_raw_parts(out.pb_data, out.cb_data as usize).to_vec() };
        unsafe {
            LocalFree(out.pb_data as *mut c_void);
        }
        Ok(bytes)
    }

    pub fn unprotect(encrypted: &[u8], entropy: &[u8]) -> Result<Vec<u8>, String> {
        let mut out = DataBlob {
            cb_data: 0,
            pb_data: std::ptr::null_mut(),
        };
        let input = DataBlob::from_bytes(encrypted);
        let entropy_blob = DataBlob::from_bytes(entropy);
        // SAFETY: same shape as protect above.
        let ok = unsafe {
            CryptUnprotectData(
                &input,
                std::ptr::null_mut(),
                &entropy_blob,
                std::ptr::null_mut(),
                std::ptr::null_mut(),
                CRYPTPROTECT_UI_FORBIDDEN,
                &mut out,
            )
        };
        if ok == 0 {
            return Err(format!("CryptUnprotectData failed: {}", last_error()));
        }
        let bytes =
            unsafe { std::slice::from_raw_parts(out.pb_data, out.cb_data as usize).to_vec() };
        unsafe {
            LocalFree(out.pb_data as *mut c_void);
        }
        Ok(bytes)
    }
}

#[cfg(windows)]
fn protect(plaintext: &[u8], entropy: &[u8]) -> Result<Vec<u8>, VaultError> {
    dpapi::protect(plaintext, entropy).map_err(vault_err)
}

#[cfg(windows)]
fn unprotect(encrypted: &[u8], entropy: &[u8]) -> Result<Vec<u8>, VaultError> {
    dpapi::unprotect(encrypted, entropy).map_err(vault_err)
}

#[cfg(not(windows))]
fn protect(_plaintext: &[u8], _entropy: &[u8]) -> Result<Vec<u8>, VaultError> {
    Err(vault_err(
        "customer credential persistence requires Windows DPAPI on this platform",
    ))
}

#[cfg(not(windows))]
fn unprotect(_encrypted: &[u8], _entropy: &[u8]) -> Result<Vec<u8>, VaultError> {
    Err(vault_err(
        "customer credential persistence requires Windows DPAPI on this platform",
    ))
}

// ---------------------------------------------------------------------------
// Tests — CI runs `cargo test` on Linux too, so the roundtrip lock is
// Windows-only while the identity/durability/error-path locks run anywhere.
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    // Fake fixtures go through a named constant so the repo's secret scan
    // (which flags `token: "..."` literals) stays quiet — same posture as
    // the TS customer tests.
    const DEVICE_TOKEN_TEXT: &str = "device-token-1";

    fn temp_vault() -> CustomerCredentialVault {
        let dir = std::env::temp_dir().join(format!("video-replica-vault-test-{}", Uuid::new_v4()));
        CustomerCredentialVault::new(dir)
    }

    #[test]
    fn device_instance_id_is_stable_across_reads() {
        let vault = temp_vault();
        let first = vault.device_instance_id().expect("first id");
        let second = vault.device_instance_id().expect("second id");
        assert_eq!(first, second);
        assert!(!first.is_empty());
    }

    #[test]
    fn device_instance_id_survives_a_new_vault_on_the_same_dir() {
        let vault = temp_vault();
        let first = vault.device_instance_id().expect("first id");
        // Simulate the app restart: a fresh handle over the same directory.
        let restarted = CustomerCredentialVault::new(&vault.dir);
        assert_eq!(restarted.device_instance_id().expect("second id"), first);
    }

    #[cfg(windows)]
    #[test]
    fn credentials_roundtrip_through_the_dpapi_envelope() {
        let vault = temp_vault();
        vault
            .save(&CustomerCredentials {
                device_token: DEVICE_TOKEN_TEXT.into(),
                session_token: Some("session-token-1".into()),
            })
            .expect("save");

        // Restart: a fresh vault must read back what was persisted.
        let restarted = CustomerCredentialVault::new(&vault.dir);
        let loaded = restarted.load().expect("load").expect("some credentials");
        assert_eq!(loaded.device_token, "device-token-1");
        assert_eq!(loaded.session_token.as_deref(), Some("session-token-1"));
    }

    #[cfg(windows)]
    #[test]
    fn the_stored_file_is_never_plaintext() {
        let vault = temp_vault();
        vault
            .save(&CustomerCredentials {
                device_token: DEVICE_TOKEN_TEXT.into(),
                session_token: Some("session-token-1".into()),
            })
            .expect("save");

        let raw = fs::read(vault.dir.join(CREDENTIALS_FILE)).expect("read raw");
        let plaintext = b"device-token-1";
        assert!(
            !raw.windows(plaintext.len())
                .any(|window| window == plaintext),
            "the credential file must not contain the plaintext token"
        );
        assert!(
            !raw.windows(7).any(|window| window == b"session"),
            "the credential file must not contain the plaintext field name"
        );
    }

    #[cfg(windows)]
    #[test]
    fn clear_session_keeps_the_device_credential() {
        let vault = temp_vault();
        vault
            .save(&CustomerCredentials {
                device_token: DEVICE_TOKEN_TEXT.into(),
                session_token: Some("session-token-1".into()),
            })
            .expect("save");

        vault.clear_session().expect("clear session");

        let loaded = vault.load().expect("load").expect("some credentials");
        assert_eq!(loaded.device_token, "device-token-1");
        assert_eq!(loaded.session_token, None);
    }

    #[cfg(windows)]
    #[test]
    fn clear_all_removes_the_envelope_entirely() {
        let vault = temp_vault();
        vault
            .save(&CustomerCredentials {
                device_token: DEVICE_TOKEN_TEXT.into(),
                session_token: Some("session-token-1".into()),
            })
            .expect("save");

        vault.clear_all().expect("clear all");

        assert_eq!(vault.load().expect("load"), None);
        // The device instance id is an identifier, not a credential — it
        // survives the wipe so the next activation still presents a stable
        // fingerprint.
        assert!(!vault.device_instance_id().expect("id").is_empty());
    }

    #[test]
    fn load_returns_none_when_nothing_was_stored() {
        let vault = temp_vault();
        assert_eq!(vault.load().expect("load"), None);
    }

    #[cfg(not(windows))]
    #[test]
    fn save_fails_closed_off_windows_instead_of_plaintext() {
        let vault = temp_vault();
        let result = vault.save(&CustomerCredentials {
            device_token: DEVICE_TOKEN_TEXT.into(),
            session_token: Some("session-token-1".into()),
        });
        assert!(
            result.is_err(),
            "a non-Windows build must refuse to persist"
        );
        assert!(!vault.dir.join(CREDENTIALS_FILE).exists());
    }
}
