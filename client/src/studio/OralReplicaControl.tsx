import { useEffect, useRef, useState } from "react";
import {
  createOralTask,
  createScriptFromAudioTask,
  customerVisibleErrorMessage,
  getLatestScriptFromAudioTask,
  getOralPrice,
  listOralAvatars,
  listOralVoices,
  type OralPrice,
  type ViralImportTask,
} from "../api";
import { isInsufficientCredits } from "../insufficientCredits";
import {
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
import type { StudioPerson, StudioVideo } from "./types";
import { Button, Field, StudioDialog } from "./ui";
import {
  clearViralImportIdempotencyKey,
  viralImportIdempotencyKey,
} from "./viralImport";

type OralDefaults = { identityId: string; avatarId: string; voiceId: string };
const EMPTY: OralDefaults = { identityId: "", avatarId: "", voiceId: "" };

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
}: {
  video: StudioVideo;
  requested: boolean;
  onRequest: () => void;
  onHandled: () => void;
  importVideo: () => Promise<ViralImportTask | undefined>;
  importError?: string;
}) {
  const { user, data, notify, navigate, refresh, openLive } = useStudio();
  const [stage, setStage] = useState<"settings" | "prepare" | "compose" | null>(
    null,
  );
  const [editDefaultsOnly, setEditDefaultsOnly] = useState(false);
  const [choice, setChoice] = useState(() => readDefaults(user.id));
  const [script, setScript] = useState("");
  const [quote, setQuote] = useState<OralPrice | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [preparing, setPreparing] = useState(false);
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

  const enter = useRef(() => {});
  enter.current = () => {
    onHandled();
    if (!stage && !locking.current) {
      setError("");
      setEditDefaultsOnly(false);
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
    setStage(editDefaultsOnly ? null : "prepare");
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

  const loadQuote = () => void run(() => quoteScript(script.trim()));

  const submit = () =>
    void run(async () => {
      if (!isCurrentQuote(quote) || !valid) {
        setQuote(null);
        throw new Error("口播报价已失效或默认素材不可用，请重新核对。");
      }
      const request = {
        ...choice,
        title,
        mode: "TTS" as const,
        scriptText: script.trim(),
      };
      // 客户端只保存哈希对应的请求键，不保存用户文案；回包丢失或刷新仍重放原任务。
      const fingerprint = await crypto.subtle.digest(
        "SHA-256",
        new TextEncoder().encode(JSON.stringify(request)),
      );
      if (!mounted.current) return;
      const scope = `adaptive-oral:${video.platformKey}:${video.nativeId}:${Array.from(new Uint8Array(fingerprint)).join(".")}`;
      const key = viralImportIdempotencyKey(user.id, scope);
      const result = await createOralTask({
        ...request,
        idempotencyKey: key,
        quoteToken: quote.quote_token,
        maxCredits: quote.max_credits,
      });
      clearViralImportIdempotencyKey(user.id, scope, key);
      if (!mounted.current) return;
      if (result.status === "FAILED")
        throw new Error("口播任务提交未成功，请核对素材后重试。");
      setStage(null);
      setQuote(null);
      setScript("");
      refresh();
      notify("数字人口播复刻已提交，可在任务中心查看进度。");
      navigate("tasks");
    });

  const close = () => setStage(null);

  return (
    <>
      <Button variant="primary" disabled={busy} onClick={onRequest}>
        一键复刻 · 数字人口播
      </Button>
      <Button
        variant="quiet"
        disabled={busy}
        onClick={() => {
          setError("");
          setEditDefaultsOnly(true);
          setStage("settings");
        }}
      >
        更换默认分身和声音
      </Button>
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
            转写可能消耗积分，成片之前还会另行报价。请确保你有权使用参考内容。
          </p>
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
              disabled={busy || !valid}
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
          <Field label="口播文案">
            <textarea
              rows={8}
              value={script}
              disabled={busy}
              onChange={(event) => {
                setScript(event.target.value);
                setQuote(null);
                setError("");
              }}
            />
          </Field>
          {Array.from(script.trim()).length > 10_000 && (
            <p role="alert">口播文案超过 1 万字，请精简后再报价。</p>
          )}
          {error && <p role="alert">{error}</p>}
          {isCurrentQuote(quote) ? (
            <>
              <p>
                口播生成预估 {quote.estimated_credits}{" "}
                积分，按服务端结算规则计费。
              </p>
              <Button variant="primary" disabled={busy} onClick={submit}>
                确认并生成口播
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
    </>
  );
}
