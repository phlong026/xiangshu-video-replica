// T36 / T40：卡单积分核对队列的管理端视图。
//
// 数据来自 /api/control/billing-reconciliation（第三批 T40 交付的后端队列）：
// 每条 = 一个「未闭合的计费预留」。运营在这里回答两件事——要不要人工核对、
// 找谁；真正的资金动作仍走专项对账/调账审批，本页不提供任何退分按钮。
import { useCallback, useEffect, useState } from "react";
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

function waitingHours(created_at: string): number {
  return Math.max(0, (Date.now() - new Date(created_at).getTime()) / 3_600_000);
}

export function BillingReconciliationQueue({
  readOnly,
}: {
  readOnly: boolean;
}) {
  const [items, setItems] = useState<ReconciliationItem[] | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    setBusy(true);
    setError("");
    try {
      const body = await adminRead<{
        items: ReconciliationItem[];
        total: number;
      }>(
        "/api/control/billing-reconciliation/queue?limit=100",
        "核对队列读取失败，请稍后重试。",
      );
      setItems(body.items);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "核对队列读取失败");
    } finally {
      setBusy(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  async function rescan() {
    if (readOnly || busy) return;
    setBusy(true);
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
      setBusy(false);
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
      {error ? (
        <p role="alert" className="settings-error">
          {error}
        </p>
      ) : null}
      {items === null ? (
        <p className="admin-hint">正在读取队列…</p>
      ) : items.length === 0 ? (
        <p className="admin-hint">当前没有卡单：所有计费预留均已闭合。</p>
      ) : (
        <DataTable
          ariaLabel="卡单积分核对队列"
          headers={
            <tr>
              <th>状态</th>
              <th>业务</th>
              <th>原因</th>
              <th>影响客户</th>
              <th>等待时长</th>
              <th>重查次数</th>
              <th>下一步</th>
            </tr>
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
            </tr>
          ))}
        </DataTable>
      )}
    </section>
  );
}
