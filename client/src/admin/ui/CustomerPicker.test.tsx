import { fireEvent, render, screen } from "@testing-library/react";
import { useState } from "react";
import { afterEach, expect, it, vi } from "vitest";
import { CustomerPicker, type PickedCustomer } from "./CustomerPicker";

afterEach(() => vi.unstubAllGlobals());

function Harness({ onPick }: { onPick: (c: PickedCustomer | null) => void }) {
  const [value, setValue] = useState<PickedCustomer | null>(null);
  return (
    <CustomerPicker
      value={value}
      onChange={(next) => {
        setValue(next);
        onPick(next);
      }}
    />
  );
}

it("按关键字联想客户，选中后显示摘要，可更换", async () => {
  const fetchMock = vi.fn(async (url: string) => ({
    ok: true,
    status: 200,
    json: async () => {
      const keyword = new URL(url).searchParams.get("username");
      return {
        items:
          keyword === "洪雅"
            ? [{ user_id: "u-1", username: "hongya", display_name: "洪雅店" }]
            : [],
        total: 0,
      };
    },
  }));
  vi.stubGlobal("fetch", fetchMock);
  const onPick = vi.fn();
  render(<Harness onPick={onPick} />);

  fireEvent.change(screen.getByRole("searchbox"), {
    target: { value: "无此客户" },
  });
  expect(await screen.findByText("没有匹配的客户。")).toBeInTheDocument();

  fireEvent.change(screen.getByRole("searchbox"), {
    target: { value: "洪雅" },
  });
  fireEvent.click(
    await screen.findByRole("button", { name: "洪雅店（hongya）" }),
  );
  expect(onPick).toHaveBeenLastCalledWith({
    user_id: "u-1",
    username: "hongya",
    display_name: "洪雅店",
  });
  expect(screen.getByText("洪雅店（hongya）")).toBeInTheDocument();
  // 关键字只取前 8 条，避免把整个客户表拉进下拉。
  expect(
    new URL(String(fetchMock.mock.calls.at(-1)?.[0])).searchParams.get("limit"),
  ).toBe("8");

  fireEvent.click(screen.getByRole("button", { name: "更换客户" }));
  expect(onPick).toHaveBeenLastCalledWith(null);
  expect(screen.getByRole("searchbox")).toBeInTheDocument();
});
