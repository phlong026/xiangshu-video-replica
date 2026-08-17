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

export type ScriptSource = "original" | "custom";

export type GenerationBusyAction =
  | "script"
  | "compile"
  | "prompt"
  | "lock"
  | "batch"
  | null;

export type IdempotencyRecord = {
  fingerprint: string;
  key: string;
  request: GenerationBatchInput;
};

const DEFAULT_LIMITS: GenerationRuntimeLimits = {
  min_quantity: 1,
  max_quantity: 1,
  estimated_cost_per_task: null,
};

const sessionIdempotencyRecords = new Map<string, IdempotencyRecord>();
export const RECOVERY_CONFLICT_MESSAGE =
  "存在待恢复的已提交批次，请先恢复后再更改生成请求。";

type UseGenerationDraftsInput = {
  characterVersionId: string | null;
  currentUserId: string;
  durationSeconds: number;
  // P0-02-03：口播稿区常驻标签页①，无首帧时 Hook 仍需运行（prompt 相关
  // 逻辑容忍 null：无首帧时 prompt 必然 stale、不可编译/建批）。
  firstFrameAssetId: string | null;
  firstFrameSelectionVersionId: string;
  originalScript: string;
  projectId: string;
  readOnly: boolean;
  referenceSelectionId: string | null;
  shotCardVersionId: string;
};

export function useGenerationDrafts({
  characterVersionId,
  currentUserId,
  durationSeconds,
  firstFrameAssetId,
  firstFrameSelectionVersionId,
  originalScript,
  projectId,
  readOnly,
  referenceSelectionId,
  shotCardVersionId,
}: UseGenerationDraftsInput) {
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
  const [busyAction, setBusyAction] = useState<GenerationBusyAction>(null);
  const loadGenerationRef = useRef(0);
  const actionGenerationRef = useRef(0);
  const isCreatingBatchRef = useRef(false);
  const idempotencyRecordRef = useRef<IdempotencyRecord | null>(null);

  useEffect(() => {
    actionGenerationRef.current += 1;
    isCreatingBatchRef.current = false;
    const storageKey = idempotencyStorageKey(currentUserId, projectId);
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
        // 无已保存时长时跟随参考时长（P0-02-03 提升后 Hook 在工作区挂载，
        // durationSeconds 需等拆解加载完成，不能只用 useState 初始值）。
        setOutputDuration(
          String(
            restoredDuration ??
              Math.min(15, Math.max(4, Math.round(durationSeconds))),
          ),
        );
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
    // eslint-disable-next-line react-hooks/exhaustive-deps -- 依赖即草稿状态源；durationSeconds 仅用于成片时长默认值回退
  }, [
    characterVersionId,
    currentUserId,
    durationSeconds,
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
  const provider: GenerationBatchInput["provider"] = import.meta.env.DEV
    ? "fake_h3"
    : "metaso";
  const batchRequest: Omit<GenerationBatchInput, "idempotency_key"> | null =
    promptVersion && quantity !== null && durationValid && firstFrameAssetId
      ? {
          quantity,
          prompt_version_id: promptVersion.id,
          first_frame_asset_id: firstFrameAssetId,
          output_duration_seconds: duration,
          resolution,
          provider,
          fake_audio_quality: "ok",
        }
      : null;
  const recoveryRecordConflicts = Boolean(
    recoveryRecord &&
      batchRequest &&
      recoveryRecord.fingerprint !== requestFingerprint(batchRequest),
  );
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
      !recoveryRecordConflicts &&
      !busyAction,
  );
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
    if (!shotCardVersionId) {
      setError("镜头卡片自动保存后才能保存口播稿。");
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
    if (!scriptVersion || !canCompile || !firstFrameAssetId) {
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

  async function createBatch(onBatchCreated: (batch: GenerationBatch) => void) {
    if (
      !promptVersion ||
      !batchRequest ||
      !canCreateBatch ||
      isCreatingBatchRef.current
    ) {
      return;
    }
    await resolveAndSubmitBatch(batchRequest, onBatchCreated);
  }

  // P0-04-01：手动建批与主按钮流水线共用的幂等提交入口（同一恢复记录、
  // 同一指纹冲突语义，保证两条路径不双轨）。
  async function resolveAndSubmitBatch(
    request: Omit<GenerationBatchInput, "idempotency_key">,
    onBatchCreated: (batch: GenerationBatch) => void,
  ) {
    const storageKey = idempotencyStorageKey(currentUserId, projectId);
    const idempotencyRecord = restoreOrCreateIdempotencyRecord(
      storageKey,
      request,
      idempotencyRecordRef.current,
    );
    if (!idempotencyRecord) {
      const unresolvedRecord =
        idempotencyRecordRef.current ?? restoreIdempotencyRecord(storageKey);
      idempotencyRecordRef.current = unresolvedRecord;
      setRecoveryRecord(unresolvedRecord);
      setError(RECOVERY_CONFLICT_MESSAGE);
      return;
    }
    idempotencyRecordRef.current = idempotencyRecord;
    setRecoveryRecord(idempotencyRecord);
    await submitBatch(idempotencyRecord, onBatchCreated);
  }

  async function recoverBatch(
    onBatchCreated: (batch: GenerationBatch) => void,
  ) {
    if (
      !recoveryRecord ||
      readOnly ||
      busyAction ||
      isCreatingBatchRef.current
    ) {
      return;
    }
    idempotencyRecordRef.current = recoveryRecord;
    await submitBatch(recoveryRecord, onBatchCreated);
  }

  // P0-04-01：主按钮一键流水线——保存脏口播稿 →（需要时）编译 →（需要时）
  // 锁定 → 幂等建批。不复用单步 UI 动作（各自的 busyAction 守卫会互相
  // 短路），直连 API 并用本地变量链接力四步；失败停在对应步并给出可重试
  // 的中文错误，已完成的步骤保留成果（不产生半成品锁定/建批）。
  async function runGenerationPipeline(
    onBatchCreated: (batch: GenerationBatch) => void,
  ) {
    if (readOnly || busyAction || isCreatingBatchRef.current || isLoading) {
      return;
    }
    if (promptDirty) {
      setError("Prompt 存在未保存修订，请先在「生成设置」中保存后再开始生成。");
      return;
    }
    if (!firstFrameAssetId) {
      setError("尚未确认首帧，无法开始生成。请先在「画面与人物」确认首帧。");
      return;
    }
    if (!durationValid) {
      setError("输出时长需为 4-15 的整数，请先在「生成设置」中调整。");
      return;
    }
    if (quantity === null) {
      setError(
        quantityError || "生成数量不在允许范围内，请先在「生成设置」中调整。",
      );
      return;
    }

    const actionGeneration = actionGenerationRef.current + 1;
    actionGenerationRef.current = actionGeneration;
    const isCurrent = () => actionGeneration === actionGenerationRef.current;
    let step = "准备";
    setError("");
    setMessage("");
    try {
      let pipelineScript = scriptVersion;
      let savedScriptThisRun = false;
      if (scriptDirty) {
        const text = scriptText.trim();
        if (!text) {
          setError("口播稿内容为空，请先补写后再开始生成。");
          return;
        }
        step = "保存口播稿";
        setBusyAction("script");
        const saved = await createScriptVersion(projectId, {
          source: scriptSource,
          text,
          shot_card_version_id: shotCardVersionId,
        });
        if (!isCurrent()) {
          return;
        }
        pipelineScript = saved;
        savedScriptThisRun = true;
        setScriptVersion(saved);
        setScriptStale(false);
        // 与手动 saveScript 对齐：新口播稿落库后旧 Prompt 即刻 stale
        // （服务端 SCRIPT_SUPERSEDED），编译失败时不能谎报就绪。
        if (promptVersion) {
          setPromptStale(true);
        }
      }

      let pipelinePrompt = promptVersion;
      // USED（已用于建批）时必须重编译产出新版本——锁定接口对 USED
      // 幂等返回，直接建批必被 409 PROMPT_ALREADY_USED 拒绝且重试死循环。
      const needsCompile =
        !pipelinePrompt ||
        readPayloadString(pipelinePrompt, "status") === "USED" ||
        promptStale ||
        savedScriptThisRun ||
        !promptParametersMatch;
      if (needsCompile) {
        if (!pipelineScript) {
          setError("口播稿尚未保存，无法编译 Prompt。");
          return;
        }
        step = "编译 Prompt";
        setBusyAction("compile");
        const compiled = await compileGenerationPrompt(projectId, {
          script_version_id: pipelineScript.id,
          shot_card_version_id: shotCardVersionId,
          first_frame_asset_id: firstFrameAssetId,
          output_duration_seconds: duration,
          resolution,
        });
        if (!isCurrent()) {
          return;
        }
        pipelinePrompt = compiled;
        const compiledText = readPayloadString(compiled, "prompt_text") ?? "";
        setPromptVersion(compiled);
        setPromptText(compiledText);
        setSavedPromptText(compiledText);
        setPromptStale(false);
      }

      // 正常情况下走到这里 prompt 必非空（未编译 ⇒ 原本存在且参数匹配），
      // 显式守卫仅为收窄类型并防御编译返回空值的异常。
      if (!pipelinePrompt) {
        setError("Prompt 缺失或编译结果为空，无法继续一键生成。可重试。");
        return;
      }

      if (readPayloadString(pipelinePrompt, "status") !== "LOCKED") {
        step = "锁定 Prompt";
        setBusyAction("lock");
        const locked = await lockGenerationPrompt(projectId, pipelinePrompt.id);
        if (!isCurrent()) {
          return;
        }
        pipelinePrompt = locked;
        setPromptVersion(locked);
      }

      step = "创建批次";
      setBusyAction("batch");
      await resolveAndSubmitBatch(
        {
          quantity,
          prompt_version_id: pipelinePrompt.id,
          first_frame_asset_id: firstFrameAssetId,
          output_duration_seconds: duration,
          resolution,
          provider,
          fake_audio_quality: "ok",
        },
        onBatchCreated,
      );
    } catch (requestError) {
      if (isCurrent()) {
        setError(
          errorMessage(requestError, `${step}失败，一键生成已停止，可重试。`),
        );
      }
    } finally {
      // 建批步的 busy/错误由 submitBatch 自管理（它推进 actionGeneration），
      // 此处仅恢复前三步的中断状态。
      if (isCurrent()) {
        setBusyAction(null);
      }
    }
  }

  async function submitBatch(
    idempotencyRecord: IdempotencyRecord,
    onBatchCreated: (batch: GenerationBatch) => void,
  ) {
    const actionGeneration = actionGenerationRef.current + 1;
    actionGenerationRef.current = actionGeneration;
    isCreatingBatchRef.current = true;
    setBusyAction("batch");
    setError("");
    setMessage("");
    const storageKey = idempotencyStorageKey(currentUserId, projectId);
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
      onBatchCreated(batch);
    } catch (requestError) {
      const definitiveRejection = isDefinitiveBatchRejection(requestError);
      if (definitiveRejection) {
        clearIdempotencyRecord(storageKey, idempotencyRecord);
      }
      if (actionGeneration === actionGenerationRef.current) {
        if (
          definitiveRejection &&
          idempotencyRecordRef.current?.key === idempotencyRecord.key
        ) {
          idempotencyRecordRef.current = null;
          setRecoveryRecord(null);
        }
        setError(errorMessage(requestError, "创建视频生成批次失败。"));
      }
    } finally {
      if (actionGeneration === actionGenerationRef.current) {
        isCreatingBatchRef.current = false;
        setBusyAction(null);
      }
    }
  }

  return {
    // script 状态
    scriptVersion,
    scriptSource,
    scriptText,
    scriptStale,
    scriptDirty,
    shotMappings,
    // prompt 状态
    promptVersion,
    promptText,
    savedPromptText,
    promptStale,
    promptDirty,
    promptStatus,
    // 生成参数
    limits,
    quantityInput,
    quantity,
    quantityError,
    outputDuration,
    resolution,
    duration,
    durationValid,
    // 派生与恢复
    canCompile,
    canCreateBatch,
    promptParametersMatch,
    recoveryRecord,
    recoveryRecordConflicts,
    // 加载与反馈
    isLoading,
    error,
    message,
    busyAction,
    // 动作
    chooseScriptSource,
    setScriptText,
    saveScript,
    compilePrompt,
    setPromptText,
    savePromptRevision,
    lockPrompt,
    setQuantityInput,
    setOutputDuration,
    setResolution,
    createBatch,
    recoverBatch,
    runGenerationPipeline,
  };
}

// P0-02-03：状态提升后由 AnalysisWorkspace 持有，注入标签页①的
// ScriptEditor 与标签页③的 GenerationComposer，保证单一状态源。
export type GenerationDrafts = ReturnType<typeof useGenerationDrafts>;

export function readPayloadString(
  version: GenerationVersion | null,
  key: string,
): string | null {
  const value = version?.payload[key];
  return typeof value === "string" ? value : null;
}

export function readPayloadNumber(
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
    firstFrameAssetId: string | null;
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

function idempotencyStorageKey(
  currentUserId: string,
  projectId: string,
): string {
  return `generation.idempotency/${encodeURIComponent(currentUserId)}/${encodeURIComponent(projectId)}`;
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
): IdempotencyRecord | null {
  const fingerprint = requestFingerprint(request);
  if (memoryRecord) {
    return memoryRecord.fingerprint === fingerprint ? memoryRecord : null;
  }
  const savedRecord = restoreIdempotencyRecord(storageKey);
  if (savedRecord) {
    return savedRecord.fingerprint === fingerprint ? savedRecord : null;
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
    const saved = window.localStorage.getItem(storageKey);
    if (!saved) {
      return;
    }
    const parsed: unknown = JSON.parse(saved);
    if (isIdempotencyRecord(parsed) && parsed.key === record.key) {
      window.localStorage.removeItem(storageKey);
    }
  } catch {
    // The remote batch is already visible; cleanup must not hide the result.
  }
}

// 测试专用：清空模块级会话幂等记录，使「重开页面」场景真实模拟刷新
// （模块重载、内存 Map 归零），迫使恢复链走 localStorage 读取→校验→
// 还原路径。运行时代码不应调用。
export function __resetSessionIdempotencyRecordsForTests() {
  sessionIdempotencyRecords.clear();
}

function isDefinitiveBatchRejection(error: unknown): boolean {
  const { status, code } = error as { status?: number; code?: string };
  if (status === 400) {
    return code === "ASSET_PROJECT_MISMATCH";
  }
  if (status === 422) {
    return (
      code === "QUANTITY_EXCEEDS_LIMIT" ||
      code === "METASO_REQUIRES_CLOUD_STORAGE"
    );
  }
  if (status !== 409) {
    return false;
  }
  return (
    code === "PROMPT_STALE" ||
    code === "PROMPT_NOT_LOCKED" ||
    code === "PROMPT_PARAMETERS_MISMATCH" ||
    code === "FIRST_FRAME_CONFIRMATION_REQUIRED" ||
    code === "FIRST_FRAME_PROMPT_MISMATCH" ||
    code === "PROMPT_ALREADY_USED"
  );
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
