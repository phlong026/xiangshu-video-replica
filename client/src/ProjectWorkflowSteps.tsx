const PROJECT_WORKFLOW_STEPS = [
  "上传参考视频",
  "拆解视频",
  "选择角色版本",
  "选择起始帧",
  "确认人物参考",
  "确认置换首帧",
  "确认口播稿",
  "锁定 Prompt",
  "设置数量并生成",
  "预览与下载",
] as const;

export function ProjectWorkflowSteps({
  completedThrough,
  currentStep,
}: {
  completedThrough?: number;
  currentStep: number;
}) {
  const completedStep = completedThrough ?? currentStep - 1;

  return (
    <ol className="workflow-steps" aria-label="复刻项目流程">
      {PROJECT_WORKFLOW_STEPS.map((label, index) => {
        const step = index + 1;
        const isComplete = step <= completedStep;
        const isCurrent = step === currentStep;
        return (
          <li
            className={
              isCurrent
                ? "workflow-step workflow-step--current"
                : isComplete
                  ? "workflow-step workflow-step--complete"
                  : "workflow-step"
            }
            key={label}
          >
            <span aria-hidden="true">{isComplete ? "✓" : step}</span>
            <strong aria-current={isCurrent ? "step" : undefined} title={label}>
              {label}
            </strong>
          </li>
        );
      })}
    </ol>
  );
}
