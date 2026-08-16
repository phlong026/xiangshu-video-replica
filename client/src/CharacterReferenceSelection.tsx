import { useCallback, useEffect, useRef, useState } from "react";

import {
  type AnalysisVersion,
  type CharacterReferenceRecommendation,
  type CharacterReferenceSelection as CharacterReferenceSelectionValue,
  getAssetDownloadUrl,
  getCharacterReferenceRecommendation,
  getLatestCharacterReferenceSelection,
  type ProjectCharacterAssetOption,
  type ProjectMainCharacter,
  selectCharacterReferences,
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

export function CharacterReferenceSelection({
  characterSelection,
  onSelectionChange,
  projectId,
  readOnly = false,
  sourceFrameSelection,
}: {
  characterSelection: ProjectMainCharacter;
  onSelectionChange?: (
    selection: CharacterReferenceSelectionValue | null,
  ) => void;
  projectId: string;
  readOnly?: boolean;
  sourceFrameSelection: AnalysisVersion;
}) {
  const [recommendation, setRecommendation] =
    useState<CharacterReferenceRecommendation | null>(null);
  const [selectedAssetIds, setSelectedAssetIds] = useState<string[]>([]);
  const [previewUrls, setPreviewUrls] = useState<Record<string, string>>({});
  const [status, setStatus] = useState("");
  const [error, setError] = useState("");
  const [isLoading, setIsLoading] = useState(true);
  const [isSubmitting, setIsSubmitting] = useState(false);
  const loadRequestId = useRef(0);

  const load = useCallback(async () => {
    const requestId = loadRequestId.current + 1;
    loadRequestId.current = requestId;
    const isCurrentRequest = () => requestId === loadRequestId.current;
    setIsLoading(true);
    setIsSubmitting(false);
    setError("");
    setStatus("");
    setPreviewUrls({});
    onSelectionChange?.(null);
    try {
      const [nextRecommendation, latestSelection] = await Promise.all([
        getCharacterReferenceRecommendation(projectId),
        getLatestCharacterReferenceSelection(projectId),
      ]);
      if (!isCurrentRequest()) {
        return;
      }
      if (
        !recommendationMatchesInputs(
          nextRecommendation,
          sourceFrameSelection,
          characterSelection,
        )
      ) {
        setRecommendation(null);
        setSelectedAssetIds([]);
        setError("人物参考图推荐与当前源画面或角色版本不一致，请重新加载。");
        return;
      }
      setRecommendation(nextRecommendation);
      const currentSelection = selectionMatchesRecommendation(
        latestSelection,
        nextRecommendation,
        characterSelection,
      )
        ? latestSelection
        : null;
      setSelectedAssetIds(
        currentSelection?.selected_asset_ids_json ??
          nextRecommendation.recommended_asset_ids_json,
      );
      if (currentSelection) {
        setStatus("当前人物参考图已确认。");
        onSelectionChange?.(currentSelection);
      } else if (latestSelection) {
        setStatus(
          "已有选择与当前源画面或角色版本不一致，请重新确认人物参考图。",
        );
      } else {
        setStatus("已按源画面特征给出推荐，请查看后手动确认。");
      }
      if (readOnly) {
        return;
      }
      const previews = await Promise.allSettled(
        nextRecommendation.candidate_assets.map(async (candidate) => {
          const download = await getAssetDownloadUrl(candidate.asset_id);
          return [candidate.asset_id, download.url] as const;
        }),
      );
      if (!isCurrentRequest()) {
        return;
      }
      setPreviewUrls(
        Object.fromEntries(
          previews.flatMap((result) =>
            result.status === "fulfilled" ? [result.value] : [],
          ),
        ),
      );
    } catch (requestError) {
      if (isCurrentRequest()) {
        setRecommendation(null);
        setSelectedAssetIds([]);
        setError(
          requestError instanceof Error
            ? requestError.message
            : "读取人物参考图推荐失败。",
        );
      }
    } finally {
      if (isCurrentRequest()) {
        setIsLoading(false);
      }
    }
  }, [
    characterSelection,
    onSelectionChange,
    projectId,
    readOnly,
    sourceFrameSelection,
  ]);

  useEffect(() => {
    void load();
    return () => {
      loadRequestId.current += 1;
    };
  }, [load]);

  function toggleAsset(assetId: string) {
    const isSelected = selectedAssetIds.includes(assetId);
    if (!isSelected && !previewUrls[assetId]) {
      setError("请先成功加载该人物参考图预览。");
      return;
    }
    if (!isSelected && selectedAssetIds.length >= 4) {
      setError("人物参考图最多选择 4 张。");
      return;
    }
    const nextIds = isSelected
      ? selectedAssetIds.filter((value) => value !== assetId)
      : [...selectedAssetIds, assetId];
    setSelectedAssetIds(nextIds);
    setError("");
    setStatus("参考图选择已修改，请重新确认。");
    onSelectionChange?.(null);
  }

  async function handleConfirm() {
    if (readOnly || !recommendation) {
      return;
    }
    if (
      selectedAssetIds.length < 1 ||
      selectedAssetIds.length > 4 ||
      selectedAssetIds.some((assetId) => !previewUrls[assetId])
    ) {
      setError("请选择并查看 1–4 张人物参考图后再确认。");
      return;
    }
    const requestId = loadRequestId.current;
    setIsSubmitting(true);
    setError("");
    try {
      const selection = await selectCharacterReferences(projectId, {
        selected_asset_ids: selectedAssetIds,
        source_frame_selection_version_id:
          recommendation.source_frame_version_id,
        character_version_id: recommendation.character_version_id,
      });
      if (requestId !== loadRequestId.current) {
        return;
      }
      setStatus("当前人物参考图已确认。");
      onSelectionChange?.(selection);
    } catch (requestError) {
      if (requestId !== loadRequestId.current) {
        return;
      }
      setError(
        requestError instanceof Error
          ? requestError.message
          : "确认人物参考图失败。",
      );
    } finally {
      if (requestId === loadRequestId.current) {
        setIsSubmitting(false);
      }
    }
  }

  return (
    <section
      className="character-reference-selection"
      aria-labelledby="character-reference-title"
    >
      <div>
        <span className="eyebrow">CHARACTER REFERENCES</span>
        <h3 id="character-reference-title">人物参考图</h3>
        <p>
          系统只负责按源画面朝向和景别推荐；请查看七视图后，手动确认 1–4 张。
        </p>
      </div>
      {isLoading ? <p className="status-note">正在读取人物参考图推荐</p> : null}
      {error ? <p className="settings-error">{error}</p> : null}
      {status ? <p className="setup-success">{status}</p> : null}
      {readOnly && recommendation ? (
        <p className="status-note">只读身份不加载素材预览。</p>
      ) : null}
      {recommendation ? (
        <fieldset className="character-reference-options">
          <legend>
            {readOnly
              ? "七视图选择记录（素材预览需要下载权限）"
              : "推荐项已预选，但只有点击确认后才会进入首帧生成"}
          </legend>
          {recommendation.candidate_assets.map((candidate) => {
            const checked = selectedAssetIds.includes(candidate.asset_id);
            const recommended =
              recommendation.recommended_asset_ids_json.includes(
                candidate.asset_id,
              );
            return (
              <label
                className={
                  checked
                    ? "source-frame-option source-frame-option--selected"
                    : "source-frame-option"
                }
                key={candidate.character_asset_id}
              >
                <input
                  checked={checked}
                  disabled={
                    readOnly ||
                    isSubmitting ||
                    (!checked && !previewUrls[candidate.asset_id])
                  }
                  onChange={() => toggleAsset(candidate.asset_id)}
                  type="checkbox"
                />
                {previewUrls[candidate.asset_id] ? (
                  <img
                    alt={`人物参考图 ${VIEW_LABELS[candidate.view_type]}`}
                    src={previewUrls[candidate.asset_id]}
                  />
                ) : (
                  <span className="source-frame-placeholder">
                    {readOnly ? "预览不可用" : "预览加载失败"}
                  </span>
                )}
                <span>
                  <strong>{VIEW_LABELS[candidate.view_type]}</strong>
                  <small>{recommended ? "系统推荐" : "可选视图"}</small>
                </span>
              </label>
            );
          })}
        </fieldset>
      ) : null}
      {readOnly ? (
        <p className="status-note">只读身份不能更改人物参考图。</p>
      ) : (
        <button
          className="source-frame-confirm"
          disabled={
            isLoading ||
            isSubmitting ||
            selectedAssetIds.length < 1 ||
            selectedAssetIds.length > 4 ||
            selectedAssetIds.some((assetId) => !previewUrls[assetId])
          }
          onClick={handleConfirm}
          type="button"
        >
          {isSubmitting ? "正在确认" : "确认人物参考"}
        </button>
      )}
    </section>
  );
}

function recommendationMatchesInputs(
  recommendation: CharacterReferenceRecommendation,
  sourceFrameSelection: AnalysisVersion,
  characterSelection: ProjectMainCharacter,
): boolean {
  return (
    recommendation.source_frame_version_id === sourceFrameSelection.id &&
    recommendation.character_version_id ===
      characterSelection.character_version_id &&
    recommendation.character_version_snapshot_json.main_character_version_id ===
      characterSelection.version_id
  );
}

function selectionMatchesRecommendation(
  selection: CharacterReferenceSelectionValue | null,
  recommendation: CharacterReferenceRecommendation,
  characterSelection: ProjectMainCharacter,
): selection is CharacterReferenceSelectionValue {
  if (!selection) {
    return false;
  }
  const candidateIds = new Set(
    recommendation.candidate_assets.map((candidate) => candidate.asset_id),
  );
  return (
    selection.source_frame_version_id ===
      recommendation.source_frame_version_id &&
    selection.character_version_id === recommendation.character_version_id &&
    selection.character_version_snapshot_json.main_character_version_id ===
      characterSelection.version_id &&
    selection.selected_asset_ids_json.length >= 1 &&
    selection.selected_asset_ids_json.length <= 4 &&
    new Set(selection.selected_asset_ids_json).size ===
      selection.selected_asset_ids_json.length &&
    selection.selected_asset_ids_json.every((assetId) =>
      candidateIds.has(assetId),
    )
  );
}
