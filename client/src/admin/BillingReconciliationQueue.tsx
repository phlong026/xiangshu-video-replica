// T36 / T40：卡单积分核对队列的管理端视图。
//
// 数据来自 /api/control/billing-reconciliation（第三批 T40 交付的后端队列）：
// 每条 = 一个「未闭合的计费预留」。运营在这里回答两件事——要不要人工核对、
// 找谁；真正的资金动作仍走专项对账/调账审批。
//
// 2026-11-15 审计补闭环：此前本页只有"已升级人工"的文案、没有收口入口，
// 卡单升级后只能永远挂在这里。现在行内提供「收口」——底层计费仍是 PENDING
// 时后端会 409 并指回专项对账（这个 409 会原样展示，不吞错误）；同时补上
// 状态筛选与总数，队列大的时候能只看待处理的。
import { useCallback, useEffect, useRef, useState } from "react";
import { adminRead, adminWrite } from "../api.admin";
import { DataTable } from "./ui/DataTable";

type ReconciliationItem = {
  id: string;
  service: string;
  source_kind: string;
  source_id: string;
  billing_round: number;
  owner_user_id: string | null;
  reason: string;
  reason_label: string;
  detail: Record<string, unknown>;
  provider_receipt_id: string | null;
  state: "OPEN" | "ESCALATED" | "RESOLVED";
  check_count: number;
  last_checked_at: string | null;
  escalated_at: string | null;
  resolution: string | null;
  resolution_note: string | null;
  resolved_by: string | null;
  resolved_at: string | null;
  created_at: string;
  updated_at: string;
  next_step: string;
};

const SERVICE_LABELS: Record<string, string> = {
  video_768p: "视频生成 768p",
  video_2k: "视频生成 2K",
  oral: "数字人口播",
  first_frame: "首帧生成",
  asr: "视频转写",
  character: "人物生成",
};

const STATE_LABELS: Record<ReconciliationItem["state"], string> = {
  OPEN: "待核对",
  ESCALATED: "已升级人工",
  RESOLVED: "已收口",
};

type StateFilter = "ALL" | ReconciliationItem["state"];

function waitingHours(created_at: string): number {
  return Math.max(0, (Date.now() - new Date(created_at).getTime()) / 3_600_000);
}

export function BillingReconciliationQueue({
  readOnly,
}: {
  readOnly: boolean;
}) {
  const [items, setItems] = useState<ReconciliationItem[] | null>(null);
  const [total, setTotal] = useState<number | null>(null);
  const [stateFilter, setStateFilter] = useState<StateFilter>("ALL");
  const [error, setError] = useState("");
  // 忙碌用计数器而不是布尔：扫描/收口是"写"，写完还要触发一次读。若用布尔，
  // 慢写操作的 finally 会把并行中读请求的忙碌态提前清掉，按钮提前解禁。
  const [busyCount, setBusyCount] = useState(0);
  const busy = busyCount > 0;
  // 收口交互：正在填写收口备注的条目 id 与备注文本。
  const [resolvingId, setResolvingId] = useState<string | null>(null);
  const [resolveNote, setResolveNote] = useState("");
  // 请求序号：筛选切换后早发请求可能晚到，过期响应一律丢弃，
  // 不让旧数据覆盖新筛选下的列表。
  const loadSeq = useRef(0);
  // 筛选的最新值放 ref：扫描/收口完成后的刷新必须取"当下"的筛选，不能用
  // 事件闭包里的旧值（旧值会借最新序号把新筛选下的列表覆盖掉）。
  const stateFilterRef = useRef<StateFilter>(stateFilter);
  useEffect(() => {
    stateFilterRef.current = stateFilter;
  }, [stateFilter]);

  const load = useCallback(async () => {
    const state = stateFilterRef.current;
    const seq = ++loadSeq.current;
    setBusyCount((count) => count + 1);
    setError("");
    try {
      const query = state === "ALL" ? "" : `&state=${state}`;
      const body = await adminRead<{
        items: ReconciliationItem[];
        total: number;
      }>(
        `/api/control/billing-reconciliation/queue?limit=100${query}`,
        "核对队列读取失败，请稍后重试。",
      );
      if (seq !== loadSeq.current) return;
      setItems(body.items);
      setTotal(body.total);
    } catch (cause) {
      if (seq !== loadSeq.current) return;
      setError(cause instanceof Error ? cause.message : "核对队列读取失败");
    } finally {
      setBusyCount((count) => Math.max(0, count - 1));
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load, stateFilter]);

  async function rescan() {
    if (readOnly || busy) return;
    setBusyCount((count) => count + 1);
    try {
      await adminWrite(
        "/api/control/billing-reconciliation/scan",
        {},
        "管理端手动触发卡单扫描",
        "核对队列扫描暂不可用，请稍后重试。",
      );
      await load();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "扫描失败");
    } finally {
      setBusyCount((count) => Math.max(0, count - 1));
    }
  }

  async function submitResolve(item: ReconciliationItem) {
    if (readOnly || busy) return;
    setBusyCount((count) => count + 1);
    setError("");
    try {
      await adminWrite(
        `/api/control/billing-reconciliation/items/${item.id}/resolve`,
        { note: resolveNote },
        "人工核对收口卡单计费预留",
        "收口请求失败，请稍后重试。",
      );
      setResolvingId(null);
      setResolveNote("");
      await load();
    } catch (cause) {
      // 409（计费仍未终态）等信息会带后端原文展示，不吞。
      setError(cause instanceof Error ? cause.message : "收口失败");
    } finally {
      setBusyCount((count) => Math.max(0, count - 1));
    }
  }

  return (
    <section className="admin-panel" aria-label="卡单积分核对队列">
      <header className="admin-toolbar">
        <div>
          <h2>卡单积分核对队列</h2>
          <p className="admin-hint">
            未闭合的计费预留：证据齐了自动结算/释放；未知结果与孤儿源只升级
            人工，系统绝不自动退分。收口走各业务专项对账或调账审批。
          </p>
        </div>
        <button
          type="button"
          disabled={readOnly || busy}
          onClick={() => void rescan()}
        >
          {busy ? "处理中…" : "立即扫描"}
        </button>
      </header>
      <div className="admin-toolbar">
        <label className="admin-toolbar__field">
          <span>状态</span>
          <select
            value={stateFilter}
            onChange={(event) =>
              setStateFilter(event.target.value as StateFilter)
            }
          >
            <option value="ALL">全部状态</option>
            <option value="OPEN">待核对</option>
            <option value="ESCALATED">已升级人工</option>
            <option value="RESOLVED">已收口</option>
          </select>
        </label>
        {total !== null ? (
          <p className="admin-hint">共 {total} 条（当前筛选）</p>
        ) : null}
      </div>
      {error ? (
        <p role="alert" className="settings-error">
          {error}
        </p>
      ) : null}
      {items === null ? (
        <p className="admin-hint">正在读取队列…</p>
      ) : items.length === 0 ? (
        <p className="admin-hint">当前筛选下没有卡单记录。</p>
      ) : (
        <DataTable
          ariaLabel="卡单积分核对队列"
          headers={
            <>
              <th>状态</th>
              <th>业务</th>
              <th>原因</th>
              <th>影响客户</th>
              <th>等待时长</th>
              <th>重查次数</th>
              <th>下一步</th>
              <th>操作</th>
            </>
          }
        >
          {items.map((item) => (
            <tr key={item.id} data-state={item.state}>
              <td>{STATE_LABELS[item.state]}</td>
              <td>{SERVICE_LABELS[item.service] ?? item.service}</td>
              <td>
                <b>{item.reason_label}</b>
                {item.provider_receipt_id ? (
                  <small className="admin-hint">
                    {" "}
                    请求标识 {item.provider_receipt_id}
                  </small>
                ) : null}
              </td>
              <td>{item.owner_user_id ?? "—"}</td>
              <td>{waitingHours(item.created_at).toFixed(1)} 小时</td>
              <td>{item.check_count}</td>
              <td>{item.next_step}</td>
              <td>
                {item.state !== "RESOLVED" && !readOnly ? (
                  resolvingId === item.id ? (
                    <div className="admin-resolve-form">
                      <input
                        name={`resolve-note-${item.id}`}
                        value={resolveNote}
                        placeholder="核对凭据/结论备注（必填）"
                        onChange={(event) => setResolveNote(event.target.value)}
                      />
                      <button
                        type="button"
                        disabled={busy || resolveNote.trim().length === 0}
                        onClick={() => void submitResolve(item)}
                      >
                        确认收口
                      </button>
                      <button
                        type="button"
                        disabled={busy}
                        onClick={() => {
                          setResolvingId(null);
                          setResolveNote("");
                        }}
                      >
                        取消
                      </button>
                    </div>
                  ) : (
                    <button
                      type="button"
                      disabled={busy}
                      onClick={() => {
                        setResolvingId(item.id);
                        setResolveNote("");
                      }}
                    >
                      收口
                    </button>
                  )
                ) : (
                  "—"
                )}
              </td>
            </tr>
          ))}
        </DataTable>
      )}
    </section>
  );
}
