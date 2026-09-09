import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import type { GenerationBatch } from "../api";
import { reviewUser } from "./fixtures";

const analysis = vi.hoisted(() => ({ props: vi.fn() }));

vi.mock("../AnalysisWorkspace", () => ({
  AnalysisWorkspace: (props: { identityId?: string }) => {
    analysis.props(props);
    return <p>analysis-workspace</p>;
  },
}));

vi.mock("../TaskRecordsPanel", () => ({
  TaskRecordsPanel: ({
    onHandoffConsumed,
  }: {
    onHandoffConsumed: () => void;
  }) => (
    <button type="button" onClick={onHandoffConsumed}>
      消费交接
    </button>
  ),
}));

import { LiveWorkspacePanel } from "./LiveWorkspacePanel";

afterEach(() => vi.restoreAllMocks());

it("任务面板消费交接后回传给工作区控制器清除暂存批次", () => {
  const onHandoffConsumed = vi.fn();
  render(
    <LiveWorkspacePanel
      currentUser={reviewUser}
      handoffBatch={{ id: "batch-1" } as GenerationBatch}
      onBatchCreated={vi.fn()}
      onBusyChange={vi.fn()}
      onClose={vi.fn()}
      onHandoffConsumed={onHandoffConsumed}
      onProjectSelected={vi.fn()}
      onRefresh={vi.fn()}
      panel="tasks"
    />,
  );

  fireEvent.click(screen.getByRole("button", { name: "消费交接" }));
  expect(onHandoffConsumed).toHaveBeenCalledTimes(1);
});

it("分析面板会把 Studio 已选 IP 传给真实分析工作区", () => {
  render(
    <LiveWorkspacePanel
      characterIdentityId="identity-1"
      currentUser={reviewUser}
      onBatchCreated={vi.fn()}
      onBusyChange={vi.fn()}
      onClose={vi.fn()}
      onHandoffConsumed={vi.fn()}
      onProjectSelected={vi.fn()}
      onRefresh={vi.fn()}
      panel="analysis"
      project={{ id: "project-1" } as never}
    />,
  );

  expect(screen.getByText("analysis-workspace")).toBeInTheDocument();
  expect(analysis.props).toHaveBeenCalledWith(
    expect.objectContaining({ identityId: "identity-1" }),
  );
});
