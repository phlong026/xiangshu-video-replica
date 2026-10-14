import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { GlobalSearch } from "./GlobalSearch";

const search = vi.fn();

vi.mock("../api.admin", () => ({
  globalSearch: (q: string) => search(q),
}));

function submit(value: string) {
  fireEvent.change(screen.getByLabelText("全局搜索"), { target: { value } });
  fireEvent.click(screen.getByRole("button", { name: "搜索" }));
}

describe("GlobalSearch", () => {
  beforeEach(() => search.mockReset());

  it("只命中一项时直接跳转并清空输入", async () => {
    search.mockResolvedValue({
      items: [{ type: "order", id: "ORD-1", label: "订单 ORD-1", detail: "x" }],
    });
    const onPick = vi.fn();
    render(<GlobalSearch onPick={onPick} />);
    submit("ORD-1");
    await waitFor(() =>
      expect(onPick).toHaveBeenCalledWith(
        expect.objectContaining({ type: "order", id: "ORD-1" }),
      ),
    );
    expect(screen.getByLabelText("全局搜索")).toHaveValue("");
  });

  it("命中多项时列出供选择，选中后跳转", async () => {
    search.mockResolvedValue({
      items: [
        { type: "customer", id: "u1", label: "洪雅店", detail: "客户 · hy" },
        { type: "customer", id: "u2", label: "洪雅二店", detail: "客户 · hy2" },
      ],
    });
    const onPick = vi.fn();
    render(<GlobalSearch onPick={onPick} />);
    submit("洪雅");
    fireEvent.click(await screen.findByRole("button", { name: /洪雅二店/ }));
    expect(onPick).toHaveBeenCalledWith(expect.objectContaining({ id: "u2" }));
  });

  it("无命中与过短输入都有明确提示，不静默", async () => {
    search.mockResolvedValue({ items: [] });
    render(<GlobalSearch onPick={vi.fn()} />);
    submit("a");
    expect(
      await screen.findByText("请至少输入 2 个字符。"),
    ).toBeInTheDocument();
    expect(search).not.toHaveBeenCalled();
    submit("没有这个");
    expect(
      await screen.findByText("没有找到匹配的客户、订单或任务。"),
    ).toBeInTheDocument();
  });
});
