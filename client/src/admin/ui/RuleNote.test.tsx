import { fireEvent, render, screen } from "@testing-library/react";
import { expect, it } from "vitest";
import { RuleNote } from "./RuleNote";

it("口径说明默认折叠，点开后可见；内容保留在 DOM 中供读屏与检索", () => {
  render(
    <RuleNote>
      <p>确认收入按所消费积分对应的实际充值金额分摊。</p>
    </RuleNote>,
  );
  const details = screen
    .getByText("口径说明")
    .closest("details") as HTMLDetailsElement;
  expect(details.open).toBe(false);
  expect(
    screen.getByText("确认收入按所消费积分对应的实际充值金额分摊。"),
  ).toBeInTheDocument();
  fireEvent.click(screen.getByText("口径说明"));
  details.open = true;
  expect(details.open).toBe(true);
});
