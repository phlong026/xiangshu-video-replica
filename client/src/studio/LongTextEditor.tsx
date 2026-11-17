import { useEffect, useRef, useState } from "react";
import {
  exportTextDocument,
  forkTextDocument,
  readTextDocumentBlocks,
  saveTextDocumentBlock,
  type TextDocumentRef,
} from "../api";

export function LongTextEditor({
  document,
  label,
  readOnly = false,
  onSaved,
  onDirty,
}: {
  document: TextDocumentRef;
  label: string;
  readOnly?: boolean;
  onSaved?: (document: TextDocumentRef, preview: string) => void;
  onDirty?: (dirty: boolean) => void;
}) {
  const [offset, setOffset] = useState(0);
  const [trail, setTrail] = useState<number[]>([]);
  const [search, setSearch] = useState("");
  const [query, setQuery] = useState("");
  const [loaded, setLoaded] = useState<{
    document: TextDocumentRef;
    index: number;
    text: string;
    next: number | null;
  }>();
  const [text, setText] = useState("");
  const [dirty, setDirty] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const generation = useRef(0);
  useEffect(() => {
    const operation = ++generation.current;
    setBusy(true);
    setError("");
    void readTextDocumentBlocks(document.id, offset, query)
      .then((page) => {
        if (generation.current !== operation) return;
        const block = page.blocks[0];
        setLoaded(
          block
            ? {
                document: page.document,
                index: block.block_index,
                text: block.text,
                next: page.next_offset,
              }
            : undefined,
        );
        setText(block?.text ?? "");
        setDirty(false);
      })
      .catch((cause: unknown) => {
        if (generation.current === operation)
          setError(cause instanceof Error ? cause.message : "读取文案失败。");
      })
      .finally(() => {
        if (generation.current === operation) setBusy(false);
      });
    return () => {
      generation.current += 1;
    };
  }, [document.id, offset, query]);
  useEffect(() => {
    onDirty?.(dirty);
    return () => onDirty?.(false);
  }, [dirty, onDirty]);
  const save = async () => {
    if (!loaded || busy || readOnly) return;
    const operation = generation.current;
    setBusy(true);
    setError("");
    try {
      const editable = loaded.document.immutable
        ? await forkTextDocument(loaded.document)
        : loaded.document;
      const saved = await saveTextDocumentBlock(editable, loaded.index, text);
      const preview =
        loaded.index === 0
          ? text.slice(0, 2000)
          : ((await readTextDocumentBlocks(saved.id)).blocks[0]?.text.slice(
              0,
              2000,
            ) ?? "");
      if (operation !== generation.current) return;
      setLoaded({ ...loaded, document: saved, text });
      setDirty(false);
      onSaved?.(saved, preview);
    } catch (cause) {
      if (operation === generation.current)
        setError(cause instanceof Error ? cause.message : "保存文案失败。");
    } finally {
      if (operation === generation.current) setBusy(false);
    }
  };
  return (
    <div className="long-text-editor">
      <p>
        {label} · 共 {document.char_count.toLocaleString()} 字 · 每次显示一段
      </p>
      <form
        onSubmit={(event) => {
          event.preventDefault();
          if (dirty) return;
          setOffset(0);
          setTrail([]);
          setQuery(search.trim());
        }}
      >
        <input
          aria-label={`${label}搜索`}
          value={search}
          maxLength={200}
          onChange={(event) => setSearch(event.target.value)}
        />
        <button type="submit" disabled={busy || dirty}>
          搜索
        </button>
      </form>
      {error ? <p role="alert">{error}</p> : null}
      {loaded ? (
        <>
          <textarea
            aria-label={label}
            className="creation-textarea"
            maxLength={5000}
            value={text}
            readOnly={readOnly || busy}
            onChange={(event) => {
              setText(event.target.value);
              setDirty(event.target.value !== loaded.text);
            }}
          />
          <p>
            第 {loaded.index + 1} 段 · 本段 {text.length} / 5000 字
            {dirty ? " · 有未保存修改" : ""}
          </p>
        </>
      ) : (
        <p>{busy ? "正在读取段落…" : "未找到匹配段落。"}</p>
      )}
      <div className="creation-actions">
        <button
          type="button"
          disabled={busy || dirty || !trail.length}
          onClick={() => {
            setOffset(trail[trail.length - 1] ?? 0);
            setTrail(trail.slice(0, -1));
          }}
        >
          上一段
        </button>
        <button
          type="button"
          disabled={busy || dirty || loaded?.next == null}
          onClick={() => {
            if (loaded?.next == null) return;
            setTrail([...trail, offset]);
            setOffset(loaded.next);
          }}
        >
          下一段
        </button>
        {!readOnly ? (
          <button
            type="button"
            disabled={busy || !dirty}
            onClick={() => void save()}
          >
            保存本段
          </button>
        ) : null}
        <button
          type="button"
          disabled={busy || dirty}
          onClick={() => {
            void exportTextDocument(document.id).catch((cause: unknown) =>
              setError(cause instanceof Error ? cause.message : "导出失败。"),
            );
          }}
        >
          导出全文 TXT
        </button>
      </div>
    </div>
  );
}
