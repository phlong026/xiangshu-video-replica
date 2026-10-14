import type { Project } from "../api";

/** 项目的「提取文案」来源：优先复刻参考视频，只上传了音轨的项目回退到音轨。 */
export function projectSourceAssetId(
  project:
    | Pick<Project, "reference_asset_id" | "script_source_asset_id">
    | undefined,
): string | undefined {
  return (
    project?.reference_asset_id ?? project?.script_source_asset_id ?? undefined
  );
}
