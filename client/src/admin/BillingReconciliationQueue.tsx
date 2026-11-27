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
import { useHashParam } from "./ui/useHashParam";
import { formatDateTime } from "./ui/vocabulary";

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
  can_verify_generation?: boolean;
  generation_check_status?: string | null;
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
  ESCALATED: "需要人工处理",
  RESOLVED: "已完成核对",
};

type StateFilter = "ALL" | ReconciliationItem["state"];

function waitingTime(createdAt: string, resolvedAt: string | null): string {
  const end = resolvedAt ? Date.parse(resolvedAt) : Date.now();
  const hours = Math.max(0, (end - Date.parse(createdAt)) / 3_600_000);
  if (!Number.isFinite(hours)) return "—";
  if (hours < 1) return "不到 1 小时";
  if (hours < 24) return `${Math.floor(hours)} 小时`;
  return `${Math.floor(hours / 24)} 天 ${Math.floor(hours % 24)} 小时`;
}

export function BillingReconciliationQueue({
  readOnly,
}: {
  readOnly: boolean;
}) {
  const [items, setItems] = useState<ReconciliationItem[] | null>(null);
  const [total, setTotal] = useState<number | null>(null);
  const [stateFilter, setStateFilter] = useHashParam<StateFilter>(
    "status",
    "ALL",
    ["ALL", "OPEN", "ESCALATED", "RESOLVED"],
  );
  const [error, setError] = useState("");
  // 忙碌用计数器而不是布尔：扫描/收口是"写"，写完还要触发一次读。若用布尔，
  // 慢写操作的 finally 会把并行中读请求的忙碌态提前清掉，按钮提前解禁。
  const [busyCount, setBusyCount] = useState(0);
  const busy = busyCount > 0;
  // 收口交互：正在填写收口备注的条目 id 与备注文本。
  const [resolvingId, setResolvingId] = useState<string | null>(null);
  const [resolveNote, setResolveNote] = useState("");
  const [verifyingId, setVerifyingId] = useState<string | null>(null);
  const [verifyReason, setVerifyReason] = useState("");
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
        "卡单清单加载失败，请稍后重试。",
      );
      if (seq !== loadSeq.current) return;
      setItems(body.items);
      setTotal(body.total);
    } catch (cause) {
      if (seq !== loadSeq.current) return;
      setError(cause instanceof Error ? cause.message : "卡单清单加载失败");
    } finally {
      setBusyCount((count) => Math.max(0, count - 1));
    }
  }, []);

  // biome-ignore lint/correctness/useExhaustiveDependencies: load 经 ref 读当前筛选（防闭包旧值），但筛选切换必须重触发本 effect——有意的额外依赖
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
        "暂时无法查找卡单，请稍后重试。",
      );
      await load();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "查找卡单失败");
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
        "未能完成核对，请稍后重试。",
      );
      setResolvingId(null);
      setResolveNote("");
      await load();
    } catch (cause) {
      // 409（计费仍未终态）等信息会带后端原文展示，不吞。
      setError(cause instanceof Error ? cause.message : "未能完成核对");
    } finally {
      setBusyCount((count) => Math.max(0, count - 1));
    }
  }

  async function submitVerify(item: ReconciliationItem) {
    if (readOnly || busy || !verifyReason.trim()) return;
    setBusyCount((count) => count + 1);
    setError("");
    try {
      await adminWrite(
        `/api/control/billing-reconciliation/items/${encodeURIComponent(item.id)}/verify-generation`,
        {},
        verifyReason,
        "未能提交视频查询，请稍后重试。",
      );
      setVerifyingId(null);
      setVerifyReason("");
      await load();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "未能提交视频查询");
    } finally {
      setBusyCount((count) => Math.max(0, count - 1));
    }
  }

  return (
    <section className="admin-panel" aria-label="卡单核对">
      <header className="admin-toolbar">
        <div>
          <h2>卡单核对</h2>
          <p className="admin-hint">
            这些任务因账务待查而产生过积分冻结。请先核实任务结果，再确认积分已扣除或解除冻结。需要人工调整积分时，请走审批。
          </p>
        </div>
        <button
          type="button"
          disabled={readOnly || busy}
          onClick={() => void rescan()}
        >
          {busy ? "处理中…" : "查找新卡单"}
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
            <option value="OPEN">{STATE_LABELS.OPEN}</option>
            <option value="ESCALATED">{STATE_LABELS.ESCALATED}</option>
            <option value="RESOLVED">{STATE_LABELS.RESOLVED}</option>
          </select>
        </label>
        {total !== null ? (
          <p className="admin-hint">共 {total} 条（当前筛选）</p>
        ) : null}
        <button type="button" disabled={busy} onClick={() => void load()}>
          刷新处理状态
        </button>
      </div>
      {error ? (
        <p role="alert" className="settings-error">
          {error}
        </p>
      ) : null}
      {items === null ? (
        <p className="admin-hint">正在加载卡单…</p>
      ) : items.length === 0 ? (
        <p className="admin-hint">当前筛选下没有卡单记录。</p>
      ) : (
        <DataTable
          ariaLabel="卡单核对明细"
          headers={
            <>
              <th>状态</th>
              <th>业务</th>
              <th>原因</th>
              <th>影响客户</th>
              <th>等待时长</th>
              <th>检查记录</th>
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
                    查询编号 {item.provider_receipt_id}
                  </small>
                ) : null}
              </td>
              <td>{item.owner_user_id ?? "—"}</td>
              <td>{waitingTime(item.created_at, item.resolved_at)}</td>
              <td>
                检查 {item.check_count} 次
                {item.last_checked_at && (
                  <small className="admin-hint">
                    上次检查 {formatDateTime(item.last_checked_at)}
                  </small>
                )}
              </td>
              <td>{item.next_step}</td>
              <td>
                {item.generation_check_status && (
                  <p role="status" className="admin-hint">
                    {item.generation_check_status === "PENDING"
                      ? "已提交查询，稍后刷新查看结果"
                      : item.generation_check_status === "COMPLETED"
                        ? "视频查询已结束，请核对积分记录"
                        : "视频查询未完成，请到生成记录查看任务"}
                  </p>
                )}
                {!readOnly &&
                  item.can_verify_generation &&
                  item.generation_check_status !== "PENDING" &&
                  (verifyingId === item.id ? (
                    <div className="admin-resolve-form">
                      <input
                        aria-label="查询原因"
                        value={verifyReason}
                        onChange={(event) =>
                          setVerifyReason(event.target.value)
                        }
                      />
                      <button
                        type="button"
                        disabled={busy || !verifyReason.trim()}
                        onClick={() => void submitVerify(item)}
                      >
                        提交查询
                      </button>
                      <button
                        type="button"
                        disabled={busy}
                        onClick={() => {
                          setVerifyingId(null);
                          setVerifyReason("");
                        }}
                      >
                        取消查询
                      </button>
                    </div>
                  ) : (
                    <button
                      type="button"
                      disabled={busy}
                      onClick={() => {
                        setResolvingId(null);
                        setVerifyingId(item.id);
                      }}
                    >
                      查询视频结果
                    </button>
                  ))}
                {item.state !== "RESOLVED" && !readOnly ? (
                  resolvingId === item.id ? (
                    <div className="admin-resolve-form">
                      <input
                        name={`resolve-note-${item.id}`}
                        value={resolveNote}
                        aria-label="核对结果"
                        placeholder="填写核对结果和凭证（必填）"
                        onChange={(event) => setResolveNote(event.target.value)}
                      />
                      <button
                        type="button"
                        disabled={busy || resolveNote.trim().length === 0}
                        onClick={() => void submitResolve(item)}
                      >
                        确认完成
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
                      完成核对
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
