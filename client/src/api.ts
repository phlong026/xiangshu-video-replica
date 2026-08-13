import type { components } from "./generated/api";

const DEFAULT_API_BASE_URL = "http://127.0.0.1:8000";
const REQUEST_TIMEOUT_MS = 5_000;

type HealthResponse = components["schemas"]["HealthResponse"];

export async function getHealth(): Promise<HealthResponse> {
  const controller = new AbortController();
  const timeout = window.setTimeout(
    () => controller.abort(),
    REQUEST_TIMEOUT_MS,
  );

  try {
    const apiBaseUrl =
      import.meta.env.VITE_API_BASE_URL ?? DEFAULT_API_BASE_URL;
    const response = await fetch(`${apiBaseUrl}/health`, {
      signal: controller.signal,
    });

    if (!response.ok) {
      throw new Error(`本地服务暂不可用（${response.status}）`);
    }

    return (await response.json()) as HealthResponse;
  } finally {
    window.clearTimeout(timeout);
  }
}
