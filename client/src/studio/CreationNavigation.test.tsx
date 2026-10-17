import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { CreationNavigation } from "./CreationNavigation";

const navigate = vi.fn();
const context = { state: { page: "replacement" }, navigate };
vi.mock("./context", () => ({ useStudio: () => context }));

describe("统一创作导航", () => {
  it("人物置换归属 <15s 视频拆解，长视频拆解与 AI 视频共用一个一级入口", () => {
    const { rerender } = render(<CreationNavigation />);
    expect(screen.getAllByRole("tab")).toHaveLength(4);
    expect(screen.getByRole("tab", { name: "<15s 视频拆解" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    expect(
      screen.queryByRole("tab", { name: "人物置换" }),
    ).not.toBeInTheDocument();
    // 长视频拆解在页签组内，不占侧栏一级入口。
    expect(screen.getByRole("tab", { name: "长视频拆解" })).toBeInTheDocument();
    context.state.page = "reference";
    rerender(<CreationNavigation />);
    expect(screen.getByRole("tab", { name: "AI 视频" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    context.state.page = "long-replica";
    rerender(<CreationNavigation />);
    expect(screen.getByRole("tab", { name: "长视频拆解" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    fireEvent.click(screen.getByRole("tab", { name: "<15s 视频拆解" }));
    expect(navigate).toHaveBeenCalledWith("replica");
  });
});
