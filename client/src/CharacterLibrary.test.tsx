import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import * as api from "./api";
import { CharacterLibrary } from "./CharacterLibrary";

vi.mock("./api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./api")>();
  return {
    ...actual,
    listPersonIdentities: vi.fn(),
    renamePersonIdentity: vi.fn(),
    uploadSimpleCharacter: vi.fn(),
  };
});

const identity: api.PersonIdentity = {
  id: "identity-1",
  owner_user_id: "employee_1",
  display_name: "林夏",
  authorization_status: "AUTHORIZED" as const,
  authorization_asset_id: "authorization-1",
  authorization_scope: ["内部短视频"],
  authorization_expires_at: "2035-01-01T00:00:00Z",
  source_asset_id: "source-1",
  source_quality_status: "PASSED" as const,
  status: "ACTIVE" as const,
  created_by: "employee_1",
  created_at: "2026-08-16T00:00:00Z",
  updated_at: "2026-08-16T00:00:00Z",
};

const foreignIdentity: api.PersonIdentity = {
  ...identity,
  id: "identity-2",
  owner_user_id: "employee_2",
  display_name: "荣哥",
};

describe("CharacterLibrary", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("renders identities with the inline one-click upload form", async () => {
    vi.mocked(api.listPersonIdentities).mockResolvedValue([
      identity,
      foreignIdentity,
    ]);

    render(<CharacterLibrary userRole="employee" userId="employee_1" />);

    expect(await screen.findByText("林夏")).toBeInTheDocument();
    expect(screen.getByText("荣哥")).toBeInTheDocument();
    expect(screen.getByLabelText("一键上传人物")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "一键生成人物" }),
    ).toBeInTheDocument();
  });

  it("creates a character via the global endpoint and refreshes the list", async () => {
    vi.mocked(api.listPersonIdentities)
      .mockResolvedValueOnce([])
      .mockResolvedValueOnce([identity]);
    vi.mocked(api.uploadSimpleCharacter).mockResolvedValue({
      identity_id: identity.id,
      persona_id: "persona-1",
      character_version_id: "version-1",
      publication_hash: "publication-sha",
    });

    render(<CharacterLibrary userRole="employee" userId="employee_1" />);

    fireEvent.change(screen.getByLabelText("人物名称"), {
      target: { value: "林夏" },
    });
    fireEvent.change(screen.getByLabelText("授权图片"), {
      target: {
        files: [new File(["png"], "source.png", { type: "image/png" })],
      },
    });
    fireEvent.click(screen.getByRole("button", { name: "一键生成人物" }));

    await waitFor(() =>
      expect(api.uploadSimpleCharacter).toHaveBeenCalledWith(
        null,
        expect.any(File),
        "林夏",
      ),
    );
    expect(await screen.findByText("林夏")).toBeInTheDocument();
    expect(screen.getByText(/七视角已生成并发布/)).toBeInTheDocument();
  });

  it("lets the owner rename an identity inline", async () => {
    vi.mocked(api.listPersonIdentities).mockResolvedValue([identity]);
    vi.mocked(api.renamePersonIdentity).mockResolvedValue({
      ...identity,
      display_name: "林小夏",
    });

    render(<CharacterLibrary userRole="employee" userId="employee_1" />);

    fireEvent.click(await screen.findByRole("button", { name: "改名" }));
    fireEvent.change(screen.getByLabelText("修改人物名称"), {
      target: { value: "林小夏" },
    });
    fireEvent.click(screen.getByRole("button", { name: "保存名称" }));

    await waitFor(() =>
      expect(api.renamePersonIdentity).toHaveBeenCalledWith(
        "identity-1",
        "林小夏",
      ),
    );
    expect(await screen.findByText("林小夏")).toBeInTheDocument();
    expect(screen.getByText(/人物名称已更新/)).toBeInTheDocument();
  });

  it("lets an admin rename identities created by others", async () => {
    vi.mocked(api.listPersonIdentities).mockResolvedValue([foreignIdentity]);
    vi.mocked(api.renamePersonIdentity).mockResolvedValue({
      ...foreignIdentity,
      display_name: "荣哥二号",
    });

    render(<CharacterLibrary userRole="admin" userId="admin_1" />);

    fireEvent.click(await screen.findByRole("button", { name: "改名" }));
    fireEvent.change(screen.getByLabelText("修改人物名称"), {
      target: { value: "荣哥二号" },
    });
    fireEvent.click(screen.getByRole("button", { name: "保存名称" }));

    await waitFor(() =>
      expect(api.renamePersonIdentity).toHaveBeenCalledWith(
        "identity-2",
        "荣哥二号",
      ),
    );
    expect(await screen.findByText("荣哥二号")).toBeInTheDocument();
  });

  it("hides rename controls from non-owner employees", async () => {
    vi.mocked(api.listPersonIdentities).mockResolvedValue([identity]);

    render(<CharacterLibrary userRole="employee" userId="employee_2" />);

    expect(await screen.findByText("林夏")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "改名" })).toBeNull();
  });

  it("keeps auditors read-only", async () => {
    vi.mocked(api.listPersonIdentities).mockResolvedValue([identity]);

    render(<CharacterLibrary userRole="auditor" userId="auditor_1" />);

    expect(await screen.findByText("林夏")).toBeInTheDocument();
    expect(screen.queryByLabelText("一键上传人物")).toBeNull();
    expect(screen.queryByRole("button", { name: "一键生成人物" })).toBeNull();
    expect(screen.queryByRole("button", { name: "改名" })).toBeNull();
    expect(screen.getByText(/审计身份只读/)).toBeInTheDocument();
  });

  it("shows a recoverable error and stays editable when renaming fails", async () => {
    vi.mocked(api.listPersonIdentities).mockResolvedValue([identity]);
    vi.mocked(api.renamePersonIdentity).mockRejectedValue(
      new Error("已归档人物身份不能修改。"),
    );

    render(<CharacterLibrary userRole="admin" userId="admin_1" />);

    fireEvent.click(await screen.findByRole("button", { name: "改名" }));
    fireEvent.change(screen.getByLabelText("修改人物名称"), {
      target: { value: "新名字" },
    });
    fireEvent.click(screen.getByRole("button", { name: "保存名称" }));

    expect(
      await screen.findByText("已归档人物身份不能修改。"),
    ).toBeInTheDocument();
    expect(screen.getByLabelText("修改人物名称")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "取消" })).toBeEnabled();
  });
});
