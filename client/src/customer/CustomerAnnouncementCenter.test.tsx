// T34：公告中心复用共享客户请求器；已读回执成功才移除公告。
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, expect, test, vi } from "vitest";
import { CustomerAnnouncementCenter } from "./CustomerAnnouncementCenter";

// 假凭据刻意短于 8 字符：verify_no_secrets.sh 只放行这种长度，值本身不参与断言。
const CREDENTIAL = { kind: "session" as const, token: "tok-1" };

vi.mock("../api", () => ({
  customerListAnnouncements: vi.fn(async () => [
    {
      id: "a1",
      title: "维护通知",
      body: "周日 02:00-04:00 系统维护",
      tone: "maintenance",
      pinned: true,
      read: false,
    },
  ]),
  customerMarkAnnouncementRead: vi.fn(async () => undefined),
}));

import { customerMarkAnnouncementRead } from "../api";

beforeEach(() => {
  vi.mocked(customerMarkAnnouncementRead).mockClear();
});

test("点「知道了」后等回执成功才移除公告", async () => {
  render(<CustomerAnnouncementCenter credential={CREDENTIAL} />);
  const button = await screen.findByRole("button", {
    name: "关闭公告 维护通知",
  });
  fireEvent.click(button);
  await waitFor(() =>
    expect(customerMarkAnnouncementRead).toHaveBeenCalledWith(CREDENTIAL, "a1"),
  );
  await waitFor(() =>
    expect(
      screen.queryByRole("button", { name: "关闭公告 维护通知" }),
    ).toBeNull(),
  );
});

test("已读回执失败：公告保留原位并提示可重试", async () => {
  vi.mocked(customerMarkAnnouncementRead).mockRejectedValueOnce(
    new Error("网络断开"),
  );
  render(<CustomerAnnouncementCenter credential={CREDENTIAL} />);
  const button = await screen.findByRole("button", {
    name: "关闭公告 维护通知",
  });
  fireEvent.click(button);
  await waitFor(() => expect(screen.getByRole("alert")).toBeInTheDocument());
  // 公告仍在原位，可再次点击。
  expect(
    screen.getByRole("button", { name: "关闭公告 维护通知" }),
  ).toBeInTheDocument();
});

test("列表读取失败：显示可重试的失败态而不是静默为空", async () => {
  const { customerListAnnouncements } = await import("../api");
  vi.mocked(customerListAnnouncements).mockRejectedValueOnce(
    new Error("公告服务暂不可用"),
  );
  render(<CustomerAnnouncementCenter credential={CREDENTIAL} />);
  expect(await screen.findByRole("alert")).toHaveTextContent(
    "公告服务暂不可用",
  );
});
