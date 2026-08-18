import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import * as api from "./api";
import { CharacterLibrary } from "./CharacterLibrary";

vi.mock("./api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./api")>();
  return {
    ...actual,
    listSimpleCharacterLibrary: vi.fn(),
    regenerateContactSheet: vi.fn(),
    renamePersonIdentity: vi.fn(),
    deleteSimpleCharacterIdentity: vi.fn(),
    uploadSimpleCharacter: vi.fn(),
    getAssetDownloadUrl: vi.fn(),
    downloadCharacterAsset: vi.fn(),
  };
});

const VIEW_TYPES: api.CharacterViewType[] = [
  "FRONT_FACE",
  "FRONT_HALF",
  "FRONT_FULL",
  "LEFT_45",
  "RIGHT_45",
  "LEFT_SIDE",
  "RIGHT_SIDE",
];

function viewsFor(prefix: string): api.SimpleCharacterView[] {
  return VIEW_TYPES.map((view_type, index) => ({
    view_type,
    asset_id: `${prefix}-${view_type}-${index}`,
  }));
}

const entry: api.SimpleLibraryEntry = {
  identity_id: "identity-1",
  display_name: "林夏",
  owner_user_id: "employee_1",
  status: "ACTIVE",
  contact_sheet_asset_id: null,
  views: viewsFor("asset"),
};

const foreignEntry: api.SimpleLibraryEntry = {
  ...entry,
  identity_id: "identity-2",
  display_name: "荣哥",
  owner_user_id: "employee_2",
  contact_sheet_asset_id: "sheet-foreign",
  views: viewsFor("foreign"),
};

describe("CharacterLibrary", () => {
  beforeEach(() => {
    // resetAllMocks (not clearAllMocks) also drops leftover mockResolvedValueOnce
    // queues from earlier tests, which would otherwise leak into this one.
    vi.resetAllMocks();
    vi.mocked(api.getAssetDownloadUrl).mockImplementation(async (assetId) => ({
      url: `http://127.0.0.1:8000/mock/${assetId}`,
    }));
    vi.mocked(api.downloadCharacterAsset).mockResolvedValue(undefined);
    vi.mocked(api.deleteSimpleCharacterIdentity).mockResolvedValue(undefined);
    vi.spyOn(window, "confirm").mockReturnValue(true);
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("renders characters with five-view contact sheets", async () => {
    vi.mocked(api.listSimpleCharacterLibrary).mockResolvedValue([
      entry,
      foreignEntry,
    ]);

    render(<CharacterLibrary userRole="employee" userId="employee_1" />);

    expect(await screen.findByText("林夏")).toBeInTheDocument();
    expect(screen.getByText("荣哥")).toBeInTheDocument();
    expect(await screen.findByAltText("林夏 正脸近景")).toBeInTheDocument();
    expect(screen.getByAltText("林夏 右侧面")).toBeInTheDocument();
    // Legacy entries without a contact sheet keep the seven-grid fallback.
    expect(screen.getAllByText("下载全部（7 张）")).toHaveLength(1);
    // New entries show the single five-view contact sheet instead.
    expect(await screen.findByAltText("荣哥 五视角拼合图")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "下载拼合图" }));
  });

  it("creates a character and shows its contact sheet immediately", async () => {
    vi.mocked(api.listSimpleCharacterLibrary)
      .mockResolvedValueOnce([])
      .mockResolvedValueOnce([
        { ...entry, contact_sheet_asset_id: "sheet-new" },
      ]);
    vi.mocked(api.uploadSimpleCharacter).mockResolvedValue({
      identity_id: entry.identity_id,
      persona_id: "persona-1",
      character_version_id: "version-1",
      publication_hash: "publication-sha",
      contact_sheet_asset_id: "sheet-new",
      views: entry.views,
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
    fireEvent.click(
      screen.getByRole("button", { name: "一键生成五视角拼合图" }),
    );

    await waitFor(() =>
      expect(api.uploadSimpleCharacter).toHaveBeenCalledWith(
        null,
        expect.any(File),
        "林夏",
      ),
    );
    expect(await screen.findByAltText("林夏 五视角拼合图")).toBeInTheDocument();
    expect(screen.getByText(/五视角拼合图已生成/)).toBeInTheDocument();
  });

  it("downloads the five-view contact sheet", async () => {
    vi.mocked(api.listSimpleCharacterLibrary).mockResolvedValue([foreignEntry]);

    render(<CharacterLibrary userRole="employee" userId="employee_2" />);

    fireEvent.click(await screen.findByRole("button", { name: "下载拼合图" }));

    await waitFor(() =>
      expect(api.downloadCharacterAsset).toHaveBeenCalledWith(
        "sheet-foreign",
        "荣哥-五视角拼合图.png",
      ),
    );
  });

  it("downloads a single view and the whole set", async () => {
    vi.mocked(api.listSimpleCharacterLibrary).mockResolvedValue([entry]);

    render(<CharacterLibrary userRole="employee" userId="employee_1" />);

    const downloadButtons = await screen.findAllByRole("button", {
      name: "下载",
    });
    fireEvent.click(downloadButtons[0]);

    await waitFor(() =>
      expect(api.downloadCharacterAsset).toHaveBeenCalledWith(
        entry.views[0].asset_id,
        "林夏-正脸近景.png",
      ),
    );

    fireEvent.click(screen.getByRole("button", { name: "下载全部（7 张）" }));

    await waitFor(() =>
      expect(api.downloadCharacterAsset).toHaveBeenCalledTimes(8),
    );
  });

  it("lets the owner rename an identity inline", async () => {
    vi.mocked(api.listSimpleCharacterLibrary).mockResolvedValue([entry]);
    vi.mocked(api.renamePersonIdentity).mockResolvedValue({
      id: "identity-1",
      display_name: "林小夏",
    } as unknown as api.PersonIdentity);

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
    vi.mocked(api.listSimpleCharacterLibrary).mockResolvedValue([foreignEntry]);
    vi.mocked(api.renamePersonIdentity).mockResolvedValue({
      id: "identity-2",
      display_name: "荣哥二号",
    } as unknown as api.PersonIdentity);

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
    vi.mocked(api.listSimpleCharacterLibrary).mockResolvedValue([entry]);

    render(<CharacterLibrary userRole="employee" userId="employee_2" />);

    expect(await screen.findByText("林夏")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "改名" })).toBeNull();
  });

  it("deletes a character after confirmation", async () => {
    vi.mocked(api.listSimpleCharacterLibrary).mockResolvedValue([entry]);

    render(<CharacterLibrary userRole="employee" userId="employee_1" />);

    fireEvent.click(
      await screen.findByRole("button", { name: "删除人物 林夏" }),
    );

    expect(window.confirm).toHaveBeenCalledWith(
      expect.stringContaining("删除“林夏”？"),
    );
    await waitFor(() =>
      expect(api.deleteSimpleCharacterIdentity).toHaveBeenCalledWith(
        "identity-1",
      ),
    );
    expect(await screen.findByText(/人物“林夏”已删除。/)).toBeInTheDocument();
    expect(screen.queryByText("林夏")).toBeNull();
  });

  it("keeps the character when the delete confirmation is dismissed", async () => {
    vi.mocked(api.listSimpleCharacterLibrary).mockResolvedValue([entry]);
    vi.spyOn(window, "confirm").mockReturnValue(false);

    render(<CharacterLibrary userRole="employee" userId="employee_1" />);

    fireEvent.click(
      await screen.findByRole("button", { name: "删除人物 林夏" }),
    );

    expect(api.deleteSimpleCharacterIdentity).not.toHaveBeenCalled();
    expect(screen.getByText("林夏")).toBeInTheDocument();
  });

  it("hides the delete button from non-owners", async () => {
    vi.mocked(api.listSimpleCharacterLibrary).mockResolvedValue([foreignEntry]);

    render(<CharacterLibrary userRole="employee" userId="employee_1" />);

    expect(await screen.findByText("荣哥")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "删除人物 荣哥" })).toBeNull();
  });

  it("keeps auditors read-only without download controls", async () => {
    vi.mocked(api.listSimpleCharacterLibrary).mockResolvedValue([entry]);

    render(<CharacterLibrary userRole="auditor" userId="auditor_1" />);

    expect(await screen.findByText("林夏")).toBeInTheDocument();
    expect(screen.queryByLabelText("一键上传人物")).toBeNull();
    expect(
      screen.queryByRole("button", { name: "一键生成五视角拼合图" }),
    ).toBeNull();
    expect(screen.queryByRole("button", { name: "改名" })).toBeNull();
    expect(screen.queryByRole("button", { name: "删除人物 林夏" })).toBeNull();
    expect(
      screen.queryByRole("button", { name: "下载全部（7 张）" }),
    ).toBeNull();
    expect(screen.queryByRole("button", { name: "下载拼合图" })).toBeNull();
    expect(screen.queryAllByRole("button", { name: "下载" })).toHaveLength(0);
    expect(screen.getByText(/审计身份只读/)).toBeInTheDocument();
  });

  it("shows a recoverable error and stays editable when renaming fails", async () => {
    vi.mocked(api.listSimpleCharacterLibrary).mockResolvedValue([entry]);
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

  it("regenerates the contact sheet as a new version and refreshes the library", async () => {
    vi.mocked(api.listSimpleCharacterLibrary)
      .mockResolvedValueOnce([foreignEntry])
      .mockResolvedValueOnce([
        { ...foreignEntry, contact_sheet_asset_id: "sheet-new" },
      ]);
    vi.mocked(api.regenerateContactSheet).mockResolvedValue({
      identity_id: "identity-2",
      persona_id: "persona-2",
      character_version_id: "cv-2",
      previous_version_id: "cv-1",
      version_number: 2,
      publication_hash: "publication-sha-2",
      contact_sheet_asset_id: "sheet-new",
      views: viewsFor("new"),
    });

    render(<CharacterLibrary userRole="employee" userId="employee_2" />);

    fireEvent.click(
      await screen.findByRole("button", {
        name: "重新生成人物 荣哥 的多视图",
      }),
    );

    await waitFor(() =>
      expect(api.regenerateContactSheet).toHaveBeenCalledWith("identity-2"),
    );
    expect(
      await screen.findByText(/多视图已重新生成（V2）/),
    ).toBeInTheDocument();
    expect(api.listSimpleCharacterLibrary).toHaveBeenCalledTimes(2);
  });

  it("shows a recoverable error when regeneration fails", async () => {
    vi.mocked(api.listSimpleCharacterLibrary).mockResolvedValue([foreignEntry]);
    vi.mocked(api.regenerateContactSheet).mockRejectedValue(
      new Error("生成服务暂不可用。"),
    );

    render(<CharacterLibrary userRole="employee" userId="employee_2" />);

    fireEvent.click(
      await screen.findByRole("button", {
        name: "重新生成人物 荣哥 的多视图",
      }),
    );

    expect(await screen.findByText("生成服务暂不可用。")).toBeInTheDocument();
    // 失败后按钮恢复可用，人物卡片仍在列表中。
    expect(
      screen.getByRole("button", { name: "重新生成人物 荣哥 的多视图" }),
    ).toBeEnabled();
  });

  it("hides the regenerate button from non-owners and auditors", async () => {
    vi.mocked(api.listSimpleCharacterLibrary).mockResolvedValue([foreignEntry]);

    render(<CharacterLibrary userRole="employee" userId="employee_1" />);

    expect(await screen.findByText("荣哥")).toBeInTheDocument();
    expect(
      screen.queryByRole("button", {
        name: "重新生成人物 荣哥 的多视图",
      }),
    ).toBeNull();
  });
});
