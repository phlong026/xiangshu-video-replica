import { useCallback, useEffect, useMemo, useState } from "react";
import {
  type AccountDiagnosisAvailability,
  type AccountDiagnosisDimension,
  type AccountDiagnosisHistoryItem,
  type AccountDiagnosisResult,
  createAccountDiagnosis,
  getAccountDiagnosis,
  getAccountDiagnosisAvailability,
  listAccountDiagnoses,
} from "../api";
import { CustomerRechargeDialog } from "../customer/CustomerRechargeDialog";
import { useStudio } from "./context";
import { Button } from "./ui";
import "./account-diagnosis.css";

const LEVEL_LABEL: Record<string, string> = {
  high: "高危",
  medium: "中危",
  low: "低危",
};

const RATING_CLASS: Record<string, string> = {
  优质: "is-excellent",
  正常: "is-normal",
  待优化: "is-warning",
  风险: "is-risk",
};

function formatCount(value: number | null | undefined): string {
  if (value === null || value === undefined) return "—";
  if (value >= 100_000_000) return `${(value / 100_000_000).toFixed(1)} 亿`;
  if (value >= 10_000) return `${(value / 10_000).toFixed(1)} 万`;
  return String(value);
}

function normalized(dimension: AccountDiagnosisDimension): number {
  if (dimension.effective_full <= 0) return 0;
  return Math.round((dimension.score / dimension.effective_full) * 1000) / 10;
}

const RADAR_SIZE = 220;
const RADAR_CENTER = RADAR_SIZE / 2;
const RADAR_RADIUS = 86;

function radarPoint(index: number, total: number, ratio: number): string {
  const angle = (Math.PI * 2 * index) / total - Math.PI / 2;
  const r = RADAR_RADIUS * ratio;
  return `${RADAR_CENTER + Math.cos(angle) * r},${RADAR_CENTER + Math.sin(angle) * r}`;
}

/** 六维雷达：确定性 SVG（无图表库依赖），维度顺序与报告一致。 */
function DimensionRadar({
  dimensions,
}: {
  dimensions: AccountDiagnosisDimension[];
}) {
  if (dimensions.length === 0) return null;
  const total = dimensions.length;
  const rings = [0.25, 0.5, 0.75, 1];
  const values = dimensions.map((dimension) =>
    Math.max(0, Math.min(1, normalized(dimension) / 100)),
  );
  const polygon = values
    .map((ratio, index) => radarPoint(index, total, ratio))
    .join(" ");
  return (
    <svg
      className="diag-radar"
      viewBox={`0 0 ${RADAR_SIZE} ${RADAR_SIZE}`}
      role="img"
      aria-label="六维得分雷达图"
    >
      {rings.map((ring) => (
        <polygon
          key={ring}
          className="diag-radar-ring"
          points={dimensions
            .map((_, index) => radarPoint(index, total, ring))
            .join(" ")}
        />
      ))}
      {dimensions.map((dimension, index) => {
        const [x, y] = radarPoint(index, total, 1.18)
          .split(",")
          .map((value) => Number(value));
        return (
          <text
            key={dimension.key}
            className="diag-radar-label"
            x={x}
            y={y}
            textAnchor="middle"
            dominantBaseline="middle"
          >
            {dimension.label.slice(0, 4)}
          </text>
        );
      })}
      <polygon className="diag-radar-value" points={polygon} />
      {values.map((ratio, index) => {
        const [x, y] = radarPoint(index, total, ratio)
          .split(",")
          .map((value) => Number(value));
        return (
          <circle
            key={dimensions[index].key}
            className="diag-radar-dot"
            cx={x}
            cy={y}
            r={3}
          />
        );
      })}
    </svg>
  );
}

/** 历史分数折线：只统计 SUCCEEDED 报告，按时间升序。 */
function ScoreTrend({
  points,
}: {
  points: Array<{ score: number; label: string }>;
}) {
  if (points.length < 2) return null;
  const width = 320;
  const height = 72;
  const padding = 10;
  const scores = points.map((point) => point.score);
  const min = Math.min(...scores, 0);
  const max = Math.max(...scores, 100);
  const step = (width - padding * 2) / (points.length - 1);
  const path = points
    .map((point, index) => {
      const x = padding + index * step;
      const y =
        height -
        padding -
        ((point.score - min) / Math.max(1, max - min)) * (height - padding * 2);
      return `${index === 0 ? "M" : "L"}${x.toFixed(1)},${y.toFixed(1)}`;
    })
    .join(" ");
  const [lastX, lastY] = path
    .slice(path.lastIndexOf("L") + 1)
    .split(",")
    .map((value) => Number(value));
  return (
    <svg
      className="diag-trend"
      viewBox={`0 0 ${width} ${height}`}
      role="img"
      aria-label="历史评分趋势"
    >
      <path className="diag-trend-line" d={path} fill="none" />
      <circle className="diag-trend-dot" cx={lastX} cy={lastY} r={3.5} />
    </svg>
  );
}

function buildReportMarkdown(result: AccountDiagnosisResult): string {
  const doc = result.report;
  if (!doc) return "";
  const lines: string[] = [
    `# 账号诊断：${doc.account.nickname}`,
    "",
    `- 综合评分：${doc.score}（${doc.rating}，${doc.tier_label}量级）`,
    `- 粉丝 ${formatCount(doc.account.followerCount)} · 作品 ${formatCount(doc.account.awemeCount)}`,
    `- 数据口径：${doc.data_level === "L2" ? "播放口径（L2）" : "公开互动口径（L1）"} · 样本 ${doc.sample_size} 条`,
    `- 采集时间：${doc.collectedAt}`,
    "",
    "## 六维评分",
    "",
  ];
  for (const dimension of doc.dimensions) {
    lines.push(
      `- **${dimension.label}**：${normalized(dimension)}（${dimension.score}/${dimension.effective_full}）—— ${dimension.comment}`,
    );
  }
  if (doc.risks.length > 0) {
    lines.push("", "## 风险预警", "");
    for (const risk of doc.risks) {
      lines.push(
        `- ${risk.label}（${LEVEL_LABEL[risk.level] ?? risk.level}）：${risk.evidence}`,
      );
    }
  }
  if (result.narrative) {
    lines.push("", "## 诊断结论", "", result.narrative.summary, "");
    for (const action of result.narrative.actions) {
      lines.push(
        `1. **${action.title}**（${action.evidence}）→ ${action.suggestion}`,
      );
    }
  }
  lines.push(
    "",
    "## 近期作品",
    "",
    "| 标题 | 点赞 | 评论 | 分享 | 收藏 |",
    "| --- | --- | --- | --- | --- |",
  );
  for (const work of doc.works) {
    lines.push(
      `| ${work.recommended ? "★ " : ""}${work.title || "（无标题）"} | ${work.digg} | ${work.comment} | ${work.share} | ${work.collect} |`,
    );
  }
  lines.push("", `> 由众墅之家账号诊断生成 · ${new Date().toLocaleString()}`);
  return lines.join("\n");
}

function errorCode(cause: unknown): string | undefined {
  if (typeof cause !== "object" || cause === null) return undefined;
  const code = (cause as { code?: unknown }).code;
  return typeof code === "string" ? code : undefined;
}

export function AccountDiagnosisPage() {
  const { requireLogin, review, notify, navigate, customerAccount, state } =
    useStudio();
  const [input, setInput] = useState(state.accountDiagnosisInput ?? "");
  const [availability, setAvailability] =
    useState<AccountDiagnosisAvailability | null>(null);
  const [result, setResult] = useState<AccountDiagnosisResult | null>(null);
  const [history, setHistory] = useState<AccountDiagnosisHistoryItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [rechargeOpen, setRechargeOpen] = useState(false);

  const refreshAvailability = useCallback(async () => {
    try {
      setAvailability(await getAccountDiagnosisAvailability());
    } catch {
      // 可用状态读取失败不阻塞输入；真正的门禁由服务端在提交时给出。
    }
  }, []);

  const refreshHistory = useCallback(async () => {
    try {
      setHistory((await listAccountDiagnoses()).items);
    } catch {
      // 历史列表是辅助信息，失败时保持上一次结果。
    }
  }, []);

  useEffect(() => {
    void refreshAvailability();
    void refreshHistory();
  }, [refreshAvailability, refreshHistory]);

  async function submit() {
    const value = input.trim();
    if (!value) {
      setError("请粘贴抖音账号主页链接或输入抖音号。");
      return;
    }
    if (requireLogin) {
      requireLogin();
      return;
    }
    if (review) {
      notify("审核示例不执行真实诊断。");
      return;
    }
    setLoading(true);
    setError("");
    try {
      const next = await createAccountDiagnosis(value);
      setResult(next);
      await refreshAvailability();
      await refreshHistory();
    } catch (cause) {
      const code = errorCode(cause);
      const message =
        cause instanceof Error ? cause.message : "账号诊断失败，请稍后重试。";
      setError(message);
      // 会员门禁与积分不足都直接弹充值：不让用户自己去钱包里找入口。
      if (
        code === "ACCOUNT_DIAGNOSIS_MEMBER_REQUIRED" ||
        code === "INSUFFICIENT_CREDITS"
      ) {
        setRechargeOpen(true);
      }
    } finally {
      setLoading(false);
    }
  }

  async function openHistory(item: AccountDiagnosisHistoryItem) {
    if (item.status !== "SUCCEEDED") return;
    try {
      setResult(await getAccountDiagnosis(item.id));
      setError("");
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "报告读取失败。");
    }
  }

  async function copyWorkLink(awemeId: string) {
    const url = `https://www.douyin.com/video/${awemeId}`;
    try {
      await navigator.clipboard.writeText(url);
      notify("已复制视频链接，去工作台粘贴即可拆解。");
    } catch {
      notify(url);
    }
  }

  function startReplica(awemeId: string, title: string) {
    // 一键拆解：把作品链接作为直达意图带进工作台，由工作台自动发起复刻导入。
    navigate("workbench", {
      replicateIntentLink: `https://www.douyin.com/video/${awemeId}`,
    });
    notify(`正在工作台发起拆解：${title.slice(0, 20) || "选中作品"}`);
  }

  function exportReport() {
    if (!result) return;
    const markdown = buildReportMarkdown(result);
    const blob = new Blob([markdown], { type: "text/markdown;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    const nickname = result.report?.account.nickname ?? "账号";
    anchor.href = url;
    anchor.download = `账号诊断-${nickname}-${new Date().toISOString().slice(0, 10)}.md`;
    anchor.click();
    URL.revokeObjectURL(url);
    notify("报告已导出为 Markdown。");
  }

  const reportDoc = result?.report ?? null;
  const narrative = result?.narrative ?? null;

  const scoreTrend = useMemo(() => {
    const points = history
      .filter((item) => item.status === "SUCCEEDED" && item.score !== null)
      .map((item) => ({
        score: item.score as number,
        label: item.createdAt ?? "",
      }))
      .reverse();
    return points;
  }, [history]);

  const availabilityText = useMemo(() => {
    if (!availability) return null;
    if (!availability.enabled) return "账号诊断暂未开放";
    if (availability.freeQuota) return "新用户首单免费";
    if (availability.member) {
      return `单次 ${availability.priceCredits ?? "—"} 积分`;
    }
    return "需先购买充值套餐";
  }, [availability]);

  return (
    <section className="studio-home diag-page">
      <h2 className="studio-home-page-title">账号诊断</h2>

      <div className="diag-intro">
        <p>
          粘贴你的抖音主页链接（或抖音号），系统会拉取主页与近期作品， 从
          <strong>
            基础画像、内容生产力、互动健康度、内容质量、内容趋势、粉丝质量
          </strong>
          六个维度打分，并给出优先修复清单。
        </p>
        {availabilityText ? (
          <span className="diag-availability">{availabilityText}</span>
        ) : null}
      </div>

      <div className="diag-input-row">
        <input
          aria-label="抖音账号链接或抖音号"
          placeholder="粘贴 https://www.douyin.com/user/... 或输入抖音号"
          value={input}
          onChange={(event) => setInput(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter" && !loading) void submit();
          }}
        />
        <Button
          variant="primary"
          disabled={loading}
          onClick={() => void submit()}
        >
          {loading ? "正在诊断…" : "开始诊断"}
        </Button>
      </div>

      {loading ? (
        <p className="diag-status" role="status">
          正在拉取主页与近期作品并计算评分，通常 10 秒内完成…
        </p>
      ) : null}
      {error ? (
        <p className="diag-error" role="alert">
          {error}
        </p>
      ) : null}

      {reportDoc ? (
        <article className="diag-report">
          <header className="diag-report-head">
            <div className="diag-account">
              {reportDoc.account.avatarUrl ? (
                <img src={reportDoc.account.avatarUrl} alt="" />
              ) : (
                <span className="diag-account-fallback" aria-hidden="true">
                  {reportDoc.account.nickname.slice(0, 1) || "账"}
                </span>
              )}
              <div>
                <h3>{reportDoc.account.nickname || "未命名账号"}</h3>
                <p>
                  {reportDoc.account.uniqueId
                    ? `@${reportDoc.account.uniqueId}`
                    : ""}
                  {reportDoc.account.uniqueId ? " · " : ""}
                  粉丝 {formatCount(reportDoc.account.followerCount)} · 作品{" "}
                  {formatCount(reportDoc.account.awemeCount)}
                </p>
              </div>
            </div>
            <div className="diag-score-wrap">
              <DimensionRadar dimensions={reportDoc.dimensions} />
              <div
                className={`diag-score ${RATING_CLASS[reportDoc.rating] ?? ""}`}
              >
                <strong>{reportDoc.score}</strong>
                <span>
                  {reportDoc.rating} · {reportDoc.tier_label}
                </span>
              </div>
            </div>
          </header>

          <div className="diag-meta">
            <span>
              {reportDoc.data_level === "L2"
                ? "播放口径（L2）"
                : "公开互动口径（L1）"}
            </span>
            <span>样本 {reportDoc.sample_size} 条</span>
            {reportDoc.l2?.avgFinishRate !== null &&
            reportDoc.l2?.avgFinishRate !== undefined ? (
              <span>平均完播率 {reportDoc.l2.avgFinishRate}%</span>
            ) : null}
            {reportDoc.l2?.avgInteractRate !== null &&
            reportDoc.l2?.avgInteractRate !== undefined ? (
              <span>平均互动率 {reportDoc.l2.avgInteractRate}%</span>
            ) : null}
            {result?.isFree ? <span>本次免费</span> : null}
            {result?.cached ? <span>24 小时内缓存</span> : null}
            {reportDoc.low_confidence ? (
              <span className="diag-low-confidence">
                样本偏少，结论置信度有限
              </span>
            ) : null}
          </div>

          <section className="diag-dimensions">
            {reportDoc.dimensions.map((dimension) => {
              const value = normalized(dimension);
              return (
                <div className="diag-dimension" key={dimension.key}>
                  <div className="diag-dimension-head">
                    <span>{dimension.label}</span>
                    <strong>{value}</strong>
                  </div>
                  <div className="diag-bar" aria-hidden="true">
                    <i style={{ width: `${Math.min(100, value)}%` }} />
                  </div>
                  <p className="diag-dimension-comment">{dimension.comment}</p>
                  <ul>
                    {dimension.sub_scores
                      .filter((sub) => sub.counted)
                      .map((sub) => (
                        <li key={sub.key}>
                          <span>{sub.label}</span>
                          <em>
                            {sub.score}/{sub.full}
                          </em>
                          <small>{sub.detail}</small>
                        </li>
                      ))}
                  </ul>
                </div>
              );
            })}
          </section>

          {reportDoc.risks.length > 0 ? (
            <section className="diag-risks">
              <h4>风险预警</h4>
              <ul>
                {reportDoc.risks.map((risk) => (
                  <li key={risk.kind} className={`is-${risk.level}`}>
                    <strong>
                      {risk.label} · {LEVEL_LABEL[risk.level] ?? risk.level}
                    </strong>
                    <span>{risk.evidence}</span>
                  </li>
                ))}
              </ul>
            </section>
          ) : null}

          {narrative ? (
            <section className="diag-narrative">
              <h4>诊断结论</h4>
              <p>{narrative.summary}</p>
              {narrative.actions.length > 0 ? (
                <ol className="diag-actions">
                  {narrative.actions.map((action) => (
                    <li
                      key={`${action.dimension}-${action.title}-${action.evidence}`}
                    >
                      <strong>{action.title}</strong>
                      <span>{action.evidence}</span>
                      <p>{action.suggestion}</p>
                    </li>
                  ))}
                </ol>
              ) : null}
            </section>
          ) : null}

          <section className="diag-works">
            <div className="diag-section-head">
              <h4>近期作品</h4>
              <div className="diag-section-actions">
                <Button variant="quiet" onClick={exportReport}>
                  导出报告
                </Button>
                <Button variant="quiet" onClick={() => navigate("workbench")}>
                  去工作台拆解
                </Button>
              </div>
            </div>
            <table>
              <thead>
                <tr>
                  <th>标题</th>
                  <th>点赞</th>
                  <th>评论</th>
                  <th>分享</th>
                  <th>收藏</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {reportDoc.works.map((work) => (
                  <tr key={work.awemeId}>
                    <td>
                      {work.recommended ? (
                        <span className="diag-recommend">建议复刻</span>
                      ) : null}
                      {work.title || "（无标题）"}
                    </td>
                    <td>{formatCount(work.digg)}</td>
                    <td>{formatCount(work.comment)}</td>
                    <td>{formatCount(work.share)}</td>
                    <td>{formatCount(work.collect)}</td>
                    <td>
                      {work.recommended ? (
                        <Button
                          variant="quiet"
                          onClick={() => startReplica(work.awemeId, work.title)}
                        >
                          一键拆解
                        </Button>
                      ) : null}
                      <Button
                        variant="quiet"
                        onClick={() => void copyWorkLink(work.awemeId)}
                      >
                        复制链接
                      </Button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </section>
        </article>
      ) : null}

      {scoreTrend.length >= 2 ? (
        <section className="diag-history">
          <h4>评分趋势</h4>
          <ScoreTrend points={scoreTrend} />
        </section>
      ) : null}

      {history.length > 0 ? (
        <section className="diag-history">
          <h4>诊断历史</h4>
          <ul>
            {history.map((item) => (
              <li key={item.id}>
                <button
                  type="button"
                  onClick={() => void openHistory(item)}
                  disabled={item.status !== "SUCCEEDED"}
                >
                  <span>{item.account?.nickname || "账号"}</span>
                  <em>{item.score ?? "—"} 分</em>
                  <small>{item.rating ?? item.status}</small>
                </button>
              </li>
            ))}
          </ul>
        </section>
      ) : null}

      {customerAccount ? (
        <CustomerRechargeDialog
          isOpen={rechargeOpen}
          onClose={() => setRechargeOpen(false)}
          onOrderCreated={() => {
            setRechargeOpen(false);
            void refreshAvailability();
          }}
          onPaid={() => {
            setRechargeOpen(false);
            notify("充值到账，可以继续诊断了。");
            void refreshAvailability();
          }}
          onSessionExpired={customerAccount.onSessionExpired}
          store={customerAccount.store}
        />
      ) : null}
    </section>
  );
}
