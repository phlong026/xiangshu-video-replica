import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { setAdminCsrfToken } from "../api";
import { QueueModeSection } from "./QueueModeSection";

function jsonResponse(payload: unknown, status = 200) {
  return Promise.resolve({
    ok: status >= 200 && status < 300,
    status,
    json: async () => payload,
    text: async () => JSON.stringify(payload),
  });
}

function installFetch(options?: { enabled?: boolean; patchFails?: boolean }) {
  const fetchMock = vi.fn((url: string, init?: RequestInit) => {
    if (url.endsWith("/api/control/settings/queue-mode")) {
      if (init?.method === "PATCH") {
        if (options?.patchFails) {
          return jsonResponse(
            {
              detail: {
                code: "RUNTIME_SERVICE_UNAVAILABLE",
                message: "Runtime settings require the PostgreSQL runtime.",
              },
            },
            503,
          );
        }
        return jsonResponse({ fair_queue_enabled: true });
      }
      return jsonResponse({
        fair_queue_enabled: options?.enabled ?? false,
      });
    }
    throw new Error(`unexpected request: ${url}`);
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

describe("QueueModeSection", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    setAdminCsrfToken("");
  });

  it("loads and displays the current queue mode", async () => {
    installFetch({ enabled: true });
    render(<QueueModeSection />);

    expect(await screen.findByText("已开启")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "关闭公平队列" }),
    ).toBeInTheDocument();
  });

  it("switches the queue mode through a confirmation dialog", async () => {
    setAdminCsrfToken("csrf-token-1");
    const fetchMock = installFetch({ enabled: false });
    render(<QueueModeSection />);

    fireEvent.click(
      await screen.findByRole("button", { name: "开启公平队列" }),
    );

    // 说明性确认（服务端契约只收 fair_queue_enabled，无原因字段）。
    await screen.findByRole("dialog", { name: "开启公平队列" });
    fireEvent.click(screen.getByRole("button", { name: "确认开启" }));

    expect(await screen.findByText("公平队列已开启。")).toBeInTheDocument();
    const patchCall = fetchMock.mock.calls.find(
      ([, init]) => (init as RequestInit | undefined)?.method === "PATCH",
    );
    expect(patchCall).toBeDefined();
    expect(JSON.parse(String(patchCall?.[1]?.body))).toEqual({
      fair_queue_enabled: true,
    });
  });

  it("stays read-only for auditors", async () => {
    installFetch();
    render(<QueueModeSection readOnly />);

    await screen.findByText("已关闭");
    expect(screen.queryByRole("button", { name: /公平队列/ })).toBeNull();
    expect(screen.getByText(/审计员只读/)).toBeInTheDocument();
  });
});
