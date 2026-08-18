import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { useState } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import * as api from "./api";
import { CharacterSelection } from "./CharacterSelection";

vi.mock("./api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./api")>();
  return {
    ...actual,
    chooseProjectMainCharacterVersion: vi.fn(),
    getProjectMainCharacter: vi.fn(),
    listProjectCharacterVersions: vi.fn(),
  };
});

const viewTypes = [
  "FRONT_FACE",
  "FRONT_HALF",
  "FRONT_FULL",
  "LEFT_45",
  "RIGHT_45",
  "LEFT_SIDE",
  "RIGHT_SIDE",
] as const;

const option: api.ProjectCharacterVersionOption = {
  character_version_id: "character-version-3",
  version_number: 3,
  identity_id: "identity-1",
  identity_name: "林夏",
  authorization_expires_at: "2035-01-01T00:00:00Z",
  persona_id: "persona-1",
  persona_snapshot_json: {
    name: "乡墅项目管理专家",
    occupation: "项目管理",
    costume_description: "工程马甲",
  },
  provider: "fake_character",
  model: "fake-character-v1",
  template_version: "character-prompt-v1",
  template_hash: "template-hash",
  published_at: "2030-01-01T00:00:00Z",
  publication_hash: "publication-hash",
  assets: viewTypes.map((viewType) => ({
    character_asset_id: `character-asset-${viewType}`,
    asset_id: `asset-${viewType}`,
    view_type: viewType,
  })),
};

const selected: api.ProjectMainCharacter = {
  project_id: "project-1",
  character_id: null,
  character_version_id: option.character_version_id,
  version_id: "project-character-selection-1",
  version_number: 1,
  character_snapshot: {
    schema_version: "project-character-selection.v1",
    character_version_id: option.character_version_id,
    character_version_number: option.version_number,
    identity: {
      id: option.identity_id,
      display_name: option.identity_name,
      authorization_expires_at: option.authorization_expires_at,
    },
    persona_snapshot_json: option.persona_snapshot_json,
    provider: option.provider,
    model: option.model,
    published_assets: option.assets,
  },
};

describe("CharacterSelection", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.getProjectMainCharacter).mockResolvedValue(null);
    vi.mocked(api.listProjectCharacterVersions).mockResolvedValue([option]);
    vi.mocked(api.chooseProjectMainCharacterVersion).mockResolvedValue(
      selected,
    );
  });

  it("restores the frozen role version as soon as the project opens", async () => {
    vi.mocked(api.getProjectMainCharacter).mockResolvedValue(selected);
    const onSelectionChange = vi.fn();
    const onVersionChange = vi.fn();

    render(
      <CharacterSelection
        onSelectionChange={onSelectionChange}
        onVersionChange={onVersionChange}
        projectId="project-1"
      />,
    );

    expect(await screen.findByText("当前角色：林夏")).toBeInTheDocument();
    expect(screen.getByText("乡墅项目管理专家 · V3")).toBeInTheDocument();
    expect(screen.getByText("授权有效至 2035-01-01")).toBeInTheDocument();
    expect(api.getProjectMainCharacter).toHaveBeenCalledWith("project-1");
    expect(onSelectionChange).toHaveBeenLastCalledWith(true);
    expect(onVersionChange).toHaveBeenLastCalledWith(selected);
  });

  it("shows only server-approved options with all seven published assets", async () => {
    render(<CharacterSelection projectId="project-1" />);

    fireEvent.click(
      await screen.findByRole("button", { name: "选择角色版本" }),
    );

    const optionLabel =
      await screen.findByLabelText(/林夏.*乡墅项目管理专家.*V3/);
    const optionCard = optionLabel.closest("label");
    expect(optionCard).not.toBeNull();
    expect(
      within(optionCard as HTMLElement).getByText("项目管理"),
    ).toBeInTheDocument();
    expect(
      within(optionCard as HTMLElement).getByText("授权有效至 2035-01-01"),
    ).toBeInTheDocument();

    fireEvent.click(optionLabel);
    const assets = await screen.findByRole("list", { name: "七类已发布资产" });
    expect(within(assets).getAllByRole("listitem")).toHaveLength(7);
    expect(within(assets).getByText("正脸近景")).toBeInTheDocument();
    expect(within(assets).getByText("右侧面")).toBeInTheDocument();
  });

  it("saves the immutable version id and advances the project workflow", async () => {
    const onSelectionChange = vi.fn();
    render(
      <CharacterSelection
        onSelectionChange={onSelectionChange}
        projectId="project-1"
      />,
    );
    fireEvent.click(
      await screen.findByRole("button", { name: "选择角色版本" }),
    );
    fireEvent.click(await screen.findByLabelText(/林夏.*乡墅项目管理专家.*V3/));
    fireEvent.click(screen.getByRole("button", { name: "确认角色版本" }));

    await waitFor(() =>
      expect(api.chooseProjectMainCharacterVersion).toHaveBeenCalledWith(
        "project-1",
        option.character_version_id,
      ),
    );
    expect(
      await screen.findByText("已选择角色“林夏 · 乡墅项目管理专家 V3”。"),
    ).toBeInTheDocument();
    expect(onSelectionChange).toHaveBeenLastCalledWith(true);
  });

  it("reports selection writes as busy until the server mutation settles", async () => {
    let resolveSelection:
      | ((value: api.ProjectMainCharacter) => void)
      | undefined;
    vi.mocked(api.chooseProjectMainCharacterVersion).mockReturnValue(
      new Promise((resolve) => {
        resolveSelection = resolve;
      }),
    );
    const onBusyChange = vi.fn();
    render(
      <CharacterSelection onBusyChange={onBusyChange} projectId="project-1" />,
    );
    fireEvent.click(
      await screen.findByRole("button", { name: "选择角色版本" }),
    );
    fireEvent.click(await screen.findByLabelText(/林夏.*乡墅项目管理专家.*V3/));
    fireEvent.click(screen.getByRole("button", { name: "确认角色版本" }));

    await waitFor(() => expect(onBusyChange).toHaveBeenLastCalledWith(true));
    resolveSelection?.(selected);
    await waitFor(() => expect(onBusyChange).toHaveBeenLastCalledWith(false));
  });

  it("lets auditors inspect options without rendering a write action", async () => {
    render(<CharacterSelection projectId="project-1" readOnly />);

    fireEvent.click(
      await screen.findByRole("button", { name: "查看角色版本" }),
    );
    const radio = await screen.findByLabelText(/林夏.*乡墅项目管理专家.*V3/);
    expect(radio).toBeDisabled();
    expect(screen.queryByRole("button", { name: "确认角色版本" })).toBeNull();
    expect(
      screen.getByText("只读身份不能更改项目角色版本。"),
    ).toBeInTheDocument();
  });

  // P0-03-01：角色版本自动预选（红灯先行）。

  it("auto-selects the latest published version and persists it when no selection exists", async () => {
    const olderOption: api.ProjectCharacterVersionOption = {
      ...option,
      character_version_id: "character-version-2",
      version_number: 2,
      published_at: "2029-01-01T00:00:00Z",
    };
    // 乱序返回，验证按 published_at 取最新而非列表首位。
    vi.mocked(api.listProjectCharacterVersions).mockResolvedValue([
      olderOption,
      option,
    ]);
    const onSelectionChange = vi.fn();
    const onVersionChange = vi.fn();

    render(
      <CharacterSelection
        onSelectionChange={onSelectionChange}
        onVersionChange={onVersionChange}
        projectId="project-1"
      />,
    );

    // 自动预选落库：最近发布版本，重复选择服务端原子复用快照（任务 11 语义）。
    await waitFor(() =>
      expect(api.chooseProjectMainCharacterVersion).toHaveBeenCalledWith(
        "project-1",
        option.character_version_id,
      ),
    );
    expect(api.chooseProjectMainCharacterVersion).toHaveBeenCalledTimes(1);
    expect(await screen.findByText("当前角色：林夏")).toBeInTheDocument();
    expect(onSelectionChange).toHaveBeenLastCalledWith(true);
    expect(onVersionChange).toHaveBeenLastCalledWith(selected);

    // 可撤回提示条：点击「更换」进入角色卡片改选。
    expect(
      await screen.findByText("已自动选择角色版本 林夏 · V3"),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "更换" }));
    expect(
      await screen.findByText("选择一个不可变角色版本"),
    ).toBeInTheDocument();
  });

  it("does not auto-select when a selection already exists", async () => {
    vi.mocked(api.getProjectMainCharacter).mockResolvedValue(selected);

    render(<CharacterSelection projectId="project-1" />);

    expect(await screen.findByText("当前角色：林夏")).toBeInTheDocument();
    expect(api.listProjectCharacterVersions).not.toHaveBeenCalled();
    expect(api.chooseProjectMainCharacterVersion).not.toHaveBeenCalled();
    expect(screen.queryByText(/已自动选择角色版本/)).toBeNull();
  });

  it("shows guidance instead of an error when no versions are available", async () => {
    vi.mocked(api.listProjectCharacterVersions).mockResolvedValue([]);

    render(<CharacterSelection projectId="project-1" />);

    expect(
      await screen.findByText(/暂无可选角色版本，请先在人物库发布角色/),
    ).toBeInTheDocument();
    expect(api.chooseProjectMainCharacterVersion).not.toHaveBeenCalled();
    expect(screen.queryByRole("button", { name: "更换" })).toBeNull();
    expect(screen.queryByText(/失败。/)).toBeNull();
  });

  // P0-03-01 评审 Critical 1：生产接线中 onBusyChange 是内联回调，
  // busy 上报会触发父级重渲染并产生新引用；自动选择不得因此中止。
  // 用 pending list 复现真实网络时序：重渲染必须插进 list 在途窗口。
  it("completes auto-selection even when the busy callback identity changes mid-flight", async () => {
    let resolveList:
      | ((value: api.ProjectCharacterVersionOption[]) => void)
      | undefined;
    vi.mocked(api.listProjectCharacterVersions).mockReturnValue(
      new Promise((resolve) => {
        resolveList = resolve;
      }),
    );
    function Host() {
      const [, setTick] = useState(0);
      // 内联回调：Host 每次渲染都是新引用，模拟 App 接线的重渲染反馈。
      const handleBusyChange = () => setTick((tick) => tick + 1);
      return (
        <CharacterSelection
          onBusyChange={handleBusyChange}
          projectId="project-1"
        />
      );
    }

    render(<Host />);

    // 自动选择开始：busy 上报触发 Host 重渲染（新回调引用），list 仍在途。
    await waitFor(() =>
      expect(api.listProjectCharacterVersions).toHaveBeenCalledTimes(1),
    );
    resolveList?.([option]);

    await waitFor(() =>
      expect(api.chooseProjectMainCharacterVersion).toHaveBeenCalledWith(
        "project-1",
        option.character_version_id,
      ),
    );
    expect(
      await screen.findByText("已自动选择角色版本 林夏 · V3"),
    ).toBeInTheDocument();
  });

  // P0-03-01 评审 Major 2：restore 失败时快照状态未知，
  // 不得自动改绑，交回用户手动选择。
  it("does not auto-select when restoring the selection fails", async () => {
    vi.mocked(api.getProjectMainCharacter).mockRejectedValue(
      new Error("网络不可用"),
    );

    render(<CharacterSelection projectId="project-1" />);

    // restore 结束但无选择（引导文案出现）：快照状态未知，不得自动改绑。
    expect(
      await screen.findByText(
        "选择一个已发布且授权有效的角色版本，用于后续人物参考匹配。",
      ),
    ).toBeInTheDocument();
    expect(api.listProjectCharacterVersions).not.toHaveBeenCalled();
    expect(api.chooseProjectMainCharacterVersion).not.toHaveBeenCalled();
    expect(screen.queryByText(/已自动选择角色版本/)).toBeNull();
  });

  it("skips auto-selection for read-only visitors", async () => {
    render(<CharacterSelection projectId="project-1" readOnly />);

    expect(
      await screen.findByRole("button", { name: "查看角色版本" }),
    ).toBeInTheDocument();
    expect(api.listProjectCharacterVersions).not.toHaveBeenCalled();
    expect(api.chooseProjectMainCharacterVersion).not.toHaveBeenCalled();
    expect(screen.queryByText(/已自动选择角色版本/)).toBeNull();
  });

  it("falls back to manual selection guidance when the auto-selection write fails", async () => {
    vi.mocked(api.chooseProjectMainCharacterVersion).mockRejectedValue(
      new Error("写入失败"),
    );

    render(<CharacterSelection projectId="project-1" />);

    expect(
      await screen.findByText("未自动选择角色版本，请手动选择角色版本。"),
    ).toBeInTheDocument();
    expect(screen.queryByText(/已自动选择角色版本/)).toBeNull();
  });
});
