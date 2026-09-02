import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { AdminActivationSection } from "./AdminActivationSection";

const adminActor = {
  user_id: "admin-1",
  username: "admin",
  display_name: "管理员一号",
  role: "admin",
};

describe("AdminActivationSection", () => {
  it("combines activation-code generation and device relationships on one page", () => {
    render(
      <AdminActivationSection actor={adminActor} onSessionExpired={vi.fn()} />,
    );

    expect(
      screen.getByRole("heading", { name: "直接生成激活码" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { name: "激活码与设备" }),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "激活码列表" })).toBeNull();
    expect(screen.queryByRole("button", { name: "激活码发放" })).toBeNull();
  });

  it("marks auditor sessions as read-only", () => {
    render(
      <AdminActivationSection
        actor={{ ...adminActor, role: "auditor" }}
        onSessionExpired={vi.fn()}
      />,
    );

    expect(screen.getByText(/审计员只读/)).toBeInTheDocument();
    // 批次生成、码列表、发放登记三个子页面各显示一条只读提示。
    expect(screen.getAllByText(/当前为只读模式/)).toHaveLength(3);
  });
});
