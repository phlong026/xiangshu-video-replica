import { afterEach, describe, expect, it, vi } from "vitest";

import {
  AdminActivationError,
  clearAdminActivationSession,
  createActivationCodeBatch,
  deleteAdminSession,
  deliverActivationCode,
  downloadActivationCodeExport,
  exchangeAdminSession,
  fetchAdminSession,
  generateActivationCodes,
  listActivationCodes,
  resumeActivationCode,
  revokeActivationCode,
  suspendActivationCode,
} from "./api.admin";

function jsonResponse(payload: unknown, status = 200) {
  return Promise.resolve({
    ok: status >= 200 && status < 300,
    status,
    json: async () => payload,
  });
}

const exchangePayload = {
  session_id: "session-1",
  expires_at: "2026-08-23T20:00:00+00:00",
  csrf_token: "csrf-token-1",
  actor: {
    user_id: "admin-1",
    username: "admin",
    display_name: "管理员一号",
    role: "admin",
  },
};

const sessionPayload = {
  session_id: "session-1",
  expires_at: "2026-08-23T20:00:00+00:00",
  last_activity_at: "2026-08-23T12:00:00+00:00",
  actor: exchangePayload.actor,
};

async function signIn(fetchMock: ReturnType<typeof vi.fn>) {
  fetchMock.mockImplementationOnce(() => jsonResponse(exchangePayload));
  await exchangeAdminSession("ASX1.body.signature");
}

describe("admin activation API adapter", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    clearAdminActivationSession();
  });

  it("exchanges a credential for a session without persisting secrets", async () => {
    const setItem = vi
      .spyOn(Storage.prototype, "setItem")
      .mockImplementation(() => undefined);
    const fetchMock = vi
      .fn()
      .mockImplementationOnce(() => jsonResponse(exchangePayload));
    vi.stubGlobal("fetch", fetchMock);

    const result = await exchangeAdminSession("ASX1.body.signature");

    expect(fetchMock).toHaveBeenCalledWith(
      "http://127.0.0.1:8000/api/control/admin/session/exchange",
      expect.objectContaining({
        method: "POST",
        credentials: "include",
        body: JSON.stringify({ credential: "ASX1.body.signature" }),
      }),
    );
    expect(result.csrf_token).toBe("csrf-token-1");
    expect(result.actor.role).toBe("admin");
    // No-Go red line: the CSRF token must never reach persistent storage.
    expect(setItem).not.toHaveBeenCalled();
  });

  it("rejects writes before any request when no CSRF token is held in memory", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);

    await expect(
      createActivationCodeBatch({
        name: "首批",
        face_value_fen: 10000,
        credits: 100,
        quantity: 50,
        activation_expires_at: "2026-09-01T00:00:00Z",
        reason: "首批投放",
      }),
    ).rejects.toBeInstanceOf(AdminActivationError);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("sends confirm, reason, idempotency key and CSRF header on batch creation", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    await signIn(fetchMock);
    fetchMock.mockImplementationOnce(() =>
      jsonResponse(
        {
          batch_id: "batch-1",
          name: "首批",
          face_value_fen: 10000,
          unit_price_fen_snapshot: 10000,
          credits_snapshot: 100,
          quantity: 50,
          activation_expires_at: "2026-09-01T00:00:00Z",
          status: "OPEN",
          created_by_user_id: "admin-1",
          request_id: "req-batch-1",
        },
        201,
      ),
    );

    const result = await createActivationCodeBatch({
      name: "首批",
      face_value_fen: 10000,
      credits: 100,
      quantity: 50,
      activation_expires_at: "2026-09-01T00:00:00Z",
      reason: "首批投放",
    });

    expect(fetchMock).toHaveBeenLastCalledWith(
      "http://127.0.0.1:8000/api/control/activation-code-batches",
      expect.objectContaining({
        method: "POST",
        credentials: "include",
        headers: expect.objectContaining({
          "Content-Type": "application/json",
          "X-Admin-CSRF": "csrf-token-1",
          "Idempotency-Key": expect.any(String),
        }),
        body: JSON.stringify({
          name: "首批",
          face_value_fen: 10000,
          credits: 100,
          quantity: 50,
          activation_expires_at: "2026-09-01T00:00:00Z",
          confirm: true,
          reason: "首批投放",
        }),
      }),
    );
    expect(result.batch_id).toBe("batch-1");
    expect(result.request_id).toBe("req-batch-1");
  });

  it("mints a fresh idempotency key per write call", async () => {
    const fetchMock = vi.fn().mockImplementation(() =>
      jsonResponse({
        code_id: "code-1",
        status: "SUSPENDED",
        request_id: "req-1",
      }),
    );
    vi.stubGlobal("fetch", fetchMock);
    await signIn(fetchMock);

    await suspendActivationCode("code-1", "涉嫌退款");
    await suspendActivationCode("code-2", "涉嫌退款");

    const keys = fetchMock.mock.calls
      .filter(([url]) => String(url).endsWith("/suspend"))
      .map(([, options]) => {
        const headers = (options as RequestInit).headers as
          | Record<string, string>
          | undefined;
        return String(headers?.["Idempotency-Key"] ?? "");
      });
    expect(keys).toHaveLength(2);
    expect(keys[0]).toBeTruthy();
    expect(keys[0]).not.toBe(keys[1]);
  });

  it("generates codes for a batch and downloads the one-time export", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    await signIn(fetchMock);
    fetchMock
      .mockImplementationOnce(() =>
        jsonResponse(
          {
            batch_id: "batch-1",
            export_id: "export-1",
            expires_at: "2026-08-23T12:15:00+00:00",
            codes: [{ code_id: "code-1", masked_code: "XS****01" }],
            request_id: "req-generate-1",
          },
          201,
        ),
      )
      .mockImplementationOnce(() =>
        jsonResponse({
          export_id: "export-1",
          batch_id: "batch-1",
          codes: ["XS-AAAA-BBBB-01"],
          downloaded_at: "2026-08-23T12:05:00+00:00",
          request_id: "req-download-1",
        }),
      );

    const generated = await generateActivationCodes("batch-1", 20, "渠道补货");
    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      "http://127.0.0.1:8000/api/control/activation-code-batches/batch-1/generate",
      expect.objectContaining({
        method: "POST",
        credentials: "include",
        headers: expect.objectContaining({
          "X-Admin-CSRF": "csrf-token-1",
          "Idempotency-Key": expect.any(String),
        }),
        body: JSON.stringify({
          quantity: 20,
          confirm: true,
          reason: "渠道补货",
        }),
      }),
    );
    expect(generated.export_id).toBe("export-1");

    const download = await downloadActivationCodeExport("export-1", "线下交付");
    expect(fetchMock).toHaveBeenNthCalledWith(
      3,
      "http://127.0.0.1:8000/api/control/activation-code-exports/export-1/download",
      expect.objectContaining({
        method: "POST",
        credentials: "include",
        headers: expect.objectContaining({
          "X-Admin-CSRF": "csrf-token-1",
          "Idempotency-Key": expect.any(String),
        }),
        body: JSON.stringify({ confirm: true, reason: "线下交付" }),
      }),
    );
    expect(download.codes).toEqual(["XS-AAAA-BBBB-01"]);
    expect(download.request_id).toBe("req-download-1");
  });

  it("lists codes with batch and status filters", async () => {
    const fetchMock = vi.fn().mockImplementationOnce(() =>
      jsonResponse({
        items: [
          {
            code_id: "code-1",
            batch_id: "batch-1",
            masked_code: "XS****01",
            status: "GENERATED",
            bound_user_id: null,
            issued_at: null,
          },
        ],
        limit: 50,
        offset: 0,
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    const page = await listActivationCodes({
      batch_id: "batch-1",
      status: "GENERATED",
      limit: 50,
      offset: 0,
    });

    expect(fetchMock).toHaveBeenCalledWith(
      "http://127.0.0.1:8000/api/control/activation-codes?batch_id=batch-1&status=GENERATED&limit=50&offset=0",
      expect.objectContaining({
        method: "GET",
        credentials: "include",
      }),
    );
    expect(page.items).toHaveLength(1);
    expect(page.items[0]?.masked_code).toBe("XS****01");
  });

  it("delivers, resumes and revokes codes with the write contract", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    await signIn(fetchMock);
    fetchMock
      .mockImplementationOnce(() =>
        jsonResponse({
          code_id: "code-1",
          status: "ISSUED",
          delivery_id: "delivery-1",
          request_id: "req-deliver-1",
        }),
      )
      .mockImplementationOnce(() =>
        jsonResponse({
          code_id: "code-1",
          status: "ACTIVE",
          request_id: "req-resume-1",
        }),
      )
      .mockImplementationOnce(() =>
        jsonResponse({
          code_id: "code-1",
          status: "REVOKED",
          request_id: "req-revoke-1",
        }),
      );

    const delivered = await deliverActivationCode("code-1", {
      channel: "offline",
      external_order_ref: "order-9",
      recipient_ref: "渠道商A",
      reason: "线下渠道发货",
    });
    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      "http://127.0.0.1:8000/api/control/activation-codes/code-1/deliver",
      expect.objectContaining({
        method: "POST",
        credentials: "include",
        headers: expect.objectContaining({ "X-Admin-CSRF": "csrf-token-1" }),
        body: JSON.stringify({
          channel: "offline",
          external_order_ref: "order-9",
          recipient_ref: "渠道商A",
          confirm: true,
          reason: "线下渠道发货",
        }),
      }),
    );
    expect(delivered.delivery_id).toBe("delivery-1");

    await resumeActivationCode("code-1", "复核通过恢复");
    expect(fetchMock).toHaveBeenNthCalledWith(
      3,
      "http://127.0.0.1:8000/api/control/activation-codes/code-1/resume",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ confirm: true, reason: "复核通过恢复" }),
      }),
    );

    await revokeActivationCode("code-1", "风控作废");
    expect(fetchMock).toHaveBeenNthCalledWith(
      4,
      "http://127.0.0.1:8000/api/control/activation-codes/code-1/revoke",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ confirm: true, reason: "风控作废" }),
      }),
    );
  });

  it("maps the server error envelope to a coded error", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    await signIn(fetchMock);
    fetchMock.mockImplementationOnce(() =>
      jsonResponse(
        {
          detail: {
            code: "EXPORT_ALREADY_DOWNLOADED",
            message: "This export package was already downloaded exactly once.",
          },
        },
        409,
      ),
    );

    const error = await downloadActivationCodeExport(
      "export-1",
      "再次下载",
    ).catch((cause: unknown) => cause);

    expect(error).toBeInstanceOf(AdminActivationError);
    const activationError = error as AdminActivationError;
    expect(activationError.status).toBe(409);
    expect(activationError.code).toBe("EXPORT_ALREADY_DOWNLOADED");
    expect(activationError.message).toContain("下载明文码失败");
    expect(activationError.message).toContain("409");
  });

  it("reads and deletes the session with the cookie plane", async () => {
    const fetchMock = vi
      .fn()
      .mockImplementationOnce(() => jsonResponse(sessionPayload))
      .mockImplementationOnce(() => jsonResponse(exchangePayload))
      .mockImplementationOnce(() => jsonResponse(undefined, 204));
    vi.stubGlobal("fetch", fetchMock);

    const session = await fetchAdminSession();
    expect(fetchMock).toHaveBeenNthCalledWith(
      1,
      "http://127.0.0.1:8000/api/control/admin/session",
      expect.objectContaining({
        method: "GET",
        credentials: "include",
      }),
    );
    expect(session.actor.display_name).toBe("管理员一号");

    await signIn(fetchMock);
    await deleteAdminSession();
    expect(fetchMock).toHaveBeenLastCalledWith(
      "http://127.0.0.1:8000/api/control/admin/session",
      expect.objectContaining({
        method: "DELETE",
        credentials: "include",
        headers: expect.objectContaining({ "X-Admin-CSRF": "csrf-token-1" }),
      }),
    );
  });
});
