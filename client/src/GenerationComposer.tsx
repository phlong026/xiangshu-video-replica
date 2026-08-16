import { useEffect, useMemo, useRef, useState } from "react";

import {
  compileGenerationPrompt,
  createGenerationBatch,
  createScriptVersion,
  type GenerationBatch,
  type GenerationBatchInput,
  type GenerationRuntimeLimits,
  type GenerationVersion,
  getGenerationRuntimeLimits,
  getLatestGenerationPrompt,
  getLatestScriptVersion,
  lockGenerationPrompt,
  reviseGenerationPrompt,
} from "./api";

type ScriptSource = "original" | "custom";

type GenerationComposerProps = {
  analysisVersionId: string;
  characterVersionId: string | null;
  durationSeconds: number;
  firstFrameAssetId: string;
  firstFrameSelectionVersionId: string;
  onBatchCreated: (batch: GenerationBatch) => void;
  onWorkflowStepChange: (step: number) => void;
  originalScript: string;
  projectId: string;
  readOnly?: boolean;
  referenceSelectionId: string | null;
  shotCardVersionId: string;
};

const DEFAULT_LIMITS: GenerationRuntimeLimits = {
  min_quantity: 1,
  max_quantity: 1,
  estimated_cost_per_task: null,
};

const sessionIdempotencyRecords = new Map<string, IdempotencyRecord>();

export function GenerationComposer({
  analysisVersionId,
  characterVersionId,
  durationSeconds,
  firstFrameAssetId,
  firstFrameSelectionVersionId,
  onBatchCreated,
  onWorkflowStepChange,
  originalScript,
  projectId,
  readOnly = false,
  referenceSelectionId,
  shotCardVersionId,
}: GenerationComposerProps) {
  const [scriptVersion, setScriptVersion] = useState<GenerationVersion | null>(
    null,
  );
  const [scriptSource, setScriptSource] = useState<ScriptSource>("original");
  const [scriptText, setScriptText] = useState(originalScript);
  const [scriptStale, setScriptStale] = useState(false);
  const [promptVersion, setPromptVersion] = useState<GenerationVersion | null>(
    null,
  );
  const [promptText, setPromptText] = useState("");
  const [savedPromptText, setSavedPromptText] = useState("");
  const [promptStale, setPromptStale] = useState(false);
  const [limits, setLimits] = useState(DEFAULT_LIMITS);
  const [quantityInput, setQuantityInput] = useState("1");
  const [outputDuration, setOutputDuration] = useState(() =>
    String(Math.min(15, Math.max(4, Math.round(durationSeconds)))),
  );
  const [resolution, setResolution] = useState<"768P" | "2K">("768P");
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [isLoading, setIsLoading] = useState(true);
  const [recoveryRecord, setRecoveryRecord] =
    useState<IdempotencyRecord | null>(null);
  const [busyAction, setBusyAction] = useState<
    "script" | "compile" | "prompt" | "lock" | "batch" | null
  >(null);
  const loadGenerationRef = useRef(0);
  const actionGenerationRef = useRef(0);
  const isCreatingBatchRef = useRef(false);
  const idempotencyRecordRef = useRef<IdempotencyRecord | null>(null);

  useEffect(() => {
    actionGenerationRef.current += 1;
    isCreatingBatchRef.current = false;
    const storageKey = idempotencyStorageKey(projectId);
    const restoredRecord = restoreIdempotencyRecord(storageKey);
    idempotencyRecordRef.current = restoredRecord;
    setRecoveryRecord(restoredRecord);
    setBusyAction(null);
    const loadGeneration = loadGenerationRef.current + 1;
    loadGenerationRef.current = loadGeneration;
    let active = true;
    setIsLoading(true);
    setError("");
    setMessage("");

    Promise.all([
      getLatestScriptVersion(projectId),
      getLatestGenerationPrompt(projectId),
      getGenerationRuntimeLimits(),
    ])
      .then(([scriptState, promptState, runtime]) => {
        if (!active || loadGeneration !== loadGenerationRef.current) {
          return;
        }
        setLimits(runtime);
        setQuantityInput(String(runtime.min_quantity));

        const restoredScript = scriptState.version;
        setScriptVersion(restoredScript);
        const restoredSource = readScriptSource(restoredScript);
        setScriptSource(restoredSource);
        setScriptText(
          readPayloadString(restoredScript, "full_text") ?? originalScript,
        );
        setScriptStale(
          scriptState.stale ||
            (restoredScript !== null &&
              readPayloadString(restoredScript, "shot_card_version_id") !==
                shotCardVersionId),
        );

        const restoredPrompt = promptState.version;
        const restoredPromptText =
          readPayloadString(restoredPrompt, "prompt_text") ?? "";
        setPromptVersion(restoredPrompt);
        setPromptText(restoredPromptText);
        setSavedPromptText(restoredPromptText);
        setPromptStale(
          promptState.stale ||
            (restoredPrompt !== null &&
              !promptMatchesCurrentInputs(restoredPrompt, {
                characterVersionId,
                firstFrameAssetId,
                firstFrameSelectionVersionId,
                referenceSelectionId,
                shotCardVersionId,
              })),
        );
        const restoredDuration = readPayloadNumber(
          restoredPrompt,
          "output_duration_seconds",
        );
        if (restoredDuration !== null) {
          setOutputDuration(String(restoredDuration));
        }
        const restoredResolution = readPayloadString(
          restoredPrompt,
          "resolution",
        );
        if (restoredResolution === "768P" || restoredResolution === "2K") {
          setResolution(restoredResolution);
        }
      })
      .catch((requestError) => {
        if (active) {
          setError(errorMessage(requestError, "读取生成工作流失败。"));
        }
      })
      .finally(() => {
        if (active) {
          setIsLoading(false);
        }
      });

    return () => {
      active = false;
      actionGenerationRef.current += 1;
      isCreatingBatchRef.current = false;
    };
  }, [
    characterVersionId,
    firstFrameAssetId,
    firstFrameSelectionVersionId,
    originalScript,
    projectId,
    referenceSelectionId,
    shotCardVersionId,
  ]);

  const promptStatus = readPayloadString(promptVersion, "status");
  const scriptDirty = Boolean(
    scriptVersion &&
      (scriptSource !== readScriptSource(scriptVersion) ||
        scriptText.trim() !==
          (readPayloadString(scriptVersion, "full_text") ?? "").trim()),
  );
  const promptDirty = Boolean(
    promptVersion && promptText.trim() !== savedPromptText.trim(),
  );
  const quantity = parseQuantity(quantityInput, limits);
  const quantityError = quantityValidationError(quantityInput, limits);
  const duration = Number(outputDuration);
  const durationValid =
    Number.isInteger(duration) && duration >= 4 && duration <= 15;
  const promptParametersMatch = Boolean(
    promptVersion &&
      payloadMatchesOrMissing(
        promptVersion,
        "output_duration_seconds",
        duration,
      ) &&
      payloadMatchesOrMissing(promptVersion, "resolution", resolution),
  );
  const canCompile = Boolean(
    !readOnly &&
      scriptVersion &&
      !scriptStale &&
      !scriptDirty &&
      !promptDirty &&
      !busyAction &&
      durationValid,
  );
  const canCreateBatch = Boolean(
    !readOnly &&
      promptVersion &&
      promptStatus === "LOCKED" &&
      !scriptDirty &&
      !promptStale &&
      !promptDirty &&
      promptParametersMatch &&
      quantity !== null &&
      durationValid &&
      !busyAction,
  );
  const workflowStep =
    scriptVersion && !scriptStale && !scriptDirty
      ? promptVersion &&
        !promptStale &&
        promptStatus === "LOCKED" &&
        promptParametersMatch
        ? 9
        : 8
      : 7;

  useEffect(() => {
    onWorkflowStepChange(workflowStep);
  }, [onWorkflowStepChange, workflowStep]);

  const shotMappings = useMemo(
    () => readShotMappings(scriptVersion),
    [scriptVersion],
  );

  function chooseScriptSource(source: ScriptSource) {
    setScriptSource(source);
    if (source === "original") {
      setScriptText(originalScript);
    }
    setMessage("");
    setError("");
  }

  async function saveScript() {
    const text = scriptText.trim();
    if (!text || readOnly || busyAction) {
      return;
    }
    const actionGeneration = actionGenerationRef.current + 1;
    actionGenerationRef.current = actionGeneration;
    setBusyAction("script");
    setError("");
    setMessage("");
    try {
      const saved = await createScriptVersion(projectId, {
        source: scriptSource,
        text,
        shot_card_version_id: shotCardVersionId,
      });
      if (actionGeneration !== actionGenerationRef.current) {
        return;
      }
      setScriptVersion(saved);
      setScriptStale(false);
      if (promptVersion) {
        setPromptStale(true);
      }
      setMessage(`口播稿已保存为版本 #${saved.version_number}。`);
    } catch (requestError) {
      if (actionGeneration === actionGenerationRef.current) {
        setError(errorMessage(requestError, "保存口播稿失败。"));
      }
    } finally {
      if (actionGeneration === actionGenerationRef.current) {
        setBusyAction(null);
      }
    }
  }

  async function compilePrompt() {
    if (!scriptVersion || !canCompile) {
      return;
    }
    const actionGeneration = actionGenerationRef.current + 1;
    actionGenerationRef.current = actionGeneration;
    setBusyAction("compile");
    setError("");
    setMessage("");
    try {
      const compiled = await compileGenerationPrompt(projectId, {
        script_version_id: scriptVersion.id,
        shot_card_version_id: shotCardVersionId,
        first_frame_asset_id: firstFrameAssetId,
        output_duration_seconds: duration,
        resolution,
      });
      if (actionGeneration !== actionGenerationRef.current) {
        return;
      }
      const compiledText = readPayloadString(compiled, "prompt_text") ?? "";
      setPromptVersion(compiled);
      setPromptText(compiledText);
      setSavedPromptText(compiledText);
      setPromptStale(false);
      setMessage(`H3 Prompt 已编译为版本 #${compiled.version_number}。`);
    } catch (requestError) {
      if (actionGeneration === actionGenerationRef.current) {
        setError(errorMessage(requestError, "编译 H3 Prompt 失败。"));
      }
    } finally {
      if (actionGeneration === actionGenerationRef.current) {
        setBusyAction(null);
      }
    }
  }

  async function savePromptRevision() {
    if (!promptVersion || !promptDirty || readOnly || busyAction) {
      return;
    }
    const actionGeneration = actionGenerationRef.current + 1;
    actionGenerationRef.current = actionGeneration;
    setBusyAction("prompt");
    setError("");
    setMessage("");
    try {
      const revised = await reviseGenerationPrompt(projectId, {
        base_prompt_version_id: promptVersion.id,
        prompt_text: promptText.trim(),
      });
      if (actionGeneration !== actionGenerationRef.current) {
        return;
      }
      const revisedText = readPayloadString(revised, "prompt_text") ?? "";
      setPromptVersion(revised);
      setPromptText(revisedText);
      setSavedPromptText(revisedText);
      setPromptStale(false);
      setMessage(`Prompt 已另存为版本 #${revised.version_number}。`);
    } catch (requestError) {
      if (actionGeneration === actionGenerationRef.current) {
        setError(errorMessage(requestError, "保存 H3 Prompt 失败。"));
      }
    } finally {
      if (actionGeneration === actionGenerationRef.current) {
        setBusyAction(null);
      }
    }
  }

  async function lockPrompt() {
    if (
      !promptVersion ||
      promptDirty ||
      promptStale ||
      readOnly ||
      busyAction
    ) {
      return;
    }
    const actionGeneration = actionGenerationRef.current + 1;
    actionGenerationRef.current = actionGeneration;
    setBusyAction("lock");
    setError("");
    setMessage("");
    try {
      const locked = await lockGenerationPrompt(projectId, promptVersion.id);
      if (actionGeneration !== actionGenerationRef.current) {
        return;
      }
      setPromptVersion(locked);
      setMessage(`Prompt 版本 #${locked.version_number} 已锁定。`);
    } catch (requestError) {
      if (actionGeneration === actionGenerationRef.current) {
        setError(errorMessage(requestError, "锁定 H3 Prompt 失败。"));
      }
    } finally {
      if (actionGeneration === actionGenerationRef.current) {
        setBusyAction(null);
      }
    }
  }

  async function createBatch() {
    if (
      !promptVersion ||
      quantity === null ||
      !canCreateBatch ||
      isCreatingBatchRef.current
    ) {
      return;
    }
    const provider = import.meta.env.DEV ? "fake_h3" : "metaso";
    const request: Omit<GenerationBatchInput, "idempotency_key"> = {
      quantity,
      prompt_version_id: promptVersion.id,
      first_frame_asset_id: firstFrameAssetId,
      output_duration_seconds: duration,
      resolution,
      provider,
      fake_audio_quality: "ok",
    };
    const storageKey = idempotencyStorageKey(projectId);
    const idempotencyRecord = restoreOrCreateIdempotencyRecord(
      storageKey,
      request,
      idempotencyRecordRef.current,
    );
    idempotencyRecordRef.current = idempotencyRecord;
    setRecoveryRecord(idempotencyRecord);
    await submitBatch(idempotencyRecord);
  }

  async function recoverBatch() {
    if (
      !recoveryRecord ||
      readOnly ||
      busyAction ||
      isCreatingBatchRef.current
    ) {
      return;
    }
    idempotencyRecordRef.current = recoveryRecord;
    await submitBatch(recoveryRecord);
  }

  async function submitBatch(idempotencyRecord: IdempotencyRecord) {
    const actionGeneration = actionGenerationRef.current + 1;
    actionGenerationRef.current = actionGeneration;
    isCreatingBatchRef.current = true;
    setBusyAction("batch");
    setError("");
    setMessage("");
    const storageKey = idempotencyStorageKey(projectId);
    try {
      const batch = await createGenerationBatch(
        projectId,
        idempotencyRecord.request,
      );
      if (actionGeneration !== actionGenerationRef.current) {
        return;
      }
      if (
        batch.project_id !== projectId ||
        batch.prompt_version_id !== idempotencyRecord.request.prompt_version_id
      ) {
        setError("服务返回的批次不属于当前项目或 Prompt，请在任务记录中核对。");
        return;
      }
      clearIdempotencyRecord(storageKey, idempotencyRecord);
      idempotencyRecordRef.current = null;
      setRecoveryRecord(null);
      onWorkflowStepChange(10);
      onBatchCreated(batch);
    } catch (requestError) {
      if (actionGeneration === actionGenerationRef.current) {
        setError(errorMessage(requestError, "创建视频生成批次失败。"));
      }
    } finally {
      if (actionGeneration === actionGenerationRef.current) {
        isCreatingBatchRef.current = false;
        setBusyAction(null);
      }
    }
  }

  if (isLoading) {
    return <p className="status-note">正在读取口播稿与 Prompt</p>;
  }

  return (
    <section
      className="generation-composer"
      aria-labelledby="generation-composer-title"
    >
      <div className="section-heading">
        <div>
          <span className="eyebrow">SCRIPT · PROMPT · H3</span>
          <h3 id="generation-composer-title">口播稿与 H3 Prompt</h3>
          <p>每次保存都会创建新版本；锁定后才能创建付费视频任务。</p>
        </div>
      </div>

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
        <p className="attention-banner">镜头卡已变化，请重新保存口播稿。</p>
      ) : null}
      {scriptDirty ? (
        <p className="attention-banner">口播稿有未保存修改，请先保存。</p>
      ) : null}
      {promptStale ? (
        <p className="attention-banner">上游输入已变化，请重新编译 Prompt。</p>
      ) : null}
      {promptVersion && !promptParametersMatch ? (
        <p className="attention-banner">生成参数已变化，请重新编译 Prompt。</p>
      ) : null}
      {error ? <p className="settings-error">{error}</p> : null}
      {message ? <p className="setup-success">{message}</p> : null}

      <fieldset className="generation-block">
        <legend>1. 确认口播稿</legend>
        <div className="generation-choice-row">
          <label>
            <input
              checked={scriptSource === "original"}
              disabled={readOnly || Boolean(busyAction)}
              name="script-source"
              onChange={() => chooseScriptSource("original")}
              type="radio"
            />
            原稿
          </label>
          <label>
            <input
              aria-label="自定义稿"
              checked={scriptSource === "custom"}
              disabled={readOnly || Boolean(busyAction)}
              name="script-source"
              onChange={() => chooseScriptSource("custom")}
              type="radio"
            />
            自定义稿
          </label>
        </div>
        <label className="generation-field">
          <span>口播稿内容</span>
          <textarea
            aria-label="口播稿内容"
            onChange={(event) => setScriptText(event.target.value)}
            readOnly={readOnly || scriptSource === "original"}
            rows={6}
            value={scriptText}
          />
        </label>
        <button
          disabled={readOnly || Boolean(busyAction) || !scriptText.trim()}
          onClick={saveScript}
          type="button"
        >
          {busyAction === "script" ? "正在保存" : "保存口播稿"}
        </button>
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

      <fieldset className="generation-block">
        <legend>2. 编译、修订并锁定 Prompt</legend>
        <div className="generation-parameter-grid">
          <label>
            <span>成片时长（秒）</span>
            <input
              aria-label="成片时长"
              disabled={readOnly || Boolean(busyAction)}
              max="15"
              min="4"
              onChange={(event) => setOutputDuration(event.target.value)}
              step="1"
              type="number"
              value={outputDuration}
            />
          </label>
          <label>
            <span>分辨率</span>
            <select
              aria-label="分辨率"
              disabled={readOnly || Boolean(busyAction)}
              onChange={(event) =>
                setResolution(event.target.value as "768P" | "2K")
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
        <button disabled={!canCompile} onClick={compilePrompt} type="button">
          {busyAction === "compile" ? "正在编译" : "编译 H3 Prompt"}
        </button>
        {promptVersion ? (
          <>
            <label className="generation-field">
              <span>H3 Prompt 内容</span>
              <textarea
                aria-label="H3 Prompt 内容"
                onChange={(event) => setPromptText(event.target.value)}
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
                disabled={
                  readOnly || !promptDirty || Boolean(busyAction) || promptStale
                }
                onClick={savePromptRevision}
                type="button"
              >
                {busyAction === "prompt" ? "正在保存" : "另存 Prompt 新版本"}
              </button>
              <button
                disabled={
                  readOnly ||
                  promptDirty ||
                  promptStale ||
                  Boolean(busyAction) ||
                  promptStatus === "LOCKED" ||
                  promptStatus === "USED"
                }
                onClick={lockPrompt}
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
            disabled={readOnly || Boolean(busyAction)}
            max={limits.max_quantity}
            min={limits.min_quantity}
            onChange={(event) => setQuantityInput(event.target.value)}
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
        <button disabled={!canCreateBatch} onClick={createBatch} type="button">
          {busyAction === "batch"
            ? "正在创建任务"
            : `创建 ${quantity ?? (quantityInput || "0")} 个生成任务`}
        </button>
        {recoveryRecord ? (
          <button
            className="secondary-button"
            disabled={readOnly || Boolean(busyAction)}
            onClick={recoverBatch}
            type="button"
          >
            恢复已提交批次
          </button>
        ) : null}
      </fieldset>
    </section>
  );
}

function readPayloadString(
  version: GenerationVersion | null,
  key: string,
): string | null {
  const value = version?.payload[key];
  return typeof value === "string" ? value : null;
}

function readPayloadNumber(
  version: GenerationVersion | null,
  key: string,
): number | null {
  const value = version?.payload[key];
  return typeof value === "number" ? value : null;
}

function payloadMatchesOrMissing(
  version: GenerationVersion,
  key: string,
  expected: number | string,
): boolean {
  const frozen = version.payload[key];
  return frozen == null || frozen === expected;
}

function readScriptSource(version: GenerationVersion | null): ScriptSource {
  return readPayloadString(version, "source") === "custom"
    ? "custom"
    : "original";
}

function readShotMappings(
  version: GenerationVersion | null,
): Array<{ shotId: string; text: string }> {
  const mappings = version?.payload.shot_mappings;
  if (!Array.isArray(mappings)) {
    return [];
  }
  return mappings.flatMap((mapping) => {
    if (
      typeof mapping !== "object" ||
      mapping === null ||
      !("shot_id" in mapping) ||
      typeof mapping.shot_id !== "string" ||
      !("text" in mapping) ||
      typeof mapping.text !== "string"
    ) {
      return [];
    }
    return [{ shotId: mapping.shot_id, text: mapping.text }];
  });
}

function promptMatchesCurrentInputs(
  prompt: GenerationVersion,
  current: {
    characterVersionId: string | null;
    firstFrameAssetId: string;
    firstFrameSelectionVersionId: string;
    referenceSelectionId: string | null;
    shotCardVersionId: string;
  },
): boolean {
  const checks: Array<[string, string | null]> = [
    ["shot_card_version_id", current.shotCardVersionId],
    ["first_frame_asset_id", current.firstFrameAssetId],
    ["first_frame_selection_version_id", current.firstFrameSelectionVersionId],
    ["character_version_id", current.characterVersionId],
    ["character_reference_selection_id", current.referenceSelectionId],
  ];
  return checks.every(([key, expected]) => {
    const frozen = prompt.payload[key];
    return frozen == null || frozen === expected;
  });
}

function parseQuantity(
  value: string,
  limits: GenerationRuntimeLimits,
): number | null {
  if (!/^\d+$/.test(value)) {
    return null;
  }
  const parsed = Number(value);
  return parsed >= limits.min_quantity && parsed <= limits.max_quantity
    ? parsed
    : null;
}

function quantityValidationError(
  value: string,
  limits: GenerationRuntimeLimits,
): string {
  if (!/^\d+$/.test(value)) {
    return "生成数量必须是整数";
  }
  const parsed = Number(value);
  if (parsed < limits.min_quantity || parsed > limits.max_quantity) {
    return `生成数量必须在 ${limits.min_quantity}–${limits.max_quantity} 之间`;
  }
  return "";
}

type IdempotencyRecord = {
  fingerprint: string;
  key: string;
  request: GenerationBatchInput;
};

function idempotencyStorageKey(projectId: string): string {
  return `generation.idempotency.${projectId}`;
}

function requestFingerprint(
  request: Omit<GenerationBatchInput, "idempotency_key">,
): string {
  return JSON.stringify(request);
}

function restoreIdempotencyRecord(
  storageKey: string,
): IdempotencyRecord | null {
  const memoryRecord = sessionIdempotencyRecords.get(storageKey);
  if (memoryRecord) {
    return memoryRecord;
  }
  try {
    const saved = window.localStorage.getItem(storageKey);
    if (!saved) {
      return null;
    }
    const parsed: unknown = JSON.parse(saved);
    if (isIdempotencyRecord(parsed)) {
      sessionIdempotencyRecords.set(storageKey, parsed);
      return parsed;
    }
  } catch {
    // Recovery also works from the session map when browser storage is blocked.
  }
  return null;
}

function restoreOrCreateIdempotencyRecord(
  storageKey: string,
  request: Omit<GenerationBatchInput, "idempotency_key">,
  memoryRecord: IdempotencyRecord | null,
): IdempotencyRecord {
  const fingerprint = requestFingerprint(request);
  if (memoryRecord?.fingerprint === fingerprint) {
    return memoryRecord;
  }
  const savedRecord = restoreIdempotencyRecord(storageKey);
  if (savedRecord?.fingerprint === fingerprint) {
    return savedRecord;
  }
  const key = createIdempotencyKey();
  const record = {
    fingerprint,
    key,
    request: { ...request, idempotency_key: key },
  };
  sessionIdempotencyRecords.set(storageKey, record);
  try {
    window.localStorage.setItem(storageKey, JSON.stringify(record));
  } catch {
    // Keep the in-memory record so an offline retry still reuses the key.
  }
  return record;
}

function clearIdempotencyRecord(storageKey: string, record: IdempotencyRecord) {
  if (sessionIdempotencyRecords.get(storageKey)?.key === record.key) {
    sessionIdempotencyRecords.delete(storageKey);
  }
  try {
    window.localStorage.removeItem(storageKey);
  } catch {
    // The remote batch is already visible; cleanup must not hide the result.
  }
}

function isIdempotencyRecord(value: unknown): value is IdempotencyRecord {
  if (typeof value !== "object" || value === null) {
    return false;
  }
  const record = value as Partial<IdempotencyRecord>;
  const request = record.request as Partial<GenerationBatchInput> | undefined;
  if (
    typeof record.fingerprint !== "string" ||
    typeof record.key !== "string" ||
    !request ||
    request.idempotency_key !== record.key ||
    typeof request.quantity !== "number" ||
    typeof request.prompt_version_id !== "string" ||
    typeof request.first_frame_asset_id !== "string" ||
    typeof request.output_duration_seconds !== "number" ||
    (request.resolution !== "768P" && request.resolution !== "2K") ||
    (request.provider !== "fake_h3" && request.provider !== "metaso") ||
    (request.fake_audio_quality !== "ok" &&
      request.fake_audio_quality !== "missing")
  ) {
    return false;
  }
  const { idempotency_key: _key, ...requestWithoutKey } = request;
  return (
    record.fingerprint ===
    requestFingerprint(
      requestWithoutKey as Omit<GenerationBatchInput, "idempotency_key">,
    )
  );
}

function createIdempotencyKey(): string {
  if (typeof globalThis.crypto?.randomUUID === "function") {
    return globalThis.crypto.randomUUID();
  }
  return `batch-${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
}

function errorMessage(error: unknown, fallback: string): string {
  return error instanceof Error ? error.message : fallback;
}
