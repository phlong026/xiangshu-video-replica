import { useEffect, useState } from "react";

import {
  chooseProjectMainCharacterVersion,
  getProjectMainCharacter,
  listProjectCharacterVersions,
  type ProjectCharacterAssetOption,
  type ProjectCharacterVersionOption,
  type ProjectMainCharacter,
} from "./api";

const VIEW_LABELS: Record<ProjectCharacterAssetOption["view_type"], string> = {
  FRONT_FACE: "正脸近景",
  FRONT_HALF: "正面半身",
  FRONT_FULL: "正面全身",
  LEFT_45: "左 45°",
  RIGHT_45: "右 45°",
  LEFT_SIDE: "左侧面",
  RIGHT_SIDE: "右侧面",
};

export function CharacterSelection({
  onSelectionChange,
  onVersionChange,
  projectId,
  readOnly = false,
}: {
  onSelectionChange?: (hasSelection: boolean) => void;
  onVersionChange?: (selection: ProjectMainCharacter | null) => void;
  projectId: string;
  readOnly?: boolean;
}) {
  const [versions, setVersions] = useState<ProjectCharacterVersionOption[]>([]);
  const [currentSelection, setCurrentSelection] =
    useState<ProjectMainCharacter | null>(null);
  const [selectedVersionId, setSelectedVersionId] = useState("");
  const [isOpen, setIsOpen] = useState(false);
  const [isRestoring, setIsRestoring] = useState(true);
  const [isLoading, setIsLoading] = useState(false);
  const [isSaving, setIsSaving] = useState(false);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");

  useEffect(() => {
    let active = true;
    setIsRestoring(true);
    setCurrentSelection(null);
    setSelectedVersionId("");
    setError("");
    setMessage("");
    getProjectMainCharacter(projectId)
      .then((selection) => {
        if (!active) {
          return;
        }
        const restoredSelection = selectionSummary(selection)
          ? selection
          : null;
        setCurrentSelection(restoredSelection);
        setSelectedVersionId(restoredSelection?.character_version_id ?? "");
        onSelectionChange?.(restoredSelection !== null);
        onVersionChange?.(restoredSelection);
      })
      .catch((requestError) => {
        if (!active) {
          return;
        }
        setError(
          requestError instanceof Error
            ? requestError.message
            : "读取当前角色版本失败。",
        );
        onSelectionChange?.(false);
        onVersionChange?.(null);
      })
      .finally(() => {
        if (active) {
          setIsRestoring(false);
        }
      });
    return () => {
      active = false;
    };
  }, [onSelectionChange, onVersionChange, projectId]);

  async function openSelection() {
    setIsOpen(true);
    setIsLoading(true);
    setError("");
    setMessage("");
    try {
      const availableVersions = await listProjectCharacterVersions(projectId);
      setVersions(availableVersions);
    } catch (requestError) {
      setError(
        requestError instanceof Error
          ? requestError.message
          : "读取可用角色版本失败。",
      );
    } finally {
      setIsLoading(false);
    }
  }

  async function saveSelection() {
    if (readOnly || !selectedVersionId) {
      return;
    }
    setIsSaving(true);
    setError("");
    setMessage("");
    try {
      const selection = await chooseProjectMainCharacterVersion(
        projectId,
        selectedVersionId,
      );
      setCurrentSelection(selection);
      onSelectionChange?.(true);
      onVersionChange?.(selection);
      const summary = selectionSummary(selection);
      setMessage(
        summary
          ? `已选择角色“${summary.identityName} · ${summary.personaName} V${summary.versionNumber}”。`
          : "角色版本已保存。",
      );
    } catch (requestError) {
      setError(
        requestError instanceof Error
          ? requestError.message
          : "选择角色版本失败。",
      );
    } finally {
      setIsSaving(false);
    }
  }

  const currentSummary = selectionSummary(currentSelection);
  const selectedOption = versions.find(
    (version) => version.character_version_id === selectedVersionId,
  );

  return (
    <section className="character-selection" aria-labelledby="character-title">
      <div className="section-heading">
        <div>
          <span className="eyebrow">CHARACTER VERSION</span>
          <h3 id="character-title">角色版本</h3>
          {isRestoring ? (
            <p>正在恢复项目角色版本。</p>
          ) : currentSummary ? (
            <div className="current-character-summary">
              <strong>当前角色：{currentSummary.identityName}</strong>
              <span>
                {currentSummary.personaName} · V{currentSummary.versionNumber}
              </span>
              <small>
                {authorizationLabel(currentSummary.authorizationExpiresAt)}
              </small>
            </div>
          ) : (
            <p>选择一个已发布且授权有效的角色版本，用于后续人物参考匹配。</p>
          )}
        </div>
        <button
          className="secondary-button"
          disabled={isRestoring}
          onClick={openSelection}
          type="button"
        >
          {readOnly ? "查看角色版本" : "选择角色版本"}
        </button>
      </div>
      {isOpen ? (
        <div className="character-selection-panel">
          <div className="panel-header">
            <button
              type="button"
              className="secondary-button"
              onClick={() => setIsOpen(false)}
            >
              关闭
            </button>
          </div>
          {isLoading ? (
            <p className="status-note">正在读取可用角色版本</p>
          ) : null}
          {error ? <p className="settings-error">{error}</p> : null}
          {!isLoading && !error && !versions.length ? (
            <p className="status-note">
              当前没有具备有效授权与完整七类资产的已发布角色版本。
            </p>
          ) : null}
          {!isLoading && !error && versions.length ? (
            <fieldset className="character-options">
              <legend>选择一个不可变角色版本</legend>
              {versions.map((version) => {
                const personaName = stringValue(
                  version.persona_snapshot_json.name,
                  "未命名人设",
                );
                const occupation = stringValue(
                  version.persona_snapshot_json.occupation,
                  "未填写职业",
                );
                const isSelected =
                  selectedVersionId === version.character_version_id;
                return (
                  <label
                    className={
                      isSelected ? "character-option--selected" : undefined
                    }
                    key={version.character_version_id}
                  >
                    <input
                      checked={isSelected}
                      disabled={readOnly}
                      name="main-character-version"
                      onChange={() => {
                        setSelectedVersionId(version.character_version_id);
                        setMessage("");
                      }}
                      type="radio"
                      value={version.character_version_id}
                    />
                    <span className="character-option-copy">
                      <strong>{version.identity_name}</strong>
                      <span>
                        {personaName} · V{version.version_number}
                      </span>
                      <small>{occupation}</small>
                      <small>
                        {authorizationLabel(version.authorization_expires_at)}
                      </small>
                    </span>
                    {isSelected ? (
                      <ul
                        aria-label="七类已发布资产"
                        className="published-view-list"
                      >
                        {version.assets.map((asset) => (
                          <li key={asset.character_asset_id}>
                            <span>{VIEW_LABELS[asset.view_type]}</span>
                            <small>已发布</small>
                          </li>
                        ))}
                      </ul>
                    ) : null}
                  </label>
                );
              })}
            </fieldset>
          ) : null}
          {selectedOption && selectedOption.assets.length !== 7 ? (
            <p className="settings-error">
              当前版本缺少完整七类已发布资产，不能选择。
            </p>
          ) : null}
          {message ? <p className="setup-success">{message}</p> : null}
          {readOnly ? (
            <p className="status-note">只读身份不能更改项目角色版本。</p>
          ) : (
            <button
              disabled={
                isLoading ||
                isSaving ||
                !selectedVersionId ||
                selectedOption?.assets.length !== 7
              }
              onClick={saveSelection}
              type="button"
            >
              {isSaving ? "正在保存" : "确认角色版本"}
            </button>
          )}
        </div>
      ) : null}
    </section>
  );
}

type SelectionSummary = {
  identityName: string;
  personaName: string;
  versionNumber: number;
  authorizationExpiresAt: string | null;
};

function selectionSummary(
  selection: ProjectMainCharacter | null,
): SelectionSummary | null {
  if (!selection) {
    return null;
  }
  const snapshot = selection.character_snapshot;
  if (!snapshot) {
    return null;
  }
  if (
    snapshot.schema_version === "project-character-selection.v1" &&
    snapshot.identity?.display_name &&
    typeof snapshot.character_version_number === "number"
  ) {
    return {
      identityName: snapshot.identity.display_name,
      personaName: stringValue(
        snapshot.persona_snapshot_json?.name,
        "未命名人设",
      ),
      versionNumber: snapshot.character_version_number,
      authorizationExpiresAt:
        snapshot.identity.authorization_expires_at ?? null,
    };
  }
  if (snapshot.name) {
    return {
      identityName: snapshot.name,
      personaName: "历史兼容人物",
      versionNumber: selection.version_number,
      authorizationExpiresAt: null,
    };
  }
  return null;
}

function stringValue(value: unknown, fallback: string): string {
  return typeof value === "string" && value.trim() ? value : fallback;
}

function authorizationLabel(value: string | null): string {
  if (!value) {
    return "授权长期有效";
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return "授权期限记录无效";
  }
  const parts = new Intl.DateTimeFormat("zh-CN", {
    day: "2-digit",
    month: "2-digit",
    timeZone: "Asia/Shanghai",
    year: "numeric",
  }).formatToParts(date);
  const part = (type: "day" | "month" | "year") =>
    parts.find((item) => item.type === type)?.value ?? "";
  return `授权有效至 ${part("year")}-${part("month")}-${part("day")}`;
}
