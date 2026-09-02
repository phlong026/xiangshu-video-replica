import { fireEvent, render, screen } from "@testing-library/react";
import { useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ConfirmDialog } from "./ConfirmDialog";
import { DataTable } from "./DataTable";
import { PageBanner } from "./PageBanner";
import { Pagination } from "./Pagination";
import {
  ActivationCodeStatusBadge,
  DeviceStatusBadge,
  OrderStatusBadge,
  PlatformBadge,
} from "./StatusBadge";

describe("PageBanner", () => {
  it("announces errors with role=alert and notices with role=status", () => {
    const { rerender } = render(<PageBanner tone="error">加载失败</PageBanner>);
    expect(screen.getByRole("alert")).toHaveTextContent("加载失败");

    rerender(<PageBanner tone="notice">已保存</PageBanner>);
    expect(screen.getByRole("status")).toHaveTextContent("已保存");
    expect(screen.queryByRole("alert")).toBeNull();
  });
});

describe("StatusBadge", () => {
  it("renders the unified vocabulary labels with a tone class", () => {
    render(
      <>
        <ActivationCodeStatusBadge status="REVOKED" />
        <ActivationCodeStatusBadge status="MYSTERY" />
        <DeviceStatusBadge status="REVOKED" />
        <OrderStatusBadge status="PAID" />
        <PlatformBadge platform="windows" />
      </>,
    );
    expect(screen.getByText("已撤销")).toHaveClass("status-badge--danger");
    expect(screen.getByText("MYSTERY")).toHaveClass("status-badge--neutral");
    expect(screen.getByText("已强制退出")).toBeInTheDocument();
    expect(screen.getByText("已支付")).toHaveClass("status-badge--good");
    expect(screen.getByText("Windows")).toBeInTheDocument();
  });
});

describe("Pagination", () => {
  it("hides itself when there is only one page", () => {
    render(
      <Pagination limit={50} offset={0} total={30} onPageChange={() => {}} />,
    );
    expect(screen.queryByRole("navigation")).toBeNull();
  });

  it("shows unified page text and drives offset changes", () => {
    const onPageChange = vi.fn();
    const middlePage = render(
      <Pagination
        limit={20}
        offset={20}
        total={55}
        onPageChange={onPageChange}
      />,
    );
    expect(screen.getByText("第 2 / 3 页（共 55 条）")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "上一页" }));
    expect(onPageChange).toHaveBeenLastCalledWith(0);

    fireEvent.click(screen.getByRole("button", { name: "下一页" }));
    expect(onPageChange).toHaveBeenLastCalledWith(40);
    middlePage.unmount();

    // 末页的"下一页"要禁用。
    const lastPage = render(
      <Pagination
        limit={20}
        offset={40}
        total={55}
        onPageChange={onPageChange}
      />,
    );
    expect(screen.getByRole("button", { name: "下一页" })).toBeDisabled();
    lastPage.unmount();

    // 第一页的"上一页"也要禁用。
    render(
      <Pagination
        limit={20}
        offset={0}
        total={55}
        onPageChange={onPageChange}
      />,
    );
    expect(screen.getByRole("button", { name: "上一页" })).toBeDisabled();
  });

  it("supports the customer noun", () => {
    render(
      <Pagination
        limit={20}
        offset={0}
        total={40}
        noun="位"
        onPageChange={() => {}}
      />,
    );
    expect(screen.getByText("第 1 / 2 页（共 40 位）")).toBeInTheDocument();
  });

  it("falls back to honest page-only text when the endpoint has no total", () => {
    const onPageChange = vi.fn();
    const firstPage = render(
      <Pagination hasMore limit={20} offset={0} onPageChange={onPageChange} />,
    );
    expect(screen.getByText("第 1 页")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "下一页" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "上一页" })).toBeDisabled();

    fireEvent.click(screen.getByRole("button", { name: "下一页" }));
    expect(onPageChange).toHaveBeenLastCalledWith(20);
    firstPage.unmount();

    render(
      <Pagination
        hasMore={false}
        limit={20}
        offset={20}
        onPageChange={onPageChange}
      />,
    );
    expect(screen.getByRole("button", { name: "下一页" })).toBeDisabled();
  });
});

describe("DataTable", () => {
  it("wraps rows in a scroll container with a labelled sticky header", () => {
    render(
      <DataTable
        ariaLabel="示例表"
        headers={
          <>
            <th>列一</th>
            <th>列二</th>
          </>
        }
      >
        <tr>
          <td>1</td>
          <td>2</td>
        </tr>
      </DataTable>,
    );
    expect(screen.getByRole("table", { name: "示例表" })).toBeInTheDocument();
    expect(screen.getByText("列一")).toBeInTheDocument();
  });
});

describe("ConfirmDialog", () => {
  function Harness({
    level,
    onConfirm,
  }: {
    level: "standard" | "reason" | "reasonAndAck";
    onConfirm: (reason: string) => void;
  }) {
    const [open, setOpen] = useState(true);
    return (
      <>
        <button type="button" onClick={() => setOpen(true)}>
          重新打开
        </button>
        <ConfirmDialog
          confirmLabel="确认执行"
          description="该操作不可逆。"
          level={level}
          open={open}
          title="确认危险操作"
          onClose={() => setOpen(false)}
          onConfirm={(reason) => {
            onConfirm(reason);
            setOpen(false);
          }}
        />
      </>
    );
  }

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("requires a reason before confirming", () => {
    const onConfirm = vi.fn();
    render(<Harness level="reason" onConfirm={onConfirm} />);

    fireEvent.click(screen.getByRole("button", { name: "确认执行" }));
    expect(screen.getByRole("alert")).toHaveTextContent("请填写操作原因");
    expect(onConfirm).not.toHaveBeenCalled();

    fireEvent.change(screen.getByLabelText("操作原因"), {
      target: { value: "客户投诉补发" },
    });
    fireEvent.click(screen.getByRole("button", { name: "确认执行" }));
    expect(onConfirm).toHaveBeenCalledWith("客户投诉补发");
  });

  it("requires the acknowledgement checkbox at the high-risk level", () => {
    const onConfirm = vi.fn();
    render(<Harness level="reasonAndAck" onConfirm={onConfirm} />);

    fireEvent.change(screen.getByLabelText("操作原因"), {
      target: { value: "客服工单补发" },
    });
    fireEvent.click(screen.getByRole("button", { name: "确认执行" }));
    expect(screen.getByRole("alert")).toHaveTextContent("请先勾选确认操作");

    fireEvent.click(screen.getByLabelText("我已知晓该操作的影响"));
    fireEvent.click(screen.getByRole("button", { name: "确认执行" }));
    expect(onConfirm).toHaveBeenCalledWith("客服工单补发");
  });

  it("focuses the reason input on open and closes on Escape", () => {
    const onConfirm = vi.fn();
    render(<Harness level="reason" onConfirm={onConfirm} />);

    expect(document.activeElement).toBe(screen.getByLabelText("操作原因"));

    fireEvent.keyDown(window, { key: "Escape" });
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(onConfirm).not.toHaveBeenCalled();

    // 关闭后可重新打开，且重新打开时输入被清空。
    fireEvent.click(screen.getByRole("button", { name: "重新打开" }));
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    expect(screen.getByLabelText("操作原因")).toHaveValue("");
  });

  it("keeps the standard level to a bare confirmation and focuses the confirm button", () => {
    const onConfirm = vi.fn();
    render(<Harness level="standard" onConfirm={onConfirm} />);

    expect(screen.queryByLabelText("操作原因")).toBeNull();
    expect(document.activeElement).toBe(
      screen.getByRole("button", { name: "确认执行" }),
    );

    fireEvent.click(screen.getByRole("button", { name: "确认执行" }));
    expect(onConfirm).toHaveBeenCalledWith("");
  });
});
