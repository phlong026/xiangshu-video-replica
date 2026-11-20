import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import type { ReactNode } from "react";
import { beforeEach, expect, it, vi } from "vitest";
import { createReviewData, reviewUser } from "./fixtures";
import { createState } from "./state";
import { WorkspaceSearch } from "./WorkspaceSearch";

const mocks = vi.hoisted(() => ({
  useStudio: vi.fn(),
  searchWorkspace: vi.fn(),
  getStudioSavedScript: vi.fn(),
}));
vi.mock("./context", () => ({ useStudio: mocks.useStudio }));
vi.mock("../api", async (original) => ({
  ...(await original<object>()),
  ...mocks,
}));
vi.mock("./StudioWorkspace", () => ({
  StudioDialog: ({ children }: { children: ReactNode }) => (
    <div>{children}</div>
  ),
}));
const value = {
  user: reviewUser,
  review: false,
  state: createState(),
  data: createReviewData(),
  navigate: vi.fn(),
  patchState: vi.fn(),
  updateData: vi.fn(),
};
beforeEach(() => {
  vi.clearAllMocks();
  mocks.useStudio.mockReturnValue(value);
  mocks.searchWorkspace.mockResolvedValue({
    items: [],
    total: 0,
    page: 1,
    page_size: 12,
  });
});

it("切换关键词后丢弃迟到的旧搜索结果", async () => {
  let complete!: (result: unknown) => void;
  mocks.searchWorkspace.mockImplementationOnce(
    () =>
      new Promise((resolve) => {
        complete = resolve;
      }),
  );
  const view = render(<WorkspaceSearch query="旧词" onClose={() => {}} />);
  view.rerender(<WorkspaceSearch query="新词" onClose={() => {}} />);
  await screen.findByText("没有匹配结果");
  await act(async () =>
    complete({
      items: [{ id: "old", kind: "script", title: "旧数据" }],
      total: 1,
      page: 1,
      page_size: 12,
    }),
  );
  expect(screen.queryByText("旧数据")).toBeNull();
  expect(mocks.searchWorkspace).toHaveBeenLastCalledWith("新词", "script", 1);
});

it("搜索读取服务器分页，打开文案重置其他创作来源", async () => {
  mocks.searchWorkspace.mockResolvedValue({
    items: [{ id: "s1", kind: "script", title: "预算文案" }],
    total: 13,
    page: 1,
    page_size: 12,
  });
  mocks.getStudioSavedScript.mockResolvedValue({
    script_id: "s1",
    title: "预算文案",
    text: "正文",
    original: "",
    version: 1,
    ip_id: null,
    source_project_id: null,
    source_kind: null,
  });
  render(<WorkspaceSearch query="预算" onClose={() => {}} />);
  fireEvent.click(screen.getByRole("button", { name: "文案" }));
  fireEvent.click(await screen.findByRole("button", { name: "下一页" }));
  await waitFor(() =>
    expect(mocks.searchWorkspace).toHaveBeenLastCalledWith("预算", "script", 2),
  );
  fireEvent.click(await screen.findByRole("button", { name: "预算文案" }));
  await waitFor(() => expect(value.navigate).toHaveBeenCalledWith("copy"));
  expect(value.patchState).toHaveBeenCalledWith({
    draft: expect.objectContaining({
      referenceIds: [],
      frameConfirmed: false,
      script: expect.objectContaining({ text: "正文", confirmed: false }),
    }),
  });
});
