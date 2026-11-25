import { type ReactNode, useEffect, useRef, useState } from "react";
import {
  createOralTask,
  createScriptFromAudioTask,
  customerVisibleErrorMessage,
  getLatestScriptFromAudioTask,
  getOralPrice,
  getReplicaPreflight,
  listOralAvatars,
  listOralVoices,
  type OralPrice,
  resolveOralSubmission,
  type ViralImportTask,
} from "../api";
import { isInsufficientCredits } from "../insufficientCredits";
import {
  ReplicaDefaultSummary,
  ReplicaDetailActions,
  replicaPreference,
  saveReplicaPreference,
} from "./AdaptiveReplicaSetup";
import { useStudio } from "./context";
import { awaitScriptFromAudioTask } from "./live";
import {
  isCurrentQuote,
  isQuoteRejection,
  watchQuoteExpiry,
} from "./quoteConfirmation";
import type { ReplicaSource, StudioPerson } from "./types";
import { Button, Field, StudioDialog } from "./ui";
import { viralImportIdempotencyKey } from "./viralImport";

type OralDefaults = { identityId: string; avatarId: string; voiceId: string };
type OralDraft = { choice: OralDefaults; script: string };
type OralRequestSnapshot = OralDefaults & {
  title: string;
  mode: "TTS";
  scriptText: string;
};
type OralAttempt = {
  request: OralRequestSnapshot;
  idempotencyKey: string;
  retried: boolean;
  receipt?: { id: string; status: string };
};
const EMPTY: OralDefaults = { identityId: "", avatarId: "", voiceId: "" };

function readDraft(accountId: string, video: ReplicaSource): OralDraft | null {
  try {
    const raw = replicaPreference(accountId, `oral-draft:${video.id}`);
    if (!raw) return null;
    const draft = JSON.parse(raw) as Partial<OralDraft> | null;
    const choice = draft?.choice;
    if (
      !choice ||
      typeof choice.identityId !== "string" ||
      typeof choice.avatarId !== "string" ||
      typeof choice.voiceId !== "string" ||
      typeof draft.script !== "string"
    )
      return null;
    return {
      choice: {
        identityId: choice.identityId,
        avatarId: choice.avatarId,
        voiceId: choice.voiceId,
      },
      script: draft.script,
    };
  } catch {
    return null;
  }
}

function attemptField(video: ReplicaSource) {
  return `oral-attempt:${video.id}`;
}

function readAttempt(
  accountId: string,
  video: ReplicaSource,
): OralAttempt | null {
  try {
    const raw = replicaPreference(accountId, attemptField(video));
    if (!raw) return null;
    const attempt = JSON.parse(raw) as Partial<OralAttempt> | null;
    const request = attempt?.request;
    if (
      request?.mode !== "TTS" ||
      typeof request.identityId !== "string" ||
      typeof request.avatarId !== "string" ||
      typeof request.voiceId !== "string" ||
      typeof request.title !== "string" ||
      typeof request.scriptText !== "string" ||
      typeof attempt?.idempotencyKey !== "string" ||
      !attempt.idempotencyKey
    )
      return null;
    return {
      request: {
        identityId: request.identityId,
        avatarId: request.avatarId,
        voiceId: request.voiceId,
        title: request.title,
        mode: "TTS",
        scriptText: request.scriptText,
      },
      idempotencyKey: attempt.idempotencyKey,
      retried: attempt.retried === true,
      ...(typeof attempt.receipt?.id === "string" &&
      typeof attempt.receipt.status === "string"
        ? {
            receipt: { id: attempt.receipt.id, status: attempt.receipt.status },
          }
        : {}),
    };
  } catch {
    return null;
  }
}

function saveAttempt(
  accountId: string,
  video: ReplicaSource,
  attempt: OralAttempt | null,
) {
  // 报价仅在当前页面有效；持久化的请求只用于同键恢复，不携带报价令牌。
  return saveReplicaPreference(
    accountId,
    attemptField(video),
    attempt ? JSON.stringify(attempt) : "",
  );
}

function availableAvatars(person: StudioPerson | undefined) {
  return (
    person?.avatars.filter(
      (item) => item.origin === "视频制作" && item.ready,
    ) ?? []
  );
}

function availableVoices(person: StudioPerson | undefined) {
  return (
    person?.voices.filter(
      (item) => item.confirmed && (!item.status || item.status === "READY"),
    ) ?? []
  );
}

function readDefaults(accountId: string): OralDefaults {
  try {
    const raw = replicaPreference(accountId, "oral-defaults");
    if (!raw) return EMPTY;
    const values: unknown = JSON.parse(raw);
    if (!Array.isArray(values) || values.length !== 3) return EMPTY;
    const [identityId, avatarId, voiceId]: unknown[] = values;
    return typeof identityId === "string" &&
      typeof avatarId === "string" &&
      typeof voiceId === "string"
      ? { identityId, avatarId, voiceId }
      : EMPTY;
  } catch {
    return EMPTY;
  }
}

export function OralReplicaControl({
  video,
  requested,
  onRequest,
  onHandled,
  importVideo,
  importError,
  presentation = "default",
  secondaryAction,
}: {
  video: ReplicaSource;
  requested: boolean;
  onRequest: () => void;
  onHandled: () => void;
  importVideo: () => Promise<
    Pick<ViralImportTask, "projectId" | "sourceAssetId"> | undefined
  >;
  importError?: string;
  presentation?: "default" | "detail";
  secondaryAction?: ReactNode;
}) {
  const { user, data, notify, navigate, refresh, openLive } = useStudio();
  const [stage, setStage] = useState<
    "settings" | "prepare" | "compose" | "receipt" | null
  >(null);
  const [editDefaultsOnly, setEditDefaultsOnly] = useState(false);
  const [choice, setChoice] = useState(() => readDefaults(user.id));
  const [script, setScript] = useState("");
  const [quote, setQuote] = useState<OralPrice | null>(null);
  const [transcribeCredits, setTranscribeCredits] = useState<number>();
  const [transcribePriceError, setTranscribePriceError] = useState(false);
  const [transcribePriceAttempt, setTranscribePriceAttempt] = useState(0);
  const durationMs = video.durationMs;
  // biome-ignore lint/correctness/useExhaustiveDependencies: 重新核对入口刷新报价。
  useEffect(() => {
    if (stage !== "prepare" || typeof durationMs !== "number") return;
    let current = true;
    setTranscribeCredits(undefined);
    setTranscribePriceError(false);
    void getReplicaPreflight("oral", durationMs).then(
      ({ transcribeCredits: credits }) => {
        if (current) {
          setTranscribeCredits(credits);
          setTranscribePriceError(credits === undefined);
        }
      },
      () => {
        if (current) setTranscribePriceError(true);
      },
    );
    return () => {
      current = false;
    };
  }, [stage, durationMs, transcribePriceAttempt]);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [preparing, setPreparing] = useState(false);
  const [attempt, setAttempt] = useState(() => readAttempt(user.id, video));
  const pendingAttempt = Boolean(attempt && !attempt.receipt);
  const mounted = useRef(false);
  const locking = useRef(false);
  const person = data.people.find((item) => item.id === choice.identityId);
  const avatar = availableAvatars(person).find(
    (item) => item.id === choice.avatarId,
  );
  const voice = availableVoices(person).find(
    (item) => item.id === choice.voiceId,
  );
  const valid = Boolean(avatar && voice);
  const title =
    (Array.from(video.title.trim()).slice(0, 113).join("") || "参考视频") +
    " · 口播";
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);
  useEffect(() => watchQuoteExpiry(quote, () => setQuote(null)), [quote]);

  const run = async (work: () => Promise<void>) => {
    if (locking.current) return;
    locking.current = true;
    setBusy(true);
    setError("");
    try {
      await work();
    } catch (cause) {
      if (!mounted.current) return;
      setError(customerVisibleErrorMessage(cause, "操作失败，请稍后重试。"));
      if (isQuoteRejection(cause)) setQuote(null);
      if (isInsufficientCredits(cause)) openLive("wallet");
    } finally {
      locking.current = false;
      if (mounted.current) setBusy(false);
    }
  };

  const restoreAttempt = (saved: OralAttempt) => {
    setAttempt(saved);
    setChoice({
      identityId: saved.request.identityId,
      avatarId: saved.request.avatarId,
      voiceId: saved.request.voiceId,
    });
    setScript(saved.request.scriptText);
    setQuote(null);
    setStage(saved.receipt ? "receipt" : "compose");
  };

  const rememberDraft = (text: string, selected: OralDefaults = choice) => {
    const saved = saveReplicaPreference(
      user.id,
      `oral-draft:${video.id}`,
      JSON.stringify({ choice: selected, script: text }),
    );
    if (!saved) {
      setError(
        "文案暂未保存到浏览器，请先复制文案，恢复本地存储后再关闭页面。",
      );
    }
    return saved;
  };

  const enter = useRef(() => {});
  enter.current = () => {
    onHandled();
    if (!stage && !locking.current) {
      setError("");
      setEditDefaultsOnly(false);
      const saved = readAttempt(user.id, video);
      if (saved) {
        restoreAttempt(saved);
        return;
      }
      const draft = readDraft(user.id, video);
      if (draft) {
        setChoice(draft.choice);
        setScript(draft.script);
        setQuote(null);
        setStage("compose");
        return;
      }
      setStage(valid ? "prepare" : "settings");
    }
  };
  useEffect(() => {
    if (requested) enter.current();
  }, [requested]);

  const save = () => {
    if (!valid) return;
    saveReplicaPreference(
      user.id,
      "oral-defaults",
      JSON.stringify([choice.identityId, choice.avatarId, choice.voiceId]),
    );
    setError("");
    const draft = readDraft(user.id, video);
    if (draft) {
      if (!rememberDraft(draft.script)) return;
      setScript(draft.script);
      setQuote(null);
    }
    setStage(editDefaultsOnly ? null : draft ? "compose" : "prepare");
  };

  const quoteScript = async (text: string) => {
    const price = await getOralPrice({ script_text: text });
    if (!mounted.current) return;
    if (!isCurrentQuote(price)) throw new Error("口播报价已失效，请重新获取。");
    setQuote(price);
  };

  const prepare = () =>
    void run(async () => {
      setPreparing(true);
      try {
        // 真实资产状态可能已改变；失效时引导重选，不先花转写积分。
        const [avatars, voices] = await Promise.all([
          listOralAvatars(choice.identityId),
          listOralVoices(choice.identityId),
        ]);
        if (!mounted.current) return;
        if (
          !avatars.some(
            (item) =>
              item.id === choice.avatarId &&
              item.status === "READY" &&
              item.source_kind === "VIDEO",
          ) ||
          !voices.some(
            (item) =>
              item.id === choice.voiceId &&
              item.status === "READY" &&
              item.confirmed &&
              item.demo_asset_id,
          )
        ) {
          setChoice({ ...choice, avatarId: "", voiceId: "" });
          setStage("settings");
          setError("默认分身或声音已不可用，请刷新人物库后重新选择。");
          return;
        }
        const imported = await importVideo();
        if (!mounted.current || !imported?.projectId || !imported.sourceAssetId)
          return;
        const { projectId, sourceAssetId } = imported;
        const latest = await getLatestScriptFromAudioTask(projectId);
        if (!mounted.current) return;
        const matching =
          latest?.source_asset_id === sourceAssetId ? latest : null;
        if (matching?.status === "SUBMISSION_UNCERTAIN") {
          throw new Error(
            "上一次转写结果待核对，请在文案工坊的提取记录中查看，不会自动重复提交。",
          );
        }
        // 导入项目复用后先恢复原转写；明确失败后才为下一次显式重试建新键。
        const task =
          matching && matching.status !== "FAILED"
            ? matching
            : await createScriptFromAudioTask(
                projectId,
                sourceAssetId,
                viralImportIdempotencyKey(
                  user.id,
                  `adaptive-asr:${sourceAssetId}:${matching?.id ?? "first"}`,
                ),
              );
        const result =
          task.status === "SUCCEEDED"
            ? task.result
            : await awaitScriptFromAudioTask(task.id);
        if (!mounted.current) return;
        const text = result?.text?.trim() ?? "";
        setScript(text);
        rememberDraft(text);
        setQuote(null);
        setStage("compose");
        if (!text) {
          setError("没有识别到有效口播，可填写自己的文案或更换视频。");
        } else if (Array.from(text).length <= 10_000) {
          await quoteScript(text);
        }
      } finally {
        if (mounted.current) setPreparing(false);
      }
    });

  const loadQuote = () =>
    void run(async () => {
      const saved = readAttempt(user.id, video);
      if (saved?.receipt) {
        restoreAttempt(saved);
        return;
      }
      if (saved) restoreAttempt(saved);
      await quoteScript(saved?.request.scriptText ?? script.trim());
    });

  const submit = () =>
    void run(async () => {
      const saved = readAttempt(user.id, video);
      if (
        saved?.receipt ||
        (saved && saved.idempotencyKey !== attempt?.idempotencyKey)
      ) {
        restoreAttempt(saved);
        return;
      }
      if (!isCurrentQuote(quote) || (!saved && !valid)) {
        setQuote(null);
        throw new Error("口播报价已失效或默认素材不可用，请重新核对。");
      }
      const submitted: OralAttempt = saved
        ? { ...saved, retried: true }
        : {
            request: {
              ...choice,
              title,
              mode: "TTS",
              scriptText: script.trim(),
            },
            idempotencyKey: crypto.randomUUID(),
            retried: false,
          };
      if (!saveAttempt(user.id, video, submitted)) {
        throw new Error("无法保存任务恢复信息，请检查浏览器本地存储后重试。");
      }
      setAttempt(submitted);
      let result: Awaited<ReturnType<typeof createOralTask>>;
      try {
        result = await createOralTask({
          ...submitted.request,
          idempotencyKey: submitted.idempotencyKey,
          quoteToken: quote.quote_token,
          maxCredits: quote.max_credits,
        });
      } catch (cause) {
        // 同键重试一旦开始，旧页面晚到的拒绝也不能解冻仍在核对的请求。
        const current = readAttempt(user.id, video);
        if (
          !saved &&
          current?.idempotencyKey === submitted.idempotencyKey &&
          !current.retried &&
          !current.receipt &&
          cause &&
          typeof cause === "object" &&
          (("code" in cause && cause.code === "ORAL_REQUEST_INVALID") ||
            isQuoteRejection(cause) ||
            isInsufficientCredits(cause))
        ) {
          saveAttempt(user.id, video, null);
          if (mounted.current) setAttempt(null);
        }
        throw cause;
      }
      // 离页后成功回执仍先保存任务号；只有用户明确新建版本才结束这个幂等周期。
      const current = readAttempt(user.id, video);
      if (current?.idempotencyKey !== submitted.idempotencyKey) return;
      const received = {
        ...submitted,
        receipt: { id: result.id, status: result.status },
      };
      saveAttempt(user.id, video, received);
      if (!mounted.current) return;
      setAttempt(received);
      setStage("receipt");
      setQuote(null);
      refresh();
      if (result.status === "FAILED" || result.status === "CANCELLED") return;
      notify("数字人口播复刻已提交，可在任务中心查看进度。");
      navigate("tasks");
    });

  const resolvePending = () =>
    void run(async () => {
      const pending = readAttempt(user.id, video);
      if (!pending || pending.receipt) return;
      const { task } = await resolveOralSubmission(pending.idempotencyKey);
      const current = readAttempt(user.id, video);
      if (current?.idempotencyKey !== pending.idempotencyKey || current.receipt)
        return;
      const resolved = task ? { ...pending, receipt: task } : null;
      if (!saveAttempt(user.id, video, resolved)) {
        throw new Error("口播提交已核对，但本地草稿状态暂未保存，请重试。");
      }
      if (!mounted.current) return;
      setQuote(null);
      if (resolved) restoreAttempt(resolved);
      else setAttempt(null);
    });

  const close = () => {
    if (presentation === "detail" && stage === "settings" && editDefaultsOnly) {
      setChoice(readDefaults(user.id));
    }
    setStage(null);
  };
  const openDefaultSettings = () => {
    if (presentation === "detail") setChoice(readDefaults(user.id));
    setError("");
    setEditDefaultsOnly(true);
    setStage("settings");
  };
  // 摘要展示已保存的默认素材；弹窗中尚未保存的选择不能冒充默认值。
  const defaults = presentation === "detail" ? readDefaults(user.id) : EMPTY;
  const defaultPerson = data.people.find(
    (item) => item.id === defaults.identityId,
  );
  const defaultAvatar = availableAvatars(defaultPerson).find(
    (item) => item.id === defaults.avatarId,
  );
  const defaultVoice = availableVoices(defaultPerson).find(
    (item) => item.id === defaults.voiceId,
  );
  const defaultName =
    defaultAvatar && defaultVoice
      ? `${defaultAvatar.name} · ${defaultVoice.name}`
      : undefined;
  const primaryAction = (
    <Button variant="primary" disabled={busy} onClick={onRequest}>
      一键复刻 · 数字人口播
    </Button>
  );

  return (
    <>
      {presentation === "detail" ? (
        <ReplicaDetailActions
          secondaryAction={secondaryAction}
          settings={
            <ReplicaDefaultSummary
              label="默认分身和声音"
              name={defaultName}
              imageUrl={defaultPerson?.portrait}
              loading={data.loading}
              unavailable={Boolean(defaults.identityId && !defaultName)}
              disabled={busy || pendingAttempt}
              emptyLabel="选择分身和声音"
              onChoose={openDefaultSettings}
            />
          }
          feeNote="转写可能消耗积分，成片生成前确认费用。"
        >
          {primaryAction}
        </ReplicaDetailActions>
      ) : (
        <>
          {primaryAction}
          <Button
            variant="quiet"
            disabled={busy || pendingAttempt}
            onClick={openDefaultSettings}
          >
            更换默认分身和声音
          </Button>
        </>
      )}
      {stage === "settings" && (
        <StudioDialog title="设置数字人口播默认素材" onClose={close}>
          <Field label="人物">
            <select
              value={choice.identityId}
              onChange={(event) =>
                setChoice({ ...EMPTY, identityId: event.target.value })
              }
            >
              <option value="">请选择</option>
              {data.people.map((item) => (
                <option key={item.id} value={item.id}>
                  {item.name}
                </option>
              ))}
            </select>
          </Field>
          <Field label="视频分身">
            <select
              value={choice.avatarId}
              onChange={(event) =>
                setChoice({ ...choice, avatarId: event.target.value })
              }
            >
              <option value="">请选择</option>
              {availableAvatars(person).map((item) => (
                <option key={item.id} value={item.id}>
                  {item.name}
                </option>
              ))}
            </select>
          </Field>
          <Field label="口播声音">
            <select
              value={choice.voiceId}
              onChange={(event) =>
                setChoice({ ...choice, voiceId: event.target.value })
              }
            >
              <option value="">请选择</option>
              {availableVoices(person).map((item) => (
                <option key={item.id} value={item.id}>
                  {item.name}
                </option>
              ))}
            </select>
          </Field>
          {error && <p role="alert">{error}</p>}
          <p>没有可选素材时，请先到人物库创建视频分身，并试听确认声音。</p>
          <footer className="replica-pipeline-actions">
            <Button variant="outline" onClick={() => navigate("people")}>
              去人物库
            </Button>
            <Button variant="primary" disabled={!valid} onClick={save}>
              {editDefaultsOnly ? "保存默认素材" : "保存默认素材并继续"}
            </Button>
          </footer>
        </StudioDialog>
      )}
      {stage === "prepare" && (
        <StudioDialog title="用数字人口播复刻" onClose={close} canClose={!busy}>
          <p>
            使用「{avatar?.name}」和「{voice?.name}
            」。系统会转写参考视频中的口播，不会复制原作者的声音或画面。
          </p>
          <p>
            {transcribeCredits === undefined
              ? "正在核对转写费用。"
              : `转写预估 ${transcribeCredits} 积分，按实际时长结算，成片费用另行确认。`}
            请确保你有权使用参考内容。
          </p>
          {transcribePriceError && (
            <p role="alert">
              暂时无法读取转写费用。
              <Button
                variant="quiet"
                onClick={() =>
                  setTranscribePriceAttempt((attempt) => attempt + 1)
                }
              >
                重新核价
              </Button>
            </p>
          )}
          {(error || importError) && <p role="alert">{error || importError}</p>}
          {preparing && (
            <p role="status">正在导入并转写，离开后可回到同一视频继续。</p>
          )}
          <footer className="replica-pipeline-actions">
            <Button
              variant="outline"
              disabled={busy}
              onClick={() => setStage("settings")}
            >
              更换素材
            </Button>
            <Button
              variant="primary"
              disabled={busy || !valid || transcribeCredits === undefined}
              onClick={prepare}
            >
              确认并提取口播
            </Button>
          </footer>
        </StudioDialog>
      )}
      {stage === "compose" && (
        <StudioDialog
          title="确认口播文案与费用"
          onClose={close}
          canClose={!busy}
        >
          {pendingAttempt && (
            <p role="status">
              上次提交结果待核对，已保留原请求。文案与素材暂时锁定；继续操作只会恢复同一次任务。
            </p>
          )}
          <Field label="口播文案">
            <textarea
              rows={8}
              value={script}
              disabled={busy || pendingAttempt}
              onChange={(event) => {
                setScript(event.target.value);
                setQuote(null);
                setError("");
                rememberDraft(event.target.value);
              }}
            />
          </Field>
          {Array.from(script.trim()).length > 10_000 && (
            <p role="alert">口播文案超过 1 万字，请精简后再报价。</p>
          )}
          {error && <p role="alert">{error}</p>}
          {pendingAttempt && (
            <Button variant="outline" disabled={busy} onClick={resolvePending}>
              核对旧提交并恢复编辑
            </Button>
          )}
          {!pendingAttempt && (
            <Button
              variant="outline"
              disabled={busy}
              onClick={() => {
                setEditDefaultsOnly(false);
                setStage("settings");
              }}
            >
              更换素材
            </Button>
          )}
          {isCurrentQuote(quote) ? (
            <>
              <p>
                口播生成预估 {quote.estimated_credits}{" "}
                积分，按服务端结算规则计费。
              </p>
              <Button variant="primary" disabled={busy} onClick={submit}>
                {pendingAttempt ? "继续核对原口播任务" : "确认并生成口播"}
              </Button>
            </>
          ) : (
            <Button
              variant="primary"
              disabled={
                busy ||
                !script.trim() ||
                Array.from(script.trim()).length > 10_000
              }
              onClick={loadQuote}
            >
              获取口播报价
            </Button>
          )}
        </StudioDialog>
      )}
      {stage === "receipt" && attempt?.receipt && (
        <StudioDialog title="口播任务已受理" onClose={close}>
          <p>任务编号：{attempt.receipt.id}</p>
          <p>本次请求已保存，回到本视频不会重复创建任务。</p>
          {["FAILED", "CANCELLED"].includes(attempt.receipt.status) && (
            <p role="alert">原任务已结束，请在任务中心核对结果和积分。</p>
          )}
          {error && <p role="alert">{error}</p>}
          <footer className="replica-pipeline-actions">
            <Button variant="primary" onClick={() => navigate("tasks")}>
              查看任务
            </Button>
            <Button
              variant="outline"
              onClick={() => {
                const current = readAttempt(user.id, video);
                if (
                  current &&
                  (!current.receipt ||
                    current.idempotencyKey !== attempt.idempotencyKey)
                ) {
                  restoreAttempt(current);
                  return;
                }
                if (!rememberDraft(script)) return;
                if (!saveAttempt(user.id, video, null)) {
                  setError("无法保存新版本状态，请检查浏览器本地存储后重试。");
                  return;
                }
                setAttempt(null);
                setQuote(null);
                setStage("compose");
              }}
            >
              新建一个口播版本
            </Button>
          </footer>
        </StudioDialog>
      )}
    </>
  );
}
