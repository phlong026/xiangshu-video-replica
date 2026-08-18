import { useEffect, useRef, useState } from "react";

import {
  chooseProjectMainCharacterVersion,
  getProjectMainCharacter,
  listProjectCharacterVersions,
  type ProjectCharacterAssetOption,
  type ProjectCharacterVersionOption,
  type ProjectMainCharacter,
  type SimpleCharacterResult,
} from "./api";
import { SimpleCharacterUpload } from "./SimpleCharacterUpload";

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
  onBusyChange,
  onSelectionChange,
  onVersionChange,
  projectId,
  readOnly = false,
}: {
  onBusyChange?: (isBusy: boolean) => void;
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
  const [isUploadOpen, setIsUploadOpen] = useState(false);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [autoSelected, setAutoSelected] = useState(false);
  const [guidance, setGuidance] = useState("");
  const [restoredEmpty, setRestoredEmpty] = useState(false);
  const [isAutoSelecting, setIsAutoSelecting] = useState(false);
  const autoSelectProjectRef = useRef<string | null>(null);
  // 回调走 ref 转发：生产接线中 busy 上报会让父级重渲染并产生新的
  // 回调引用，若进入自动选择 effect 的依赖面会触发 cleanup 中止
  // in-flight 落库（评审 Critical 1），ref 保持依赖面纯净。
  const onBusyChangeRef = useRef(onBusyChange);
  const onSelectionChangeRef = useRef(onSelectionChange);
  const onVersionChangeRef = useRef(onVersionChange);

  useEffect(() => {
    onBusyChangeRef.current = onBusyChange;
    onSelectionChangeRef.current = onSelectionChange;
    onVersionChangeRef.current = onVersionChange;
  });

  useEffect(() => {
    let active = true;
    setIsRestoring(true);
    setCurrentSelection(null);
    setSelectedVersionId("");
    setError("");
    setMessage("");
    setAutoSelected(false);
    setGuidance("");
    setRestoredEmpty(false);
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
        // 仅「restore 成功且无快照」才允许自动预选：restore 失败时快照
        // 状态未知，自动改绑可能覆盖用户既有选择（评审 Major 2）。
        setRestoredEmpty(restoredSelection === null);
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

  // P0-03-01：项目无角色快照进入时，自动预选最近发布的可用版本并落库
  //（choose 服务端原子复用快照，重复选择幂等）；仅每个项目自动一次，
  // 只读身份、restore 失败、空列表与写入失败都静默转手动态选择。
  useEffect(() => {
    if (
      isRestoring ||
      !restoredEmpty ||
      readOnly ||
      currentSelection ||
      autoSelectProjectRef.current === projectId
    ) {
      return;
    }
    autoSelectProjectRef.current = projectId;
    let active = true;
    void (async () => {
      setIsAutoSelecting(true);
      onBusyChangeRef.current?.(true);
      try {
        const availableVersions = await listProjectCharacterVersions(projectId);
        if (!active) {
          return;
        }
        if (!availableVersions.length) {
          setGuidance(
            "暂无可选角色版本，请先在人物库发布角色，或使用一键上传人物。",
          );
          return;
        }
        const latestVersion = availableVersions.reduce((latest, current) =>
          Date.parse(current.published_at) > Date.parse(latest.published_at)
            ? current
            : latest,
        );
        const selection = await chooseProjectMainCharacterVersion(
          projectId,
          latestVersion.character_version_id,
        );
        if (!active) {
          return;
        }
        setCurrentSelection(selection);
        setSelectedVersionId(selection.character_version_id ?? "");
        setAutoSelected(true);
        onSelectionChangeRef.current?.(true);
        onVersionChangeRef.current?.(selection);
      } catch {
        if (active) {
          setGuidance("未自动选择角色版本，请手动选择角色版本。");
        }
      } finally {
        setIsAutoSelecting(false);
        onBusyChangeRef.current?.(false);
      }
    })();
    return () => {
      active = false;
    };
  }, [currentSelection, isRestoring, projectId, readOnly, restoredEmpty]);

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

  async function refreshVersions() {
    try {
      const availableVersions = await listProjectCharacterVersions(projectId);
      setVersions(availableVersions);
    } catch {
      // Keep the previously loaded list; the next openSelection will retry.
    }
  }

  async function handleSimpleCharacterCreated(result: SimpleCharacterResult) {
    await refreshVersions();
    setSelectedVersionId(result.character_version_id);
    setIsUploadOpen(false);
    setMessage("新人物已发布并自动选中，确认后保存即可作为项目角色版本。");
  }

  async function saveSelection() {
    // 自动预选写入在途时跳过手动保存，避免双 PUT 竞态改写落库结果
    //（评审 Minor 4）；窗口极短，按钮同步禁用防采。
    if (readOnly || !selectedVersionId || isAutoSelecting) {
      return;
    }
    onBusyChange?.(true);
    setIsSaving(true);
    setError("");
    setMessage("");
    try {
      const selection = await chooseProjectMainCharacterVersion(
        projectId,
        selectedVersionId,
      );
      setCurrentSelection(selection);
      setAutoSelected(false);
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
      onBusyChange?.(false);
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
          <h3 id="character-title">角色版本</h3>
          {isRestoring ? (
            <p>恢复角色版本中…</p>
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
      {autoSelected && currentSummary ? (
        <div className="auto-selection-notice" role="status">
          <span>
            已自动选择角色版本 {currentSummary.identityName} · V
            {currentSummary.versionNumber}
          </span>
          <button onClick={openSelection} type="button">
            更换
          </button>
        </div>
      ) : null}
      {guidance ? (
        <p className="status-note" role="status">
          {guidance}
        </p>
      ) : null}
      {isOpen ? (
        <div className="character-selection-panel">
          <div className="panel-header">
            <div>
              {!readOnly ? (
                <button
                  className="secondary-button"
                  onClick={() => setIsUploadOpen((open) => !open)}
                  type="button"
                >
                  {isUploadOpen ? "收起一键上传" : "一键上传人物"}
                </button>
              ) : null}
            </div>
            <button
              type="button"
              className="secondary-button"
              onClick={() => setIsOpen(false)}
            >
              关闭
            </button>
          </div>
          {!readOnly && isUploadOpen ? (
            <SimpleCharacterUpload
              onCreated={handleSimpleCharacterCreated}
              projectId={projectId}
            />
          ) : null}
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
                isAutoSelecting ||
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
