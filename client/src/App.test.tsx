import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { App } from "./App";

describe("App", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("starts on the internal login screen", () => {
    vi.stubGlobal("fetch", vi.fn());

    render(<App />);

    expect(
      screen.getByRole("heading", { name: "短视频复刻工作台" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "进入工作台" }),
    ).toBeInTheDocument();
  });

  it("navigates from login to the project page and loads local API health", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ status: "ok", service: "video-replica-api" }),
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<App />);
    fireEvent.click(screen.getByRole("button", { name: "进入工作台" }));

    expect(screen.getByRole("heading", { name: "项目" })).toBeInTheDocument();
    await waitFor(() =>
      expect(screen.getByText("本地服务已连接")).toBeInTheDocument(),
    );
    expect(fetchMock).toHaveBeenCalledWith("http://127.0.0.1:8000/health", {
      signal: expect.any(AbortSignal),
    });
  });
});
