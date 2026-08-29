import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { AdminActivationSection } from "./AdminActivationSection";

const adminActor = {
  user_id: "admin-1",
  username: "admin",
  display_name: "管理员一号",
  role: "admin",
};

describe("AdminActivationSection", () => {
  it("shows activation management inside an authenticated admin session", () => {
    render(
      <AdminActivationSection
        actor={adminActor}
        unitPriceFen={1000}
        onSessionExpired={vi.fn()}
      />,
    );

    expect(
      screen.getByRole("button", { name: "生成激活码" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "激活码列表" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "激活码发放" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { name: "直接生成激活码" }),
    ).toBeInTheDocument();
  });

  it("marks auditor sessions as read-only", () => {
    render(
      <AdminActivationSection
        actor={{ ...adminActor, role: "auditor" }}
        unitPriceFen={1000}
        onSessionExpired={vi.fn()}
      />,
    );

    expect(screen.getByText(/审计员只读/)).toBeInTheDocument();
    expect(screen.getByText(/当前为只读模式/)).toBeInTheDocument();
  });

  it("switches between activation subpages", () => {
    render(
      <AdminActivationSection
        actor={adminActor}
        unitPriceFen={1000}
        onSessionExpired={vi.fn()}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "激活码发放" }));
    expect(
      screen.getByRole("heading", { name: "发放激活码" }),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "生成激活码" }));
    expect(
      screen.getByRole("heading", { name: "直接生成激活码" }),
    ).toBeInTheDocument();
  });
});
