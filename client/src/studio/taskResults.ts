import type { StudioTask } from "./types";

/** 任务是否已有可预览 / 下载 / 保存的成片：生成批次按可交付条数判断，
 * 部分完成或待核对的批次里已成功的成片同样可用，不能只认「已完成」。 */
export function taskHasDeliverable(
  task: Pick<StudioTask, "status" | "backendKind" | "deliverableCount">,
): boolean {
  if (task.status === "completed") return true;
  return (
    task.backendKind === "generation_batch" && (task.deliverableCount ?? 0) > 0
  );
}
