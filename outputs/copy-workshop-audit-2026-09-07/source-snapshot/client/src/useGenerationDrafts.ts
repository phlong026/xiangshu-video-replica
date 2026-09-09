import { useEffect, useMemo, useRef, useState } from "react";

import {
  applySavedGenerationPrompt,
  compileGenerationPrompt,
  createGenerationBatch,
  createScriptVersion,
  defaultBatchProvider,
  type GenerationBatch,
  type GenerationBatchInput,
  type GenerationPriceQuote,
  type GenerationRatio,
  type GenerationRuntimeLimits,
  type GenerationVersion,
  getGenerationPriceQuote,
  getGenerationRuntimeLimits,
  getLatestGenerationPrompt,
  getLatestScriptRewriteTask,
  getLatestScriptVersion,
  listSavedGenerationPrompts,
  lockGenerationPrompt,
  reviseGenerationPrompt,
  rewriteProjectScript,
  type ScriptRewriteTask,
  saveGenerationPrompt,
  waitForScriptRewriteTask,
} from "./api";

export type ScriptSource = "original" | "custom";

export type GenerationBusyAction =
  | "script"
  | "rewrite"
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
  identityId?: string | null;
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
  identityId,
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
    String(normalizeDurationOption(durationSeconds)),
  );
  const [resolution, setResolution] = useState<"768P" | "2K">("768P");
  const [ratio, setRatio] = useState<GenerationRatio>("adaptive");
  const [priceQuote, setPriceQuote] = useState<GenerationPriceQuote | null>(
    null,
  );
  const [savedPrompts, setSavedPrompts] = useState<GenerationVersion[]>([]);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [isLoading, setIsLoading] = useState(true);
  const [recoveryRecord, setRecoveryRecord] =
    useState<IdempotencyRecord | null>(null);
  const [busyAction, setBusyAction] = useState<GenerationBusyAction>(null);
  const loadGenerationRef = useRef(0);
  const actionGenerationRef = useRef(0);
  const identityIdRef = useRef(identityId);
  identityIdRef.current = identityId;
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
      getLatestScriptRewriteTask(projectId, identityId),
    ])
      .then(([scriptState, promptState, runtime, latestRewriteTask]) => {
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
          String(normalizeDurationOption(restoredDuration ?? durationSeconds)),
        );
        const restoredResolution = readPayloadString(
          restoredPrompt,
          "resolution",
        );
        if (restoredResolution === "768P" || restoredResolution === "2K") {
          setResolution(restoredResolution);
        }
        const restoredRatio = readPayloadString(restoredPrompt, "ratio");
        if (isGenerationRatio(restoredRatio)) {
          setRatio(restoredRatio);
        }

        if (
          latestRewriteTask &&
          rewriteTaskMatchesIdentity(latestRewriteTask, identityId) &&
          shouldRecoverScriptRewrite(latestRewriteTask, restoredScript)
        ) {
          if (latestRewriteTask.status === "SUCCEEDED") {
            applyRecoveredScriptRewrite(
              latestRewriteTask,
              identityId,
              setScriptSource,
              setScriptText,
              setMessage,
              setError,
            );
          } else if (
            latestRewriteTask.status === "FAILED" ||
            latestRewriteTask.status === "SUBMISSION_UNCERTAIN"
          ) {
            setError(
              latestRewriteTask.error_message ||
                (latestRewriteTask.status === "SUBMISSION_UNCERTAIN"
                  ? "AI 改写提交状态不确定，请确认服务商记录后再重试。"
                  : "AI 改写失败，请重新提交。"),
            );
          } else {
            setMessage(rewriteRunningMessage(latestRewriteTask, identityId));
            void waitForScriptRewriteTask(latestRewriteTask.id)
              .then((completedTask) => {
                if (
                  active &&
                  loadGeneration === loadGenerationRef.current &&
                  rewriteTaskMatchesIdentity(completedTask, identityId)
                ) {
                  applyRecoveredScriptRewrite(
                    completedTask,
                    identityId,
                    setScriptSource,
                    setScriptText,
                    setMessage,
                    setError,
                  );
                }
              })
              .catch((requestError) => {
                if (active && loadGeneration === loadGenerationRef.current) {
                  setError(errorMessage(requestError, "AI 改写失败。"));
                  setMessage("");
                }
              });
          }
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
    identityId,
    originalScript,
    projectId,
    referenceSelectionId,
    shotCardVersionId,
  ]);

  useEffect(() => {
    let active = true;
    if (typeof listSavedGenerationPrompts !== "function") {
      return;
    }
    listSavedGenerationPrompts(projectId)
      .then((items) => {
        if (active) setSavedPrompts(items);
      })
      .catch((requestError: unknown) => {
        if (active) {
          setSavedPrompts([]);
          setError(errorMessage(requestError, "读取我的提示词失败。"));
        }
      });
    return () => {
      active = false;
    };
  }, [projectId]);

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
  const durationValid = duration === 4 || duration === 15;
  const provider = defaultBatchProvider();
  const batchRequest: Omit<GenerationBatchInput, "idempotency_key"> | null =
    promptVersion && quantity !== null && durationValid && firstFrameAssetId
      ? {
          quantity,
          prompt_version_id: promptVersion.id,
          first_frame_asset_id: firstFrameAssetId,
          output_duration_seconds: duration,
          resolution,
          ratio,
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
      payloadMatchesOrMissing(promptVersion, "resolution", resolution) &&
      payloadMatchesOrMissing(promptVersion, "ratio", ratio),
  );

  useEffect(() => {
    let active = true;
    if (
      typeof getGenerationPriceQuote !== "function" ||
      !durationValid ||
      quantity === null ||
      !isCustomerQuantity(quantity)
    ) {
      setPriceQuote(null);
      return;
    }
    setPriceQuote(null);
    getGenerationPriceQuote({
      resolution,
      duration_seconds: duration as 4 | 15,
      quantity,
    })
      .then((quote) => {
        if (active) setPriceQuote(quote);
      })
      .catch(() => {
        if (active) setPriceQuote(null);
      });
    return () => {
      active = false;
    };
  }, [duration, durationValid, quantity, resolution]);
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

  // 需求：AI 改写（DeepSeek 二创口播稿）——把当前口播稿交给后台改写，
  // 结果作为自定义稿回填编辑框，需用户确认后手动保存，不自动落库。
  async function rewriteScriptWithAi() {
    const text = scriptText.trim();
    if (!text || readOnly || busyAction) {
      return;
    }
    const actionGeneration = actionGenerationRef.current + 1;
    actionGenerationRef.current = actionGeneration;
    setBusyAction("rewrite");
    setError("");
    setMessage("");
    try {
      const task = identityId
        ? await rewriteProjectScript(projectId, text, identityId)
        : await rewriteProjectScript(projectId, text);
      if (
        actionGeneration !== actionGenerationRef.current ||
        !sameIdentity(identityIdRef.current, identityId)
      ) {
        return;
      }
      if (!rewriteTaskMatchesIdentity(task, identityId)) {
        setError("改写任务的人物与当前选择不一致，已停止回填。");
        return;
      }
      // 任务已持久化后立即释放页面级 busy；Provider 调用由 Worker 完成，
      // 不应再阻止切换标签、项目或页面。
      setBusyAction(null);
      setMessage(rewriteRunningMessage(task, identityId));
      const completedTask = await waitForScriptRewriteTask(task.id);
      if (
        actionGeneration !== actionGenerationRef.current ||
        !sameIdentity(identityIdRef.current, identityId) ||
        !rewriteTaskMatchesIdentity(completedTask, identityId)
      ) {
        return;
      }
      applyRecoveredScriptRewrite(
        completedTask,
        identityId,
        setScriptSource,
        setScriptText,
        setMessage,
        setError,
      );
    } catch (requestError) {
      if (
        actionGeneration === actionGenerationRef.current &&
        sameIdentity(identityIdRef.current, identityId)
      ) {
        setError(errorMessage(requestError, "AI 改写失败。"));
      }
    } finally {
      if (
        actionGeneration === actionGenerationRef.current &&
        sameIdentity(identityIdRef.current, identityId)
      ) {
        setBusyAction(null);
      }
    }
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
        ratio,
      });
      if (actionGeneration !== actionGenerationRef.current) {
        return;
      }
      const compiledText = readPayloadString(compiled, "prompt_text") ?? "";
      setPromptVersion(compiled);
      setPromptText(compiledText);
      setSavedPromptText(compiledText);
      setPromptStale(false);
      setMessage(`视频生成提示词已编译为版本 #${compiled.version_number}。`);
    } catch (requestError) {
      if (actionGeneration === actionGenerationRef.current) {
        setError(errorMessage(requestError, "编译视频生成提示词失败。"));
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
      try {
        const saved = await saveGenerationPrompt(projectId, {
          name: `我的提示词 ${new Date().toLocaleString("zh-CN")}`,
          prompt_text: revisedText,
          base_prompt_version_id: revised.id,
        });
        setSavedPrompts((current) => [saved, ...current]);
        setMessage(
          `Prompt 已另存为版本 #${revised.version_number}，并加入我的提示词。`,
        );
      } catch (libraryError) {
        setMessage(
          `Prompt 已另存为版本 #${revised.version_number}；${errorMessage(libraryError, "加入我的提示词失败。")}`,
        );
      }
    } catch (requestError) {
      if (actionGeneration === actionGenerationRef.current) {
        setError(errorMessage(requestError, "保存视频生成提示词失败。"));
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
        setError(errorMessage(requestError, "锁定视频生成提示词失败。"));
      }
    } finally {
      if (actionGeneration === actionGenerationRef.current) {
        setBusyAction(null);
      }
    }
  }

  async function applySavedPrompt(savedPromptId: string) {
    if (!promptVersion || readOnly || busyAction) return;
    setBusyAction("prompt");
    setError("");
    try {
      const applied = await applySavedGenerationPrompt(
        projectId,
        savedPromptId,
        promptVersion.id,
      );
      const text = readPayloadString(applied, "prompt_text") ?? "";
      setPromptVersion(applied);
      setPromptText(text);
      setSavedPromptText(text);
      setPromptStale(false);
      setMessage("已将我的提示词应用到本次生成。");
    } catch (requestError) {
      setError(errorMessage(requestError, "应用我的提示词失败。"));
    } finally {
      setBusyAction(null);
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
          ratio,
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
          ratio,
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
    ratio,
    priceQuote,
    savedPrompts,
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
    rewriteScriptWithAi,
    saveScript,
    compilePrompt,
    setPromptText,
    savePromptRevision,
    lockPrompt,
    setQuantityInput,
    setOutputDuration,
    setResolution,
    setRatio,
    applySavedPrompt,
    createBatch,
    recoverBatch,
    runGenerationPipeline,
  };
}

function shouldRecoverScriptRewrite(
  task: ScriptRewriteTask,
  savedScript: GenerationVersion | null,
): boolean {
  if (!savedScript || !task.completed_at) {
    return true;
  }
  if (
    task.result?.rewritten_text.trim() ===
    (readPayloadString(savedScript, "full_text") ?? "").trim()
  ) {
    return false;
  }
  return timestampMs(task.completed_at) >= timestampMs(savedScript.created_at);
}

function timestampMs(value: string): number {
  const normalized = value.includes("T")
    ? value
    : `${value.replace(" ", "T")}Z`;
  const parsed = Date.parse(normalized);
  return Number.isNaN(parsed) ? 0 : parsed;
}

function applyRecoveredScriptRewrite(
  task: ScriptRewriteTask,
  expectedIdentityId: string | null | undefined,
  setScriptSource: (source: ScriptSource) => void,
  setScriptText: (text: string) => void,
  setMessage: (message: string) => void,
  setError: (message: string) => void,
) {
  if (!task.result) {
    setError("AI 改写已完成，但结果暂不可用，请刷新后重试。");
    setMessage("");
    return;
  }
  setScriptSource("custom");
  setScriptText(task.result.rewritten_text);
  setError("");
  const identity = rewriteIdentityLabel(task, expectedIdentityId);
  setMessage(
    `AI 改写完成${identity ? `（人物：${identity}）` : ""}，请确认后点击「保存口播稿」存为二创稿。`,
  );
}

function rewriteTaskMatchesIdentity(
  task: ScriptRewriteTask,
  identityId: string | null | undefined,
): boolean {
  return sameIdentity(task.identity_id, identityId);
}

function sameIdentity(
  left: string | null | undefined,
  right: string | null | undefined,
): boolean {
  return (left ?? null) === (right ?? null);
}

function rewriteIdentityLabel(
  task: ScriptRewriteTask,
  identityId: string | null | undefined,
): string | undefined {
  return (
    task.ip_profile_snapshot?.display_name ||
    task.identity_id ||
    identityId ||
    undefined
  );
}

function rewriteRunningMessage(
  task: ScriptRewriteTask,
  identityId: string | null | undefined,
): string {
  const identity = rewriteIdentityLabel(task, identityId);
  return `AI 改写正在后台执行${identity ? `（人物：${identity}）` : ""}，可离开本页继续其他操作。`;
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
  return parsed >= limits.min_quantity &&
    parsed <= limits.max_quantity &&
    isCustomerQuantity(parsed)
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
  if (!isCustomerQuantity(parsed)) {
    return "生成数量请选择 1、2 或 4";
  }
  return "";
}

function isCustomerQuantity(value: number): value is 1 | 2 | 4 {
  return value === 1 || value === 2 || value === 4;
}

function normalizeDurationOption(value: number): 4 | 15 {
  return value <= 9 ? 4 : 15;
}

function isGenerationRatio(value: string | null): value is GenerationRatio {
  return (
    value === "adaptive" ||
    value === "21:9" ||
    value === "16:9" ||
    value === "4:3" ||
    value === "1:1" ||
    value === "3:4" ||
    value === "9:16"
  );
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
