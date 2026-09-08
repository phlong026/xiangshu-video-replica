import { invoke } from "@tauri-apps/api/core";
import { useCallback, useEffect, useReducer, useRef, useState } from "react";

import {
  CUSTOMER_SESSION_EXPIRED_EVENT,
  CUSTOMER_SESSION_REPLACED_EVENT,
  CUSTOMER_SESSION_REVOKED_EVENT,
  CustomerApiError,
  type CustomerDeviceCredential,
  customerActivate,
  customerHeartbeat,
  customerLogin,
  customerLogout,
  customerRecover,
  customerSwitch,
} from "../api";
import {
  type CustomerScreen,
  customerScreenReducer,
  initialCustomerScreen,
} from "./customer-state";

/** The persistent-credential boundary for the customer lane (dev doc §14).
 *
 * The desktop build backs this with the Tauri DPAPI command bridge
 * (customer_credentials.rs); tests and the browser lane inject an isolated,
 * non-persistent implementation. The hook itself never touches
 * localStorage/sessionStorage — §7/§10.2 forbid a plaintext secret in Web
 * Storage, and the injected store is the only place a credential survives.
 */
export interface CustomerCredentialStore {
  loadDeviceCredentialToken(): Promise<string | null>;
  loadSessionToken(): Promise<string | null>;
  saveActivation(deviceToken: string, sessionToken: string): Promise<void>;
  saveSessionToken(sessionToken: string): Promise<void>;
  clearSessionToken(): Promise<void>;
  clearAllCredentials(): Promise<void>;
  /** The stable device fingerprint (§14: generated once, read forever). */
  deviceInstanceId(): Promise<string>;
  devicePlatform(): string;
  /** Only the desktop's registry-backed identity is durable enough to act as
   * an unattended recovery proof. Browser/test stores opt out by default. */
  readonly automaticRecovery?: boolean;
}

export type CustomerSessionConflict = {
  deviceNameMasked: string;
  slotNo: number;
  leaseExpiresAt: string;
};

/** Live session health for the workspace UI (A1 wiring of FE-04 / T31):
 * when the last heartbeat succeeded and when the server lease lapses.
 * Client-clock timestamps; `null` while no session is established. */
export type CustomerSessionRuntime = {
  connectivity: "reachable" | "unreachable";
  lastHeartbeatAt: string;
  leaseExpiresAt: string | null;
};

export type CustomerActivationFormInput = {
  activationCode: string;
  deviceName: string;
};

/** The workspace identity for the customer lane. Activation returns the
 * username; a restart-restore login response carries only the user id (no
 * customer /me endpoint exists yet), so the username degrades to null. */
export type CustomerWorkspaceUser = {
  userId: string;
  username: string | null;
};

const DEFAULT_HEARTBEAT_INTERVAL_MS = 30_000;

function newIdempotencyKey(): string {
  return crypto.randomUUID();
}

/** A vault (credential-store) failure surfaced as a determinate customer
 * error: the UI gets a readable message instead of a swallowed rejection.
 * The credential store is local and opaque to the server; any I/O error is
 * treated as "unknown" transport kind rather than inferred from a status code.
 */
function credentialStoreError(cause: unknown): CustomerApiError {
  // The underlying I/O error is intentionally not surfaced verbatim: the
  // vault message may be OS-specific noise the customer cannot act on, and
  // the error path is already observable through the UI copy.
  void cause;
  return new CustomerApiError({
    message: "本机凭据读写失败，请重试或重新激活",
    transportKind: "unknown",
  });
}

/**
 * The customer session orchestrator (FE-02): boots from the credential
 * store, drives activate/login/logout, listens for the three lifecycle
 * events (§10.1), and keeps the lease alive with heartbeats while the
 * workspace is live. Screen transitions all flow through the
 * customer-state reducer — nothing here jumps screens directly.
 */
export function useCustomerSession(
  store: CustomerCredentialStore,
  options?: { heartbeatIntervalMs?: number },
): {
  screen: CustomerScreen;
  isBusy: boolean;
  error: CustomerApiError | null;
  conflict: CustomerSessionConflict | null;
  user: CustomerWorkspaceUser | null;
  /** Heartbeat/lease health while a session is live; null otherwise. */
  sessionRuntime: CustomerSessionRuntime | null;
  activate(input: CustomerActivationFormInput): Promise<void>;
  retryLogin(): Promise<void>;
  /** The explicit takeover (FE-03): the user confirmed in the conflict dialog,
   * the server atomically displaces the other device's lease and mints a
   * fresh session token (§14). Never invoked without a prior
   * ``conflict-detected`` transition — no silent switching. */
  switchSession(): Promise<void>;
  /** The user declined the takeover: back to the login screen, the other
   * device keeps the lease. */
  cancelSessionSwitch(): void;
  /** Manual lease renewal for the HeartbeatStatus / LeaseCountdown refresh
   * buttons. Failures stay silent — terminal outcomes arrive as the
   * lifecycle events, transient ones are retried by the next tick. */
  sendHeartbeatNow(): Promise<void>;
  logout(): Promise<void>;
  restartAfterExpiry(): void;
  restartAfterRevocation(): void;
} {
  const [screen, dispatch] = useReducer(
    customerScreenReducer,
    initialCustomerScreen,
  );
  const [isBusy, setIsBusy] = useState(false);
  const [error, setError] = useState<CustomerApiError | null>(null);
  const [conflict, setConflict] = useState<CustomerSessionConflict | null>(
    null,
  );
  // The in-memory session token powers heartbeat/logout. The persistent
  // copy lives only in the injected store; this state is intentionally not
  // persisted anywhere else.
  const [sessionToken, setSessionToken] = useState<string | null>(null);
  const [user, setUser] = useState<CustomerWorkspaceUser | null>(null);
  const [sessionRuntime, setSessionRuntime] =
    useState<CustomerSessionRuntime | null>(null);
  const sessionTokenRef = useRef<string | null>(null);
  const sessionGenerationRef = useRef(0);
  const latestHeartbeatRequestIdRef = useRef(0);
  const bootstrappedRef = useRef(false);

  // Every established/renewed lease updates the runtime view: the heartbeat
  // timestamp is the client clock at success, the lease comes from the
  // server response.
  const noteLease = useCallback((leaseExpiresAt: string | null) => {
    setSessionRuntime({
      connectivity: "reachable",
      lastHeartbeatAt: new Date().toISOString(),
      leaseExpiresAt,
    });
  }, []);

  const noteHeartbeatFailure = useCallback(() => {
    setSessionRuntime((current) =>
      current ? { ...current, connectivity: "unreachable" } : current,
    );
  }, []);

  const sendHeartbeat = useCallback(
    async (token: string) => {
      const requestId = ++latestHeartbeatRequestIdRef.current;
      const sessionGeneration = sessionGenerationRef.current;
      const belongsToCurrentSession = () =>
        requestId === latestHeartbeatRequestIdRef.current &&
        sessionGeneration === sessionGenerationRef.current &&
        token === sessionTokenRef.current;
      try {
        const body = await customerHeartbeat(
          { kind: "session", token },
          { shouldDispatchLifecycle: belongsToCurrentSession },
        );
        if (belongsToCurrentSession()) {
          noteLease(body.lease_expires_at);
        }
      } catch {
        if (belongsToCurrentSession()) {
          noteHeartbeatFailure();
        }
      }
    },
    [noteHeartbeatFailure, noteLease],
  );

  // For the current activation attempt, retain this key until final outcome to
  // enable retries when the response is lost/timed out. The server can recover
  // by replaying the original key; a new key would reject the already-consumed
  // activation code and leave the customer stranded.
  const currentActivationIdempotencyKeyRef = useRef<string | null>(null);

  useEffect(
    () => () => {
      sessionGenerationRef.current += 1;
      latestHeartbeatRequestIdRef.current += 1;
    },
    [],
  );

  const establishSession = useCallback(
    async (deviceToken: string) => {
      const credential: CustomerDeviceCredential = {
        kind: "device",
        token: deviceToken,
      };
      const previousSessionToken = await store.loadSessionToken();
      const result = await customerLogin(credential, {
        idempotencyKey: newIdempotencyKey(),
        sessionToken: previousSessionToken ?? undefined,
      });
      try {
        await store.saveSessionToken(result.session.session_token);
      } catch (cause) {
        throw credentialStoreError(cause);
      }
      sessionTokenRef.current = result.session.session_token;
      sessionGenerationRef.current += 1;
      setSessionToken(result.session.session_token);
      setUser({ userId: result.session.user_id, username: null });
      noteLease(result.session.session_lease_expires_at);
      return result;
    },
    [store, noteLease],
  );

  // Boot: check the credential store once, then (FE-02 restart gate) try to
  // restore a session automatically when a device credential exists. The
  // cleanup resets the guard for React 18 StrictMode double-mounting — the
  // customer lane would otherwise stay stuck on the checking screen forever.
  useEffect(() => {
    if (bootstrappedRef.current) {
      return;
    }
    bootstrappedRef.current = true;
    let cancelled = false;
    (async () => {
      let deviceToken: string | null = null;
      let credentialLoadFailed = false;
      try {
        deviceToken = await store.loadDeviceCredentialToken();
      } catch {
        // A missing/corrupt local envelope is precisely what the durable
        // machine-identity recovery lane repairs. If recovery is unavailable,
        // the activation page still gets a readable local-vault error.
        credentialLoadFailed = true;
      }
      if (cancelled) {
        return;
      }
      if (deviceToken === null && store.automaticRecovery) {
        setIsBusy(true);
        try {
          const response = await customerRecover({
            deviceFingerprint: await store.deviceInstanceId(),
            deviceName: "本机设备",
            devicePlatform: store.devicePlatform(),
            idempotencyKey: newIdempotencyKey(),
          });
          await store.saveActivation(
            response.device_token,
            response.session_token,
          );
          if (cancelled) {
            return;
          }
          sessionTokenRef.current = response.session_token;
          sessionGenerationRef.current += 1;
          setSessionToken(response.session_token);
          setUser({ userId: response.user_id, username: response.username });
          noteLease(response.session_lease_expires_at);
          dispatch({
            type: "boot-check-completed",
            hasDeviceCredential: false,
          });
          dispatch({ type: "activation-succeeded" });
          return;
        } catch (cause) {
          if (cancelled) {
            return;
          }
          dispatch({
            type: "boot-check-completed",
            hasDeviceCredential: false,
          });
          if (
            cause instanceof CustomerApiError &&
            cause.code === "ACTIVATION_UNAVAILABLE"
          ) {
            // A genuinely new/unbound computer belongs on first activation;
            // the expected recovery miss is not an error banner.
            return;
          }
          setError(
            cause instanceof CustomerApiError
              ? cause
              : credentialStoreError(cause),
          );
          return;
        } finally {
          if (!cancelled) {
            setIsBusy(false);
          }
        }
      }
      dispatch({
        type: "boot-check-completed",
        hasDeviceCredential: deviceToken !== null,
      });
      if (deviceToken === null) {
        if (credentialLoadFailed) {
          setError(credentialStoreError(null));
        }
        return;
      }
      setIsBusy(true);
      try {
        await establishSession(deviceToken);
        if (!cancelled) {
          dispatch({ type: "login-succeeded" });
        }
      } catch (cause) {
        if (cancelled) {
          return;
        }
        if (cause instanceof CustomerApiError) {
          setError(cause);
          if (cause.kind === "other-device-online") {
            setConflict({
              deviceNameMasked: cause.onlineDeviceNameMasked ?? "",
              slotNo: cause.onlineSlotNo ?? 0,
              leaseExpiresAt: cause.leaseExpiresAt ?? "",
            });
            dispatch({ type: "conflict-detected" });
          }
        } else {
          setError(credentialStoreError(cause));
        }
      } finally {
        if (!cancelled) {
          setIsBusy(false);
        }
      }
    })();
    return () => {
      cancelled = true;
      // StrictMode double-mounts the effect; the second mount must boot again
      // or the customer lane would sit on the checking screen forever.
      bootstrappedRef.current = false;
    };
  }, [establishSession, store, noteLease]);

  // The three lifecycle events (§10.1) arrive on window — dispatched by the
  // customer transport on any 401 that ends the session. Each one also does
  // the right thing to the persisted credential: expired/replaced keep the
  // device credential (§13.2: back to the login screen), revoked clears
  // everything (recovery flow).
  useEffect(() => {
    const clearSession = () => {
      void store.clearSessionToken();
      sessionTokenRef.current = null;
      sessionGenerationRef.current += 1;
      latestHeartbeatRequestIdRef.current += 1;
      setSessionToken(null);
      setUser(null);
      setConflict(null);
      setSessionRuntime(null);
    };
    const onExpired = () => {
      clearSession();
      dispatch({ type: "session-expired" });
    };
    const onReplaced = () => {
      clearSession();
      dispatch({ type: "session-replaced" });
    };
    const onRevoked = () => {
      void store.clearAllCredentials();
      sessionTokenRef.current = null;
      sessionGenerationRef.current += 1;
      latestHeartbeatRequestIdRef.current += 1;
      setSessionToken(null);
      setUser(null);
      setConflict(null);
      setSessionRuntime(null);
      dispatch({ type: "device-revoked" });
    };
    window.addEventListener(CUSTOMER_SESSION_EXPIRED_EVENT, onExpired);
    window.addEventListener(CUSTOMER_SESSION_REPLACED_EVENT, onReplaced);
    window.addEventListener(CUSTOMER_SESSION_REVOKED_EVENT, onRevoked);
    return () => {
      window.removeEventListener(CUSTOMER_SESSION_EXPIRED_EVENT, onExpired);
      window.removeEventListener(CUSTOMER_SESSION_REPLACED_EVENT, onReplaced);
      window.removeEventListener(CUSTOMER_SESSION_REVOKED_EVENT, onRevoked);
    };
  }, [store]);

  // Keep the lease alive while the workspace is live. A failing heartbeat
  // ends through the lifecycle events above (the transport dispatches them
  // on the terminal 401s), so errors here intentionally do not switch
  // screens — §4.2: a displaced/expired session must not masquerade as a
  // generic network/service failure either.
  const heartbeatIntervalMs =
    options?.heartbeatIntervalMs ?? DEFAULT_HEARTBEAT_INTERVAL_MS;
  useEffect(() => {
    if (screen !== "workspace" || sessionToken === null) {
      return;
    }
    const timer = window.setInterval(() => {
      const token = sessionTokenRef.current;
      if (token === null) {
        return;
      }
      void sendHeartbeat(token);
    }, heartbeatIntervalMs);
    return () => {
      window.clearInterval(timer);
    };
  }, [heartbeatIntervalMs, screen, sessionToken, sendHeartbeat]);

  const activate = useCallback(
    async (input: CustomerActivationFormInput) => {
      setIsBusy(true);
      setError(null);
      setConflict(null);
      try {
        // Generate or reuse the idempotency key for this activation attempt
        const idempotencyKey =
          currentActivationIdempotencyKeyRef.current ?? newIdempotencyKey();
        if (!currentActivationIdempotencyKeyRef.current) {
          currentActivationIdempotencyKeyRef.current = idempotencyKey;
        }

        const response = await customerActivate({
          activationCode: input.activationCode,
          deviceFingerprint: await store.deviceInstanceId(),
          deviceName: input.deviceName,
          devicePlatform: store.devicePlatform(),
          idempotencyKey,
        });

        // Clear the key only after successful activation completion
        currentActivationIdempotencyKeyRef.current = null;
        try {
          await store.saveActivation(
            response.device_token,
            response.session_token,
          );
        } catch (cause) {
          throw credentialStoreError(cause);
        }
        sessionTokenRef.current = response.session_token;
        sessionGenerationRef.current += 1;
        setSessionToken(response.session_token);
        setUser({ userId: response.user_id, username: response.username });
        noteLease(response.session_lease_expires_at);
        dispatch({ type: "activation-succeeded" });
      } catch (cause) {
        setError(
          cause instanceof CustomerApiError
            ? cause
            : credentialStoreError(cause),
        );
      } finally {
        setIsBusy(false);
      }
    },
    [store, noteLease],
  );

  // A guard for concurrent retries: while a login attempt is in flight, further
  // clicks are ignored until the in-flight request completes. This avoids
  // duplicate login calls and keeps the UI state consistent.
  const retryInFlightRef = useRef(false);

  const retryLogin = useCallback(async () => {
    if (retryInFlightRef.current) {
      return;
    }
    retryInFlightRef.current = true;
    setError(null);
    setConflict(null);
    setIsBusy(true);
    try {
      let deviceToken: string | null = null;

      try {
        deviceToken = await store.loadDeviceCredentialToken();
      } catch (loadCause) {
        // Surface credential-load failures during manual login by transitioning
        // to credential-missing state with a readable error, matching the boot
        // path behavior. This avoids unhandled rejections with no recovery.
        const storeError = credentialStoreError(loadCause);
        setError(storeError);
        dispatch({ type: "credential-missing" });
        return;
      }

      if (deviceToken === null) {
        // The stored device credential vanished (vault cleared / I/O failure):
        // back to activation for recovery.
        dispatch({ type: "credential-missing" });
        return;
      }
      try {
        await establishSession(deviceToken);
        dispatch({ type: "login-succeeded" });
      } catch (cause) {
        if (cause instanceof CustomerApiError) {
          setError(cause);
          if (cause.kind === "other-device-online") {
            setConflict({
              deviceNameMasked: cause.onlineDeviceNameMasked ?? "",
              slotNo: cause.onlineSlotNo ?? 0,
              leaseExpiresAt: cause.leaseExpiresAt ?? "",
            });
            dispatch({ type: "conflict-detected" });
          }
        } else {
          setError(credentialStoreError(cause));
        }
      }
    } finally {
      retryInFlightRef.current = false;
      setIsBusy(false);
    }
  }, [establishSession, store]);

  // Explicit device switch (FE-03 / T30): the customer confirms the takeover
  // in the conflict dialog; the server atomically replaces the lease and
  // mints a fresh session token. The UI must never assume the switch
  // succeeded before the server confirms it — only this action transitions
  // to the workspace from the conflict screen.
  const switchSession = useCallback(async () => {
    setIsBusy(true);
    setError(null);
    try {
      const deviceToken = await store.loadDeviceCredentialToken();
      if (deviceToken === null) {
        dispatch({ type: "credential-missing" });
        return;
      }
      const previousSessionToken = await store.loadSessionToken();
      const result = await customerSwitch(
        { kind: "device", token: deviceToken },
        {
          idempotencyKey: newIdempotencyKey(),
          sessionToken: previousSessionToken ?? undefined,
        },
      );
      try {
        await store.saveSessionToken(result.session.session_token);
      } catch (cause) {
        throw credentialStoreError(cause);
      }
      sessionTokenRef.current = result.session.session_token;
      sessionGenerationRef.current += 1;
      setSessionToken(result.session.session_token);
      setUser({ userId: result.session.user_id, username: null });
      setConflict(null);
      noteLease(result.session.session_lease_expires_at);
      dispatch({ type: "login-succeeded" });
    } catch (cause) {
      setError(
        cause instanceof CustomerApiError ? cause : credentialStoreError(cause),
      );
    } finally {
      setIsBusy(false);
    }
  }, [store, noteLease]);

  const cancelSessionSwitch = useCallback(() => {
    setError(null);
    setConflict(null);
    dispatch({ type: "conflict-cancelled" });
  }, []);

  const sendHeartbeatNow = useCallback(async () => {
    const token = sessionTokenRef.current;
    if (token === null) {
      return;
    }
    await sendHeartbeat(token);
  }, [sendHeartbeat]);

  const logoutInFlightRef = useRef(false);
  const logout = useCallback(async () => {
    if (logoutInFlightRef.current) {
      return;
    }
    logoutInFlightRef.current = true;
    try {
      const token = sessionTokenRef.current;
      sessionTokenRef.current = null;
      sessionGenerationRef.current += 1;
      latestHeartbeatRequestIdRef.current += 1;
      setError(null);
      setConflict(null);
      if (token !== null) {
        setIsBusy(true);
        try {
          await customerLogout(
            { kind: "session", token },
            { idempotencyKey: newIdempotencyKey() },
          );
        } catch {
          // The session is being discarded locally regardless; a failing
          // logout (network/timeout) must still return the user to the login
          // screen with the device credential intact.
        } finally {
          setIsBusy(false);
        }
      }
      try {
        await store.clearSessionToken();
      } catch {
        // A failing vault write must still return the user to the login screen;
        // the stale session token stays on disk and the next login overwrites it.
      }
      setSessionToken(null);
      setUser(null);
      setSessionRuntime(null);
      dispatch({ type: "logout" });
    } finally {
      logoutInFlightRef.current = false;
    }
  }, [store]);

  const restartAfterExpiry = useCallback(() => {
    setError(null);
    setConflict(null);
    dispatch({ type: "restart-login" });
  }, []);

  const restartAfterRevocation = useCallback(() => {
    setError(null);
    setConflict(null);
    dispatch({ type: "restart-activation" });
  }, []);

  return {
    screen,
    isBusy,
    error,
    conflict,
    user,
    sessionRuntime,
    activate,
    retryLogin,
    switchSession,
    cancelSessionSwitch,
    sendHeartbeatNow,
    logout,
    restartAfterExpiry,
    restartAfterRevocation,
  };
}

// ---------------------------------------------------------------------------
// Credential-store adapters (dev doc §14: the desktop and browser lanes stay
// separate). The desktop lane talks to the Tauri DPAPI vault
// (customer_credentials.rs); the browser lane gets an isolated,
// non-persistent store — a browser session restart simply returns to the
// activation screen, and no secret ever lands in Web Storage (§7).
// ---------------------------------------------------------------------------

export function isTauriRuntime(): boolean {
  return typeof window !== "undefined" && "__TAURI_INTERNALS__" in window;
}

type StoredCustomerCredentials = {
  device_token: string;
  session_token: string | null;
};

function tauriCustomerCredentialStore(): CustomerCredentialStore {
  return {
    automaticRecovery: true,
    async loadDeviceCredentialToken() {
      const stored = await invoke<StoredCustomerCredentials | null>(
        "customer_load_credentials",
      );
      return stored?.device_token ?? null;
    },
    async loadSessionToken() {
      const stored = await invoke<StoredCustomerCredentials | null>(
        "customer_load_credentials",
      );
      const sessionToken = stored?.session_token?.trim();
      return sessionToken || null;
    },
    async saveActivation(deviceToken, sessionToken) {
      await invoke("customer_save_credentials", {
        deviceToken,
        sessionToken,
      });
    },
    async saveSessionToken(sessionToken) {
      const stored = await invoke<StoredCustomerCredentials | null>(
        "customer_load_credentials",
      );
      if (stored === null) {
        throw new Error("cannot renew a session without a stored device token");
      }
      await invoke("customer_save_credentials", {
        deviceToken: stored.device_token,
        sessionToken,
      });
    },
    async clearSessionToken() {
      await invoke("customer_clear_session_token");
    },
    async clearAllCredentials() {
      await invoke("customer_clear_all_credentials");
    },
    async deviceInstanceId() {
      return invoke<string>("customer_device_instance_id");
    },
    devicePlatform() {
      // The customer desktop build ships Windows-only (DESK-03 NSIS x64).
      return "windows";
    },
  };
}

function inMemoryCustomerCredentialStore(): CustomerCredentialStore {
  let deviceToken: string | null = null;
  let sessionToken: string | null = null;
  const instanceId = newIdempotencyKey();
  return {
    automaticRecovery: false,
    async loadDeviceCredentialToken() {
      return deviceToken;
    },
    async loadSessionToken() {
      return sessionToken;
    },
    async saveActivation(nextDeviceToken, nextSessionToken) {
      deviceToken = nextDeviceToken;
      sessionToken = nextSessionToken.trim() || null;
    },
    async saveSessionToken(nextSessionToken) {
      sessionToken = nextSessionToken;
    },
    async clearSessionToken() {
      sessionToken = null;
    },
    async clearAllCredentials() {
      deviceToken = null;
      sessionToken = null;
    },
    async deviceInstanceId() {
      return instanceId;
    },
    devicePlatform() {
      return "browser";
    },
  };
}

/** The production credential store for the current runtime: the Tauri DPAPI
 * vault on the desktop build, the isolated in-memory store on the browser
 * lane. Tests inject their own store instead. */
export function customerCredentialStore(): CustomerCredentialStore {
  return isTauriRuntime()
    ? tauriCustomerCredentialStore()
    : inMemoryCustomerCredentialStore();
}
