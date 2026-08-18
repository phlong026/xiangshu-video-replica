import type { GenerationRuntimeLimits, GenerationVersion } from "./api";
import {
  type GenerationBusyAction,
  type IdempotencyRecord,
  RECOVERY_CONFLICT_MESSAGE,
  readPayloadString,
} from "./useGenerationDrafts";

type GenerationLauncherProps = {
  analysisVersionId: string;
  busyAction: GenerationBusyAction;
  canCompile: boolean;
  canCreateBatch: boolean;
  characterVersionId: string | null;
  durationValid: boolean;
  firstFrameAssetId: string;
  firstFrameSelectionVersionId: string;
  limits: GenerationRuntimeLimits;
  onCompilePrompt: () => void;
  onCreateBatch: () => void;
  onDurationChange: (value: string) => void;
  onLockPrompt: () => void;
  onPromptTextChange: (text: string) => void;
  onQuantityChange: (value: string) => void;
  onRecoverBatch: () => void;
  onResolutionChange: (value: "768P" | "2K") => void;
  onSavePromptRevision: () => void;
  outputDuration: string;
  promptDirty: boolean;
  promptParametersMatch: boolean;
  promptStale: boolean;
  promptText: string;
  promptVersion: GenerationVersion | null;
  quantity: number | null;
  quantityError: string;
  quantityInput: string;
  readOnly: boolean;
  recoveryRecord: IdempotencyRecord | null;
  recoveryRecordConflicts: boolean;
  referenceSelectionId: string | null;
  resolution: "768P" | "2K";
  savedPromptText: string;
  scriptStale: boolean;
  shotCardVersionId: string;
};

export function GenerationLauncher({
  analysisVersionId,
  busyAction,
  canCompile,
  canCreateBatch,
  characterVersionId,
  durationValid,
  firstFrameAssetId,
  firstFrameSelectionVersionId,
  limits,
  onCompilePrompt,
  onCreateBatch,
  onDurationChange,
  onLockPrompt,
  onPromptTextChange,
  onQuantityChange,
  onRecoverBatch,
  onResolutionChange,
  onSavePromptRevision,
  outputDuration,
  promptDirty,
  promptParametersMatch,
  promptStale,
  promptText,
  promptVersion,
  quantity,
  quantityError,
  quantityInput,
  readOnly,
  recoveryRecord,
  recoveryRecordConflicts,
  referenceSelectionId,
  resolution,
  savedPromptText,
  scriptStale,
  shotCardVersionId,
}: GenerationLauncherProps) {
  const busy = Boolean(busyAction);
  const promptStatus = readPayloadString(promptVersion, "status");

  return (
    <>
      <fieldset className="generation-source-grid">
        <legend>冻结输入来源</legend>
        <span>分析版本：{analysisVersionId}</span>
        <span>镜头卡版本：{shotCardVersionId}</span>
        <span>人物版本：{characterVersionId ?? "历史兼容人物"}</span>
        <span>人物参考：{referenceSelectionId ?? "历史兼容参考"}</span>
        <span>首帧选择：{firstFrameSelectionVersionId}</span>
        <span>首帧素材：{firstFrameAssetId}</span>
        {promptVersion ? (
          <>
            <span>
              模板版本：
              {readPayloadString(promptVersion, "template_version") ??
                "历史模板"}
            </span>
            <span>
              模板哈希：
              {readPayloadString(promptVersion, "template_hash") ?? "未记录"}
            </span>
          </>
        ) : null}
      </fieldset>

      {scriptStale ? (
        <p className="attention-banner">镜头卡已变化，请重新保存口播稿</p>
      ) : null}
      {promptStale ? (
        <p className="attention-banner">上游输入已变化，请重新编译 Prompt</p>
      ) : null}
      {promptVersion && !promptParametersMatch ? (
        <p className="attention-banner">生成参数已变化，请重新编译 Prompt</p>
      ) : null}

      <fieldset className="generation-block">
        <legend>2. 编译、修订并锁定 Prompt</legend>
        <div className="generation-parameter-grid">
          <label>
            <span>成片时长（秒）</span>
            <input
              aria-label="成片时长"
              disabled={readOnly || busy}
              max="15"
              min="4"
              onChange={(event) => onDurationChange(event.target.value)}
              step="1"
              type="number"
              value={outputDuration}
            />
          </label>
          <label>
            <span>分辨率</span>
            <select
              aria-label="分辨率"
              disabled={readOnly || busy}
              onChange={(event) =>
                onResolutionChange(event.target.value as "768P" | "2K")
              }
              value={resolution}
            >
              <option value="768P">768P</option>
              <option value="2K">2K</option>
            </select>
          </label>
        </div>
        {!durationValid ? (
          <p className="settings-error">成片时长必须是 4–15 秒的整数。</p>
        ) : null}
        <button disabled={!canCompile} onClick={onCompilePrompt} type="button">
          {busyAction === "compile" ? "正在编译" : "编译 H3 Prompt"}
        </button>
        {promptVersion ? (
          <>
            <label className="generation-field">
              <span>H3 Prompt 内容</span>
              <textarea
                aria-label="H3 Prompt 内容"
                onChange={(event) => onPromptTextChange(event.target.value)}
                readOnly={
                  readOnly ||
                  busyAction === "compile" ||
                  busyAction === "prompt" ||
                  promptStatus === "LOCKED" ||
                  promptStatus === "USED"
                }
                rows={10}
                value={promptText}
              />
            </label>
            <fieldset className="prompt-diff">
              <legend>Prompt 差异</legend>
              <div>
                <strong>已保存版本</strong>
                <pre>{savedPromptText}</pre>
              </div>
              <div>
                <strong>当前编辑</strong>
                <pre>{promptText}</pre>
              </div>
            </fieldset>
            {promptDirty ? (
              <p className="attention-banner">当前编辑与已保存版本存在差异</p>
            ) : null}
            <div className="generation-actions">
              <button
                disabled={readOnly || !promptDirty || busy || promptStale}
                onClick={onSavePromptRevision}
                type="button"
              >
                {busyAction === "prompt" ? "正在保存" : "另存 Prompt 新版本"}
              </button>
              <button
                disabled={
                  readOnly ||
                  promptDirty ||
                  promptStale ||
                  busy ||
                  promptStatus === "LOCKED" ||
                  promptStatus === "USED"
                }
                onClick={onLockPrompt}
                type="button"
              >
                {busyAction === "lock" ? "正在锁定" : "锁定 Prompt"}
              </button>
              <span className="status-note">
                当前状态：{promptStatus ?? "未知"}
              </span>
            </div>
          </>
        ) : null}
      </fieldset>

      <fieldset className="generation-block">
        <legend>3. 设置数量并生成</legend>
        <label className="generation-field generation-quantity-field">
          <span>生成数量</span>
          <input
            aria-label="生成数量"
            disabled={readOnly || busy}
            max={limits.max_quantity}
            min={limits.min_quantity}
            onChange={(event) => onQuantityChange(event.target.value)}
            step="1"
            type="number"
            value={quantityInput}
          />
        </label>
        {quantityError ? (
          <p className="settings-error">{quantityError}</p>
        ) : null}
        {quantity !== null ? (
          <div className="paid-task-warning">
            <strong>将创建 {quantity} 个付费生成任务</strong>
            <span>
              {limits.estimated_cost_per_task == null
                ? "预计费用暂不可用"
                : `预计费用：¥${(
                    limits.estimated_cost_per_task * quantity
                  ).toFixed(2)}`}
            </span>
          </div>
        ) : null}
        {recoveryRecordConflicts ? (
          <p className="attention-banner">{RECOVERY_CONFLICT_MESSAGE}</p>
        ) : null}
        <button
          disabled={!canCreateBatch}
          onClick={onCreateBatch}
          type="button"
        >
          {busyAction === "batch"
            ? "正在创建任务"
            : `创建 ${quantity ?? (quantityInput || "0")} 个生成任务`}
        </button>
        {recoveryRecord ? (
          <button
            className="secondary-button"
            disabled={readOnly || busy}
            onClick={onRecoverBatch}
            type="button"
          >
            恢复已提交批次
          </button>
        ) : null}
      </fieldset>
    </>
  );
}
