import { useEffect, useRef, useState } from "react";
import {
  getStudioSavedScript,
  resolveMaterials,
  searchWorkspace,
  type WorkspaceSearchItem,
  type WorkspaceSearchKind,
  type WorkspaceSearchPage,
} from "../api";
import { useStudio } from "./context";
import {
  loadPeopleEntries,
  savedScriptFromRecord,
  studioAssetFromMaterial,
} from "./live";
import { createDraft, pageTitles } from "./state";
import type { StudioPage } from "./types";
import { Button, Empty } from "./ui";

// 爆款数据功能已下线（2026-11-16 定价决策）：搜索不再提供「视频」类型，
// 避免结果点开后只剩下线说明。文案/人物/素材仍走原工作区搜索。
const kinds = {
  script: "文案",
  person: "人物",
  material: "素材",
} as const;

export function WorkspaceSearch({
  query,
  onClose,
}: {
  query: string;
  onClose(): void;
}) {
  const { data, state, review, user, navigate, patchState, updateData } =
    useStudio();
  const [kind, setKind] = useState<WorkspaceSearchKind>("script");
  const [page, setPage] = useState(1);
  const [result, setResult] = useState<WorkspaceSearchPage | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [retry, setRetry] = useState(0);
  const operation = useRef(0);
  const q = query.trim();
  useEffect(() => {
    void q;
    setPage(1);
  }, [q]);
  useEffect(() => {
    void retry;
    void user.id;
    const current = ++operation.current;
    setError("");
    setResult(null);
    setBusy(false);
    if (!q || review) return;
    void searchWorkspace(q, kind, page)
      .then((next) => {
        if (operation.current === current) setResult(next);
      })
      .catch((cause) => {
        if (operation.current === current)
          setError(cause instanceof Error ? cause.message : "搜索失败");
      });
    return () => {
      operation.current += 1;
    };
  }, [q, kind, page, review, retry, user.id]);

  const reviewItems: WorkspaceSearchItem[] = (
    kind === "person"
      ? data.people.map((item) => ({ id: item.id, title: item.name }))
      : kind === "script"
        ? state.savedScripts.map((item) => ({
            id: item.id,
            title: item.title,
          }))
        : data.assets.map((item) => ({ id: item.id, title: item.name }))
  )
    .filter((item) => item.title.includes(q))
    .map((item) => ({
      ...item,
      kind,
      description: "审核示例",
      platform: null,
      person: null,
    }));
  const items = review ? reviewItems : (result?.items ?? []);

  async function openItem(item: WorkspaceSearchItem) {
    if (busy) return;
    const current = ++operation.current;
    setBusy(true);
    setError("");
    try {
      if (item.kind === "person") {
        if (!review) {
          if (!item.person) throw new Error("人物信息缺失，请刷新搜索。");
          const loaded = await loadPeopleEntries([item.person], null, 1);
          if (current !== operation.current) return;
          updateData((previous) => ({
            ...previous,
            people: [
              ...previous.people.filter((p) => p.id !== item.id),
              ...loaded.people,
            ],
            assets: [
              ...previous.assets.filter(
                (a) => !loaded.assets.some((next) => next.id === a.id),
              ),
              ...loaded.assets,
            ],
          }));
        }
        navigate("person-ip", { selectedPersonId: item.id });
      } else if (item.kind === "material") {
        let id = item.id;
        if (!review) {
          const resolved = await resolveMaterials([id]);
          if (current !== operation.current) return;
          const material = resolved.items.find((m) => m.id === id);
          if (!material) throw new Error("素材已移除或不可访问，请重新搜索。");
          const asset = studioAssetFromMaterial(material);
          id = asset.id;
          updateData((previous) => ({
            ...previous,
            assets: [...previous.assets.filter((a) => a.id !== id), asset],
          }));
        }
        navigate("materials", { selectedAssetId: id });
      } else {
        const script = review
          ? state.savedScripts.find((s) => s.id === item.id)
          : savedScriptFromRecord(await getStudioSavedScript(item.id));
        if (current !== operation.current) return;
        if (!script) throw new Error("文案已移除，请重新搜索。");
        patchState({
          draft: {
            ...createDraft(),
            script: { ...script, confirmed: false },
            projectId: script.sourceProjectId,
            ipId: script.ipId,
            scriptEdited: true,
          },
        });
        navigate("copy");
      }
      onClose();
    } catch (cause) {
      if (current === operation.current)
        setError(
          cause instanceof Error ? cause.message : "打开结果失败，请重试。",
        );
    } finally {
      if (current === operation.current) setBusy(false);
    }
  }

  return (
    <div>
      <p>{q ? `搜索“${q}”` : "输入关键词搜索已保存文案、人物与素材"}</p>
      <div className="studio-search-results">
        {Object.entries(pageTitles)
          .filter(
            ([id, title]) =>
              (!q || title.includes(q)) &&
              // 下线路由保留兼容（旧书签仍能解析），但不再作为可见入口。
              id !== "viral" &&
              id !== "viral-detail" &&
              (id !== "settings" || user.role === "admin"),
          )
          .map(([id, title]) => (
            <Button
              key={id}
              onClick={() => {
                navigate(id as StudioPage);
                onClose();
              }}
            >
              {title}
            </Button>
          ))}
      </div>
      {q && (
        <>
          <fieldset className="studio-search-kinds">
            <legend>搜索类型</legend>
            {(Object.keys(kinds) as Array<keyof typeof kinds>).map((value) => (
              <Button
                key={value}
                aria-pressed={kind === value}
                onClick={() => {
                  setKind(value);
                  setPage(1);
                }}
              >
                {kinds[value]}
              </Button>
            ))}
          </fieldset>
          {error && (
            <p role="alert">
              {error}{" "}
              <Button onClick={() => setRetry((value) => value + 1)}>
                重试
              </Button>
            </p>
          )}
          {!review && !result && !error && <p role="status">正在搜索…</p>}
          <div className="studio-search-results">
            {items.map((item) => (
              <Button
                key={`${item.platform ?? item.kind}:${item.id}`}
                disabled={busy}
                onClick={() => void openItem(item)}
              >
                {item.title}
                <small>{item.description}</small>
              </Button>
            ))}
          </div>
          {(review || result) && items.length === 0 && (
            <Empty
              title="没有匹配结果"
              description="试试其他关键词或切换搜索类型。"
            />
          )}
          {!review && result && result.total > result.page_size && (
            <div className="studio-search-pagination">
              <Button
                disabled={page <= 1 || busy}
                onClick={() => setPage((value) => value - 1)}
              >
                上一页
              </Button>
              <span>
                {page} / {Math.ceil(result.total / result.page_size)} · 共{" "}
                {result.total} 条
              </span>
              <Button
                disabled={page * result.page_size >= result.total || busy}
                onClick={() => setPage((value) => value + 1)}
              >
                下一页
              </Button>
            </div>
          )}
        </>
      )}
    </div>
  );
}
