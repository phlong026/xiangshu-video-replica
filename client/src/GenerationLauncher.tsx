import type {
  GenerationPriceQuote,
  GenerationRatio,
  GenerationRuntimeLimits,
  GenerationVersion,
} from "./api";
import {
  type GenerationBusyAction,
  type GenerationQuoteStatus,
  type IdempotencyRecord,
  RECOVERY_CONFLICT_MESSAGE,
  readPayloadString,
} from "./useGenerationDrafts";
import "./generation-controls.css";

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
  onRetryPriceQuote?: () => void;
  onRatioChange?: (value: GenerationRatio) => void;
  onQuantityChange: (value: string) => void;
  onRecoverBatch: () => void;
  onResolutionChange: (value: "768P" | "2K") => void;
  onSavePromptRevision: () => void;
  onApplySavedPrompt?: (savedPromptId: string) => void;
  outputDuration: string;
  promptDirty: boolean;
  promptParametersMatch: boolean;
  promptStale: boolean;
  promptText: string;
  promptVersion: GenerationVersion | null;
  priceQuote?: GenerationPriceQuote | null;
  priceQuoteError?: string;
  priceQuoteStatus?: GenerationQuoteStatus;
  quantity: number | null;
  quantityError: string;
  quantityInput: string;
  readOnly: boolean;
  recoveryRecord: IdempotencyRecord | null;
  recoveryRecordConflicts: boolean;
  referenceSelectionId: string | null;
  resolution: "768P" | "2K";
  ratio?: GenerationRatio;
  savedPrompts?: GenerationVersion[];
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
  onRetryPriceQuote,
  onRatioChange,
  onQuantityChange,
  onRecoverBatch,
  onResolutionChange,
  onSavePromptRevision,
  onApplySavedPrompt,
  outputDuration,
  promptDirty,
  promptParametersMatch,
  promptStale,
  promptText,
  promptVersion,
  priceQuote = null,
  priceQuoteError = "",
  priceQuoteStatus = priceQuote ? "ready" : "idle",
  quantity,
  quantityError,
  quantityInput,
  readOnly,
  recoveryRecord,
  recoveryRecordConflicts,
  referenceSelectionId,
  resolution,
  ratio = "adaptive",
  savedPrompts = [],
  savedPromptText,
  scriptStale,
  shotCardVersionId,
}: GenerationLauncherProps) {
  const busy = Boolean(busyAction);
  const promptStatus = readPayloadString(promptVersion, "status");
  const displayedQuantity = recoveryRecord?.request.quantity ?? quantity;
  const displayedDuration =
    recoveryRecord?.request.output_duration_seconds ?? Number(outputDuration);

  return (
    <>
      <fieldset className="generation-source-grid">
        <legend>冻结输入来源</legend>
        <span>拆解版本：{analysisVersionId}</span>
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
        <fieldset className="generation-ratio-options">
          <legend>画面比例</legend>
          {(
            [
              ["adaptive", "自动"],
              ["21:9", "21:9"],
              ["16:9", "16:9"],
              ["4:3", "4:3"],
              ["1:1", "1:1"],
              ["3:4", "3:4"],
              ["9:16", "9:16"],
            ] as const
          ).map(([value, label]) => (
            <label key={value}>
              <input
                checked={ratio === value}
                disabled={readOnly || busy}
                name="generation-ratio"
                onChange={() => onRatioChange?.(value)}
                type="radio"
                value={value}
              />
              <span>{label}</span>
            </label>
          ))}
        </fieldset>
        <div className="generation-parameter-grid">
          <label>
            <span>成片时长（秒）</span>
            <select
              aria-label="成片时长"
              disabled={readOnly || busy}
              onChange={(event) => onDurationChange(event.target.value)}
              value={outputDuration}
            >
              <option value="4">4 秒</option>
              <option value="15">15 秒</option>
            </select>
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
          <p className="settings-error">成片时长请选择 4 秒或 15 秒。</p>
        ) : null}
        <button disabled={!canCompile} onClick={onCompilePrompt} type="button">
          {busyAction === "compile" ? "正在编译" : "编译视频生成提示词"}
        </button>
        {promptVersion ? (
          <>
            <label className="generation-field">
              <span>视频生成提示词内容</span>
              <textarea
                aria-label="视频生成提示词内容"
                onChange={(event) => onPromptTextChange(event.target.value)}
                readOnly={
                  readOnly ||
                  busyAction === "compile" ||
                  busyAction === "prompt" ||
                  promptStatus === "LOCKED" ||
                  promptStatus === "USED"
                }
                rows={10}
                maxLength={7000}
                value={promptText}
              />
              <small>{promptText.length}/7000 字</small>
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
            {savedPrompts.length ? (
              <section
                className="saved-prompt-library"
                aria-label="我的提示词库"
              >
                <strong>我的提示词</strong>
                {savedPrompts.map((saved) => (
                  <button
                    className="secondary-button"
                    disabled={readOnly || busy}
                    key={saved.id}
                    onClick={() => onApplySavedPrompt?.(saved.id)}
                    type="button"
                  >
                    应用{" "}
                    {String(
                      saved.payload.name ?? `版本 #${saved.version_number}`,
                    )}
                  </button>
                ))}
              </section>
            ) : null}
          </>
        ) : null}
      </fieldset>

      <fieldset className="generation-block">
        <legend>3. 设置数量并生成</legend>
        <label className="generation-field generation-quantity-field">
          <span>生成数量</span>
          <select
            aria-label="生成数量"
            disabled={readOnly || busy}
            onChange={(event) => onQuantityChange(event.target.value)}
            value={quantityInput}
          >
            {[1, 2, 4]
              .filter((value) => value <= limits.max_quantity)
              .map((value) => (
                <option key={value} value={value}>
                  {value}
                </option>
              ))}
          </select>
        </label>
        {quantityError ? (
          <p className="settings-error">{quantityError}</p>
        ) : null}
        {displayedQuantity !== null ? (
          <div className="paid-task-warning">
            <strong>
              {recoveryRecord ? "待恢复" : "将创建"} {displayedQuantity}{" "}
              个付费生成任务
            </strong>
            <span>
              预计消耗{" "}
              {priceQuoteStatus === "ready" && priceQuote
                ? priceQuote.estimated_seconds
                : displayedDuration * displayedQuantity}{" "}
              秒额度
            </span>
            {priceQuoteStatus === "ready" && priceQuote ? (
              <span>
                约 ¥{(priceQuote.estimated_price_fen / 100).toFixed(2)}（
                {priceQuote.unit_price_fen_per_second} 分/秒）
              </span>
            ) : priceQuoteStatus === "loading" ? (
              <span>正在读取准确费用…</span>
            ) : (
              <span>准确费用暂不可用</span>
            )}
          </div>
        ) : null}
        {priceQuoteStatus === "error" ? (
          <div className="settings-error" role="alert">
            <p>{priceQuoteError}</p>
            <button
              className="secondary-button"
              onClick={onRetryPriceQuote}
              type="button"
            >
              重新获取生成报价
            </button>
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
            disabled={
              readOnly || busy || priceQuoteStatus !== "ready" || !priceQuote
            }
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
