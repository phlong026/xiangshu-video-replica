const PROJECT_WORKFLOW_STAGES = [
  "上传与拆解",
  "画面与人物",
  "口播与生成",
] as const;

export function ProjectWorkflowSteps({
  completedThrough,
  currentStep,
}: {
  completedThrough?: number;
  currentStep: number;
}) {
  const completedStage = completedThrough ?? currentStep - 1;

  return (
    <ol className="workflow-steps" aria-label="复刻项目流程">
      {PROJECT_WORKFLOW_STAGES.map((label, index) => {
        const stage = index + 1;
        const isComplete = stage <= completedStage;
        const isCurrent = stage === currentStep;
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
            <span aria-hidden="true">{isComplete ? "✓" : `0${stage}`}</span>
            <strong aria-current={isCurrent ? "step" : undefined} title={label}>
              {label}
            </strong>
          </li>
        );
      })}
    </ol>
  );
}
