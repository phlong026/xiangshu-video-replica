import { fireEvent, render, screen } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import { FilterBar } from "./FilterBar";
import { ListPage } from "./ListPage";

function page(props: Partial<Parameters<typeof ListPage>[0]> = {}) {
  return (
    <ListPage
      ariaLabel="测试列表"
      hasItems={false}
      loading={false}
      emptyText="暂无记录。"
      {...props}
    >
      <table aria-label="数据表">
        <tbody>
          <tr>
            <td>一行数据</td>
          </tr>
        </tbody>
      </table>
    </ListPage>
  );
}

it("首次加载显示骨架与加载文案，不显示空态或表格", () => {
  render(page({ loading: true, loadingText: "正在读取工单…" }));
  expect(screen.getByRole("status")).toHaveTextContent("正在读取工单…");
  expect(screen.queryByText("暂无记录。")).toBeNull();
  expect(screen.queryByRole("table", { name: "数据表" })).toBeNull();
});

it("刷新中沿用旧数据，不闪回骨架", () => {
  render(page({ loading: true, hasItems: true }));
  expect(screen.getByRole("table", { name: "数据表" })).toBeInTheDocument();
  expect(screen.queryByText("加载中…")).toBeNull();
});

it("无数据且无错误显示空态", () => {
  render(page());
  expect(screen.getByText("暂无记录。")).toBeInTheDocument();
});

it("无数据且有错误只显示错误并可重试，不叠空态", () => {
  const onRetry = vi.fn();
  render(page({ error: "读取失败：网络中断", onRetry }));
  expect(screen.getByRole("alert")).toHaveTextContent("读取失败：网络中断");
  expect(screen.queryByText("暂无记录。")).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "重试" }));
  expect(onRetry).toHaveBeenCalledTimes(1);
});

it("有数据时渲染表格、提示条与分页槽", () => {
  render(
    page({
      hasItems: true,
      notice: "已保存",
      pagination: <nav aria-label="分页">第 1 页</nav>,
    }),
  );
  expect(screen.getByRole("table", { name: "数据表" })).toBeInTheDocument();
  expect(screen.getByRole("status")).toHaveTextContent("已保存");
  expect(screen.getByRole("navigation", { name: "分页" })).toBeInTheDocument();
});

it("FilterBar：查询提交、重置、其它动作按固定顺序排列", () => {
  const onSubmit = vi.fn();
  const onReset = vi.fn();
  render(
    <FilterBar
      actions={<button type="button">导出 CSV</button>}
      onReset={onReset}
      onSubmit={onSubmit}
    >
      <label>
        客户
        <input />
      </label>
    </FilterBar>,
  );
  const buttons = screen.getAllByRole("button").map((b) => b.textContent);
  expect(buttons).toEqual(["查询", "重置", "导出 CSV"]);
  fireEvent.click(screen.getByRole("button", { name: "查询" }));
  expect(onSubmit).toHaveBeenCalledTimes(1);
  fireEvent.click(screen.getByRole("button", { name: "重置" }));
  expect(onReset).toHaveBeenCalledTimes(1);
});

it("FilterBar：不传 onReset 就没有重置按钮；busy 时禁用", () => {
  render(
    <FilterBar busy onSubmit={vi.fn()}>
      <span>条件</span>
    </FilterBar>,
  );
  expect(screen.queryByRole("button", { name: "重置" })).toBeNull();
  expect(screen.getByRole("button", { name: "查询" })).toBeDisabled();
});
