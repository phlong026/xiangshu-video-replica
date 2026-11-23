import type { SourceReview } from "./api";

export function SourceReviewNotice({
  review,
}: {
  review: SourceReview | null | undefined;
}) {
  if (!review)
    return (
      <p className="status-note" role="status" aria-label="原片校对状态">
        此结果未记录原片校对状态。
      </p>
    );
  const passed =
    review.status === "PASSED" &&
    review.attempted &&
    review.issues.length === 0;
  const stale = review.issues.some(
    (issue) => issue.code === "SOURCE_REVIEW_STALE",
  );
  const title = passed
    ? "原片校对已通过"
    : stale
      ? "分镜已修改，原片校对待确认"
      : review.status === "UNAVAILABLE"
        ? "原片校对未完成"
        : "拆解已完成，原片校对有待确认";
  return (
    <div
      className={
        passed
          ? "source-review-notice"
          : "source-review-notice shot-table-review"
      }
      role="status"
      aria-label="原片校对状态"
    >
      <strong>{title}</strong>
      {!passed ? <p>已保留拆解结果，可继续核对分镜中的具体项目。</p> : null}
      {review.issues.length ? (
        <ul>
          {review.issues.map((issue) => (
            <li key={`${issue.code}:${issue.shot_id ?? ""}:${issue.message}`}>
              {issue.shot_id ? `${issue.shot_id}：` : ""}
              {issue.message}
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}
