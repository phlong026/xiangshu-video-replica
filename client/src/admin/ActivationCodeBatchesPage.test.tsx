import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  clearAdminActivationSession,
  exchangeAdminSession,
} from "../api.admin";
import { ActivationCodeBatchesPage } from "./ActivationCodeBatchesPage";

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

const batchCreated = {
  batch_id: "batch-1",
  name: "首批渠道码",
  face_value_fen: 10000,
  unit_price_fen_snapshot: 10000,
  credits_snapshot: 100,
  quantity: 50,
  activation_expires_at: "2026-12-31T23:59",
  status: "OPEN",
  created_by_user_id: "admin-1",
  request_id: "req-batch-1",
};

const generatedPayload = {
  batch_id: "batch-1",
  export_id: "export-1",
  expires_at: "2026-08-23T12:15:00+00:00",
  codes: [{ code_id: "code-1", masked_code: "XS****01" }],
  request_id: "req-generate-1",
};

const downloadedPayload = {
  export_id: "export-1",
  batch_id: "batch-1",
  codes: ["XS-AAAA-BBBB-01"],
  downloaded_at: "2026-08-23T12:05:00+00:00",
  request_id: "req-download-1",
};

function installFetch(options?: {
  createBatch?: "ok" | "unauthorized";
  download?: "ok" | "already";
}) {
  const createBatchState = options?.createBatch ?? "ok";
  const downloadState = options?.download ?? "ok";
  const fetchMock = vi.fn((url: string, init?: RequestInit) => {
    if (url.endsWith("/api/control/admin/session/exchange")) {
      return jsonResponse(exchangePayload);
    }
    if (
      url.endsWith("/api/control/activation-code-batches") &&
      init?.method === "POST"
    ) {
      if (createBatchState === "unauthorized") {
        return jsonResponse(
          {
            detail: {
              code: "ADMIN_SESSION_INVALID",
              message: "Admin session is missing, revoked or invalid.",
            },
          },
          401,
        );
      }
      return jsonResponse(batchCreated, 201);
    }
    if (url.endsWith("/generate") && init?.method === "POST") {
      return jsonResponse(generatedPayload, 201);
    }
    if (url.endsWith("/download") && init?.method === "POST") {
      if (downloadState === "already") {
        return jsonResponse(
          {
            detail: {
              code: "EXPORT_ALREADY_DOWNLOADED",
              message:
                "This export package was already downloaded exactly once.",
            },
          },
          409,
        );
      }
      return jsonResponse(downloadedPayload);
    }
    throw new Error(`unexpected request: ${url}`);
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

async function fillCreateForm() {
  fireEvent.change(screen.getByLabelText("批次名称"), {
    target: { value: "首批渠道码" },
  });
  fireEvent.change(screen.getByLabelText("面值（分）"), {
    target: { value: "10000" },
  });
  fireEvent.change(screen.getByLabelText("到账条数"), {
    target: { value: "100" },
  });
  fireEvent.change(screen.getByLabelText("生成数量"), {
    target: { value: "50" },
  });
  fireEvent.change(screen.getByLabelText("激活有效期至"), {
    target: { value: "2026-12-31T23:59" },
  });
  fireEvent.change(screen.getByLabelText("创建原因"), {
    target: { value: "首批渠道投放" },
  });
  fireEvent.click(screen.getByLabelText("我已确认创建"));
}

describe("ActivationCodeBatchesPage", () => {
  beforeEach(async () => {
    installFetch();
    await exchangeAdminSession("ASX1.body.signature");
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    clearAdminActivationSession();
  });

  it("creates a batch with the write contract and shows the request id", async () => {
    const fetchMock = installFetch();

    render(<ActivationCodeBatchesPage />);
    await fillCreateForm();
    fireEvent.click(screen.getByRole("button", { name: "创建批次" }));

    expect(await screen.findByText("req-batch-1")).toBeInTheDocument();
    expect(screen.getByText("batch-1")).toBeInTheDocument();
    const createCall = fetchMock.mock.calls.find(([url]) =>
      String(url).endsWith("/api/control/activation-code-batches"),
    );
    expect(createCall?.[1]?.body).toBe(
      JSON.stringify({
        name: "首批渠道码",
        face_value_fen: 10000,
        credits: 100,
        quantity: 50,
        activation_expires_at: "2026-12-31T15:59:00.000Z",
        confirm: true,
        reason: "首批渠道投放",
      }),
    );
    expect(createCall?.[1]?.headers).toMatchObject({
      "X-Admin-CSRF": "csrf-token-1",
      "Idempotency-Key": expect.any(String),
    });
  });

  it("refuses to submit without a reason or confirmation", async () => {
    const fetchMock = installFetch();

    render(<ActivationCodeBatchesPage />);
    fireEvent.change(screen.getByLabelText("批次名称"), {
      target: { value: "首批渠道码" },
    });
    fireEvent.change(screen.getByLabelText("面值（分）"), {
      target: { value: "10000" },
    });
    fireEvent.change(screen.getByLabelText("到账条数"), {
      target: { value: "100" },
    });
    fireEvent.change(screen.getByLabelText("生成数量"), {
      target: { value: "50" },
    });
    fireEvent.change(screen.getByLabelText("激活有效期至"), {
      target: { value: "2026-12-31T23:59" },
    });
    fireEvent.click(screen.getByRole("button", { name: "创建批次" }));

    expect(await screen.findByText("请填写创建原因")).toBeInTheDocument();
    expect(fetchMock.mock.calls).toHaveLength(0); // validation refused the write
  });

  it("generates codes and reveals the one-time download panel", async () => {
    const fetchMock = installFetch();

    render(<ActivationCodeBatchesPage />);
    fireEvent.change(screen.getByLabelText("批次 ID"), {
      target: { value: "batch-1" },
    });
    fireEvent.change(screen.getByLabelText("本次生成数量"), {
      target: { value: "20" },
    });
    fireEvent.change(screen.getByLabelText("生成原因"), {
      target: { value: "渠道补货" },
    });
    fireEvent.click(screen.getByLabelText("我已确认生成"));
    fireEvent.click(screen.getByRole("button", { name: "生成激活码" }));

    expect(await screen.findByText("export-1")).toBeInTheDocument();
    expect(screen.getByText("XS****01")).toBeInTheDocument();
    expect(screen.getByText("req-generate-1")).toBeInTheDocument();
    const generateCall = fetchMock.mock.calls.find(([url]) =>
      String(url).endsWith("/activation-code-batches/batch-1/generate"),
    );
    expect(generateCall?.[1]?.body).toBe(
      JSON.stringify({ quantity: 20, confirm: true, reason: "渠道补货" }),
    );

    fireEvent.change(screen.getByLabelText("下载原因"), {
      target: { value: "线下交付" },
    });
    fireEvent.click(screen.getByLabelText("我已确认下载"));
    fireEvent.click(screen.getByRole("button", { name: "下载明文码" }));

    expect(await screen.findByText("XS-AAAA-BBBB-01")).toBeInTheDocument();
    expect(screen.getByText(/仅此一次/)).toBeInTheDocument();
    expect(screen.getByText("req-download-1")).toBeInTheDocument();
    const downloadCall = fetchMock.mock.calls.find(([url]) =>
      String(url).endsWith("/activation-code-exports/export-1/download"),
    );
    expect(downloadCall?.[1]?.body).toBe(
      JSON.stringify({ confirm: true, reason: "线下交付" }),
    );
  });

  it("surfaces a deterministic error when the export was already downloaded", async () => {
    installFetch({ download: "already" });

    render(<ActivationCodeBatchesPage />);
    fireEvent.change(screen.getByLabelText("批次 ID"), {
      target: { value: "batch-1" },
    });
    fireEvent.change(screen.getByLabelText("本次生成数量"), {
      target: { value: "20" },
    });
    fireEvent.change(screen.getByLabelText("生成原因"), {
      target: { value: "渠道补货" },
    });
    fireEvent.click(screen.getByLabelText("我已确认生成"));
    fireEvent.click(screen.getByRole("button", { name: "生成激活码" }));

    await screen.findByText("export-1");
    fireEvent.change(screen.getByLabelText("下载原因"), {
      target: { value: "重复下载" },
    });
    fireEvent.click(screen.getByLabelText("我已确认下载"));
    fireEvent.click(screen.getByRole("button", { name: "下载明文码" }));

    expect(
      await screen.findByText(/已被下载过，无法再次下载/),
    ).toBeInTheDocument();
    expect(screen.queryByText("XS-AAAA-BBBB-01")).toBeNull();
  });

  it("reports the session as expired on a 401 write", async () => {
    installFetch({ createBatch: "unauthorized" });
    const onSessionExpired = vi.fn();

    render(<ActivationCodeBatchesPage onSessionExpired={onSessionExpired} />);
    await fillCreateForm();
    fireEvent.click(screen.getByRole("button", { name: "创建批次" }));

    expect(
      await screen.findByText(/会话已失效，请重新登录/),
    ).toBeInTheDocument();
    expect(onSessionExpired).toHaveBeenCalled();
  });

  it("disables every write control in read-only mode", () => {
    installFetch();

    render(<ActivationCodeBatchesPage readOnly />);

    expect(screen.getByRole("button", { name: "创建批次" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "生成激活码" })).toBeDisabled();
    // The download form only exists after a generation, so in read-only mode
    // there is simply no plaintext download control to abuse.
    expect(screen.queryByRole("button", { name: "下载明文码" })).toBeNull();
    expect(screen.getByText(/当前为只读模式/)).toBeInTheDocument();
  });
});
