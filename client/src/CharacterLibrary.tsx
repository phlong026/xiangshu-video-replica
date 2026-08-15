import {
  type ChangeEvent,
  type FormEvent,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";

import {
  type CharacterAsset,
  type CharacterGenerationTask,
  type CharacterPersona,
  type CharacterPersonaInput,
  type CharacterVersion,
  type CompletedIdentitySource,
  completeIdentityAuthorizationUpload,
  completeIdentitySourceUpload,
  createCharacterPersona,
  createCharacterVersion,
  createIdentityUploadIntent,
  createPersonIdentity,
  generateCharacterAssets,
  getAssetDownloadUrl,
  listCharacterAssets,
  listCharacterGenerationTasks,
  listCharacterPersonas,
  listCharacterVersions,
  listPersonIdentities,
  type PersonIdentity,
  publishCharacterVersion,
  type RequiredCharacterViewType,
  regenerateCharacterAsset,
  reviewCharacterAsset,
  type UserRole,
  updateCharacterPersona,
  uploadIdentityAsset,
} from "./api";

const REQUIRED_VIEWS: ReadonlyArray<{
  type: RequiredCharacterViewType;
  label: string;
}> = [
  { type: "FRONT_FACE", label: "正面头像" },
  { type: "FRONT_HALF", label: "正面半身" },
  { type: "FRONT_FULL", label: "正面全身" },
  { type: "LEFT_45", label: "左 45°" },
  { type: "RIGHT_45", label: "右 45°" },
  { type: "LEFT_SIDE", label: "左侧面" },
  { type: "RIGHT_SIDE", label: "右侧面" },
];

const TERMINAL_GENERATION_STATUSES = new Set(["SUCCEEDED", "FAILED"]);
const CHARACTER_POLL_INTERVAL_MS = 2_000;
const GENERATION_SUBMISSION_STORAGE_PREFIX =
  "character.generate-all.idempotency";

export function CharacterLibrary({ userRole }: { userRole: UserRole }) {
  const isAdmin = userRole === "admin";
  const [identities, setIdentities] = useState<PersonIdentity[]>([]);
  const [selectedIdentityId, setSelectedIdentityId] = useState("");
  const [personas, setPersonas] = useState<CharacterPersona[]>([]);
  const [selectedPersonaId, setSelectedPersonaId] = useState("");
  const [versions, setVersions] = useState<CharacterVersion[]>([]);
  const [selectedVersionId, setSelectedVersionId] = useState("");
  const [assets, setAssets] = useState<CharacterAsset[]>([]);
  const [generationTasks, setGenerationTasks] = useState<
    CharacterGenerationTask[]
  >([]);
  const [selectedAssets, setSelectedAssets] = useState<
    Partial<Record<RequiredCharacterViewType, string>>
  >({});
  const [reviewComments, setReviewComments] = useState<Record<string, string>>(
    {},
  );
  const [isLoading, setIsLoading] = useState(true);
  const [isVersionLoading, setIsVersionLoading] = useState(false);
  const [busyAction, setBusyAction] = useState("");
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [identityWizardIdentity, setIdentityWizardIdentity] = useState<
    PersonIdentity | null | undefined
  >(undefined);
  const [personaEditor, setPersonaEditor] = useState<"create" | "edit" | null>(
    null,
  );
  const [personaForm, setPersonaForm] = useState<CharacterPersonaInput>(
    emptyPersonaForm(),
  );
  const versionRequestSequence = useRef(0);

  const selectedIdentity = identities.find(
    (identity) => identity.id === selectedIdentityId,
  );
  const selectedPersona = personas.find(
    (persona) => persona.id === selectedPersonaId,
  );
  const selectedVersion = versions.find(
    (version) => version.id === selectedVersionId,
  );

  useEffect(() => {
    let active = true;
    setIsLoading(true);
    listPersonIdentities()
      .then((result) => {
        if (!active) {
          return;
        }
        setIdentities(result);
        setSelectedIdentityId((current) =>
          current && result.some((item) => item.id === current)
            ? current
            : (result[0]?.id ?? ""),
        );
        setError("");
      })
      .catch((loadError) => {
        if (active) {
          setError(errorMessage(loadError, "人物身份列表暂不可用，请重试。"));
        }
      })
      .finally(() => {
        if (active) {
          setIsLoading(false);
        }
      });
    return () => {
      active = false;
    };
  }, []);

  useEffect(() => {
    if (!selectedIdentityId) {
      setPersonas([]);
      setSelectedPersonaId("");
      return;
    }
    let active = true;
    listCharacterPersonas(selectedIdentityId)
      .then((result) => {
        if (!active) {
          return;
        }
        setPersonas(result);
        setSelectedPersonaId((current) =>
          current && result.some((item) => item.id === current)
            ? current
            : (result[0]?.id ?? ""),
        );
        setError("");
      })
      .catch((loadError) => {
        if (active) {
          setError(errorMessage(loadError, "人物人设暂不可用，请重试。"));
        }
      });
    return () => {
      active = false;
    };
  }, [selectedIdentityId]);

  useEffect(() => {
    if (!selectedPersonaId) {
      setVersions([]);
      setSelectedVersionId("");
      return;
    }
    let active = true;
    listCharacterVersions(selectedPersonaId)
      .then((result) => {
        if (!active) {
          return;
        }
        const ordered = [...result].sort(
          (left, right) => right.version_number - left.version_number,
        );
        setVersions(ordered);
        setSelectedVersionId((current) =>
          current && ordered.some((item) => item.id === current)
            ? current
            : (ordered[0]?.id ?? ""),
        );
        setError("");
      })
      .catch((loadError) => {
        if (active) {
          setError(errorMessage(loadError, "角色版本历史暂不可用，请重试。"));
        }
      });
    return () => {
      active = false;
    };
  }, [selectedPersonaId]);

  const refreshVersionData = useCallback(
    async (versionId: string) => {
      const requestSequence = ++versionRequestSequence.current;
      setIsVersionLoading(true);
      try {
        const [nextAssets, nextTasks] = await Promise.all([
          listCharacterAssets(versionId),
          userRole === "employee"
            ? Promise.resolve([])
            : listCharacterGenerationTasks(versionId),
        ]);
        if (requestSequence !== versionRequestSequence.current) {
          return;
        }
        setAssets(nextAssets);
        setGenerationTasks(nextTasks);
        clearKnownGenerationSubmission(versionId, nextTasks);
        setError("");
      } catch (loadError) {
        if (requestSequence === versionRequestSequence.current) {
          setError(errorMessage(loadError, "人物视角资产暂不可用，请重试。"));
        }
      } finally {
        if (requestSequence === versionRequestSequence.current) {
          setIsVersionLoading(false);
        }
      }
    },
    [userRole],
  );

  useEffect(() => {
    if (!selectedVersionId) {
      setAssets([]);
      setGenerationTasks([]);
      setSelectedAssets({});
      return;
    }
    void refreshVersionData(selectedVersionId);
  }, [refreshVersionData, selectedVersionId]);

  useEffect(() => {
    if (
      !selectedVersionId ||
      generationTasks.every((task) =>
        TERMINAL_GENERATION_STATUSES.has(task.status),
      )
    ) {
      return;
    }
    const timeout = window.setTimeout(
      () => void refreshVersionData(selectedVersionId),
      CHARACTER_POLL_INTERVAL_MS,
    );
    return () => window.clearTimeout(timeout);
  }, [generationTasks, refreshVersionData, selectedVersionId]);

  useEffect(() => {
    setSelectedAssets((current) => {
      const next = { ...current };
      for (const view of REQUIRED_VIEWS) {
        const approved = assets.filter(
          (asset) =>
            asset.view_type === view.type && asset.review_status === "APPROVED",
        );
        const currentStillValid = approved.some(
          (asset) => asset.id === current[view.type],
        );
        if (!currentStillValid) {
          next[view.type] =
            approved.find((asset) => asset.is_published_selection)?.id ??
            approved[0]?.id;
        }
      }
      return next;
    });
  }, [assets]);

  const assetsByView = useMemo(() => {
    const grouped = new Map<RequiredCharacterViewType, CharacterAsset[]>();
    for (const view of REQUIRED_VIEWS) {
      grouped.set(
        view.type,
        assets
          .filter((asset) => asset.view_type === view.type)
          .sort(
            (left, right) => right.candidate_number - left.candidate_number,
          ),
      );
    }
    return grouped;
  }, [assets]);

  const publishReady = REQUIRED_VIEWS.every((view) => {
    const selectedId = selectedAssets[view.type];
    return assets.some(
      (asset) =>
        asset.id === selectedId &&
        asset.view_type === view.type &&
        asset.review_status === "APPROVED",
    );
  });

  function selectIdentity(identityId: string) {
    if (identityId === selectedIdentityId) {
      return;
    }
    setSelectedIdentityId(identityId);
    setPersonas([]);
    setSelectedPersonaId("");
    setVersions([]);
    setSelectedVersionId("");
    resetVersionData();
    setPersonaEditor(null);
    setMessage("");
    setError("");
  }

  function selectPersona(personaId: string) {
    if (personaId === selectedPersonaId) {
      return;
    }
    setSelectedPersonaId(personaId);
    setVersions([]);
    setSelectedVersionId("");
    resetVersionData();
    setPersonaEditor(null);
    setMessage("");
    setError("");
  }

  function selectVersion(versionId: string) {
    if (versionId === selectedVersionId) {
      return;
    }
    resetVersionData();
    setSelectedVersionId(versionId);
    setMessage("");
    setError("");
  }

  function resetVersionData() {
    versionRequestSequence.current += 1;
    setAssets([]);
    setGenerationTasks([]);
    setSelectedAssets({});
    setReviewComments({});
    setIsVersionLoading(false);
  }

  function handleIdentityChanged(identity: PersonIdentity) {
    setIdentities((current) => {
      const exists = current.some((item) => item.id === identity.id);
      return exists
        ? current.map((item) => (item.id === identity.id ? identity : item))
        : [identity, ...current];
    });
    if (identity.id !== selectedIdentityId) {
      selectIdentity(identity.id);
    }
  }

  function startPersonaEditor(mode: "create" | "edit") {
    if (mode === "edit" && selectedPersona) {
      setPersonaForm({
        name: selectedPersona.name,
        occupation: selectedPersona.occupation,
        scene_description: selectedPersona.scene_description,
        appearance_constraints_json:
          selectedPersona.appearance_constraints_json,
        costume_description: selectedPersona.costume_description,
        default_background: selectedPersona.default_background,
        positive_prompt: selectedPersona.positive_prompt,
        negative_prompt: selectedPersona.negative_prompt,
        usage_scope_json: selectedPersona.usage_scope_json,
      });
    } else {
      setPersonaForm(emptyPersonaForm());
    }
    setPersonaEditor(mode);
    setError("");
    setMessage("");
  }

  async function savePersona(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!selectedIdentity || !personaForm.name.trim()) {
      setError("请填写人设名称。");
      return;
    }
    setBusyAction("persona");
    setError("");
    try {
      const normalized = normalizePersonaInput(personaForm);
      const saved =
        personaEditor === "edit" && selectedPersona
          ? await updateCharacterPersona(selectedPersona.id, normalized)
          : await createCharacterPersona(selectedIdentity.id, normalized);
      setPersonas((current) => {
        const exists = current.some((item) => item.id === saved.id);
        return exists
          ? current.map((item) => (item.id === saved.id ? saved : item))
          : [saved, ...current];
      });
      if (saved.id !== selectedPersonaId) {
        selectPersona(saved.id);
      }
      setPersonaEditor(null);
      setMessage(personaEditor === "edit" ? "人设已更新。" : "人设已创建。");
    } catch (saveError) {
      setError(errorMessage(saveError, "保存人设失败，请修正后重试。"));
    } finally {
      setBusyAction("");
    }
  }

  async function handleCreateVersion() {
    if (!selectedPersona) {
      return;
    }
    setBusyAction("create-version");
    setError("");
    try {
      const created = await createCharacterVersion(selectedPersona.id, {
        provider: "fake_character",
        model: "fake-character-v1",
        generation_params_json: {},
      });
      setVersions((current) => [created, ...current]);
      selectVersion(created.id);
      setMessage(`已创建 V${created.version_number} DRAFT 版本。`);
    } catch (createError) {
      setError(errorMessage(createError, "创建角色版本失败，请重试。"));
    } finally {
      setBusyAction("");
    }
  }

  async function handleGenerateAll() {
    if (!selectedVersion) {
      return;
    }
    const submissionKey = generationSubmissionKey(selectedVersion.id);
    setBusyAction("generate-all");
    setError("");
    try {
      const tasks = await generateCharacterAssets(selectedVersion.id, {
        idempotency_key: submissionKey,
        candidates_per_view: 1,
      });
      clearGenerationSubmissionKey(selectedVersion.id, submissionKey);
      setGenerationTasks(tasks);
      setMessage("七类视角生成任务已提交，页面会自动刷新。");
      await refreshVersionData(selectedVersion.id);
    } catch (generateError) {
      setError(errorMessage(generateError, "提交七类视角生成失败，请重试。"));
    } finally {
      setBusyAction("");
    }
  }

  async function handleRegenerate(asset: CharacterAsset, label: string) {
    setBusyAction(`regenerate:${asset.id}`);
    setError("");
    try {
      const tasks = await regenerateCharacterAsset(
        asset.id,
        idempotencyKey(`character-${asset.view_type.toLowerCase()}`),
      );
      setGenerationTasks((current) => [...tasks, ...current]);
      setMessage(`${label}重新生成任务已提交。`);
      if (selectedVersion) {
        await refreshVersionData(selectedVersion.id);
      }
    } catch (regenerateError) {
      setError(errorMessage(regenerateError, "重新生成失败，请重试。"));
    } finally {
      setBusyAction("");
    }
  }

  async function handleReview(
    asset: CharacterAsset,
    decision: "APPROVED" | "REJECTED",
  ) {
    const comment = reviewComments[asset.id]?.trim() ?? "";
    if (decision === "REJECTED" && !comment) {
      setError("驳回前请填写审核说明。");
      return;
    }
    setBusyAction(`review:${asset.id}`);
    setError("");
    try {
      await reviewCharacterAsset(asset.id, decision, comment);
      setAssets((current) =>
        current.map((item) =>
          item.id === asset.id ? { ...item, review_status: decision } : item,
        ),
      );
      setMessage(
        decision === "APPROVED"
          ? "候选已人工批准。"
          : "候选已驳回，可重新生成。",
      );
    } catch (reviewError) {
      setError(errorMessage(reviewError, "保存人工审核失败，请重试。"));
    } finally {
      setBusyAction("");
    }
  }

  async function handlePublish() {
    if (!selectedVersion || !publishReady) {
      return;
    }
    if (!window.confirm("发布后角色版本与七类资产选择不可修改。确认发布？")) {
      return;
    }
    setBusyAction("publish");
    setError("");
    try {
      const selected = Object.fromEntries(
        REQUIRED_VIEWS.map((view) => [view.type, selectedAssets[view.type]]),
      ) as Record<RequiredCharacterViewType, string>;
      const published = await publishCharacterVersion(
        selectedVersion.id,
        selected,
      );
      setVersions((current) =>
        current.map((item) => (item.id === published.id ? published : item)),
      );
      setMessage("版本已发布，内容不可修改");
    } catch (publishError) {
      setError(errorMessage(publishError, "发布角色版本失败，请重试。"));
    } finally {
      setBusyAction("");
    }
  }

  return (
    <section className="character-library" aria-label="人物库工作区">
      <div className="character-library-toolbar">
        <div>
          <h2>人物身份与角色版本</h2>
          <p>真人授权、人设、版本、七视角资产与人工审核使用同一条证据链。</p>
        </div>
        {isAdmin ? (
          <button type="button" onClick={() => setIdentityWizardIdentity(null)}>
            创建人物身份
          </button>
        ) : (
          <span className="read-only-badge">只读</span>
        )}
      </div>

      {error ? (
        <div className="character-alert character-alert--error" role="alert">
          <span>{error}</span>
          <button
            className="secondary-button"
            type="button"
            onClick={() => window.location.reload()}
          >
            重新加载
          </button>
        </div>
      ) : null}
      {message ? (
        <p className="character-alert character-alert--success" role="status">
          {message}
        </p>
      ) : null}

      {identityWizardIdentity !== undefined ? (
        <IdentityWizard
          initialIdentity={identityWizardIdentity}
          onCancel={() => setIdentityWizardIdentity(undefined)}
          onComplete={(completed) => {
            handleIdentityChanged(completed);
            setIdentityWizardIdentity(undefined);
            setMessage("人物身份已激活，可继续创建人设。");
          }}
          onIdentityChanged={handleIdentityChanged}
        />
      ) : null}

      <div className="character-library-layout">
        <aside className="identity-rail" aria-label="人物身份列表">
          <div className="identity-rail-heading">
            <strong>人物身份</strong>
            <span>{identities.length}</span>
          </div>
          {isLoading ? <IdentitySkeleton /> : null}
          {!isLoading && identities.length === 0 ? (
            <div className="character-empty compact-empty">
              <strong>还没有人物身份</strong>
              <p>
                {isAdmin
                  ? "创建并上传已授权真人资料。"
                  : "等待管理员发布可用人物。"}
              </p>
            </div>
          ) : null}
          <div className="identity-list">
            {identities.map((identity) => (
              <button
                className={
                  identity.id === selectedIdentityId
                    ? "identity-list-item identity-list-item--active"
                    : "identity-list-item"
                }
                key={identity.id}
                onClick={() => selectIdentity(identity.id)}
                type="button"
              >
                <AssetThumbnail
                  allowDownload={isAdmin}
                  alt={`${identity.display_name} 真人源图`}
                  assetId={identity.source_asset_id}
                  compact
                />
                <span>
                  <strong>{identity.display_name}</strong>
                  <small>{identityStatusLabel(identity)}</small>
                </span>
              </button>
            ))}
          </div>
        </aside>

        <div className="character-library-main">
          {selectedIdentity ? (
            <>
              <IdentitySummary
                identity={selectedIdentity}
                onResume={
                  isAdmin && selectedIdentity.status === "DRAFT"
                    ? () => setIdentityWizardIdentity(selectedIdentity)
                    : undefined
                }
                userRole={userRole}
              />
              <section
                className="persona-section"
                aria-labelledby="persona-title"
              >
                <div className="subsection-heading">
                  <div>
                    <h3 id="persona-title">人物人设</h3>
                    <p>人设可编辑，角色版本会冻结创建时的快照。</p>
                  </div>
                  {isAdmin ? (
                    <div className="inline-actions">
                      {selectedPersona ? (
                        <button
                          className="secondary-button"
                          type="button"
                          onClick={() => startPersonaEditor("edit")}
                        >
                          编辑人设
                        </button>
                      ) : null}
                      <button
                        type="button"
                        onClick={() => startPersonaEditor("create")}
                      >
                        新建人设
                      </button>
                    </div>
                  ) : null}
                </div>

                {personaEditor ? (
                  <PersonaEditor
                    busy={busyAction === "persona"}
                    form={personaForm}
                    mode={personaEditor}
                    onCancel={() => setPersonaEditor(null)}
                    onChange={setPersonaForm}
                    onSubmit={savePersona}
                  />
                ) : null}

                {personas.length > 0 ? (
                  <div
                    className="persona-tabs"
                    role="tablist"
                    aria-label="人物人设"
                  >
                    {personas.map((persona) => (
                      <button
                        aria-selected={persona.id === selectedPersonaId}
                        className={
                          persona.id === selectedPersonaId
                            ? "persona-tab persona-tab--active"
                            : "persona-tab"
                        }
                        key={persona.id}
                        onClick={() => selectPersona(persona.id)}
                        role="tab"
                        type="button"
                      >
                        <strong>{persona.name}</strong>
                        <small>{persona.occupation || "未填写职业"}</small>
                      </button>
                    ))}
                  </div>
                ) : personaEditor ? null : (
                  <div className="character-empty compact-empty">
                    <strong>暂无人设</strong>
                    <p>
                      {isAdmin
                        ? "先创建一个用于视频生产的人设。"
                        : "当前人物暂无可查看人设。"}
                    </p>
                  </div>
                )}
              </section>

              {selectedPersona ? (
                <section
                  className="version-section"
                  aria-labelledby="version-title"
                >
                  <div className="subsection-heading">
                    <div>
                      <h3 id="version-title">角色版本历史</h3>
                      <p>每个版本保留创建时的人设与源图快照。</p>
                    </div>
                    {isAdmin ? (
                      <button
                        disabled={busyAction === "create-version"}
                        onClick={handleCreateVersion}
                        type="button"
                      >
                        {busyAction === "create-version"
                          ? "正在创建"
                          : "创建 DRAFT 版本"}
                      </button>
                    ) : null}
                  </div>
                  {versions.length > 0 ? (
                    <div
                      className="version-tabs"
                      role="tablist"
                      aria-label="角色版本历史"
                    >
                      {versions.map((item) => (
                        <button
                          aria-selected={item.id === selectedVersionId}
                          className={
                            item.id === selectedVersionId
                              ? "version-tab version-tab--active"
                              : "version-tab"
                          }
                          key={item.id}
                          onClick={() => selectVersion(item.id)}
                          role="tab"
                          type="button"
                        >
                          <strong>V{item.version_number}</strong>
                          <StatusBadge status={item.status} />
                          <small>{formatDateTime(item.created_at)}</small>
                        </button>
                      ))}
                    </div>
                  ) : (
                    <div className="character-empty compact-empty">
                      <strong>暂无角色版本</strong>
                      <p>
                        {isAdmin
                          ? "创建 DRAFT 版本后开始生成七类视角。"
                          : "当前人设暂无可查看版本。"}
                      </p>
                    </div>
                  )}
                </section>
              ) : null}

              {selectedVersion ? (
                <section
                  className="view-review-section"
                  aria-labelledby="view-review-title"
                >
                  <div className="subsection-heading">
                    <div>
                      <h3 id="view-review-title">七视角生成与人工审核</h3>
                      <p>自动检查只提供提示，人工批准是发布的唯一门禁。</p>
                    </div>
                    {isAdmin && selectedVersion.status !== "PUBLISHED" ? (
                      <button
                        disabled={busyAction === "generate-all"}
                        onClick={handleGenerateAll}
                        type="button"
                      >
                        {busyAction === "generate-all"
                          ? "正在提交"
                          : "开始生成 7 类视角"}
                      </button>
                    ) : null}
                  </div>

                  {generationTasks.length > 0 ? (
                    <GenerationTaskSummary tasks={generationTasks} />
                  ) : null}
                  {isVersionLoading && assets.length === 0 ? (
                    <ViewSlotsSkeleton />
                  ) : (
                    <div className="view-slot-grid">
                      {REQUIRED_VIEWS.map((view) => (
                        <ViewSlot
                          allowDownload={userRole !== "auditor"}
                          assets={assetsByView.get(view.type) ?? []}
                          busyAction={busyAction}
                          canManage={
                            isAdmin && selectedVersion.status !== "PUBLISHED"
                          }
                          key={view.type}
                          label={view.label}
                          onComment={(assetId, comment) =>
                            setReviewComments((current) => ({
                              ...current,
                              [assetId]: comment,
                            }))
                          }
                          onRegenerate={handleRegenerate}
                          onReview={handleReview}
                          onSelect={(assetId) =>
                            setSelectedAssets((current) => ({
                              ...current,
                              [view.type]: assetId,
                            }))
                          }
                          reviewComments={reviewComments}
                          selectedAssetId={selectedAssets[view.type]}
                          viewType={view.type}
                        />
                      ))}
                    </div>
                  )}

                  <div className="publish-panel">
                    <div>
                      <strong>
                        {selectedVersion.status === "PUBLISHED"
                          ? "版本已发布，内容不可修改"
                          : "发布后不可修改"}
                      </strong>
                      <p>
                        {selectedVersion.status === "PUBLISHED"
                          ? `发布于 ${formatDateTime(selectedVersion.published_at)}`
                          : "确认七类视角均由人工批准，并为每类选择一个发布资产。"}
                      </p>
                    </div>
                    {isAdmin && selectedVersion.status !== "PUBLISHED" ? (
                      <button
                        disabled={!publishReady || busyAction === "publish"}
                        onClick={handlePublish}
                        type="button"
                      >
                        {busyAction === "publish" ? "正在发布" : "发布角色版本"}
                      </button>
                    ) : null}
                  </div>
                </section>
              ) : null}
            </>
          ) : !isLoading ? (
            <div className="character-empty">
              <h3>选择一个人物身份</h3>
              <p>这里会展示授权、人设、版本和七视角审核记录。</p>
            </div>
          ) : null}
        </div>
      </div>
    </section>
  );
}

function IdentitySummary({
  identity,
  onResume,
  userRole,
}: {
  identity: PersonIdentity;
  onResume?: () => void;
  userRole: UserRole;
}) {
  return (
    <section
      className="identity-summary"
      aria-labelledby="identity-summary-title"
    >
      <AssetThumbnail
        allowDownload={userRole === "admin"}
        alt={`${identity.display_name} 真人源图预览`}
        assetId={identity.source_asset_id}
      />
      <div>
        <div className="identity-summary-title-row">
          <h3 id="identity-summary-title">{identity.display_name}</h3>
          <StatusBadge status={identity.status} />
        </div>
        <dl className="identity-facts">
          <div>
            <dt>肖像授权</dt>
            <dd>{authorizationStatusLabel(identity.authorization_status)}</dd>
          </div>
          <div>
            <dt>授权期限</dt>
            <dd>
              {identity.authorization_expires_at
                ? `授权有效至 ${formatDate(identity.authorization_expires_at)}`
                : "未设置到期日"}
            </dd>
          </div>
          <div>
            <dt>源图质量</dt>
            <dd>{qualityStatusLabel(identity.source_quality_status)}</dd>
          </div>
          <div>
            <dt>使用范围</dt>
            <dd>{identity.authorization_scope.join("、") || "未填写"}</dd>
          </div>
        </dl>
        {onResume ? (
          <button className="secondary-button" type="button" onClick={onResume}>
            继续完成人物身份
          </button>
        ) : null}
        {userRole === "auditor" ? (
          <p className="evidence-note">
            审计身份可读元数据，但不能创建源图下载链接。
          </p>
        ) : null}
      </div>
    </section>
  );
}

function IdentityWizard({
  initialIdentity,
  onCancel,
  onComplete,
  onIdentityChanged,
}: {
  initialIdentity: PersonIdentity | null;
  onCancel: () => void;
  onComplete: (identity: PersonIdentity) => void;
  onIdentityChanged: (identity: PersonIdentity) => void;
}) {
  const [phase, setPhase] = useState<
    "authorization" | "source" | "quality" | "confirm"
  >(
    initialIdentity?.authorization_status === "AUTHORIZED"
      ? "source"
      : "authorization",
  );
  const [displayName, setDisplayName] = useState(
    initialIdentity?.display_name ?? "",
  );
  const [authorizationScope, setAuthorizationScope] = useState(
    initialIdentity?.authorization_scope.join("、") ?? "",
  );
  const [expiresOn, setExpiresOn] = useState(
    localDateInputValue(initialIdentity?.authorization_expires_at),
  );
  const [authorizationFile, setAuthorizationFile] = useState<File | null>(null);
  const [sourceFile, setSourceFile] = useState<File | null>(null);
  const [identity, setIdentity] = useState<PersonIdentity | null>(
    initialIdentity,
  );
  const [sourceResult, setSourceResult] =
    useState<CompletedIdentitySource | null>(null);
  const [uploadProgress, setUploadProgress] = useState<number | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function submitAuthorization(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const scopes = splitList(authorizationScope);
    if (!displayName.trim() || scopes.length === 0 || !authorizationFile) {
      setError("请填写人物显示名、授权使用范围并选择授权文件。");
      return;
    }
    setBusy(true);
    setError("");
    try {
      const currentIdentity =
        identity ??
        (await createPersonIdentity({
          display_name: displayName.trim(),
          authorization_scope: scopes,
          authorization_expires_at: expiresOn
            ? endOfLocalDayIso(expiresOn)
            : null,
        }));
      setIdentity(currentIdentity);
      onIdentityChanged(currentIdentity);
      const intent = await createIdentityUploadIntent(
        currentIdentity.id,
        "authorization",
        authorizationFile,
      );
      await uploadIdentityAsset(intent, authorizationFile, setUploadProgress);
      const authorized = await completeIdentityAuthorizationUpload(
        currentIdentity.id,
        intent.asset_id,
      );
      setIdentity(authorized);
      onIdentityChanged(authorized);
      setUploadProgress(null);
      setPhase("source");
    } catch (uploadError) {
      setUploadProgress(null);
      setError(
        errorMessage(
          uploadError,
          "授权文件上传失败，身份草稿已保留，可直接重试。",
        ),
      );
    } finally {
      setBusy(false);
    }
  }

  async function submitSource(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!identity || !sourceFile) {
      setError("请选择 JPG 或 PNG 真人源图。");
      return;
    }
    setBusy(true);
    setError("");
    try {
      const intent = await createIdentityUploadIntent(
        identity.id,
        "source",
        sourceFile,
      );
      await uploadIdentityAsset(intent, sourceFile, setUploadProgress);
      const completed = await completeIdentitySourceUpload(
        identity.id,
        intent.asset_id,
      );
      setIdentity(completed.identity);
      setSourceResult(completed);
      onIdentityChanged(completed.identity);
      setUploadProgress(null);
      setPhase("quality");
    } catch (uploadError) {
      setUploadProgress(null);
      setError(
        errorMessage(uploadError, "源图上传或质量检查失败，请更换图片后重试。"),
      );
    } finally {
      setBusy(false);
    }
  }

  return (
    <section
      className="identity-wizard"
      aria-labelledby="identity-wizard-title"
    >
      <div className="identity-wizard-heading">
        <div>
          <h3 id="identity-wizard-title">
            {initialIdentity ? "继续人物身份" : "创建人物身份"}
          </h3>
          <p>授权、源图、质量结果、确认</p>
        </div>
        <button className="secondary-button" type="button" onClick={onCancel}>
          关闭
        </button>
      </div>
      <ol className="identity-wizard-steps" aria-label="创建人物身份进度">
        {[
          ["authorization", "肖像授权"],
          ["source", "真人源图"],
          ["quality", "质量结果"],
          ["confirm", "确认"],
        ].map(([key, label]) => (
          <li
            className={
              phase === key ? "identity-wizard-step--active" : undefined
            }
            key={key}
          >
            {label}
          </li>
        ))}
      </ol>
      {error ? (
        <p className="character-alert character-alert--error" role="alert">
          {error}
        </p>
      ) : null}
      {uploadProgress !== null ? (
        <div
          aria-label="上传进度"
          aria-valuemax={100}
          aria-valuemin={0}
          aria-valuenow={uploadProgress}
          className="character-upload-progress"
          role="progressbar"
        >
          <span style={{ width: `${uploadProgress}%` }} />
          <strong>{uploadProgress}%</strong>
        </div>
      ) : null}

      {phase === "authorization" ? (
        <form className="character-form" onSubmit={submitAuthorization}>
          <label htmlFor="identity-display-name">人物显示名</label>
          <input
            id="identity-display-name"
            maxLength={120}
            value={displayName}
            onChange={(event) => setDisplayName(event.target.value)}
          />
          <label htmlFor="identity-authorization-scope">授权使用范围</label>
          <input
            id="identity-authorization-scope"
            placeholder="例如：内部短视频、培训材料"
            value={authorizationScope}
            onChange={(event) => setAuthorizationScope(event.target.value)}
          />
          <label htmlFor="identity-expires-on">授权到期日</label>
          <input
            id="identity-expires-on"
            type="date"
            value={expiresOn}
            onChange={(event) => setExpiresOn(event.target.value)}
          />
          <label htmlFor="identity-authorization-file">肖像授权文件</label>
          <input
            accept=".pdf,.jpg,.jpeg,.png,application/pdf,image/jpeg,image/png"
            id="identity-authorization-file"
            type="file"
            onChange={(event) => setAuthorizationFile(fileFromEvent(event))}
          />
          <small>支持 PDF、JPG、PNG，最大 20MB。</small>
          <button disabled={busy} type="submit">
            {busy ? "正在上传" : "创建并上传授权"}
          </button>
        </form>
      ) : null}

      {phase === "source" ? (
        <form className="character-form" onSubmit={submitSource}>
          <h4>上传真人源图</h4>
          <p>请使用单人、正面清晰、无遮挡、无水印的 JPG 或 PNG 图片。</p>
          <label htmlFor="identity-source-file">真人源图</label>
          <input
            accept=".jpg,.jpeg,.png,image/jpeg,image/png"
            id="identity-source-file"
            type="file"
            onChange={(event) => setSourceFile(fileFromEvent(event))}
          />
          <button disabled={busy} type="submit">
            {busy ? "正在检查" : "上传并检查源图"}
          </button>
        </form>
      ) : null}

      {phase === "quality" && sourceResult ? (
        <div className="quality-result">
          <h4>
            {sourceResult.quality.passed ? "源图质量通过" : "源图质量未通过"}
          </h4>
          <dl>
            <div>
              <dt>画面尺寸</dt>
              <dd>
                {sourceResult.quality.width} × {sourceResult.quality.height}
              </dd>
            </div>
            <div>
              <dt>人物与人脸</dt>
              <dd>
                {sourceResult.quality.person_count} 人，
                {sourceResult.quality.face_count} 张脸
              </dd>
            </div>
            <div>
              <dt>清晰度</dt>
              <dd>{sourceResult.quality.sharpness_score.toFixed(2)}</dd>
            </div>
            <div>
              <dt>检查器</dt>
              <dd>
                {sourceResult.quality.provider} / {sourceResult.quality.model}
              </dd>
            </div>
          </dl>
          {sourceResult.quality.provider.startsWith("fake") ? (
            <p className="simulation-note">模拟检查结果，仅用于本地流程验证</p>
          ) : null}
          {sourceResult.quality.issue_codes.length > 0 ? (
            <p className="quality-issues">
              问题：{sourceResult.quality.issue_codes.join("、")}
            </p>
          ) : null}
          <div className="inline-actions">
            {!sourceResult.quality.passed ? (
              <button
                className="secondary-button"
                type="button"
                onClick={() => setPhase("source")}
              >
                重新选择源图
              </button>
            ) : (
              <button type="button" onClick={() => setPhase("confirm")}>
                确认质检结果
              </button>
            )}
          </div>
        </div>
      ) : null}

      {phase === "confirm" && identity ? (
        <div className="identity-confirmation">
          <h4>人物身份已激活</h4>
          <p>{identity.display_name} 的授权和源图质量均已确认。</p>
          <button type="button" onClick={() => onComplete(identity)}>
            完成并返回人物库
          </button>
        </div>
      ) : null}
    </section>
  );
}

function PersonaEditor({
  busy,
  form,
  mode,
  onCancel,
  onChange,
  onSubmit,
}: {
  busy: boolean;
  form: CharacterPersonaInput;
  mode: "create" | "edit";
  onCancel: () => void;
  onChange: (form: CharacterPersonaInput) => void;
  onSubmit: (event: FormEvent<HTMLFormElement>) => void;
}) {
  function update(field: keyof CharacterPersonaInput, value: string) {
    onChange({ ...form, [field]: value });
  }
  return (
    <form className="persona-editor" onSubmit={onSubmit}>
      <h4>{mode === "create" ? "新建人设" : "编辑人设"}</h4>
      <div className="persona-form-grid">
        <label>
          人设名称
          <input
            value={form.name}
            onChange={(event) => update("name", event.target.value)}
          />
        </label>
        <label>
          职业定位
          <input
            value={form.occupation ?? ""}
            onChange={(event) => update("occupation", event.target.value)}
          />
        </label>
        <label>
          使用场景
          <input
            value={form.scene_description ?? ""}
            onChange={(event) =>
              update("scene_description", event.target.value)
            }
          />
        </label>
        <label>
          服装描述
          <input
            value={form.costume_description ?? ""}
            onChange={(event) =>
              update("costume_description", event.target.value)
            }
          />
        </label>
        <label>
          默认背景
          <input
            value={form.default_background ?? ""}
            onChange={(event) =>
              update("default_background", event.target.value)
            }
          />
        </label>
        <label>
          使用范围
          <input
            value={(form.usage_scope_json ?? []).join("、")}
            onChange={(event) =>
              onChange({
                ...form,
                usage_scope_json: splitList(event.target.value),
              })
            }
          />
        </label>
        <label className="wide-field">
          正向提示词
          <textarea
            rows={2}
            value={form.positive_prompt ?? ""}
            onChange={(event) => update("positive_prompt", event.target.value)}
          />
        </label>
        <label className="wide-field">
          负向提示词
          <textarea
            rows={2}
            value={form.negative_prompt ?? ""}
            onChange={(event) => update("negative_prompt", event.target.value)}
          />
        </label>
      </div>
      <div className="inline-actions">
        <button disabled={busy} type="submit">
          {busy ? "正在保存" : "保存人设"}
        </button>
        <button className="secondary-button" type="button" onClick={onCancel}>
          取消
        </button>
      </div>
    </form>
  );
}

function ViewSlot({
  allowDownload,
  assets,
  busyAction,
  canManage,
  label,
  onComment,
  onRegenerate,
  onReview,
  onSelect,
  reviewComments,
  selectedAssetId,
  viewType,
}: {
  allowDownload: boolean;
  assets: CharacterAsset[];
  busyAction: string;
  canManage: boolean;
  label: string;
  onComment: (assetId: string, comment: string) => void;
  onRegenerate: (asset: CharacterAsset, label: string) => void;
  onReview: (asset: CharacterAsset, decision: "APPROVED" | "REJECTED") => void;
  onSelect: (assetId: string) => void;
  reviewComments: Record<string, string>;
  selectedAssetId: string | undefined;
  viewType: RequiredCharacterViewType;
}) {
  return (
    <article className="view-slot">
      <div className="view-slot-heading">
        <h4>{label}</h4>
        <span>{viewType}</span>
      </div>
      {assets.length === 0 ? (
        <div className="view-slot-empty">
          <strong>等待生成</strong>
          <p>该视角暂无候选。</p>
        </div>
      ) : (
        <div className="view-candidates">
          {assets.map((asset) => (
            <div className="view-candidate" key={asset.id}>
              <AssetThumbnail
                allowDownload={allowDownload}
                alt={`${label}候选 ${asset.candidate_number}`}
                assetId={asset.asset_id}
              />
              <div className="candidate-meta">
                <strong>候选 {asset.candidate_number}</strong>
                <StatusBadge status={asset.review_status} />
              </div>
              <div className="auto-quality-note">
                <strong>自动提示（仅供参考）</strong>
                <span>{autoQualitySummary(asset.auto_quality_json)}</span>
              </div>
              <p className="human-review-label">
                人工审核：{reviewStatusLabel(asset.review_status)}
              </p>
              {asset.review_status === "APPROVED" ? (
                canManage ? (
                  <label className="publish-choice">
                    <input
                      checked={selectedAssetId === asset.id}
                      name={`publish-${viewType}`}
                      type="radio"
                      onChange={() => onSelect(asset.id)}
                    />
                    选为发布资产
                  </label>
                ) : (
                  <span className="publish-choice publish-choice--readonly">
                    {asset.is_published_selection
                      ? "当前发布资产"
                      : "已批准候选"}
                  </span>
                )
              ) : null}
              {canManage ? (
                <>
                  <label className="review-comment">
                    审核说明
                    <textarea
                      aria-label="审核说明"
                      rows={2}
                      value={reviewComments[asset.id] ?? ""}
                      onChange={(event) =>
                        onComment(asset.id, event.target.value)
                      }
                    />
                  </label>
                  <div className="candidate-actions">
                    <button
                      className="secondary-button"
                      disabled={busyAction === `review:${asset.id}`}
                      type="button"
                      onClick={() => void onReview(asset, "APPROVED")}
                    >
                      批准
                    </button>
                    <button
                      className="danger-button"
                      disabled={
                        busyAction === `review:${asset.id}` ||
                        !reviewComments[asset.id]?.trim()
                      }
                      type="button"
                      onClick={() => void onReview(asset, "REJECTED")}
                    >
                      驳回
                    </button>
                  </div>
                  {asset.review_status === "REJECTED" ? (
                    <button
                      className="secondary-button regenerate-button"
                      disabled={busyAction === `regenerate:${asset.id}`}
                      type="button"
                      onClick={() => void onRegenerate(asset, label)}
                    >
                      重新生成{label}
                    </button>
                  ) : null}
                </>
              ) : null}
            </div>
          ))}
        </div>
      )}
    </article>
  );
}

function AssetThumbnail({
  allowDownload,
  alt,
  assetId,
  compact = false,
}: {
  allowDownload: boolean;
  alt: string;
  assetId: string | null;
  compact?: boolean;
}) {
  const [url, setUrl] = useState("");
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    if (!assetId || !allowDownload) {
      setUrl("");
      setFailed(false);
      return;
    }
    let active = true;
    getAssetDownloadUrl(assetId)
      .then((result) => {
        if (active) {
          setUrl(result.url);
          setFailed(false);
        }
      })
      .catch(() => {
        if (active) {
          setFailed(true);
        }
      });
    return () => {
      active = false;
    };
  }, [allowDownload, assetId]);

  const className = compact
    ? "asset-thumbnail asset-thumbnail--compact"
    : "asset-thumbnail";
  if (url && !failed) {
    return (
      <img
        alt={alt}
        className={className}
        src={url}
        onError={() => setFailed(true)}
      />
    );
  }
  return (
    <div
      aria-label={alt}
      className={`${className} asset-thumbnail--placeholder`}
      role="img"
    >
      <span>
        {failed
          ? "预览失败"
          : assetId
            ? allowDownload
              ? "加载中"
              : "仅元数据"
            : "暂无源图"}
      </span>
    </div>
  );
}

function GenerationTaskSummary({
  tasks,
}: {
  tasks: CharacterGenerationTask[];
}) {
  const counts = tasks.reduce<Record<string, number>>((current, task) => {
    current[task.status] = (current[task.status] ?? 0) + 1;
    return current;
  }, {});
  return (
    <div className="character-task-summary" role="status">
      <span>生成任务 {tasks.length}</span>
      <span>等待 {counts.PENDING ?? 0}</span>
      <span>运行 {counts.RUNNING ?? 0}</span>
      <span>成功 {counts.SUCCEEDED ?? 0}</span>
      <span>失败 {counts.FAILED ?? 0}</span>
    </div>
  );
}

function StatusBadge({ status }: { status: string }) {
  return (
    <span
      className={`character-status character-status--${status.toLowerCase()}`}
    >
      {statusLabel(status)}
    </span>
  );
}

function IdentitySkeleton() {
  return (
    <div
      aria-label="正在加载人物身份"
      className="identity-skeleton"
      role="status"
    >
      <span />
      <span />
      <span />
    </div>
  );
}

function ViewSlotsSkeleton() {
  return (
    <div
      aria-label="正在加载七视角资产"
      className="view-slot-grid"
      role="status"
    >
      {REQUIRED_VIEWS.map((view) => (
        <div className="view-slot view-slot--skeleton" key={view.type}>
          <span />
          <span />
        </div>
      ))}
    </div>
  );
}

function emptyPersonaForm(): CharacterPersonaInput {
  return {
    name: "",
    occupation: "",
    scene_description: "",
    appearance_constraints_json: {},
    costume_description: "",
    default_background: "",
    positive_prompt: "",
    negative_prompt: "",
    usage_scope_json: [],
  };
}

function normalizePersonaInput(
  input: CharacterPersonaInput,
): CharacterPersonaInput {
  return {
    ...input,
    name: input.name.trim(),
    occupation: optionalText(input.occupation),
    scene_description: optionalText(input.scene_description),
    costume_description: optionalText(input.costume_description),
    default_background: optionalText(input.default_background),
    positive_prompt: optionalText(input.positive_prompt),
    negative_prompt: optionalText(input.negative_prompt),
    usage_scope_json: input.usage_scope_json ?? [],
  };
}

function optionalText(value: string | null | undefined): string | null {
  const normalized = value?.trim() ?? "";
  return normalized || null;
}

function splitList(value: string): string[] {
  return value
    .split(/[、,，\n]/)
    .map((item) => item.trim())
    .filter(Boolean);
}

function fileFromEvent(event: ChangeEvent<HTMLInputElement>): File | null {
  return event.target.files?.[0] ?? null;
}

function errorMessage(error: unknown, fallback: string): string {
  return error instanceof Error && error.message ? error.message : fallback;
}

function idempotencyKey(prefix: string): string {
  return `${prefix}:${crypto.randomUUID()}`;
}

function generationSubmissionStorageKey(versionId: string): string {
  return `${GENERATION_SUBMISSION_STORAGE_PREFIX}:${versionId}`;
}

function generationSubmissionKey(versionId: string): string {
  const storageKey = generationSubmissionStorageKey(versionId);
  const existing = window.localStorage.getItem(storageKey);
  if (existing) {
    return existing;
  }
  const created = idempotencyKey("character-all");
  window.localStorage.setItem(storageKey, created);
  return created;
}

function clearGenerationSubmissionKey(
  versionId: string,
  expectedKey: string,
): void {
  const storageKey = generationSubmissionStorageKey(versionId);
  if (window.localStorage.getItem(storageKey) === expectedKey) {
    window.localStorage.removeItem(storageKey);
  }
}

function clearKnownGenerationSubmission(
  versionId: string,
  tasks: CharacterGenerationTask[],
): void {
  const storedKey = window.localStorage.getItem(
    generationSubmissionStorageKey(versionId),
  );
  if (storedKey && tasks.some((task) => task.idempotency_key === storedKey)) {
    clearGenerationSubmissionKey(versionId, storedKey);
  }
}

function endOfLocalDayIso(value: string): string {
  const [year, month, day] = value.split("-").map(Number);
  return new Date(year, month - 1, day, 23, 59, 59, 999).toISOString();
}

function localDateInputValue(value: string | null | undefined): string {
  if (!value) {
    return "";
  }
  const date = new Date(value);
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

function formatDate(value: string): string {
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  })
    .format(new Date(value))
    .replaceAll("/", "-");
}

function formatDateTime(value: string | null): string {
  if (!value) {
    return "未记录";
  }
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(new Date(value));
}

function identityStatusLabel(identity: PersonIdentity): string {
  return `${statusLabel(identity.status)} / ${authorizationStatusLabel(identity.authorization_status)}`;
}

function authorizationStatusLabel(status: string): string {
  return (
    {
      PENDING: "待授权",
      AUTHORIZED: "已授权",
      EXPIRED: "已到期",
      REVOKED: "已撤销",
    }[status] ?? status
  );
}

function qualityStatusLabel(status: string): string {
  return (
    {
      PENDING: "待检查",
      PASSED: "已通过",
      FAILED: "未通过",
      IMPORTED: "历史导入",
    }[status] ?? status
  );
}

function reviewStatusLabel(status: string): string {
  return (
    {
      NOT_REVIEWED: "待审核",
      APPROVED: "已批准",
      REJECTED: "已驳回",
    }[status] ?? status
  );
}

function statusLabel(status: string): string {
  return (
    {
      DRAFT: "草稿",
      ACTIVE: "可用",
      EXPIRED: "已到期",
      REVOKED: "已撤销",
      ARCHIVED: "已归档",
      GENERATING: "生成中",
      REVIEWING: "审核中",
      PUBLISHED: "已发布",
      FAILED: "失败",
      NOT_REVIEWED: "待审核",
      APPROVED: "已批准",
      REJECTED: "已驳回",
    }[status] ?? status
  );
}

function autoQualitySummary(value: Record<string, unknown>): string {
  const simulated = value.simulated === true ? "模拟检查" : "自动检查";
  const scores = value.scores;
  if (typeof scores === "object" && scores !== null && !Array.isArray(scores)) {
    const identityScore = (scores as Record<string, unknown>)
      .identity_consistency;
    if (typeof identityScore === "number") {
      return `${simulated}，身份一致性 ${identityScore.toFixed(2)}`;
    }
  }
  return `${simulated}，请以人工审核结论为准`;
}
