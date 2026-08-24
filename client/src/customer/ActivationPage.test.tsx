import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { CustomerApiError } from "../api";
import { ActivationPage } from "./ActivationPage";

function errorOf(
  kind: ReturnType<() => CustomerApiError>["kind"] | string,
  properties: Partial<ConstructorParameters<typeof CustomerApiError>[0]> = {},
): CustomerApiError {
  return new CustomerApiError({
    message: "请求失败",
    ...properties,
    // The kind getter derives from status/code/transportKind; the helper
    // below keeps the fixture explicit for the display cases that matter.
    ...(kind === "rate-limited" ? { status: 429, code: "RATE_LIMITED" } : {}),
    ...(kind === "idempotency-conflict"
      ? { status: 409, code: "IDEMPOTENCY_CONFLICT" }
      : {}),
    ...(kind === "bad-request"
      ? { status: 400, code: "ACTIVATION_UNAVAILABLE" }
      : {}),
  });
}

describe("ActivationPage", () => {
  it("renders the activation form without any internal-token field", () => {
    const onActivate = vi.fn();
    render(
      <ActivationPage onActivate={onActivate} isBusy={false} error={null} />,
    );

    expect(screen.getByLabelText(/激活码/)).toBeInTheDocument();
    expect(screen.getByLabelText(/设备名称/)).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "激活并进入工作台" }),
    ).toBeInTheDocument();
    // FE-02 red line: the internal access-token field is not a customer
    // entrance — it must not exist on this page.
    expect(screen.queryByLabelText(/内部访问令牌/)).toBeNull();
  });

  it("does not submit until both fields are filled", () => {
    const onActivate = vi.fn();
    render(
      <ActivationPage onActivate={onActivate} isBusy={false} error={null} />,
    );

    fireEvent.click(screen.getByRole("button", { name: "激活并进入工作台" }));
    expect(onActivate).not.toHaveBeenCalled();

    fireEvent.change(screen.getByLabelText(/激活码/), {
      target: { value: "XS04-AAAAAAA" },
    });
    fireEvent.click(screen.getByRole("button", { name: "激活并进入工作台" }));
    expect(onActivate).not.toHaveBeenCalled();

    fireEvent.change(screen.getByLabelText(/设备名称/), {
      target: { value: "工作电脑" },
    });
    fireEvent.click(screen.getByRole("button", { name: "激活并进入工作台" }));
    expect(onActivate).toHaveBeenCalledWith({
      activationCode: "XS04-AAAAAAA",
      deviceName: "工作电脑",
    });
  });

  it("disables the form while a submission is in flight", () => {
    render(<ActivationPage onActivate={vi.fn()} isBusy={true} error={null} />);

    expect(screen.getByRole("button", { name: "正在激活…" })).toBeDisabled();
    expect(screen.getByLabelText(/激活码/)).toBeDisabled();
    expect(screen.getByLabelText(/设备名称/)).toBeDisabled();
  });

  it("shows the server's anti-enumeration message verbatim", () => {
    render(
      <ActivationPage
        onActivate={vi.fn()}
        isBusy={false}
        error={
          new CustomerApiError({
            message: "激活码不可用",
            status: 400,
            code: "ACTIVATION_UNAVAILABLE",
          })
        }
      />,
    );

    expect(screen.getByText("激活码不可用")).toBeInTheDocument();
  });

  it("shows the retry wait in seconds when rate limited", () => {
    render(
      <ActivationPage
        onActivate={vi.fn()}
        isBusy={false}
        error={
          new CustomerApiError({
            message: "请求过于频繁",
            status: 429,
            code: "RATE_LIMITED",
            retryAfterSeconds: 17,
          })
        }
      />,
    );

    expect(screen.getByText(/17 秒/)).toBeInTheDocument();
  });

  it("reports the request id on an idempotency conflict", () => {
    render(
      <ActivationPage
        onActivate={vi.fn()}
        isBusy={false}
        error={
          new CustomerApiError({
            message: "幂等键冲突",
            status: 409,
            code: "IDEMPOTENCY_CONFLICT",
            requestId: "req-conflict-1",
          })
        }
      />,
    );

    expect(screen.getByText(/req-conflict-1/)).toBeInTheDocument();
  });

  it("falls back to the error message for other failures", () => {
    render(
      <ActivationPage
        onActivate={vi.fn()}
        isBusy={false}
        error={errorOf("unknown", {
          message: "网络连接失败，请检查网络",
          transportKind: "network",
        })}
      />,
    );

    expect(screen.getByText("网络连接失败，请检查网络")).toBeInTheDocument();
  });
});
