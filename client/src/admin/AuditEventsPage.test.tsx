import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { AuditLogItem } from "../api.admin";
import { AuditEventsPage } from "./AuditEventsPage";

function jsonResponse(payload: unknown, status = 200) {
  return Promise.resolve({
    ok: status >= 200 && status < 300,
    status,
    json: async () => payload,
    text: async () => JSON.stringify(payload),
  });
}

function auditItem(partial: Partial<AuditLogItem> = {}): AuditLogItem {
  return {
    event_id: "evt-1",
    event_type: "ADMIN_ADJUSTMENT",
    actor_user_id: "admin-1",
    actor_username: "admin_op",
    target_user_id: "customer-1",
    source_document_type: "CS_TICKET",
    source_document_ref: "manual-20260901-001",
    reason: "客户电话反馈补发",
    request_id: "req-audit-1",
    created_at: "2026-09-01T10:00:00+00:00",
    ...partial,
  };
}

const PAGE_SIZE = 20;

function installFetch(options?: { status?: number }) {
  const fetchMock = vi.fn((url: string) => {
    if (!String(url).includes("/api/control/audit-log")) {
      throw new Error(`unexpected request: ${url}`);
    }
    if (options?.status) {
      return jsonResponse({}, options.status);
    }
    const { searchParams } = new URL(String(url));
    const actor = searchParams.get("actor_user_id");
    const offset = Number(searchParams.get("offset") ?? "0");
    if (actor) {
      return jsonResponse({
        items: [auditItem({ actor_user_id: actor, actor_username: actor })],
        total: 1,
        limit: PAGE_SIZE,
        offset,
      });
    }
    if (offset >= PAGE_SIZE) {
      return jsonResponse({
        items: [
          auditItem({
            event_id: "evt-21",
            event_type: "CODE_REVEAL",
            target_user_id: "customer-9",
            request_id: "req-audit-21",
          }),
        ],
        total: 21,
        limit: PAGE_SIZE,
        offset,
      });
    }
    return jsonResponse({
      items: [auditItem()],
      total: 21,
      limit: PAGE_SIZE,
      offset,
    });
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

describe("AuditEventsPage", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("loads the first audit page and renders key row content", async () => {
    installFetch();
    render(<AuditEventsPage />);

    expect(await screen.findByText("ADMIN_ADJUSTMENT")).toBeInTheDocument();
    expect(screen.getByText("admin_op")).toBeInTheDocument();
    expect(screen.getByText("customer-1")).toBeInTheDocument();
    expect(screen.getByText("客户电话反馈补发")).toBeInTheDocument();
    expect(screen.getByText("req-audit-1")).toBeInTheDocument();
    expect(
      screen.getByRole("table", { name: "审计事件列表" }),
    ).toBeInTheDocument();

    // 21 rows over a 20-row page size expose the pagination controls.
    expect(screen.getByRole("navigation", { name: "分页" })).toBeVisible();
    expect(screen.getByRole("button", { name: "上一页" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "下一页" })).toBeEnabled();
  });

  it("requests offset=20 when moving to the next page", async () => {
    const fetchMock = installFetch();
    render(<AuditEventsPage />);

    await screen.findByText("ADMIN_ADJUSTMENT");
    fireEvent.click(screen.getByRole("button", { name: "下一页" }));

    await screen.findByText("CODE_REVEAL");
    await waitFor(() => {
      expect(
        fetchMock.mock.calls.some(([url]) =>
          String(url).endsWith("/api/control/audit-log?limit=20&offset=20"),
        ),
      ).toBe(true);
    });
    expect(screen.getByText("req-audit-21")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "上一页" })).toBeEnabled();
    expect(screen.getByText(/第 2 \/ 2 页（共 21 条）/)).toBeInTheDocument();
  });

  it("sends the actor filter only on submit, not while typing", async () => {
    // 整改清单 评估登记 5 的回归锁定：输入不触发请求，提交只发一次。
    const fetchMock = installFetch();
    render(<AuditEventsPage />);

    await screen.findByText("ADMIN_ADJUSTMENT");
    const requestsAfterLoad = fetchMock.mock.calls.length;

    fireEvent.change(screen.getByLabelText("操作人 ID"), {
      target: { value: "admin_u" },
    });
    expect(fetchMock.mock.calls.length).toBe(requestsAfterLoad);

    fireEvent.click(screen.getByRole("button", { name: "筛选" }));

    await screen.findAllByText("admin_u");
    await waitFor(() => {
      expect(
        fetchMock.mock.calls.some(
          ([url]) =>
            String(url).includes("/api/control/audit-log?") &&
            String(url).includes("actor_user_id=admin_u"),
        ),
      ).toBe(true);
    });
  });

  it("shows the load failure as an alert", async () => {
    installFetch({ status: 500 });
    render(<AuditEventsPage />);

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("加载失败：读取审计日志失败（500）");
  });
});
