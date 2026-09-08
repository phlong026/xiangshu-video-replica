import type { GenerationBusyAction, ScriptSource } from "./useGenerationDrafts";

type ScriptEditorProps = {
  busyAction: GenerationBusyAction;
  onChooseSource: (source: ScriptSource) => void;
  onRewriteScript: () => void;
  onSaveScript: () => void;
  onScriptTextChange: (text: string) => void;
  readOnly: boolean;
  scriptDirty: boolean;
  scriptSource: ScriptSource;
  scriptStale: boolean;
  scriptText: string;
  shotMappings: Array<{ shotId: string; text: string }>;
};

export function ScriptEditor({
  busyAction,
  onChooseSource,
  onRewriteScript,
  onSaveScript,
  onScriptTextChange,
  readOnly,
  scriptDirty,
  scriptSource,
  scriptStale,
  scriptText,
  shotMappings,
}: ScriptEditorProps) {
  const busy = Boolean(busyAction);

  return (
    <>
      {scriptStale ? (
        <p className="attention-banner">镜头卡已变化，请重新保存口播稿</p>
      ) : null}
      {scriptDirty ? (
        <p className="attention-banner">口播稿有未保存修改，请先保存</p>
      ) : null}

      <fieldset className="generation-block">
        <legend>1. 确认口播稿</legend>
        <div className="generation-choice-row">
          <label>
            <input
              checked={scriptSource === "original"}
              disabled={readOnly || busy}
              name="script-source"
              onChange={() => onChooseSource("original")}
              type="radio"
            />
            原稿
          </label>
          <label>
            <input
              aria-label="自定义稿"
              checked={scriptSource === "custom"}
              disabled={readOnly || busy}
              name="script-source"
              onChange={() => onChooseSource("custom")}
              type="radio"
            />
            自定义稿
          </label>
        </div>
        <label className="generation-field">
          <span>口播稿内容</span>
          <textarea
            aria-label="口播稿内容"
            onChange={(event) => onScriptTextChange(event.target.value)}
            readOnly={readOnly || scriptSource === "original"}
            rows={6}
            value={scriptText}
          />
        </label>
        <div className="script-actions-row">
          <button
            disabled={readOnly || busy || !scriptText.trim()}
            onClick={onRewriteScript}
            type="button"
          >
            {busyAction === "rewrite" ? "正在改写…" : "AI 改写"}
          </button>
          <button
            disabled={readOnly || busy || !scriptText.trim()}
            onClick={onSaveScript}
            type="button"
          >
            {busyAction === "script" ? "正在保存" : "保存口播稿"}
          </button>
        </div>
        {shotMappings.length > 0 ? (
          <ul className="shot-mapping-list" aria-label="口播镜头映射">
            {shotMappings.map((mapping) => (
              <li key={`${mapping.shotId}-${mapping.text}`}>
                {mapping.shotId}：{mapping.text || "（无口播）"}
              </li>
            ))}
          </ul>
        ) : null}
      </fieldset>
    </>
  );
}
