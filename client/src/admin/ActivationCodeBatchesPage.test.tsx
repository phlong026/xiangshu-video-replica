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

const CSRF_TOKEN_TEXT = "csrf-token-1";

const exchangePayload = {
  session_id: "session-1",
  expires_at: "2026-08-23T20:00:00+00:00",
  csrf_token: CSRF_TOKEN_TEXT,
  actor: {
    user_id: "admin-1",
    username: "admin",
    display_name: "管理员一号",
    role: "admin",
  },
};

const batchCreated = {
  batch_id: "batch-1",
  name: "200元激活码",
  face_value_fen: 1000,
  unit_price_fen_snapshot: 1000,
  credits_snapshot: 20,
  quantity: 2,
  activation_expires_at: "2027-08-27T10:00:00.000Z",
  status: "OPEN",
  created_by_user_id: "admin-1",
  request_id: "req-batch-1",
};

const generatedPayload = {
  batch_id: "batch-1",
  export_id: "export-1",
  expires_at: "2026-08-27T10:15:00+00:00",
  codes: [
    { code_id: "code-1", masked_code: "XS****01" },
    { code_id: "code-2", masked_code: "XS****02" },
  ],
  request_id: "req-generate-1",
};

const downloadedPayload = {
  export_id: "export-1",
  batch_id: "batch-1",
  codes: ["XS-AAAA-BBBB-01", "XS-CCCC-DDDD-02"],
  downloaded_at: "2026-08-27T10:05:00+00:00",
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

describe("ActivationCodeBatchesPage", () => {
  beforeEach(async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    vi.setSystemTime(new Date("2026-08-27T10:00:00.000Z"));
    installFetch();
    await exchangeAdminSession("ASX1.body.signature");
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
    clearAdminActivationSession();
  });

  it("replaces batch fields with fixed amounts and one custom option", () => {
    render(<ActivationCodeBatchesPage unitPriceFen={1000} />);

    expect(
      screen.getByRole("heading", { name: "直接生成激活码" }),
    ).toBeInTheDocument();
    for (const amount of ["¥100", "¥200", "¥500", "¥1000"]) {
      expect(screen.getByRole("button", { name: amount })).toBeInTheDocument();
    }
    expect(
      screen.getByRole("button", { name: "自定义金额" }),
    ).toBeInTheDocument();
    expect(screen.queryByLabelText("批次名称")).toBeNull();
    expect(screen.queryByLabelText("批次 ID")).toBeNull();
    expect(screen.queryByLabelText("创建原因")).toBeNull();
    expect(screen.queryByLabelText("下载原因")).toBeNull();
  });

  it("creates the hidden batch, generates and reveals plaintext in one action", async () => {
    const fetchMock = installFetch();
    render(<ActivationCodeBatchesPage unitPriceFen={1000} />);

    fireEvent.click(screen.getByRole("button", { name: "¥200" }));
    fireEvent.change(screen.getByLabelText("生成数量"), {
      target: { value: "2" },
    });
    fireEvent.click(
      screen.getByRole("button", { name: "生成 2 个 ¥200 激活码" }),
    );

    expect(await screen.findByText("XS-AAAA-BBBB-01")).toBeInTheDocument();
    expect(screen.getByText("XS-CCCC-DDDD-02")).toBeInTheDocument();
    expect(screen.getByText("已生成 2 个 ¥200 激活码")).toBeInTheDocument();
    expect(
      screen.getByText(/明文激活码仅在本页显示这一次/),
    ).toBeInTheDocument();

    const createCall = fetchMock.mock.calls.find(([url]) =>
      String(url).endsWith("/api/control/activation-code-batches"),
    );
    expect(JSON.parse(String(createCall?.[1]?.body))).toMatchObject({
      face_value_fen: 1000,
      credits: 20,
      quantity: 2,
      activation_expires_at: "2027-08-27T10:00:00.000Z",
      confirm: true,
    });

    const generateCall = fetchMock.mock.calls.find(([url]) =>
      String(url).endsWith("/activation-code-batches/batch-1/generate"),
    );
    expect(JSON.parse(String(generateCall?.[1]?.body))).toMatchObject({
      quantity: 2,
      auto_issue: true,
      confirm: true,
    });
    expect(
      fetchMock.mock.calls.some(([url]) =>
        String(url).endsWith("/activation-code-exports/export-1/download"),
      ),
    ).toBe(true);
  });

  it("accepts a custom amount when it is an exact multiple of the unit price", async () => {
    const fetchMock = installFetch();
    render(<ActivationCodeBatchesPage unitPriceFen={1000} />);

    fireEvent.click(screen.getByRole("button", { name: "自定义金额" }));
    fireEvent.change(screen.getByLabelText("自定义金额（元）"), {
      target: { value: "30" },
    });
    fireEvent.click(
      screen.getByRole("button", { name: "生成 1 个 ¥30 激活码" }),
    );

    await screen.findByText("XS-AAAA-BBBB-01");
    const createCall = fetchMock.mock.calls.find(([url]) =>
      String(url).endsWith("/api/control/activation-code-batches"),
    );
    expect(JSON.parse(String(createCall?.[1]?.body))).toMatchObject({
      face_value_fen: 1000,
      credits: 3,
      quantity: 1,
    });
  });

  it("explains why an unsupported custom amount cannot be generated", async () => {
    const fetchMock = installFetch();
    render(<ActivationCodeBatchesPage unitPriceFen={1000} />);

    fireEvent.click(screen.getByRole("button", { name: "自定义金额" }));
    fireEvent.change(screen.getByLabelText("自定义金额（元）"), {
      target: { value: "35" },
    });
    fireEvent.click(
      screen.getByRole("button", { name: "生成 1 个 ¥35 激活码" }),
    );

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "金额需为 10 元的整数倍",
    );
    expect(
      fetchMock.mock.calls.some(([url]) =>
        String(url).endsWith("/api/control/activation-code-batches"),
      ),
    ).toBe(false);
  });

  it("surfaces a deterministic error when the one-time plaintext was consumed", async () => {
    installFetch({ download: "already" });
    render(<ActivationCodeBatchesPage unitPriceFen={1000} />);

    fireEvent.click(
      screen.getByRole("button", { name: "生成 1 个 ¥100 激活码" }),
    );

    expect(
      await screen.findByText(/已被下载过，无法再次下载/),
    ).toBeInTheDocument();
    expect(screen.queryByText("XS-AAAA-BBBB-01")).toBeNull();
  });

  it("reports the session as expired on a 401 write", async () => {
    installFetch({ createBatch: "unauthorized" });
    const onSessionExpired = vi.fn();
    render(
      <ActivationCodeBatchesPage
        unitPriceFen={1000}
        onSessionExpired={onSessionExpired}
      />,
    );

    fireEvent.click(
      screen.getByRole("button", { name: "生成 1 个 ¥100 激活码" }),
    );

    expect(
      await screen.findByText(/会话已失效，请重新登录/),
    ).toBeInTheDocument();
    expect(onSessionExpired).toHaveBeenCalled();
  });

  it("keeps activation creation unavailable for auditors", () => {
    render(<ActivationCodeBatchesPage readOnly unitPriceFen={1000} />);

    expect(
      screen.getByRole("button", { name: "生成 1 个 ¥100 激活码" }),
    ).toBeDisabled();
    expect(screen.getByText(/当前为只读模式/)).toBeInTheDocument();
  });

  it("waits for the configured unit price before allowing generation", () => {
    render(<ActivationCodeBatchesPage unitPriceFen={null} />);

    expect(screen.getByText("正在读取当前价格…")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "生成 1 个 ¥100 激活码" }),
    ).toBeDisabled();
  });
});
