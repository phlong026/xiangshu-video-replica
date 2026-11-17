import { webcrypto } from "node:crypto";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  CharacterSheetTaskError,
  setInternalAccessToken,
  uploadSimpleCharacter,
} from "./api";
import { CharacterLibrary, PendingCharacterCard } from "./CharacterLibrary";
import { SimpleCharacterUpload } from "./SimpleCharacterUpload";

vi.mock("./api", async (original) => ({
  ...(await original<typeof import("./api")>()),
  uploadSimpleCharacter: vi.fn(),
}));

beforeEach(() => {
  vi.resetAllMocks();
  localStorage.clear();
  setInternalAccessToken("test-credential");
  vi.stubGlobal("crypto", webcrypto);
});

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

  it("先从待核对卡读到成功，再从上传表单核对，仍只读取原任务且不二次受理", async () => {
    const actual = await vi.importActual<typeof import("./api")>("./api");
    vi.mocked(uploadSimpleCharacter).mockImplementation(
      actual.uploadSimpleCharacter,
    );
    const result = {
      identity_id: "recovered-person",
      persona_id: "recovered-persona",
      character_version_id: "recovered-version",
      publication_hash: "publication-hash",
      contact_sheet_asset_id: "recovered-sheet",
      generation_source: "image_provider",
      views: [],
    };
    let accepted = false;
    let succeeded = false;
    const postKeys: string[] = [];
    const readTask = () => ({
      id: "original-upload-task",
      operation: "CREATE",
      status: succeeded ? "SUCCEEDED" : "RUNNING",
      display_name: "人物",
      result: succeeded ? result : null,
    });
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string, init?: RequestInit) => {
        const path = new URL(String(url)).pathname;
        if (path === "/api/auth/me")
          return Response.json({
            id: "owner",
            username: "owner",
            display_name: "用户",
            role: "employee",
          });
        if (path === "/api/simple-characters/library")
          return Response.json({ items: [], total: 0, next_cursor: null });
        if (path === "/api/simple-characters/tasks/active-or-latest")
          return Response.json(accepted ? readTask() : null);
        if (
          path === "/api/simple-characters/tasks/generate" &&
          init?.method === "POST"
        ) {
          postKeys.push(String((init.body as FormData).get("idempotency_key")));
          accepted = true;
          return Response.json(readTask());
        }
        if (
          path === "/api/simple-characters/task-status/original-upload-task"
        ) {
          if (!succeeded) throw new TypeError("读取任务时连接中断");
          return Response.json(readTask());
        }
        if (path.endsWith("/cached-url"))
          return Response.json({
            url: "/preview.png",
            expires_at: "2099-01-01",
          });
        throw new Error(`未预期的测试请求：${path}`);
      }),
    );
    const changed = vi.fn();
    render(
      <CharacterLibrary
        userRole="employee"
        userId="owner"
        onChanged={changed}
      />,
    );
    await screen.findByText("还没有人物，上传一张图片开始创建。");
    fireEvent.change(screen.getByLabelText("人物名称"), {
      target: { value: "人物" },
    });
    fireEvent.change(screen.getByLabelText("授权图片"), {
      target: {
        files: [new File(["image"], "source.png", { type: "image/png" })],
      },
    });
    fireEvent.click(
      screen.getByRole("button", { name: "一键生成五视图拼合图" }),
    );
    fireEvent.click(
      screen.getByRole("checkbox", { name: "我已阅读并确认以上图像授权声明" }),
    );
    fireEvent.click(screen.getByRole("button", { name: "确认授权并生成" }));
    await screen.findByRole("button", { name: "核对上次生成状态" });
    expect(postKeys).toHaveLength(1);
    succeeded = true;
    fireEvent.click(screen.getByRole("button", { name: "核对人物任务状态" }));
    await waitFor(() =>
      expect(
        screen.queryByRole("button", { name: "核对人物任务状态" }),
      ).not.toBeInTheDocument(),
    );
    fireEvent.click(screen.getByRole("button", { name: "核对上次生成状态" }));
    await waitFor(() => expect(changed).toHaveBeenCalledOnce());
    expect(
      screen.queryByRole("button", { name: "核对上次生成状态" }),
    ).not.toBeInTheDocument();
    expect(postKeys).toHaveLength(1);
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
