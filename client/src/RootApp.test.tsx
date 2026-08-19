import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { RootApp } from "./RootApp";

function jsonResponse(payload: unknown, status = 200) {
  return Promise.resolve({
    ok: status >= 200 && status < 300,
    status,
    json: async () => payload,
  });
}

describe("RootApp", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it.each(["/admin", "/admin/"])(
    "routes %s to the internal management page",
    (path) => {
      vi.stubGlobal(
        "fetch",
        vi.fn(() => jsonResponse({ items: [] })),
      );

      render(<RootApp path={path} />);

      expect(
        screen.getByRole("heading", { name: "内部运营管理" }),
      ).toBeInTheDocument();
      expect(screen.queryByRole("heading", { name: "镜序 Studio" })).toBeNull();
    },
  );

  it("keeps normal paths on the user workspace", async () => {
    const fetchMock = vi.fn((url: string) => {
      if (url.endsWith("/api/auth/me")) {
        return jsonResponse({
          id: "user-1",
          username: "user-1",
          display_name: "运营",
          role: "employee",
        });
      }
      if (url.endsWith("/health")) {
        return jsonResponse({ status: "ok", service: "video-replica-api" });
      }
      return jsonResponse([]);
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<RootApp path="/" />);

    expect(
      await screen.findByRole("heading", { name: "项目" }),
    ).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "内部运营管理" })).toBeNull();
  });
});
