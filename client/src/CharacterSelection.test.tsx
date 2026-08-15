import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
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

    render(
      <CharacterSelection
        onSelectionChange={onSelectionChange}
        projectId="project-1"
      />,
    );

    expect(await screen.findByText("当前角色：林夏")).toBeInTheDocument();
    expect(screen.getByText("乡墅项目管理专家 · V3")).toBeInTheDocument();
    expect(screen.getByText("授权有效至 2035-01-01")).toBeInTheDocument();
    expect(api.getProjectMainCharacter).toHaveBeenCalledWith("project-1");
    expect(onSelectionChange).toHaveBeenLastCalledWith(true);
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
});
