import { useCallback, useEffect, useState } from "react";

import {
  listPersonIdentities,
  type PersonIdentity,
  renamePersonIdentity,
  type UserRole,
} from "./api";
import { SimpleCharacterUpload } from "./SimpleCharacterUpload";

function errorMessage(error: unknown, fallback: string): string {
  return error instanceof Error && error.message ? error.message : fallback;
}

export function CharacterLibrary({
  userRole,
  userId,
}: {
  userRole: UserRole;
  userId: string;
}) {
  const canManage = userRole !== "auditor";
  const [identities, setIdentities] = useState<PersonIdentity[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [editingId, setEditingId] = useState("");
  const [editingName, setEditingName] = useState("");
  const [busyRenameId, setBusyRenameId] = useState("");

  const loadIdentities = useCallback(async () => {
    setIsLoading(true);
    try {
      const result = await listPersonIdentities();
      setIdentities(result);
      setError("");
    } catch (loadError) {
      setError(errorMessage(loadError, "人物列表暂不可用，请重试。"));
    } finally {
      setIsLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadIdentities();
  }, [loadIdentities]);

  function canRename(identity: PersonIdentity): boolean {
    if (!canManage || identity.status === "ARCHIVED") {
      return false;
    }
    return userRole === "admin" || identity.owner_user_id === userId;
  }

  function startRename(identity: PersonIdentity) {
    setEditingId(identity.id);
    setEditingName(identity.display_name);
    setError("");
    setMessage("");
  }

  function cancelRename() {
    setEditingId("");
    setEditingName("");
    setBusyRenameId("");
  }

  async function saveRename(identity: PersonIdentity) {
    const name = editingName.trim();
    if (!name) {
      setError("人物名称不能为空。");
      return;
    }
    if (name === identity.display_name) {
      cancelRename();
      return;
    }
    setBusyRenameId(identity.id);
    setError("");
    try {
      const updated = await renamePersonIdentity(identity.id, name);
      setIdentities((current) =>
        current.map((item) => (item.id === updated.id ? updated : item)),
      );
      setMessage(`人物名称已更新为“${updated.display_name}”。`);
      cancelRename();
    } catch (renameError) {
      setError(errorMessage(renameError, "修改人物名称失败，请重试。"));
    } finally {
      setBusyRenameId("");
    }
  }

  return (
    <section aria-label="人物库" className="character-library-simple">
      <div className="character-library-simple__head">
        <h2>人物库</h2>
        <p className="status-note">
          上传一张图片即可一键生成七视角人物；创建者与管理员可随时修改人物名称。
        </p>
      </div>
      {canManage ? (
        <SimpleCharacterUpload onCreated={() => void loadIdentities()} />
      ) : (
        <p className="status-note">
          审计身份只读，如需创建或改名请联系管理员。
        </p>
      )}
      {error ? (
        <p className="settings-error" role="alert">
          {error}
        </p>
      ) : null}
      {message ? <p className="setup-success">{message}</p> : null}
      {isLoading ? (
        <p className="status-note">正在读取人物列表…</p>
      ) : identities.length === 0 ? (
        <p className="status-note">还没有人物，上传一张图片开始创建。</p>
      ) : (
        <ul className="character-identity-list">
          {identities.map((identity) => {
            const isEditing = editingId === identity.id;
            const isRenaming = busyRenameId === identity.id;
            return (
              <li className="character-identity-item" key={identity.id}>
                {isEditing ? (
                  <>
                    <input
                      aria-label="修改人物名称"
                      onChange={(event) => setEditingName(event.target.value)}
                      type="text"
                      value={editingName}
                    />
                    <button
                      disabled={isRenaming}
                      onClick={() => void saveRename(identity)}
                      type="button"
                    >
                      {isRenaming ? "正在保存…" : "保存名称"}
                    </button>
                    <button
                      className="secondary-button"
                      disabled={isRenaming}
                      onClick={cancelRename}
                      type="button"
                    >
                      取消
                    </button>
                  </>
                ) : (
                  <>
                    <span className="character-identity-item__name">
                      {identity.display_name}
                    </span>
                    {identity.status === "ARCHIVED" ? (
                      <span className="status-badge">已归档</span>
                    ) : null}
                    {canRename(identity) ? (
                      <button
                        className="secondary-button"
                        onClick={() => startRename(identity)}
                        type="button"
                      >
                        改名
                      </button>
                    ) : null}
                  </>
                )}
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}
