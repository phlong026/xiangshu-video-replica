// 工作台生成任务状态的统一中文文案。
//
// 2026-10-31 前TaskRecordsPanel 与 VideoResultStage 各持一份映射，PENDING/
// QUEUED 两处分别是「等待中/排队中」与「生成中」——用户在结果面板与任务
// 记录里看到同一状态两种说法（VideoResultStage 自己的步骤条也在区分
// 「排队中/生成中」）。收敛为单一映射，以任务记录的细粒度口径为准。
//
// 注意与 admin/ui/vocabulary 的 GENERATION_STATUS_LABELS 是两套口径：
// 管理端面向运营（PENDING=待处理、SUCCEEDED=成功），工作台面向客户
// （PENDING=等待中、SUCCEEDED=已完成）；合并两套属产品文案决策，不在
// 本次冗余清理范围内。
const TASK_STATUS_LABELS: Record<string, string> = {
  ARCHIVED: "已归档",
  ARCHIVING: "归档中",
  ARCHIVE_FAILED: "归档失败",
  CANCELLED: "已取消",
  COMPLETED: "已归档",
  COMPLETED_WITH_FAILURES: "部分失败",
  FAILED: "失败",
  NEEDS_ATTENTION: "需要处理",
  PENDING: "等待中",
  QUALITY_FAILED: "结果暂不可用",
  QUEUED: "排队中",
  RUNNING: "生成中",
  SUBMISSION_UNCERTAIN: "提交结果待确认",
  SUBMITTING: "提交中",
  SUCCEEDED: "已完成",
};

/** 查映射并回退到原始值——未知状态原样展示，便于发现新枚举。 */
export function formatTaskStatus(status: string): string {
  return TASK_STATUS_LABELS[status] ?? status;
}
