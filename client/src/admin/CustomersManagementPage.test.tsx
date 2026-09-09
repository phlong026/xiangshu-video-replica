import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { CustomersManagementPage } from "./CustomersManagementPage";

vi.mock("./CustomersPage", () => ({
  CustomersPage: ({
    onOpenDevices,
    onOpenSessions,
  }: {
    onOpenDevices: (userId: string) => void;
    onOpenSessions: (userId: string) => void;
  }) => (
    <>
      <button type="button" onClick={() => onOpenDevices("customer-a")}>
        查看 A 设备
      </button>
      <button type="button" onClick={() => onOpenSessions("customer-b")}>
        查看 B 会话
      </button>
    </>
  ),
}));

vi.mock("./DevicesPage", () => ({
  DevicesPage: ({ userId }: { userId?: string }) => (
    <p>设备客户：{userId ?? "全部"}</p>
  ),
}));

vi.mock("./SessionsPage", () => ({
  SessionsPage: ({
    userId,
    onCustomerChange,
  }: {
    userId?: string;
    onCustomerChange?: (userId: string | undefined) => void;
  }) => (
    <>
      <p>会话客户：{userId ?? "全部"}</p>
      <button type="button" onClick={() => onCustomerChange?.("customer-c")}>
        会话内选择 C
      </button>
    </>
  ),
}));

vi.mock("./AdminActivationSection", () => ({
  AdminActivationSection: () => null,
}));

describe("CustomersManagementPage", () => {
  it("keeps device and session panels on one customer context", () => {
    render(
      <CustomersManagementPage
        actor={{
          user_id: "admin-1",
          username: "admin",
          display_name: "管理员",
          role: "admin",
        }}
        onSessionExpired={() => {}}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "查看 A 设备" }));
    expect(screen.getByText("设备客户：customer-a")).toBeInTheDocument();
    expect(screen.getByText("会话客户：customer-a")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("tab", { name: "客户列表" }));
    fireEvent.click(screen.getByRole("button", { name: "查看 B 会话" }));
    expect(screen.getByText("设备客户：customer-b")).toBeInTheDocument();
    expect(screen.getByText("会话客户：customer-b")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "会话内选择 C" }));
    expect(screen.getByText("设备客户：customer-c")).toBeInTheDocument();
    expect(screen.getByText("会话客户：customer-c")).toBeInTheDocument();
  });
});
