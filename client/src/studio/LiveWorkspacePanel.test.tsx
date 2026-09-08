import { fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import type { GenerationBatch } from "../api";
import * as api from "../api";
import type { CustomerCredentialStore } from "../customer/useCustomerSession";
import { reviewUser } from "./fixtures";

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

vi.mock("../CharacterLibrary", () => ({
  CharacterLibrary: ({
    initialIdentityId,
    initialTab,
  }: {
    initialIdentityId?: string;
    initialTab?: string;
  }) => <p>{`${initialIdentityId ?? "none"}:${initialTab ?? "base"}`}</p>,
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

it("人物面板会带入 Studio 已选人物和场景造型页签", () => {
  render(
    <LiveWorkspacePanel
      characterIdentityId="identity-1"
      characterInitialTab="scenes"
      currentUser={reviewUser}
      onBatchCreated={vi.fn()}
      onBusyChange={vi.fn()}
      onClose={vi.fn()}
      onHandoffConsumed={vi.fn()}
      onProjectSelected={vi.fn()}
      onRefresh={vi.fn()}
      panel="characters"
    />,
  );

  expect(screen.getByText("identity-1:scenes")).toBeInTheDocument();
});
