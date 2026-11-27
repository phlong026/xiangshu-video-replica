import { useEffect, useState } from "react";
import { adminRead } from "../api.admin";
import { PageBanner } from "./ui/PageBanner";

const queues = [
  { key: "pending", label: "待结算" },
  { key: "unknown_cost", label: "待核对成本" },
  { key: "unknown_revenue", label: "待核对收入" },
  { key: "all_cost", label: "累计成本待核对" },
] as const;

function queueScope(query: string, key: (typeof queues)[number]["key"]) {
  const params = new URLSearchParams(query);
  params.set("attention", key === "all_cost" ? "unknown_cost" : key);
  if (key === "all_cost") params.set("start", "2019-01-01");
  return params;
}
export function CostQueues({
  query,
  revision,
  onPick,
}: {
  query: string;
  revision: number;
  onPick: (query: string) => void;
}) {
  const [counts, setCounts] = useState<Record<string, number | null>>({});
  useEffect(() => {
    void revision;
    let active = true;
    setCounts({});
    void Promise.all(
      queues.map(async (q) => {
        const p = queueScope(query, q.key);
        p.set("limit", "1");
        try {
          const r = await adminRead<{ total: number }>(
            `/api/control/billing/operations?${p}`,
            "读取待核对队列失败",
          );
          return [q.key, r.total] as const;
        } catch {
          return [q.key, null] as const;
        }
      }),
    ).then((r) => {
      if (active) setCounts(Object.fromEntries(r));
    });
    return () => {
      active = false;
    };
  }, [query, revision]);
  const range = new URLSearchParams(query);
  return (
    <section className="economics-kpis cost-queues" aria-label="待核对队列">
      {Object.values(counts).includes(null) && (
        <PageBanner tone="error">
          部分队列计数读取失败，可点击「查询」重试。
        </PageBanner>
      )}
      {queues.map((q) => (
        <article key={q.key}>
          <button
            type="button"
            disabled={counts[q.key] == null}
            onClick={() => onPick(queueScope(query, q.key).toString())}
          >
            <span>{q.label}</span>
            <strong>
              {counts[q.key] === null ? "读取失败" : (counts[q.key] ?? "…")}
            </strong>
            <small>
              {q.key === "all_cost" ? "2019-01-01" : range.get("start")} 至{" "}
              {range.get("end")}
            </small>
          </button>
        </article>
      ))}
    </section>
  );
}
