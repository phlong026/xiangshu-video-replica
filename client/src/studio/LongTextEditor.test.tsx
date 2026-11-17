import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { TextDocumentRef } from "../api";
import { LongTextEditor } from "./LongTextEditor";

const api = vi.hoisted(() => ({
  readTextDocumentBlocks: vi.fn(),
  forkTextDocument: vi.fn(),
  saveTextDocumentBlock: vi.fn(),
  exportTextDocument: vi.fn(),
}));
vi.mock("../api", () => api);
const document: TextDocumentRef = {
  id: "document",
  revision: 1,
  char_count: 20000,
  sha256: "abc",
  immutable: true,
};

beforeEach(() => {
  vi.clearAllMocks();
  api.exportTextDocument.mockResolvedValue(undefined);
  api.readTextDocumentBlocks.mockResolvedValue({
    document,
    blocks: [{ block_index: 0, text: "原段落" }],
    next_offset: 1,
  });
  api.forkTextDocument.mockResolvedValue({
    ...document,
    id: "draft",
    immutable: false,
  });
  api.saveTextDocumentBlock.mockResolvedValue({
    ...document,
    id: "draft",
    revision: 2,
    immutable: false,
  });
});

describe("长文案分块编辑", () => {
  it("只加载一段，未保存的修改阻止翻页", async () => {
    render(<LongTextEditor document={document} label="长文案" />);
    const input = await screen.findByRole("textbox", { name: "长文案" });
    fireEvent.change(input, { target: { value: "手动修改" } });
    expect(screen.getByRole("button", { name: "下一段" })).toBeDisabled();
    expect(api.readTextDocumentBlocks).toHaveBeenCalledTimes(1);
    expect(api.readTextDocumentBlocks).toHaveBeenCalledWith("document", 0, "");
  });

  it("先从不可变快照建立副本，再按读取版本保存", async () => {
    const onSaved = vi.fn();
    render(
      <LongTextEditor document={document} label="长文案" onSaved={onSaved} />,
    );
    fireEvent.change(await screen.findByRole("textbox", { name: "长文案" }), {
      target: { value: "修改内容" },
    });
    fireEvent.click(screen.getByRole("button", { name: "保存本段" }));
    await waitFor(() => expect(onSaved).toHaveBeenCalled());
    expect(api.forkTextDocument).toHaveBeenCalledWith(document);
    expect(api.saveTextDocumentBlock).toHaveBeenCalledWith(
      expect.objectContaining({ id: "draft", revision: 1 }),
      0,
      "修改内容",
    );
    expect(onSaved).toHaveBeenCalledWith(
      expect.objectContaining({ revision: 2 }),
      "修改内容",
    );
  });

  it("版本冲突保留输入并明确提示", async () => {
    api.saveTextDocumentBlock.mockRejectedValue(
      new Error("文案已更新，请刷新。"),
    );
    render(<LongTextEditor document={document} label="长文案" />);
    const input = await screen.findByRole("textbox", { name: "长文案" });
    fireEvent.change(input, { target: { value: "不能丢的手改" } });
    fireEvent.click(screen.getByRole("button", { name: "保存本段" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("文案已更新");
    expect(input).toHaveValue("不能丢的手改");
  });

  it("只读文案可翻页、导出，不能保存", async () => {
    render(<LongTextEditor document={document} label="长文案" readOnly />);
    expect(
      await screen.findByRole("textbox", { name: "长文案" }),
    ).toHaveAttribute("readonly");
    expect(
      screen.queryByRole("button", { name: "保存本段" }),
    ).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "导出全文 TXT" }));
    expect(api.exportTextDocument).toHaveBeenCalledWith("document");
  });
});
