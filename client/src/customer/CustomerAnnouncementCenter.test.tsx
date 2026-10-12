import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { CustomerAnnouncementCenter } from "./CustomerAnnouncementCenter";

afterEach(() => vi.unstubAllGlobals());

it("公告使用原生按钮展开，关闭仍只提交已读回执", async () => {
  const body = "合成公告正文".repeat(20);
  const fetcher = vi.fn().mockResolvedValue({
    ok: true,
    json: async () => ({
      items: [
        { id: "a1", title: "测试公告", body, tone: "info", pinned: false },
      ],
    }),
  });
  vi.stubGlobal("fetch", fetcher);
  render(<CustomerAnnouncementCenter token="fixture" />);
  expect(
    await screen.findByRole("region", { name: "平台公告" }),
  ).toBeInTheDocument();
  const expand = screen.getByRole("button", { name: /点击展开/ });
  expect(expand.tagName).toBe("BUTTON");
  expect(expand).toHaveAttribute("aria-expanded", "false");
  fireEvent.click(expand);
  expect(screen.getByRole("button", { name: body })).toHaveAttribute(
    "aria-expanded",
    "true",
  );
  fireEvent.click(screen.getByRole("button", { name: "关闭公告 测试公告" }));
  await waitFor(() =>
    expect(
      screen.queryByRole("region", { name: "平台公告" }),
    ).not.toBeInTheDocument(),
  );
  expect(fetcher.mock.calls.at(-1)?.[0]).toMatch(
    /\/api\/announcements\/a1\/read$/,
  );
  expect(fetcher.mock.calls.at(-1)?.[1]).toMatchObject({ method: "POST" });
});
