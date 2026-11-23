import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { SourceReviewNotice } from "./SourceReviewNotice";

describe("原片校对状态", () => {
  it("只有实际完成的通过状态显示校对通过，不承诺完全准确", () => {
    render(
      <SourceReviewNotice
        review={{ status: "PASSED", attempted: true, issues: [] }}
      />,
    );
    expect(
      screen.getByRole("status", { name: "原片校对状态" }),
    ).toHaveTextContent("原片校对已通过");
    expect(screen.queryByText(/100%|完美复刻/)).not.toBeInTheDocument();
  });

  it("未实际发起校对不能显示通过", () => {
    render(
      <SourceReviewNotice
        review={{ status: "PASSED", attempted: false, issues: [] }}
      />,
    );
    expect(
      screen.getByRole("status", { name: "原片校对状态" }),
    ).not.toHaveTextContent("已通过");
  });

  it.each(["INCOMPLETE", "UNAVAILABLE"] as const)(
    "%s 保留初稿并集中显示定位问题，不增加拦截按钮",
    (status) => {
      render(
        <SourceReviewNotice
          review={{
            status,
            attempted: true,
            issues: [
              {
                code: "SOURCE_REVIEW_INCOMPLETE",
                shot_id: "s1",
                message: "主体手部被遮挡，局部动作待确认",
              },
            ],
          }}
        />,
      );
      expect(
        screen.getByRole("status", { name: "原片校对状态" }),
      ).toHaveTextContent("s1：主体手部被遮挡，局部动作待确认");
      expect(
        screen.getByRole("status", { name: "原片校对状态" }),
      ).toHaveTextContent("已保留拆解结果");
      expect(screen.queryByRole("button")).not.toBeInTheDocument();
    },
  );
});
