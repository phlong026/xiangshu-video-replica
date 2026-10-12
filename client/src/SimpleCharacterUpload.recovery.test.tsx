import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { CharacterSheetTaskError, uploadSimpleCharacter } from "./api";
import { PendingCharacterCard } from "./CharacterLibrary";
import { SimpleCharacterUpload } from "./SimpleCharacterUpload";

vi.mock("./api", async (original) => ({
  ...(await original<typeof import("./api")>()),
  uploadSimpleCharacter: vi.fn(),
}));

describe("未知人物生成的恢复入口", () => {
  it("未知后保留原图片参数，只显示核对入口", async () => {
    vi.mocked(uploadSimpleCharacter).mockRejectedValue(
      new CharacterSheetTaskError(
        "结果待核对",
        "task-1",
        "SUBMISSION_UNCERTAIN",
      ),
    );
    render(<SimpleCharacterUpload onCreated={vi.fn()} />);
    fireEvent.change(screen.getByLabelText("人物名称"), {
      target: { value: "人物" },
    });
    const file = new File(["image"], "source.png", { type: "image/png" });
    fireEvent.change(screen.getByLabelText("授权图片"), {
      target: { files: [file] },
    });
    fireEvent.click(
      screen.getByRole("button", { name: "一键生成五视图拼合图" }),
    );
    fireEvent.click(screen.getByRole("checkbox"));
    fireEvent.click(screen.getByRole("button", { name: "确认授权并生成" }));
    await screen.findByText("结果待核对");
    expect(screen.getByLabelText("人物名称")).toBeDisabled();
    expect(screen.getByRole("button", { name: "选择图片" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "核对上次生成状态" }));
    await waitFor(() => expect(uploadSimpleCharacter).toHaveBeenCalledTimes(2));
    expect(vi.mocked(uploadSimpleCharacter).mock.calls[1]).toEqual(
      vi.mocked(uploadSimpleCharacter).mock.calls[0],
    );
  });

  it("待核对卡片不会声称失败或允许清除后重新生成", () => {
    const recover = vi.fn();
    render(
      <PendingCharacterCard
        character={{
          displayName: "人物",
          progress: 72,
          stage: "未知",
          status: "uncertain",
        }}
        onClear={vi.fn()}
        onRecover={recover}
      />,
    );
    expect(screen.getByText("待核对")).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "移除失败任务" }),
    ).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "核对人物任务状态" }));
    expect(recover).toHaveBeenCalledOnce();
  });
});
