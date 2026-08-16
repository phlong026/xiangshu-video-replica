import { mkdir, writeFile } from "node:fs/promises";
import path from "node:path";

export function observePage(page) {
  const consoleErrors = [];
  const networkFailures = [];

  page.on("console", (message) => {
    if (message.type() === "error") {
      consoleErrors.push({ kind: "console", text: message.text() });
    }
  });
  page.on("pageerror", (error) => {
    consoleErrors.push({ kind: "pageerror", text: error.message });
  });
  page.on("requestfailed", (request) => {
    networkFailures.push({
      kind: "requestfailed",
      method: request.method(),
      url: request.url(),
      error: request.failure()?.errorText ?? "unknown",
    });
  });
  page.on("response", (response) => {
    if (response.status() >= 400) {
      networkFailures.push({
        kind: "http",
        method: response.request().method(),
        url: response.url(),
        status: response.status(),
      });
    }
  });

  return {
    consoleErrors,
    networkFailures,
    async save(runDir, name) {
      const logsDir = path.join(runDir, "logs");
      await mkdir(logsDir, { recursive: true });
      await Promise.all([
        writeJson(
          path.join(logsDir, `${name}-console-errors.json`),
          consoleErrors,
        ),
        writeJson(
          path.join(logsDir, `${name}-network-failures.json`),
          networkFailures,
        ),
      ]);
    },
  };
}

export function requiredRunDir() {
  const value = process.env.GATE1_RUN_DIR?.trim();
  if (!value) {
    throw new Error("GATE1_RUN_DIR is required");
  }
  return value;
}

async function writeJson(filePath, value) {
  await writeFile(filePath, `${JSON.stringify(value, null, 2)}\n`, "utf8");
}
