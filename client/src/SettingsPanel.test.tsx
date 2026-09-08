import { fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { SESSION_EXPIRED_EVENT } from "./api";
import { SettingsPanel } from "./SettingsPanel";

function jsonResponse(payload: unknown, status = 200) {
  return Promise.resolve({
    ok: status >= 200 && status < 300,
    status,
    json: async () => payload,
    text: async () => JSON.stringify(payload),
  });
}

// Secret discipline: only ever a dummy placeholder value, never a real key.
const DUMMY_KEY = "test-key-1";

const settingsSnapshot = {
  providers: {
    metaso: { provider: "metaso", configured: true, config: {} },
    apilio: { provider: "apilio", configured: false, config: {} },
    cos: {
      provider: "cos",
      configured: true,
      config: { bucket: "bucket-1", region: "ap-shanghai" },
    },
    deepseek: { provider: "deepseek", configured: false, config: {} },
    hifly: {
      provider: "hifly",
      configured: true,
      config: {},
    },
    tikhub: { provider: "tikhub", configured: false, config: {} },
  },
  runtime: {
    max_generation_count_per_batch: 5,
    max_concurrent_h3_tasks: 2,
    active_storage_provider: "cos",
  },
};

function installFetch(options?: {
  providerSave?: "ok" | "fail";
  hiflyCheck?:
    | "ok"
    | "auth"
    | "timeout"
    | "missing-credit"
    | "unsafe-credit"
    | "infinite-credit";
}) {
  const fetchMock = vi.fn((url: string, init?: RequestInit) => {
    if (url.endsWith("/api/admin/settings")) {
      return jsonResponse(settingsSnapshot);
    }
    if (
      url.endsWith("/api/admin/settings/providers/metaso") &&
      init?.method === "PUT"
    ) {
      if (options?.providerSave === "fail") {
        return jsonResponse({}, 500);
      }
      return jsonResponse({
        provider: "metaso",
        configured: true,
        config: {},
      });
    }
    if (
      url.endsWith("/api/admin/settings/providers/metaso/connection-test") &&
      init?.method === "POST"
    ) {
      return jsonResponse({
        status: "ok",
        provider: "metaso",
        test_kind: "metaso_h3",
      });
    }
    if (
      url.endsWith("/api/admin/settings/providers/hifly/connection-test") &&
      init?.method === "POST"
    ) {
      if (options?.hiflyCheck === "auth") {
        return jsonResponse(
          {
            detail: {
              code: "HIFLY_AUTH_FAILED",
              message: "Hifly 凭据认证失败；未创建收费任务。",
            },
          },
          422,
        );
      }
      if (options?.hiflyCheck === "timeout") {
        return jsonResponse(
          {
            detail: {
              code: "HIFLY_ACCOUNT_CHECK_TIMEOUT",
              message: "Hifly 只读账户检查超时；未创建收费任务。",
            },
          },
          504,
        );
      }
      if (options?.hiflyCheck === "missing-credit") {
        return jsonResponse({
          status: "ok",
          provider: "hifly",
          test_kind: "account_credit",
        });
      }
      if (options?.hiflyCheck === "unsafe-credit") {
        return jsonResponse({
          status: "ok",
          provider: "hifly",
          test_kind: "account_credit",
          account_credit: Number.MAX_SAFE_INTEGER + 1,
        });
      }
      if (options?.hiflyCheck === "infinite-credit") {
        return jsonResponse({
          status: "ok",
          provider: "hifly",
          test_kind: "account_credit",
          account_credit: Number.POSITIVE_INFINITY,
        });
      }
      return jsonResponse({
        status: "ok",
        provider: "hifly",
        test_kind: "account_credit",
        account_credit: 321,
      });
    }
    if (
      url.endsWith("/api/admin/settings/providers/hifly") &&
      init?.method === "PUT"
    ) {
      return jsonResponse({
        provider: "hifly",
        configured: true,
        config: {},
      });
    }
    throw new Error(`unexpected request: ${url} ${init?.method ?? "GET"}`);
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

describe("SettingsPanel", () => {
  function providerCard(container: HTMLElement, provider: string) {
    const card = container.querySelector(
      `form[data-provider="${provider}"]`,
    ) as HTMLElement | null;
    if (!card) {
      throw new Error(`provider card not rendered: ${provider}`);
    }
    return within(card);
  }

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("renders provider cards and runtime values from the loaded snapshot", async () => {
    installFetch();
    const { container } = render(<SettingsPanel />);

    expect(screen.getByText("正在读取服务设置")).toBeInTheDocument();
    expect(
      await screen.findByRole("region", { name: "服务设置" }),
    ).toBeInTheDocument();

    const metaso = providerCard(container, "metaso");
    expect(metaso.getByText("视频生成")).toBeInTheDocument();
    expect(metaso.getByText("已配置")).toBeInTheDocument();

    const apilio = providerCard(container, "apilio");
    expect(apilio.getByText("未配置")).toBeInTheDocument();

    const cos = providerCard(container, "cos");
    expect(cos.getByLabelText("Bucket")).toHaveValue("bucket-1");

    const hifly = providerCard(container, "hifly");
    expect(hifly.getByText("数字人口播")).toBeInTheDocument();
    expect(
      hifly.getByText("Hifly · 只读检查账户余额，不会创建收费任务"),
    ).toBeInTheDocument();
    expect(hifly.getByLabelText("API Key")).toHaveValue("");
    expect(hifly.getByLabelText("API Key")).toHaveAttribute(
      "placeholder",
      "已保存，留空不修改",
    );

    expect(screen.getByText("运行设置")).toBeInTheDocument();
    expect(screen.getByLabelText("单次生成数量上限")).toHaveValue(5);
    expect(screen.getByLabelText("视频生成并发数")).toHaveValue(2);
  });

  it("shows a role=alert error when the settings snapshot cannot be loaded", async () => {
    const fetchMock = vi.fn(() => jsonResponse({}, 500));
    vi.stubGlobal("fetch", fetchMock);

    render(<SettingsPanel />);

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("设置暂不可用（500）");
    expect(screen.queryByText("视频生成")).toBeNull();
  });

  it("saves an edited provider key through the PUT settings route", async () => {
    const fetchMock = installFetch();
    const { container } = render(<SettingsPanel />);

    await screen.findByText("视频生成");
    const metaso = providerCard(container, "metaso");

    const keyInput = metaso.getByLabelText("API Key");
    expect(keyInput).toHaveAttribute("type", "password");

    fireEvent.change(keyInput, { target: { value: DUMMY_KEY } });
    fireEvent.click(metaso.getByRole("button", { name: "保存" }));

    expect(await metaso.findByText("已保存")).toBeInTheDocument();
    // A saved secret never lingers in the form.
    expect(metaso.getByLabelText("API Key")).toHaveValue("");

    const saveCall = fetchMock.mock.calls.find(
      ([url, init]) =>
        String(url).endsWith("/api/admin/settings/providers/metaso") &&
        init?.method === "PUT",
    );
    expect(saveCall).toBeDefined();
    expect(saveCall?.[1]?.body).toBe(
      JSON.stringify({ config: { api_key: DUMMY_KEY } }),
    );
  });

  it("reports a provider save failure inline without crashing", async () => {
    installFetch({ providerSave: "fail" });
    const { container } = render(<SettingsPanel />);

    await screen.findByText("视频生成");
    const metaso = providerCard(container, "metaso");

    fireEvent.change(metaso.getByLabelText("API Key"), {
      target: { value: DUMMY_KEY },
    });
    fireEvent.click(metaso.getByRole("button", { name: "保存" }));

    expect(
      await metaso.findByText("保存失败，请检查必填项与管理员权限。"),
    ).toBeInTheDocument();
    // 失败提示必须是 role="alert"（整改清单 评估登记 6：读屏即时播报）。
    expect(metaso.getByRole("alert")).toHaveTextContent("保存失败");
  });

  it("runs the connection test and surfaces the ok result", async () => {
    const fetchMock = installFetch();
    const { container } = render(<SettingsPanel />);

    await screen.findByText("视频生成");
    const metaso = providerCard(container, "metaso");

    fireEvent.click(metaso.getByRole("button", { name: "测试连接" }));

    expect(await metaso.findByText("连接测试通过")).toBeInTheDocument();
    expect(
      fetchMock.mock.calls.some(
        ([url, init]) =>
          String(url).endsWith(
            "/api/admin/settings/providers/metaso/connection-test",
          ) && init?.method === "POST",
      ),
    ).toBe(true);
  });

  it("shows the Hifly read-only account result without starting a paid task", async () => {
    const fetchMock = installFetch();
    const { container } = render(<SettingsPanel />);

    await screen.findByText("数字人口播");
    const hifly = providerCard(container, "hifly");
    fireEvent.click(hifly.getByRole("button", { name: "只读检查" }));

    expect(
      await hifly.findByText("只读账户检查通过，余额 321 积分；未创建收费任务"),
    ).toBeInTheDocument();
    expect(
      fetchMock.mock.calls.some(
        ([url, init]) =>
          String(url).endsWith(
            "/api/admin/settings/providers/hifly/connection-test",
          ) && init?.method === "POST",
      ),
    ).toBe(true);
    expect(
      fetchMock.mock.calls.some(([url]) =>
        String(url).includes("/api/oral/tasks"),
      ),
    ).toBe(false);
  });

  it("saves the Hifly token through the encrypted provider settings route", async () => {
    const fetchMock = installFetch();
    const { container } = render(<SettingsPanel />);

    await screen.findByText("数字人口播");
    const hifly = providerCard(container, "hifly");
    fireEvent.change(hifly.getByLabelText("API Key"), {
      target: { value: DUMMY_KEY },
    });
    fireEvent.click(hifly.getByRole("button", { name: "保存" }));

    expect(await hifly.findByText("已保存")).toBeInTheDocument();
    expect(hifly.getByLabelText("API Key")).toHaveValue("");
    const call = fetchMock.mock.calls.find(
      ([url, init]) =>
        String(url).endsWith("/api/admin/settings/providers/hifly") &&
        init?.method === "PUT",
    );
    expect(call?.[1]?.body).toBe(
      JSON.stringify({ config: { api_key: DUMMY_KEY } }),
    );
  });

  it.each([
    ["auth", "Hifly 凭据认证失败；未创建收费任务。"],
    ["timeout", "Hifly 只读账户检查超时；未创建收费任务。"],
  ] as const)(
    "surfaces the Hifly %s result without claiming success",
    async (kind, message) => {
      installFetch({ hiflyCheck: kind });
      const { container } = render(<SettingsPanel />);

      await screen.findByText("数字人口播");
      const hifly = providerCard(container, "hifly");
      fireEvent.click(hifly.getByRole("button", { name: "只读检查" }));

      expect(await hifly.findByRole("alert")).toHaveTextContent(message);
      expect(hifly.queryByText(/检查通过/)).toBeNull();
    },
  );

  it.each(["missing-credit", "unsafe-credit", "infinite-credit"] as const)(
    "rejects a Hifly %s response without claiming success",
    async (kind) => {
      installFetch({ hiflyCheck: kind });
      const { container } = render(<SettingsPanel />);

      await screen.findByText("数字人口播");
      const hifly = providerCard(container, "hifly");
      fireEvent.click(hifly.getByRole("button", { name: "只读检查" }));

      expect(await hifly.findByRole("alert")).toHaveTextContent(
        "只读账户检查响应异常，请稍后重试。",
      );
      expect(hifly.queryByText(/检查通过/)).toBeNull();
      expect(hifly.queryByText(/连接测试通过/)).toBeNull();
    },
  );

  it.each([
    ["workspace", "/api/admin/settings"],
    ["control", "/api/control/settings"],
  ] as const)(
    "keeps the %s settings session active after Hifly rejects its own credential",
    async (source, basePath) => {
      const onSessionExpired = vi.fn();
      window.addEventListener(SESSION_EXPIRED_EVENT, onSessionExpired);
      const fetchMock = vi.fn((url: string, init?: RequestInit) => {
        if (url.endsWith(basePath) && !init?.method) {
          return jsonResponse(settingsSnapshot);
        }
        if (
          url.endsWith(`${basePath}/providers/hifly/connection-test`) &&
          init?.method === "POST"
        ) {
          return jsonResponse(
            {
              detail: {
                code: "HIFLY_AUTH_FAILED",
                message: "Hifly 凭据认证失败；未创建收费任务。",
              },
            },
            422,
          );
        }
        throw new Error(`unexpected request: ${url} ${init?.method ?? "GET"}`);
      });
      vi.stubGlobal("fetch", fetchMock);

      const { container } = render(<SettingsPanel source={source} />);
      await screen.findByText("数字人口播");
      const hifly = providerCard(container, "hifly");
      fireEvent.click(hifly.getByRole("button", { name: "只读检查" }));

      expect(await hifly.findByRole("alert")).toHaveTextContent(
        "Hifly 凭据认证失败；未创建收费任务。",
      );
      expect(onSessionExpired).not.toHaveBeenCalled();
      expect(hifly.getByRole("button", { name: "只读检查" })).toBeEnabled();
      expect(hifly.getByLabelText("API Key")).toBeEnabled();
      window.removeEventListener(SESSION_EXPIRED_EVENT, onSessionExpired);
    },
  );

  it("keeps provider settings and checks disabled for read-only operators", async () => {
    installFetch();
    const { container } = render(<SettingsPanel readOnly />);

    await screen.findByText("数字人口播");
    const hifly = providerCard(container, "hifly");
    expect(hifly.getByLabelText("API Key")).toBeDisabled();
    expect(hifly.getByRole("button", { name: "保存" })).toBeDisabled();
    expect(hifly.getByRole("button", { name: "只读检查" })).toBeDisabled();
  });
});
