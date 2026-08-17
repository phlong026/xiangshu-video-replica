import { fireEvent, render, screen, within } from "@testing-library/react";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";

import { WorkspaceTabs } from "./WorkspaceTabs";

function StatefulProbe({ id }: { id: string }) {
  const [count, setCount] = useState(0);
  return (
    <button type="button" onClick={() => setCount((value) => value + 1)}>
      {id} 点击次数 {count}
    </button>
  );
}

function renderTabs(overrides: Record<string, unknown> = {}) {
  const callbacks = { onTabChange: vi.fn() };
  const props = {
    activeKey: "content" as const,
    busy: false,
    onTabChange: callbacks.onTabChange,
    readOnly: false,
    tabs: [
      {
        badge: { kind: "ready" } as const,
        content: (
          <>
            <StatefulProbe id="内容页" />
            <p>内容配置区块</p>
          </>
        ),
        key: "content" as const,
        label: "内容配置",
      },
      {
        badge: { kind: "missing", count: 4 } as const,
        content: <StatefulProbe id="人物页" />,
        key: "people" as const,
        label: "人物设定",
      },
      {
        badge: { kind: "missing", count: 1 } as const,
        content: <StatefulProbe id="生成页" />,
        key: "launch" as const,
        label: "生成设置",
      },
    ],
    ...overrides,
  };
  return { callbacks, props, ...render(<WorkspaceTabs {...props} />) };
}

describe("WorkspaceTabs 三标签页壳层", () => {
  it("渲染全部标签与三个面板（含非活动面板，keep-alive）", () => {
    renderTabs();

    expect(screen.getByRole("tab", { name: /内容配置/ })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: /人物设定/ })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: /生成设置/ })).toBeInTheDocument();
    expect(screen.getByText("内容配置区块")).toBeInTheDocument();
    expect(screen.getByText("人物页 点击次数 0")).toBeInTheDocument();
    expect(screen.getByText("生成页 点击次数 0")).toBeInTheDocument();
  });

  it("活动面板可见，非活动面板带 CSS 隐藏类", () => {
    renderTabs();

    const activePanel = screen
      .getByText("内容配置区块")
      .closest('[role="tabpanel"]');
    expect(activePanel).not.toBeNull();
    expect(activePanel).not.toHaveClass("workspace-tab-panel--inactive");

    const inactivePanel = screen
      .getByText("人物页 点击次数 0")
      .closest('[role="tabpanel"]');
    expect(inactivePanel).toHaveClass("workspace-tab-panel--inactive");
    expect(inactivePanel).not.toHaveAttribute("hidden");
  });

  it("aria-selected 与 activeKey 一致", () => {
    renderTabs();

    expect(screen.getByRole("tab", { name: /内容配置/ })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    expect(screen.getByRole("tab", { name: /人物设定/ })).toHaveAttribute(
      "aria-selected",
      "false",
    );
  });

  it("点击标签上抛 onTabChange", () => {
    const { callbacks } = renderTabs();

    fireEvent.click(screen.getByRole("tab", { name: /人物设定/ }));
    expect(callbacks.onTabChange).toHaveBeenCalledWith("people");

    fireEvent.click(screen.getByRole("tab", { name: /生成设置/ }));
    expect(callbacks.onTabChange).toHaveBeenCalledWith("launch");
  });

  it("切换标签不卸载面板内容（子组件状态保留）", () => {
    renderTabs({ activeKey: "people" });

    fireEvent.click(screen.getByText("人物页 点击次数 0"));
    fireEvent.click(screen.getByText("人物页 点击次数 1"));

    expect(screen.getByText("人物页 点击次数 2")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("tab", { name: /内容配置/ }));
    expect(screen.getByText("内容配置区块")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("tab", { name: /人物设定/ }));
    expect(screen.getByText("人物页 点击次数 2")).toBeInTheDocument();
  });

  it("标签徽章显示就绪与缺失数", () => {
    renderTabs();

    const contentTab = screen.getByRole("tab", { name: /内容配置/ });
    expect(within(contentTab).getByText("✓")).toBeInTheDocument();

    const peopleTab = screen.getByRole("tab", { name: /人物设定/ });
    expect(within(peopleTab).getByText("缺失 4 项")).toBeInTheDocument();

    const launchTab = screen.getByRole("tab", { name: /生成设置/ });
    expect(within(launchTab).getByText("缺失 1 项")).toBeInTheDocument();
  });

  it("readOnly 下标签按钮禁用", () => {
    renderTabs({ readOnly: true });

    expect(screen.getByRole("tab", { name: /内容配置/ })).toBeDisabled();
    expect(screen.getByRole("tab", { name: /人物设定/ })).toBeDisabled();
    expect(screen.getByRole("tab", { name: /生成设置/ })).toBeDisabled();
  });

  it("busy 下标签按钮禁用", () => {
    renderTabs({ busy: true });

    expect(screen.getByRole("tab", { name: /内容配置/ })).toBeDisabled();
  });

  it("缺失数为 0 的 missing 徽章显示就绪", () => {
    renderTabs({
      tabs: [
        {
          badge: { kind: "missing", count: 0 },
          content: <p>测试面板</p>,
          key: "content" as const,
          label: "内容配置",
        },
      ],
    });

    expect(
      within(screen.getByRole("tab", { name: /内容配置/ })).getByText("✓"),
    ).toBeInTheDocument();
  });
});
