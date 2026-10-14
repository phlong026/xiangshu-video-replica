import { useCallback, useEffect, useState } from "react";
import { listWorkerObservations, type WorkerObservations } from "../api.admin";
import { PageBanner } from "./ui/PageBanner";
import { StatusBadge } from "./ui/StatusBadge";

/**
 * M4 队列与 Worker 观测（ADM-11）：任务积压从"客户报障才知道"变成一眼可见。
 *
 * 三块：各队列按状态计数与最高龄任务；oral 隔离区（冻结预扣积分，需人工
 * reconcile）；卡住的素材准备。全部只读，刷新按钮手动拉取。
 */
const QUEUE_LABELS: Array<{
  key: keyof WorkerObservations["queues"];
  label: string;
  hint: string;
}> = [
  { key: "video", label: "视频任务", hint: "视频生成后台进程" },
  { key: "oral", label: "口播任务", hint: "数字人口播后台进程" },
  {
    key: "viralRefresh",
    label: "采集/转存",
    hint: "搜索与素材准备后台进程",
  },
  {
    key: "publish",
    label: "发布队列",
    hint: "抖音/视频号投递后台进程",
  },
];

function staleTone(minutes: number | null, threshold: number) {
  if (minutes == null) return "neutral" as const;
  if (minutes > threshold * 2) return "danger" as const;
  if (minutes > threshold) return "warn" as const;
  return "good" as const;
}

export function WorkerObservationsPage({
  readOnly = false,
}: {
  readOnly?: boolean;
}) {
  const [data, setData] = useState<WorkerObservations | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    try {
      setLoading(true);
      setError("");
      setData(await listWorkerObservations());
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "读取队列观测失败");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  return (
    <section
      className="admin-panel admin-worker-observations"
      aria-label="队列观测"
    >
      <header>
        <p className="admin-hint">
          各类后台任务队列的实时积压。高龄任务超过阈值先查对应后台进程
          与日志；隔离区任务冻结了客户积分，按失败诊断页的建议人工核对。
        </p>
        {!readOnly ? (
          <button type="button" onClick={() => void load()} disabled={loading}>
            刷新
          </button>
        ) : null}
      </header>
      {error ? <PageBanner tone="error">{error}</PageBanner> : null}
      {loading && !data ? (
        <p role="status">正在读取队列观测…</p>
      ) : data ? (
        <>
          <dl className="admin-viral-tiles">
            {QUEUE_LABELS.map(({ key, label }) => (
              <div className="admin-viral-tile" key={key}>
                <dt>{label}</dt>
                <dd>
                  {data.queues[key].length === 0
                    ? "0"
                    : data.queues[key]
                        .map(
                          (row) =>
                            `${row.status} ${row.count}${
                              "oldest_minutes" in row && row.oldest_minutes
                                ? `（最高龄 ${row.oldest_minutes} 分）`
                                : ""
                            }`,
                        )
                        .join(" · ")}
                </dd>
              </div>
            ))}
          </dl>
          <h3>需要关注</h3>
          <ul className="dashboard-todos">
            <li>
              <span>
                视频任务高龄（阈值 {data.attention.videoStaleThresholdMinutes}{" "}
                分）
              </span>
              <StatusBadge
                tone={staleTone(
                  data.attention.videoStaleMinutes,
                  data.attention.videoStaleThresholdMinutes,
                )}
              >
                {data.attention.videoStaleMinutes == null
                  ? "无任务"
                  : `${data.attention.videoStaleMinutes} 分`}
              </StatusBadge>
            </li>
            <li>
              <span>
                口播任务高龄（阈值 {data.attention.oralStaleThresholdMinutes}{" "}
                分）
              </span>
              <StatusBadge
                tone={staleTone(
                  data.attention.oralStaleMinutes,
                  data.attention.oralStaleThresholdMinutes,
                )}
              >
                {data.attention.oralStaleMinutes == null
                  ? "无任务"
                  : `${data.attention.oralStaleMinutes} 分`}
              </StatusBadge>
            </li>
            <li>
              <span>
                发布队列高龄（阈值 {data.attention.publishStaleThresholdMinutes}{" "}
                分）
              </span>
              <StatusBadge
                tone={staleTone(
                  data.attention.publishStaleMinutes,
                  data.attention.publishStaleThresholdMinutes,
                )}
              >
                {data.attention.publishStaleMinutes == null
                  ? "无任务"
                  : `${data.attention.publishStaleMinutes} 分`}
              </StatusBadge>
            </li>
            <li>
              <span>口播隔离区（提交结果未知，冻结积分）</span>
              <StatusBadge
                tone={
                  data.attention.oralQuarantinedTasks +
                    data.attention.oralQuarantinedAvatars +
                    data.attention.oralQuarantinedVoices >
                  0
                    ? "danger"
                    : "good"
                }
              >
                口播 {data.attention.oralQuarantinedTasks} · 分身{" "}
                {data.attention.oralQuarantinedAvatars} · 声音{" "}
                {data.attention.oralQuarantinedVoices}
              </StatusBadge>
            </li>
            <li>
              <span>卡住的素材准备（RUNNING 超 10 分钟）</span>
              <StatusBadge
                tone={data.attention.stuckPreparations > 0 ? "warn" : "good"}
              >
                {data.attention.stuckPreparations} 项
                {data.attention.stuckPreparations > 0
                  ? `（最高龄 ${data.attention.stuckPreparationOldestMinutes} 分）`
                  : ""}
              </StatusBadge>
            </li>
          </ul>
          <p className="admin-hint">{data.rule}</p>
        </>
      ) : null}
    </section>
  );
}
