import type { components } from "./generated/api";

const DEFAULT_API_BASE_URL = "http://127.0.0.1:8000";
const REQUEST_TIMEOUT_MS = 5_000;

type HealthResponse = components["schemas"]["HealthResponse"];

export type GenerationTask = {
  id: string;
  status: string;
  archive_status: string;
  quality_status: string;
  quality_issue_codes: string[];
  result_asset_id: string | null;
  result_url: string | null;
  prompt_snapshot: Record<string, unknown> | null;
};

export type BatchProgress = {
  total_count: number;
  terminal_count: number;
  progress_percent: number;
  counts: Record<string, number>;
};

export type GenerationBatch = {
  id: string;
  status: string;
  quantity: number;
  progress: BatchProgress;
  tasks: GenerationTask[];
};

export async function getHealth(): Promise<HealthResponse> {
  return requestJson<HealthResponse>("/health", "本地服务暂不可用");
}

export async function getGenerationBatch(
  batchId: string,
): Promise<GenerationBatch> {
  return requestJson<GenerationBatch>(
    `/api/generation-batches/${encodeURIComponent(batchId)}`,
    "任务批次暂不可用",
  );
}

async function requestJson<T>(path: string, errorPrefix: string): Promise<T> {
  const controller = new AbortController();
  const timeout = window.setTimeout(
    () => controller.abort(),
    REQUEST_TIMEOUT_MS,
  );

  try {
    const apiBaseUrl =
      import.meta.env.VITE_API_BASE_URL ?? DEFAULT_API_BASE_URL;
    const response = await fetch(`${apiBaseUrl}${path}`, {
      signal: controller.signal,
    });

    if (!response.ok) {
      throw new Error(`${errorPrefix}（${response.status}）`);
    }

    return (await response.json()) as T;
  } finally {
    window.clearTimeout(timeout);
  }
}
