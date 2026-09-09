import { useEffect, useLayoutEffect, useRef, useState } from "react";
import {
  completeMaterialUpload,
  confirmOralVoice,
  createMaterialUploadIntent,
  createOralAvatarClone,
  createOralConsent,
  createOralVoiceClone,
  customerVisibleErrorMessage,
  refreshOralAvatar,
  refreshOralVoice,
  updateSimpleCharacterProfile,
  uploadMaterial,
} from "../api";
import { useStudio } from "./context";
import {
  loadMorePeople,
  loadPersonAssets,
  readAudioDuration,
  uploadOralAudioMaterial,
  validateOralAudioFile,
} from "./live";
import type { StudioPage, StudioPerson } from "./types";
import { Button, Empty, Field, Hint, Media, Panel, Tabs } from "./ui";
import "./people.css";

const personTabs: Array<{ id: StudioPage; label: string }> = [
  { id: "person-ip", label: "IP 定位" },
  { id: "person-photos", label: "形象照片" },
  { id: "person-avatars", label: "口播分身" },
  { id: "person-voices", label: "声音档案" },
];

const MAX_ORAL_SOURCE_BYTES = 50 * 1024 * 1024;
const ORAL_POLL_INTERVAL_MS = 6_000;
const ORAL_POLL_MAX_ATTEMPTS = 50;

type UploadedOralSource = { assetId: string; fileName: string };
type CloneSubmission = {
  fingerprint: string;
  idempotencyKey: string;
  consentId?: string;
};

const MISSING_ORAL_SOURCE_CODES = new Set([
  "ASSET_NOT_FOUND",
  "MATERIAL_NOT_FOUND",
  "SOURCE_ASSET_NOT_FOUND",
]);

function isMissingOralSource(cause: unknown): boolean {
  if (!cause || typeof cause !== "object") return false;
  const code = (cause as { code?: string }).code?.trim().toUpperCase();
  return MISSING_ORAL_SOURCE_CODES.has(code ?? "");
}

function createCloneIdempotencyKey(kind: "avatar" | "voice") {
  const suffix =
    globalThis.crypto?.randomUUID?.() ??
    `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  return `oral-${kind}-${suffix}`;
}

function validateOralSource(file: File, kind: "video" | "audio") {
  const suffix = file.name.toLowerCase().split(".").pop();
  const allowed = kind === "video" ? ["mp4", "mov"] : ["mp3"];
  if (!suffix || !allowed.includes(suffix)) {
    return kind === "video" ? "仅支持 MP4 或 MOV 视频。" : "仅支持 MP3 音频。";
  }
  if (file.size <= 0) return "上传文件不能为空。";
  if (file.size > MAX_ORAL_SOURCE_BYTES) return "上传文件不能超过 50 MB。";
  return undefined;
}

async function uploadOralSource(
  file: File,
  group: string,
  onProgress: (progress: number) => void,
): Promise<UploadedOralSource> {
  const intent = await createMaterialUploadIntent(file, {
    title: file.name,
    group,
  });
  await uploadMaterial(intent, file, onProgress);
  const material = await completeMaterialUpload(intent.asset_id);
  const assetId = material.asset_id ?? intent.asset_id;
  return { assetId, fileName: file.name };
}

function useOralStatusPolling(
  enabled: boolean,
  ids: string[],
  refreshItem: (id: string) => Promise<unknown>,
  refreshPage: () => void,
  setError: (message: string) => void,
  fallbackError: string,
) {
  const idKey = ids.join("\n");
  useEffect(() => {
    if (!enabled || !idKey) return;
    let active = true;
    let attempts = 0;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const poll = async () => {
      const results = await Promise.allSettled(
        idKey.split("\n").map((id) => refreshItem(id)),
      );
      if (!active) return;
      const rejected = results.find(
        (result): result is PromiseRejectedResult =>
          result.status === "rejected",
      );
      if (rejected) {
        setError(customerVisibleErrorMessage(rejected.reason, fallbackError));
      }
      if (results.some((result) => result.status === "fulfilled"))
        refreshPage();
      attempts += 1;
      if (attempts < ORAL_POLL_MAX_ATTEMPTS) {
        timer = setTimeout(poll, ORAL_POLL_INTERVAL_MS);
      }
    };
    timer = setTimeout(poll, ORAL_POLL_INTERVAL_MS);
    return () => {
      active = false;
      if (timer !== undefined) clearTimeout(timer);
    };
  }, [enabled, fallbackError, idKey, refreshItem, refreshPage, setError]);
}

function selectedPerson(people: StudioPerson[], id?: string) {
  return id ? people.find((person) => person.id === id) : people[0];
}

export function PeoplePage() {
  const { data, navigate, openLive, review, updateData, notify, user } =
    useStudio();
  const readOnly = user.role === "auditor";
  const [roleFilter, setRoleFilter] = useState("全部");
  const [loadingMore, setLoadingMore] = useState(false);
  const people = data.people;
  const visiblePeople =
    roleFilter === "全部"
      ? people
      : people.filter((person) => person.role === roleFilter);
  const peoplePage = data.pagination?.people;
  const loadNextPeoplePage = async () => {
    if (review || loadingMore || !peoplePage?.nextCursor) return;
    setLoadingMore(true);
    try {
      const result = await loadMorePeople(peoplePage.nextCursor);
      updateData((current) => {
        const knownPeople = new Set(current.people.map((person) => person.id));
        const knownAssets = new Set(current.assets.map((asset) => asset.id));
        return {
          ...current,
          people: [
            ...current.people,
            ...result.people.filter((person) => !knownPeople.has(person.id)),
          ],
          assets: [
            ...current.assets,
            ...result.assets.filter((asset) => !knownAssets.has(asset.id)),
          ],
          errors: [...current.errors, ...result.errors],
          pagination: {
            ...current.pagination,
            people: {
              nextCursor: result.nextCursor,
              total: result.total,
            },
          },
        };
      });
    } catch (cause) {
      notify(customerVisibleErrorMessage(cause, "加载更多人物失败，请重试。"));
    } finally {
      setLoadingMore(false);
    }
  };
  return (
    <section className="people-page" aria-label="人物库">
      <div className="people-toolbar">
        <div>
          <h1>人物库</h1>
          <p>统一管理人物定位、形象照片、口播分身与声音</p>
        </div>
        <Button
          aria-label="新增人物"
          disabled={readOnly}
          variant="primary"
          onClick={() => {
            if (!readOnly) openLive("characters");
          }}
        >
          ＋ 新增人物
        </Button>
      </div>
      <div className="people-filters">
        {["全部", "乡墅设计师", "项目负责人", "客户经理"].map((label) => (
          <button
            className={
              label === roleFilter ? "people-filter is-active" : "people-filter"
            }
            key={label}
            type="button"
            onClick={() => setRoleFilter(label)}
          >
            {label}
          </button>
        ))}
      </div>
      {visiblePeople.length === 0 ? (
        <Empty
          title="还没有人物"
          description="从人物库创建第一位乡墅行业 IP。"
          action={
            <Button
              disabled={readOnly}
              variant="primary"
              onClick={() => {
                if (readOnly) return;
                openLive("characters");
              }}
            >
              新增人物
            </Button>
          }
        />
      ) : (
        <div className="people-grid">
          {visiblePeople.map((person) => (
            <PersonCard
              key={person.id}
              person={person}
              onOpen={() =>
                navigate("person-ip", { selectedPersonId: person.id })
              }
            />
          ))}
        </div>
      )}
      {peoplePage && peoplePage.total > people.length ? (
        <div className="people-pagination">
          <span>
            已加载 {people.length} / {peoplePage.total} 位人物
          </span>
          <Button
            disabled={loadingMore || !peoplePage.nextCursor}
            onClick={() => void loadNextPeoplePage()}
          >
            {loadingMore ? "正在加载…" : "加载更多人物"}
          </Button>
        </div>
      ) : null}
      <Hint>
        普通照片用于画面创作；口播需要可用分身，文案模式还需要已确认声音。
      </Hint>
    </section>
  );
}

function PersonCard({
  person,
  onOpen,
}: {
  person: StudioPerson;
  onOpen(): void;
}) {
  const { navigate, review } = useStudio();
  const portrait = person.portrait;
  const voices = person.voices.filter((voice) => voice.confirmed).length;
  const avatars = person.avatars.filter((avatar) => avatar.ready).length;
  const photoCount =
    person.photoCount ?? (review ? person.photoIds.length : undefined);
  return (
    <article className="person-card">
      <Media
        asset={
          portrait
            ? {
                id: `${person.id}-portrait`,
                name: person.name,
                kind: "image",
                url: portrait,
                group: "人物",
                source: "人物库",
                saved: true,
              }
            : undefined
        }
        alt={`${person.name}形象`}
        className="person-card__portrait"
      />
      <div className="person-card__body">
        <h2>{person.name}</h2>
        <p>{person.role}</p>
        <div className="person-card__meta">
          <span>
            {photoCount === undefined
              ? "形象照片数量未知"
              : `✓ 形象照片 ${photoCount} 张`}
          </span>
          <span>✓ 口播分身 {avatars} 个</span>
          <span className={voices ? "" : "is-warning"}>
            {voices ? `✓ 可用声音 ${voices} 个` : "！声音待添加"}
          </span>
        </div>
        <div className="person-card__actions">
          <Button variant="primary" onClick={onOpen}>
            查看人物
          </Button>
          <Button
            variant="outline"
            onClick={() =>
              navigate("person-photos", { selectedPersonId: person.id })
            }
          >
            用于创作
          </Button>
        </div>
      </div>
    </article>
  );
}

export function PersonPage() {
  const { state, data, navigate } = useStudio();
  const person = selectedPerson(data.people, state.selectedPersonId);
  const tab = state.page === "people" ? "person-ip" : state.page;
  if (!person)
    return (
      <Empty title="未选择人物" description="请先从人物库选择一位人物。" />
    );
  return (
    <section className="person-page" aria-label="人物详情">
      <button
        className="people-back"
        type="button"
        onClick={() =>
          navigate(state.returnTo ?? "people", { returnTo: undefined })
        }
      >
        ← 返回{state.returnTo ? "创作" : "人物库"}
      </button>
      <PersonHeader person={person} />
      <Tabs
        items={personTabs}
        value={tab}
        onChange={(next) =>
          navigate(next as StudioPage, { selectedPersonId: person.id })
        }
      />
      <div className="person-content">
        {tab === "person-ip" ? <IpPanel person={person} /> : null}
        {tab === "person-photos" ? <PhotosPanel person={person} /> : null}
        {tab === "person-avatars" ? <AvatarPanel person={person} /> : null}
        {tab === "person-voices" ? <VoicePanel person={person} /> : null}
      </div>
    </section>
  );
}

function PersonHeader({ person }: { person: StudioPerson }) {
  const portrait = person.portrait;
  return (
    <header className="person-header">
      <Media
        asset={
          portrait
            ? {
                id: `${person.id}-portrait`,
                name: person.name,
                kind: "image",
                url: portrait,
                group: "人物",
                source: "人物库",
                saved: true,
              }
            : undefined
        }
        alt={`${person.name}头像`}
        className="person-header__portrait"
      />
      <div>
        <h1>{person.name}</h1>
        <p>{person.role}</p>
        {person.version > 0 ? (
          <span>人物版本 V{person.version} · 乡墅行业 IP</span>
        ) : null}
      </div>
    </header>
  );
}

function IpPanel({ person }: { person: StudioPerson }) {
  const { review, updateData, navigate, notify, patchDraft, user } =
    useStudio();
  const readOnly = user.role === "auditor";
  const [saving, setSaving] = useState(false);
  const [draft, setDraft] = useState(() => ({
    name: person.name,
    role: person.role,
    scope: person.scope,
    audience: person.audience,
    expression: person.expression,
  }));
  const update = (key: keyof typeof draft, value: string) =>
    setDraft((current) => ({ ...current, [key]: value }));
  const save = async () => {
    if (readOnly || saving) return;
    if (review) {
      updateData((data) => ({
        ...data,
        people: data.people.map((item) =>
          item.id === person.id ? { ...item, ...draft } : item,
        ),
      }));
      notify("定位草稿已更新");
      return;
    }
    setSaving(true);
    try {
      await updateSimpleCharacterProfile(person.id, {
        display_name: draft.name,
        role: draft.role,
        service_scope: draft.scope,
        target_audience: draft.audience,
        expression_style: draft.expression,
      });
      updateData((data) => ({
        ...data,
        people: data.people.map((item) =>
          item.id === person.id ? { ...item, ...draft } : item,
        ),
      }));
      notify("IP 定位已保存。");
    } catch (cause) {
      notify(customerVisibleErrorMessage(cause, "IP 定位保存失败"));
    } finally {
      setSaving(false);
    }
  };
  return (
    <div className="ip-layout">
      <Panel>
        <h2>人物定位</h2>
        <Field label="姓名">
          <input
            disabled={readOnly}
            value={draft.name}
            onChange={(event) => update("name", event.target.value)}
          />
        </Field>
        <Field label="身份">
          <input
            disabled={readOnly}
            value={draft.role}
            onChange={(event) => update("role", event.target.value)}
          />
        </Field>
        <Field label="服务范围">
          <input
            disabled={readOnly}
            value={draft.scope}
            onChange={(event) => update("scope", event.target.value)}
          />
        </Field>
        <Field label="目标人群">
          <input
            disabled={readOnly}
            value={draft.audience}
            onChange={(event) => update("audience", event.target.value)}
          />
        </Field>
        <Field label="表达特点">
          <input
            disabled={readOnly}
            value={draft.expression}
            onChange={(event) => update("expression", event.target.value)}
          />
        </Field>
      </Panel>
      <Panel>
        <h2>人物简介预览</h2>
        <p className="ip-preview">
          大家好，我是{draft.name}，一名{draft.role}。我专注于{draft.scope}
          ，服务{draft.audience}，表达风格是{draft.expression}。
        </p>
        <Hint>文案工坊会引用此定位生成更贴合乡墅行业的内容。</Hint>
      </Panel>
      <div className="person-footer">
        <Button
          variant="primary"
          disabled={readOnly || saving}
          onClick={() => void save()}
        >
          {review ? "保存定位草稿" : saving ? "保存中…" : "保存 IP 定位"}
        </Button>
        <Button
          disabled={readOnly}
          variant="outline"
          onClick={() => {
            if (readOnly) return;
            patchDraft({ ipId: person.id });
            navigate("copy", { selectedPersonId: person.id });
          }}
        >
          去文案工坊创作
        </Button>
      </div>
    </div>
  );
}

function PhotosPanel({ person }: { person: StudioPerson }) {
  const {
    openLive,
    navigate,
    patchDraft,
    data,
    notify,
    review,
    updateData,
    user,
  } = useStudio();
  const readOnly = user.role === "auditor";
  const [loadingMore, setLoadingMore] = useState(false);
  const assets = data.assets.filter(
    (asset) => asset.personId === person.id && asset.kind === "image",
  );
  const scenePage = data.pagination?.scenes?.[person.id];
  const loadNextScenes = async () => {
    if (review || loadingMore || !scenePage) return;
    setLoadingMore(true);
    try {
      const result = await loadPersonAssets(person.id, scenePage.loaded);
      updateData((current) => {
        const knownAssets = new Set(current.assets.map((asset) => asset.id));
        const nextAssets = result.assets.filter(
          (asset) => !knownAssets.has(asset.id),
        );
        return {
          ...current,
          assets: [...current.assets, ...nextAssets],
          people: current.people.map((entry) =>
            entry.id === person.id
              ? {
                  ...entry,
                  photoIds: [
                    ...entry.photoIds,
                    ...nextAssets.map((asset) => asset.id),
                  ],
                  photoCount: result.total,
                }
              : entry,
          ),
          errors: [...current.errors, ...result.errors],
          pagination: {
            ...current.pagination,
            scenes: {
              ...current.pagination?.scenes,
              [person.id]: { loaded: result.loaded, total: result.total },
            },
          },
        };
      });
    } catch (cause) {
      notify(customerVisibleErrorMessage(cause, "加载更多场景失败，请重试。"));
    } finally {
      setLoadingMore(false);
    }
  };
  return (
    <div className="photos-panel">
      <div className="panel-heading">
        <div>
          <h2>形象照片</h2>
          <p>五视图是一张合成图；场景形象照按套单独管理。</p>
        </div>
        <div>
          <Button
            disabled={readOnly}
            variant="outline"
            onClick={() => {
              if (readOnly) return;
              notify(`正在打开${person.name}的五视图与场景造型。`);
              navigate("people", { selectedPersonId: person.id });
            }}
          >
            管理形象照
          </Button>
          <Button
            disabled={readOnly}
            variant="primary"
            onClick={() => {
              if (readOnly) return;
              notify(`将在人物管理中为${person.name}选择并生成场景形象照。`);
              navigate("people", { selectedPersonId: person.id });
            }}
          >
            AI 生成场景照
          </Button>
        </div>
      </div>
      <Panel>
        <h3>基础五视图 · 1 张合成图</h3>
        {person.sheetId || assets.find((asset) => asset.composite) ? (
          <Media
            asset={
              assets.find((asset) => asset.composite) ?? {
                id: person.sheetId ?? "sheet",
                name: "五视图合成图",
                kind: "image",
                group: "人物",
                source: "人物库",
                saved: true,
                composite: true,
              }
            }
            alt="五视图合成图"
            className="people-sheet"
          />
        ) : (
          <Empty
            title="暂无五视图合成图"
            description="前往人物管理上传照片创建。"
            action={
              <Button
                disabled={readOnly}
                variant="outline"
                onClick={() => {
                  if (readOnly) return;
                  openLive("characters");
                }}
              >
                打开人物管理
              </Button>
            }
          />
        )}
      </Panel>
      <h3>场景形象照</h3>
      <div className="scene-grid">
        {assets
          .filter((asset) => !asset.composite)
          .map((asset) => (
            <Panel key={asset.id}>
              <Media asset={asset} alt={asset.name} className="scene-image" />
              <strong>{asset.name}</strong>
              <div>
                <Button
                  disabled={readOnly}
                  variant="outline"
                  onClick={() => {
                    if (readOnly) return;
                    patchDraft({ ipId: person.id, imageId: asset.id });
                    navigate("person-avatars", {
                      selectedPersonId: person.id,
                      selectedAssetId: asset.id,
                    });
                  }}
                >
                  制作口播分身
                </Button>
                <Button
                  disabled={readOnly}
                  variant="primary"
                  onClick={() => {
                    if (readOnly) return;
                    patchDraft({ ipId: person.id, imageId: asset.id });
                    navigate("replacement", {
                      selectedPersonId: person.id,
                      selectedAssetId: asset.id,
                    });
                  }}
                >
                  用于人物置换
                </Button>
              </div>
            </Panel>
          ))}
      </div>
      {scenePage && scenePage.loaded < scenePage.total ? (
        <div className="people-pagination">
          <span>
            已加载 {scenePage.loaded} / {scenePage.total} 套场景
          </span>
          <Button disabled={loadingMore} onClick={() => void loadNextScenes()}>
            {loadingMore ? "正在加载…" : "加载更多场景"}
          </Button>
        </div>
      ) : null}
    </div>
  );
}

function AvatarPanel({ person }: { person: StudioPerson }) {
  const {
    state,
    data,
    review,
    openPicker,
    navigate,
    patchDraft,
    notify,
    refresh,
    user,
  } = useStudio();
  const readOnly = user.role === "auditor";
  const [title, setTitle] = useState(`${person.name}照片分身`);
  const [busy, setBusy] = useState(false);
  const [consentedSourceId, setConsentedSourceId] = useState<string>();
  const [error, setError] = useState<string>();
  const [uploadProgress, setUploadProgress] = useState<number>();
  const [uploadedVideo, setUploadedVideo] = useState<UploadedOralSource>();
  const [invalidPhotoSourceKey, setInvalidPhotoSourceKey] = useState<string>();
  const cloneSubmissionRef = useRef<CloneSubmission | undefined>(undefined);
  const ready = person.avatars.filter((avatar) => avatar.ready);
  const pending = person.avatars.filter((avatar) => !avatar.ready);
  const selectedPhotoAsset = data.assets.find(
    (asset) =>
      asset.id === state.draft.imageId &&
      asset.kind === "image" &&
      asset.personId === person.id &&
      !asset.composite,
  );
  const selectedPhotoSourceKey = selectedPhotoAsset
    ? `${person.id}:IMAGE:${selectedPhotoAsset.id}`
    : undefined;
  const sourceAsset =
    invalidPhotoSourceKey === selectedPhotoSourceKey
      ? undefined
      : selectedPhotoAsset;
  const photoSelectionInvalid = Boolean(
    state.draft.imageId && (!selectedPhotoAsset || !sourceAsset),
  );
  const selectedSourceAssetId = sourceAsset?.id;
  const previousSourceAssetId = useRef(selectedSourceAssetId);
  useEffect(() => {
    if (previousSourceAssetId.current === selectedSourceAssetId) return;
    previousSourceAssetId.current = selectedSourceAssetId;
    setUploadedVideo(undefined);
    setConsentedSourceId(undefined);
    cloneSubmissionRef.current = undefined;
  }, [selectedSourceAssetId]);
  useOralStatusPolling(
    !review,
    pending
      .filter(
        (avatar) =>
          avatar.submissionState !== "SUBMISSION_UNKNOWN" &&
          (avatar.status === "PENDING" || avatar.status === "RUNNING"),
      )
      .map((avatar) => avatar.id),
    refreshOralAvatar,
    refresh,
    setError,
    "分身状态自动刷新失败",
  );
  const source = uploadedVideo
    ? {
        id: uploadedVideo.assetId,
        name: uploadedVideo.fileName,
        kind: "VIDEO" as const,
      }
    : sourceAsset
      ? { id: sourceAsset.id, name: sourceAsset.name, kind: "IMAGE" as const }
      : undefined;
  const sourceKey = source
    ? `${person.id}:${source.kind}:${source.id}`
    : undefined;
  const selectionLifecycleKey = uploadedVideo
    ? `${person.id}:VIDEO:${uploadedVideo.assetId}`
    : `${person.id}:IMAGE:${state.draft.imageId ?? ""}`;
  const selectionLifecycleRef = useRef(selectionLifecycleKey);
  const contextVersionRef = useRef(0);
  const mountedRef = useRef(true);
  const activeOperationRef = useRef<
    { contextVersion: number; id: number; selectionKey: string } | undefined
  >(undefined);
  const operationSequenceRef = useRef(0);
  const isCurrentOperation = (
    operation: NonNullable<typeof activeOperationRef.current>,
  ) =>
    mountedRef.current &&
    operation.contextVersion === contextVersionRef.current &&
    operation.selectionKey === selectionLifecycleRef.current &&
    activeOperationRef.current === operation;
  // Invalidate before a parent layout effect can settle an earlier request.
  useLayoutEffect(() => {
    selectionLifecycleRef.current = selectionLifecycleKey;
    contextVersionRef.current += 1;
    activeOperationRef.current = undefined;
    setBusy(false);
  }, [selectionLifecycleKey]);
  useLayoutEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      contextVersionRef.current += 1;
      activeOperationRef.current = undefined;
    };
  }, []);
  const consented = Boolean(source && consentedSourceId === source.id);
  const handleVideoUpload = async (file: File) => {
    if (review || readOnly || busy) return;
    const validationError = validateOralSource(file, "video");
    if (validationError) {
      setError(validationError);
      return;
    }
    setBusy(true);
    setError(undefined);
    setConsentedSourceId(undefined);
    cloneSubmissionRef.current = undefined;
    setTitle((current) =>
      current === `${person.name}照片分身` ? `${person.name}视频分身` : current,
    );
    setUploadProgress(0);
    try {
      setUploadedVideo(
        await uploadOralSource(file, "口播分身素材", setUploadProgress),
      );
    } catch (cause) {
      setError(customerVisibleErrorMessage(cause, "人物视频上传失败"));
    } finally {
      setBusy(false);
      setUploadProgress(undefined);
    }
  };
  const startClone = async () => {
    if (review || readOnly || busy || !source || !consented) return;
    const operation = {
      contextVersion: contextVersionRef.current,
      id: operationSequenceRef.current + 1,
      selectionKey: selectionLifecycleRef.current,
    };
    operationSequenceRef.current = operation.id;
    activeOperationRef.current = operation;
    setBusy(true);
    setError(undefined);
    try {
      const cloneTitle =
        title.trim() ||
        `${person.name}${source.kind === "VIDEO" ? "视频" : "照片"}分身`;
      const fingerprint = JSON.stringify([
        person.id,
        source.id,
        source.kind,
        cloneTitle,
      ]);
      let submission = cloneSubmissionRef.current;
      if (!submission || submission.fingerprint !== fingerprint) {
        submission = {
          fingerprint,
          idempotencyKey: createCloneIdempotencyKey("avatar"),
        };
        cloneSubmissionRef.current = submission;
      }
      if (!submission.consentId) {
        const consent = await createOralConsent({
          identityId: person.id,
          sourceAssetId: source.id,
          purpose: "AVATAR",
        });
        if (!isCurrentOperation(operation)) return;
        submission.consentId = consent.id;
      }
      await createOralAvatarClone({
        identityId: person.id,
        title: cloneTitle,
        sourceAssetId: source.id,
        sourceKind: source.kind,
        consentId: submission.consentId,
        idempotencyKey: submission.idempotencyKey,
      });
      if (!isCurrentOperation(operation)) return;
      notify(
        `${source.kind === "VIDEO" ? "视频" : "照片"}分身已提交，可在本页刷新制作状态。`,
      );
      refresh();
    } catch (cause) {
      if (!isCurrentOperation(operation)) return;
      if (source.kind === "IMAGE" && isMissingOralSource(cause)) {
        setInvalidPhotoSourceKey(sourceKey);
        setConsentedSourceId(undefined);
        cloneSubmissionRef.current = undefined;
        setError(undefined);
        return;
      }
      const message = customerVisibleErrorMessage(cause, "口播分身制作失败");
      setError(message);
      notify(message);
    } finally {
      if (isCurrentOperation(operation)) {
        activeOperationRef.current = undefined;
        setBusy(false);
      }
    }
  };
  const refreshClone = async (avatarId: string) => {
    if (busy) return;
    setBusy(true);
    try {
      await refreshOralAvatar(avatarId);
      refresh();
    } catch (cause) {
      notify(customerVisibleErrorMessage(cause, "口播分身状态刷新失败"));
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="avatar-layout">
      <Panel>
        <h2>可用于数字人口播的分身</h2>
        <div className="avatar-grid">
          {ready.map((avatar) => (
            <article className="avatar-card" key={avatar.id}>
              <Media
                asset={data.assets.find((asset) => asset.id === avatar.imageId)}
                alt={avatar.name}
              />
              <h3>{avatar.name}</h3>
              <p>
                {avatar.origin} · {avatar.duration}
              </p>
              <Button
                disabled={readOnly}
                variant="outline"
                onClick={() => {
                  if (readOnly) return;
                  patchDraft({ ipId: person.id, avatarId: avatar.id });
                  navigate(state.returnTo ?? "oral", {
                    selectedPersonId: person.id,
                  });
                }}
              >
                用于数字人口播
              </Button>
            </article>
          ))}
        </div>
        {ready.length === 0 ? (
          <Empty
            title="暂无可用分身"
            description="请从当前人物的实拍视频或场景形象照制作。"
          />
        ) : null}
        {pending.map((avatar) => (
          <article className="avatar-card" key={avatar.id}>
            <Media
              asset={data.assets.find((asset) => asset.id === avatar.imageId)}
              alt={avatar.name}
            />
            <h3>{avatar.name}</h3>
            <p>
              {avatar.submissionState === "SUBMISSION_UNKNOWN"
                ? "提交结果待人工核对，禁止重复提交"
                : avatar.error || avatar.duration}
            </p>
            {avatar.submissionState !== "SUBMISSION_UNKNOWN" &&
            (avatar.status === "PENDING" || avatar.status === "RUNNING") ? (
              <Button
                variant="outline"
                disabled={busy}
                onClick={() => void refreshClone(avatar.id)}
              >
                刷新制作状态
              </Button>
            ) : null}
          </article>
        ))}
      </Panel>
      <Panel>
        <h2>制作口播分身</h2>
        <p>只使用当前人物的素材，不会新建人物。完成后可直接用于数字人口播。</p>
        <Button
          disabled={readOnly}
          variant="outline"
          onClick={() => {
            if (readOnly) return;
            patchDraft({
              ipId: person.id,
              ...(photoSelectionInvalid ? { imageId: undefined } : {}),
            });
            setInvalidPhotoSourceKey(undefined);
            openPicker("avatar-photo");
          }}
        >
          {photoSelectionInvalid ? "重新选择形象照片" : "用形象照片制作"}
        </Button>
        {review ? (
          <Button
            variant="outline"
            onClick={() => notify("审核模式保留示例素材，不执行真实上传")}
          >
            上传人物视频制作
          </Button>
        ) : (
          <Field label="上传人物视频">
            <input
              accept=".mp4,.mov,video/mp4,video/quicktime"
              aria-label="选择人物视频"
              disabled={readOnly || busy}
              type="file"
              onChange={(event) => {
                const file = event.target.files?.[0];
                if (file) void handleVideoUpload(file);
              }}
            />
          </Field>
        )}
        <Field label="分身名称">
          <input
            disabled={readOnly}
            value={title}
            onChange={(event) => {
              setTitle(event.target.value);
              cloneSubmissionRef.current = undefined;
            }}
          />
        </Field>
        <p>
          {uploadedVideo
            ? `已上传：${uploadedVideo.fileName}`
            : sourceAsset
              ? `已选：${sourceAsset.name}`
              : photoSelectionInvalid
                ? "所选照片已失效或不属于当前人物，请重新选择。"
                : "请先选择当前人物的一张场景形象照。"}
        </p>
        {uploadProgress !== undefined ? <p>上传中 {uploadProgress}%</p> : null}
        {error ? <p role="alert">{error}</p> : null}
        <label>
          <input
            aria-label="确认分身克隆授权"
            checked={consented}
            disabled={review || readOnly || busy || !source}
            type="checkbox"
            onChange={(event) => {
              cloneSubmissionRef.current = undefined;
              setConsentedSourceId(
                event.target.checked ? source?.id : undefined,
              );
            }}
          />
          我确认这是本人素材，或已获得用于数字人分身的明确授权。
        </label>
        <Hint>支持 MP4/MOV，上限 50 MB；分身制作为异步任务。</Hint>
        <Button
          variant="primary"
          disabled={review || readOnly || busy || !source || !consented}
          onClick={() => void startClone()}
        >
          {busy
            ? "处理中…"
            : `开始制作${source?.kind === "VIDEO" ? "视频" : "照片"}分身`}
        </Button>
      </Panel>
    </div>
  );
}

function VoicePanel({ person }: { person: StudioPerson }) {
  const {
    state,
    data,
    review,
    openPicker,
    navigate,
    patchDraft,
    updateData,
    notify,
    refresh,
    user,
  } = useStudio();
  const readOnly = user.role === "auditor";
  const [title, setTitle] = useState(`${person.name}本人音色`);
  const [busy, setBusy] = useState(false);
  const [consentedSourceId, setConsentedSourceId] = useState<string>();
  const [error, setError] = useState<string>();
  const [uploadProgress, setUploadProgress] = useState<number>();
  const [uploadedAudio, setUploadedAudio] = useState<UploadedOralSource>();
  const uploadInputRef = useRef<HTMLInputElement>(null);
  const uploadAbortRef = useRef<AbortController | undefined>(undefined);
  const uploadOperationRef = useRef(0);
  const personContextRef = useRef(person.id);
  const mountedRef = useRef(true);
  const cloneSubmissionRef = useRef<CloneSubmission | undefined>(undefined);
  const sourceAsset = data.assets.find(
    (asset) =>
      asset.id === state.draft.audioId &&
      asset.kind === "audio" &&
      (!asset.allowedUses || asset.allowedUses.includes("voice_clone")),
  );
  const selectedSourceAssetId = sourceAsset?.id;
  const previousSourceAssetId = useRef(selectedSourceAssetId);
  useLayoutEffect(() => {
    personContextRef.current = person.id;
    uploadOperationRef.current += 1;
    uploadAbortRef.current?.abort();
    uploadAbortRef.current = undefined;
    setBusy(false);
    setUploadProgress(undefined);
    setUploadedAudio(undefined);
    setConsentedSourceId(undefined);
    cloneSubmissionRef.current = undefined;
  }, [person.id]);
  useLayoutEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      uploadOperationRef.current += 1;
      uploadAbortRef.current?.abort();
      uploadAbortRef.current = undefined;
    };
  }, []);
  useEffect(() => {
    if (previousSourceAssetId.current === selectedSourceAssetId) return;
    previousSourceAssetId.current = selectedSourceAssetId;
    setUploadedAudio(undefined);
    setConsentedSourceId(undefined);
    cloneSubmissionRef.current = undefined;
  }, [selectedSourceAssetId]);
  useOralStatusPolling(
    !review,
    person.voices
      .filter(
        (voice) =>
          !voice.confirmed &&
          voice.submissionState !== "SUBMISSION_UNKNOWN" &&
          (voice.status === "PENDING" ||
            voice.status === "RUNNING" ||
            (voice.status === "READY" && !voice.url)),
      )
      .map((voice) => voice.id),
    refreshOralVoice,
    refresh,
    setError,
    "声音状态自动刷新失败",
  );
  const source = uploadedAudio
    ? { id: uploadedAudio.assetId, name: uploadedAudio.fileName }
    : sourceAsset
      ? { id: sourceAsset.id, name: sourceAsset.name }
      : undefined;
  const consented = Boolean(source && consentedSourceId === source.id);
  const cancelAudioUpload = () => {
    uploadOperationRef.current += 1;
    uploadAbortRef.current?.abort();
    uploadAbortRef.current = undefined;
    setBusy(false);
    setUploadProgress(undefined);
    if (uploadInputRef.current) uploadInputRef.current.value = "";
  };
  const handleAudioUpload = async (file: File) => {
    if (review || readOnly || busy) return;
    const validationError = validateOralAudioFile(file);
    if (validationError) {
      setError(validationError);
      return;
    }
    const operation = ++uploadOperationRef.current;
    const personId = person.id;
    uploadAbortRef.current?.abort();
    const controller = new AbortController();
    uploadAbortRef.current = controller;
    setBusy(true);
    setError(undefined);
    setConsentedSourceId(undefined);
    cloneSubmissionRef.current = undefined;
    setUploadProgress(0);
    try {
      const duration = await readAudioDuration(file);
      if (
        !mountedRef.current ||
        personId !== personContextRef.current ||
        operation !== uploadOperationRef.current
      )
        return;
      if (duration < 5 || duration > 180) {
        setError("声音克隆样本时长必须为 5–180 秒。");
        return;
      }
      const uploaded = await uploadOralAudioMaterial(
        file,
        "voice_clone",
        duration,
        (progress) => {
          if (
            mountedRef.current &&
            personId === personContextRef.current &&
            operation === uploadOperationRef.current
          )
            setUploadProgress(progress);
        },
        controller.signal,
      );
      if (
        !mountedRef.current ||
        personId !== personContextRef.current ||
        operation !== uploadOperationRef.current
      )
        return;
      setUploadedAudio({ assetId: uploaded.id, fileName: file.name });
    } catch (cause) {
      if (
        !mountedRef.current ||
        personId !== personContextRef.current ||
        operation !== uploadOperationRef.current
      )
        return;
      setError(customerVisibleErrorMessage(cause, "声音样本上传失败"));
    } finally {
      if (
        mountedRef.current &&
        personId === personContextRef.current &&
        operation === uploadOperationRef.current
      ) {
        uploadAbortRef.current = undefined;
        setBusy(false);
        setUploadProgress(undefined);
        if (uploadInputRef.current) uploadInputRef.current.value = "";
      }
    }
  };
  const startClone = async () => {
    if (review || readOnly || busy || !source || !consented) return;
    setBusy(true);
    setError(undefined);
    try {
      const cloneTitle = title.trim() || `${person.name}本人音色`;
      const fingerprint = JSON.stringify([person.id, source.id, cloneTitle]);
      let submission = cloneSubmissionRef.current;
      if (!submission || submission.fingerprint !== fingerprint) {
        submission = {
          fingerprint,
          idempotencyKey: createCloneIdempotencyKey("voice"),
        };
        cloneSubmissionRef.current = submission;
      }
      if (!submission.consentId) {
        const consent = await createOralConsent({
          identityId: person.id,
          sourceAssetId: source.id,
          purpose: "VOICE",
        });
        submission.consentId = consent.id;
      }
      await createOralVoiceClone({
        identityId: person.id,
        title: cloneTitle,
        sourceAssetId: source.id,
        consentId: submission.consentId,
        idempotencyKey: submission.idempotencyKey,
      });
      notify("声音克隆已提交，通常在 10 分钟内完成。");
      refresh();
    } catch (cause) {
      const message = customerVisibleErrorMessage(cause, "声音克隆失败");
      setError(message);
      notify(message);
    } finally {
      setBusy(false);
    }
  };
  const refreshClone = async (voiceId: string) => {
    if (busy) return;
    setBusy(true);
    try {
      await refreshOralVoice(voiceId);
      refresh();
    } catch (cause) {
      notify(customerVisibleErrorMessage(cause, "声音克隆状态刷新失败"));
    } finally {
      setBusy(false);
    }
  };
  const confirmVoice = async (voiceId: string) => {
    if (readOnly || busy) return;
    setBusy(true);
    setError(undefined);
    try {
      if (!review) await confirmOralVoice(voiceId);
      if (review) {
        updateData((data) => ({
          ...data,
          people: data.people.map((item) =>
            item.id !== person.id
              ? item
              : {
                  ...item,
                  voices: item.voices.map((candidate) =>
                    candidate.id === voiceId
                      ? { ...candidate, confirmed: true }
                      : candidate,
                  ),
                },
          ),
        }));
      } else {
        refresh();
      }
      patchDraft({ ipId: person.id, voiceId });
      navigate(state.returnTo ?? "oral", {
        selectedPersonId: person.id,
        returnTo: undefined,
      });
    } catch (cause) {
      const message = customerVisibleErrorMessage(cause, "声音确认失败");
      setError(message);
      notify(message);
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="voice-layout">
      <Panel>
        <h2>我的声音</h2>
        {person.voices.map((voice) => (
          <article
            className={
              voice.confirmed ? "voice-card is-confirmed" : "voice-card"
            }
            key={voice.id}
          >
            <div>
              <h3>{voice.name}</h3>
              <p>
                {voice.confirmed
                  ? "已确认，可用于文案口播"
                  : voice.submissionState === "SUBMISSION_UNKNOWN"
                    ? "提交结果待人工核对，禁止重复提交"
                    : voice.status === "FAILED"
                      ? voice.error || "克隆失败，请更换样本重试"
                      : voice.status === "PENDING" || voice.status === "RUNNING"
                        ? "声音克隆中，暂不可选用"
                        : voice.status === "READY" && !voice.url
                          ? "试听样例归档中"
                          : "待试听确认，暂不可选用"}
              </p>
            </div>
            <div>
              {voice.url && (voice.confirmed || voice.status === "READY") ? (
                <audio
                  controls
                  preload="none"
                  src={voice.url}
                  aria-label={`${voice.name}试听`}
                >
                  <track kind="captions" label="声音样本" />
                </audio>
              ) : voice.status === "READY" ? null : (
                <Button
                  variant="outline"
                  onClick={() => notify("该声音暂无可播放样本")}
                >
                  试听
                </Button>
              )}
              {voice.confirmed ? (
                <Button
                  disabled={readOnly}
                  variant="primary"
                  onClick={() => {
                    if (readOnly) return;
                    patchDraft({ ipId: person.id, voiceId: voice.id });
                    navigate(state.returnTo ?? "oral", {
                      selectedPersonId: person.id,
                      returnTo: undefined,
                    });
                  }}
                >
                  使用此声音
                </Button>
              ) : null}
              {!voice.confirmed ? (
                voice.submissionState ===
                "SUBMISSION_UNKNOWN" ? null : voice.status === "PENDING" ||
                  voice.status === "RUNNING" ? (
                  <Button
                    variant="outline"
                    disabled={busy}
                    onClick={() => void refreshClone(voice.id)}
                  >
                    刷新克隆状态
                  </Button>
                ) : (voice.status === "READY" && Boolean(voice.url)) ||
                  (review && !voice.status) ? (
                  <Button
                    variant="primary"
                    disabled={readOnly || busy}
                    onClick={() => void confirmVoice(voice.id)}
                  >
                    确认使用此声音
                  </Button>
                ) : null
              ) : null}
            </div>
          </article>
        ))}
        {person.voices.length === 0 ? (
          <Empty
            title="暂无声音档案"
            description="上传本人或已授权的声音样本创建声音版本。"
          />
        ) : null}
      </Panel>
      <Panel>
        <h2>克隆声音</h2>
        <p>仅使用本人或已获授权的声音样本，建议 5–180 秒（3 分钟）清晰干声。</p>
        <Button
          disabled={readOnly}
          variant="outline"
          onClick={() => {
            if (readOnly) return;
            patchDraft({ ipId: person.id });
            openPicker("voice-audio");
          }}
        >
          从素材选择声音样本
        </Button>
        {review ? null : (
          <Field label="上传声音样本">
            <input
              ref={uploadInputRef}
              accept=".mp3,audio/mpeg"
              aria-label="选择声音样本"
              disabled={readOnly || busy}
              type="file"
              onChange={(event) => {
                const file = event.target.files?.[0];
                if (file) void handleAudioUpload(file);
              }}
            />
          </Field>
        )}
        <Field label="声音名称">
          <input
            disabled={readOnly}
            value={title}
            onChange={(event) => {
              setTitle(event.target.value);
              cloneSubmissionRef.current = undefined;
            }}
            placeholder="例如：张工本人音色 V2"
          />
        </Field>
        <p>
          {uploadedAudio
            ? `已上传：${uploadedAudio.fileName}`
            : sourceAsset
              ? `已选：${sourceAsset.name}`
              : "请先选择声音样本。"}
        </p>
        {uploadProgress !== undefined ? <p>上传中 {uploadProgress}%</p> : null}
        {uploadProgress !== undefined ? (
          <Button variant="outline" onClick={cancelAudioUpload}>
            取消上传
          </Button>
        ) : null}
        {error ? <p role="alert">{error}</p> : null}
        <label>
          <input
            aria-label="确认声音克隆授权"
            checked={consented}
            disabled={review || readOnly || busy}
            type="checkbox"
            onChange={(event) => {
              cloneSubmissionRef.current = undefined;
              setConsentedSourceId(
                event.target.checked ? source?.id : undefined,
              );
            }}
          />
          我确认这是本人声音，或已获得用于声音克隆的明确授权。
        </label>
        <Button
          variant="primary"
          disabled={review || readOnly || busy || !source || !consented}
          onClick={() => void startClone()}
        >
          {busy ? "提交中…" : "开始克隆声音"}
        </Button>
        <Hint>
          仅支持 MP3，时长 5–180 秒（3 分钟），上限 50
          MB；克隆任务是异步的，只有已就绪且确认的声音可用于口播。
        </Hint>
      </Panel>
    </div>
  );
}
