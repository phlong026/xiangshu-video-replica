import { useEffect, useRef, useState } from "react";

export function SourceRangeDialog({ duration, onConfirm, onClose }: {
  duration: number;
  onConfirm: (start: number, end: number) => Promise<void>;
  onClose: () => void;
}) {
  const [start, setStart] = useState(0);
  const [end, setEnd] = useState(Math.min(180, duration));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const mounted = useRef(true);
  useEffect(() => () => { mounted.current = false; }, []);
  const length = end - start;
  const valid = Number.isFinite(start) && Number.isFinite(end) && start >= 0 && end <= duration && length >= 4 && length <= 180;
  return <div className="studio-dialog-backdrop"><section className="studio-dialog" role="dialog" aria-modal="true" aria-labelledby="source-range-title">
    <h2 id="source-range-title">选择需要拆解的视频片段</h2>
    <p>原视频共 {duration.toFixed(1)} 秒。请选择 4–180 秒的连续片段，完整原片和已提取文案会保留。</p>
    <label>开始时间（秒）<input type="number" min={0} max={duration} step="0.1" value={start} disabled={busy} onChange={(event) => setStart(Number(event.target.value))} /></label>
    <label>结束时间（秒）<input type="number" min={0} max={duration} step="0.1" value={end} disabled={busy} onChange={(event) => setEnd(Number(event.target.value))} /></label>
    <p>{valid ? `选中 ${length.toFixed(1)} 秒，将进入${length <= 30 ? "普通复刻" : "长视频拆解"}。` : "请选择原视频内 4–180 秒的片段。"}</p>
    {error ? <p role="alert">{error}</p> : null}
    <button type="button" disabled={busy || !valid} onClick={() => {
      setBusy(true); setError("");
      void onConfirm(start, end).catch((cause: unknown) => { if (mounted.current) setError(cause instanceof Error ? cause.message : "片段处理失败，请重试。"); }).finally(() => { if (mounted.current) setBusy(false); });
    }}>{busy ? "正在处理选段…" : "拆解选中片段"}</button>
    <button type="button" onClick={onClose}>关闭</button>
  </section></div>;
}
