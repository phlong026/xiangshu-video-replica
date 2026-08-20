import { mkdir, readFile, stat, writeFile } from "node:fs/promises";
import path from "node:path";

import { expect, test } from "@playwright/test";

import { observePage, requiredRunDir } from "./evidence.mjs";
import { restartApi, withBrokenGenerationArchive } from "./runtime.mjs";
import { runWorkerOnce } from "./worker.mjs";

test("@positive creates a character, restarts, and restores three completed videos", async ({
  browser,
  context,
  page,
}) => {
  const runDir = requiredRunDir();
  const evidenceRuns = [{ name: "positive-flow", evidence: observePage(page) }];
  let recoveryContext = null;

  try {
    await enterWorkspace(page);
    await createAndPublishCharacter(page);
    await page.screenshot({
      path: path.join(
        runDir,
        "screenshots",
        "1584x1024-published-character.png",
      ),
      fullPage: true,
    });

    await createProjectBatchViaOneClick(page);
    const progress = page.getByRole("progressbar", { name: "批次进度" });
    await expect(progress).toHaveAttribute("aria-valuenow", "0");

    for (const expectedProgress of [33, 66, 100]) {
      await runWorkerOnce({
        label: `positive-video-${expectedProgress}`,
        maxTasks: 1,
      });
      await expect(progress).toHaveAttribute(
        "aria-valuenow",
        String(expectedProgress),
        { timeout: 15_000 },
      );
    }

    await previewAndDownloadResults(page, runDir);
    await page.screenshot({
      path: path.join(runDir, "screenshots", "1584x1024-positive-results.png"),
      fullPage: true,
    });

    await context.close();
    await restartApi();
    recoveryContext = await browser.newContext({
      acceptDownloads: true,
      baseURL: requiredEnvironmentPath("GATE1_WEB_URL"),
      recordVideo: { dir: path.join(runDir, "browser", "recovery-video") },
      viewport: { width: 1584, height: 1024 },
    });
    const recoveryPage = await recoveryContext.newPage();
    evidenceRuns.push({
      name: "recovery-flow",
      evidence: observePage(recoveryPage),
    });
    await verifyRestoredWorkspace(recoveryPage, runDir);
    await verifyFailureRecoveryPaths(recoveryPage, runDir);
  } finally {
    await Promise.all(
      evidenceRuns.map(({ evidence, name }) => evidence.save(runDir, name)),
    );
    await recoveryContext?.close();
  }

  for (const { evidence } of evidenceRuns) {
    expect(evidence.consoleErrors).toEqual([]);
    expect(evidence.consoleWarnings).toEqual([]);
    expect(evidence.networkFailures).toEqual([]);
  }
});

async function enterWorkspace(page) {
  await page.goto("/");
  // 启动即自动验证身份并直接进入工作台，无需点击“进入”。
  const serviceStatus = page.getByRole("status", {
    name: "本地服务已连接",
  });
  await expect(serviceStatus).toBeVisible();
  await expect(serviceStatus).toHaveText("");
}

async function createAndPublishCharacter(page) {
  const mediaDir = requiredEnvironmentPath("GATE1_MEDIA_DIR");
  await page.getByRole("button", { name: "人物库" }).click();
  await page.getByLabel("人物名称").fill("Gate 1 林夏");
  await page
    .getByLabel("授权图片")
    .setInputFiles(path.join(mediaDir, "source.png"));
  await page.getByRole("button", { name: "一键生成五视角拼合图" }).click();
  await expect(
    page.getByText(/人物“Gate 1 林夏”五视角拼合图已生成/),
  ).toBeVisible({ timeout: 30_000 });

  await page.getByRole("button", { name: "查看人物 Gate 1 林夏 大图" }).click();
  await expect(
    page.getByRole("dialog", { name: "人物预览 Gate 1 林夏" }),
  ).toBeVisible();
  await expect(page.getByAltText("Gate 1 林夏 五视角拼合图")).toBeVisible({
    timeout: 15_000,
  });
  await page.getByRole("button", { name: "关闭人物预览" }).click();
}

// P0-05-02：V1.4 单屏闭环一键动线——打开项目 → 预填确认（角色自动预选/
// 源画面特征/人物参考/首帧/口播稿均为预填后一次确认）→ 一键生成
//（编译→锁定→建批合并为一次点击，契约红线 4）→ N=3 预览下载。
// 角色版本自动预选已合入（P0-03-01）：无快照进入自动落库最近发布
// 版本，零点击；fake 分析 original_script 为空，原稿预填为空稿，
// 仍需切自定义稿保存。用户确认类动作 = 4（源画面/参考/首帧对/生成）。
async function createProjectBatchViaOneClick(page) {
  const mediaDir = requiredEnvironmentPath("GATE1_MEDIA_DIR");
  // 当前项目页以视频文件名自动建项目并立即拆解，不再要求先填写项目名。
  await page.getByRole("button", { name: "项目", exact: true }).click();
  await expect(page.getByRole("region", { name: "项目" })).toBeVisible();
  await page.getByLabel("选择一个或多个参考视频").setInputFiles({
    name: "Gate 1 夏日咖啡馆口播.mp4",
    mimeType: "video/mp4",
    buffer: await readFile(path.join(mediaDir, "reference.mp4")),
  });
  await expect(page.getByText("已提交拆解")).toBeVisible({
    timeout: 30_000,
  });
  await page
    .getByRole("button", { name: "打开项目 Gate 1 夏日咖啡馆口播" })
    .click();
  // 三标签页改版后工作台主标题为 h2「复刻工作台」（原「镜头卡片」
  // heading 已随内容配置标签页结构移除）。
  await expect(page.getByRole("heading", { name: "复刻工作台" })).toBeVisible({
    timeout: 30_000,
  });
  await expect(page.getByText(/FakeGemini 演示拆解/)).toBeVisible();

  // 标签页①（默认激活）：S01 原口播触发 800ms 防抖自动保存（P0-02-03），
  // 首个镜头卡版本无需手动保存按钮。
  await page
    .getByLabel("S01 原口播")
    .fill("夏日咖啡馆的好项目，要从真实需求出发。");
  await expect(page.getByText(/已自动保存 · 版本 #/)).toBeVisible();

  await page.getByRole("radio", { name: "自定义稿" }).check();
  await page
    .getByLabel("口播稿内容")
    .fill("夏日咖啡馆的好项目，要从真实需求出发。");
  await page.getByRole("button", { name: "保存口播稿" }).click();
  await expect(page.getByText(/口播稿已保存为版本 #/)).toBeVisible();

  // 标签页②：角色版本自动预选并落库（P0-03-01）——无快照进入自动选择
  // 最近发布版本，零点击；源画面/参考/首帧均为预填后一次确认。
  await page.getByRole("tab", { name: "人物设定" }).click();

  await expect(
    page.getByText("已自动选择角色版本 Gate 1 林夏 · V1"),
  ).toBeVisible({ timeout: 15_000 });
  await expect(page.getByText("当前角色：Gate 1 林夏")).toBeVisible();

  // 源画面：角色就绪后自动提取候选（P0-03-02，本地截帧无费用），候选与
  // 特征按镜头卡建议预填（S01 近景 → CLOSE_UP/FACE_ONLY），确认即可。
  await expect(
    page.getByText("已自动提取候选源画面，请核对后确认。"),
  ).toBeVisible({ timeout: 20_000 });
  await expect(page.getByAltText("候选源画面 1")).toBeVisible();
  await expect(page.getByLabel("人物朝向")).toHaveValue("FRONT");
  await expect(page.getByLabel("人物景别")).toHaveValue("CLOSE_UP");
  await expect(page.getByLabel("面部可见性")).toHaveValue("VISIBLE");
  await expect(page.getByLabel("身体完整度")).toHaveValue("FACE_ONLY");
  await expect(page.getByRole("radio", { name: /候选 1/ })).toBeChecked();
  await page.getByRole("button", { name: "确认源画面" }).click();
  await expect(page.getByText("已确认候选源画面 1。")).toBeVisible();

  // 人物参考：推荐自动加载并默认勾选（P0-03-03），一键确认落库（红线 1）。
  const confirmReferences = page.getByRole("button", { name: "确认人物参考" });
  await expect(confirmReferences).toBeEnabled({ timeout: 15_000 });
  await confirmReferences.click();
  await expect(page.getByText("当前人物参考图已确认。")).toBeVisible();

  // 首帧：生成仍为显式付费触发（红线 3），生成后自动预选第一张（P0-03-04）。
  await page.getByLabel("候选数量").fill("1");
  await page.getByRole("button", { name: "重新生成候选首帧" }).click();
  await expect(page.getByAltText("首帧候选 1")).toBeVisible({
    timeout: 15_000,
  });
  await expect(page.getByRole("radio", { name: /首帧候选 1/ })).toBeChecked();
  await page.getByRole("button", { name: "确认用于 H3 的首帧" }).click();
  await expect(page.getByText(/已确认首帧候选 1/)).toBeVisible();

  // 标签页③：数量 3 → 工具栏付费提醒在确认前可见（P0-04-02）。
  await page.getByRole("tab", { name: "生成设置" }).click();
  await page.getByLabel("生成数量").fill("3");
  const toolbarWarning = page.locator(".paid-task-warning--toolbar");
  await expect(
    toolbarWarning.getByText("将创建 3 个付费生成任务"),
  ).toBeVisible();

  // 主按钮一键生成：编译→锁定→建批合并为一次点击（红线 4），
  // 成功后自动交接任务记录页（无需粘贴 Batch ID）。
  await page.getByRole("button", { name: "开始生成" }).click();
  await expect(
    page.getByRole("heading", { level: 1, name: "任务记录" }),
  ).toBeVisible();
}

async function previewAndDownloadResults(page, runDir) {
  const loadPreviewButtons = page.getByRole("button", { name: /^加载预览 / });
  await expect(loadPreviewButtons).toHaveCount(3);
  for (let expectedVideos = 1; expectedVideos <= 3; expectedVideos += 1) {
    await loadPreviewButtons.first().click();
    const videos = page.getByLabel(/^结果预览 /);
    await expect(videos).toHaveCount(expectedVideos);
    await expect
      .poll(() =>
        videos.nth(expectedVideos - 1).evaluate((video) => video.readyState),
      )
      .toBe(4);
  }

  const downloadDir = path.join(runDir, "downloads");
  await mkdir(downloadDir, { recursive: true });
  const downloadButtons = page.getByRole("button", { name: /^下载 MP4 / });
  await expect(downloadButtons).toHaveCount(3);
  for (let index = 0; index < 3; index += 1) {
    const [download] = await Promise.all([
      page.waitForEvent("download"),
      downloadButtons.nth(index).click(),
    ]);
    const targetPath = path.join(downloadDir, `result-${index + 1}.mp4`);
    await download.saveAs(targetPath);
    expect((await stat(targetPath)).size).toBeGreaterThan(0);
    const content = await readFile(targetPath);
    expect(content.length).toBeGreaterThan(8);
    expect(content.subarray(4, 8).toString("ascii")).toBe("ftyp");
  }
}

async function verifyRestoredWorkspace(page, runDir) {
  await enterWorkspace(page);
  await expect(page.getByRole("region", { name: "项目" })).toBeVisible();
  await expect(
    page.getByRole("button", {
      name: "打开项目 Gate 1 夏日咖啡馆口播",
    }),
  ).toBeVisible();

  await page.getByRole("button", { name: "人物库" }).click();
  const characterPreview = page.getByRole("button", {
    name: "查看人物 Gate 1 林夏 大图",
  });
  await expect(characterPreview).toBeVisible();
  await characterPreview.click();
  await expect(
    page.getByRole("dialog", { name: "人物预览 Gate 1 林夏" }),
  ).toBeVisible();
  await expect(page.getByAltText("Gate 1 林夏 五视角拼合图")).toBeVisible({
    timeout: 15_000,
  });
  await page.getByRole("button", { name: "关闭人物预览" }).click();

  await page.getByRole("button", { name: "任务记录" }).click();
  await expect(
    page.getByRole("progressbar", { name: "批次进度" }),
  ).toHaveAttribute("aria-valuenow", "100");
  await expect(page.getByRole("button", { name: /^下载 MP4 / })).toHaveCount(3);
  const previewResponsePromise = page.waitForResponse(
    (response) =>
      response
        .url()
        .includes("/api/assets/local-objects/generation-results/") &&
      response.ok(),
  );
  await page
    .getByRole("button", { name: /^加载预览 / })
    .first()
    .click();
  const previewResponse = await previewResponsePromise;
  const video = page.getByLabel(/^结果预览 /).first();
  await expect
    .poll(() => video.evaluate((element) => element.readyState))
    .toBe(4);
  await previewResponse.finished();

  const [download] = await Promise.all([
    page.waitForEvent("download"),
    page
      .getByRole("button", { name: /^下载 MP4 / })
      .first()
      .click(),
  ]);
  const targetPath = path.join(runDir, "downloads", "recovered-result.mp4");
  await download.saveAs(targetPath);
  const content = await readFile(targetPath);
  expect(content.subarray(4, 8).toString("ascii")).toBe("ftyp");
  await page.screenshot({
    path: path.join(runDir, "screenshots", "1584x1024-recovered-results.png"),
    fullPage: true,
  });
}

async function verifyFailureRecoveryPaths(page, runDir) {
  await page
    .getByLabel("整批重生成原因")
    .fill("Gate 1 验证异常分类与不重复付费边界");
  await page.getByLabel("确认新建 3 个付费任务").check();
  await page.getByRole("button", { name: "整批付费再次生成" }).click();
  await expect(
    page.getByRole("progressbar", { name: "批次进度" }),
  ).toHaveAttribute("aria-valuenow", "0");

  await runWorkerOnce({
    label: "failure-submission-uncertain",
    fakeH3Outcome: "submission_uncertain",
  });
  await runWorkerOnce({
    label: "failure-provider-terminal",
    fakeH3Outcome: "provider_failed",
  });
  await withBrokenGenerationArchive(() =>
    runWorkerOnce({ label: "failure-archive", fakeH3Outcome: "ok" }),
  );
  await refreshActiveBatch(page);

  const failedCard = taskCardForStage(page, "失败");
  const archiveCard = taskCardForStage(page, "归档失败");
  const uncertainCard = taskCardForStage(page, "提交结果待确认");
  await expect(failedCard).toHaveCount(1);
  await expect(archiveCard).toHaveCount(1);
  await expect(uncertainCard).toHaveCount(1);
  await expect(page.locator(".attention-banner")).toHaveText("需要处理 2");

  const failedTaskId = await taskIdFromCard(failedCard);
  const archiveTaskId = await taskIdFromCard(archiveCard);
  const uncertainTaskId = await taskIdFromCard(uncertainCard);
  const archiveProviderTail = await archiveCard
    .getByText(/^Provider 尾号 /)
    .textContent();
  expect(archiveProviderTail).toMatch(/^Provider 尾号 \S+$/);

  await expect(
    failedCard.getByText(
      "METASO H3 task failed or returned an invalid result.",
    ),
  ).toBeVisible();
  await expect(
    failedCard.getByLabel(`重新生成原因 ${failedTaskId}`),
  ).toBeVisible();
  await expect(
    failedCard.getByLabel(`确认为任务 ${failedTaskId} 新增一次付费生成`),
  ).toBeVisible();
  await expect(
    failedCard.getByLabel(`付费重新生成 ${failedTaskId}`),
  ).toBeDisabled();

  await expect(
    archiveCard.getByText(
      "Generation result could not be archived to configured storage.",
    ),
  ).toBeVisible();
  await expect(
    archiveCard.getByLabel(`重试归档 ${archiveTaskId}`),
  ).toBeDisabled();
  await expect(
    archiveCard.getByLabel(new RegExp(`付费重新生成 ${archiveTaskId}`)),
  ).toHaveCount(0);
  await expect(
    archiveCard.getByText("尝试 1 次 · 归档重试 0 次"),
  ).toBeVisible();

  await expect(
    uncertainCard.getByText("Fake H3 submission result is unknown"),
  ).toBeVisible();
  await expect(
    uncertainCard.getByText("Provider 尾号未公开", { exact: true }),
  ).toBeVisible();
  await expect(
    uncertainCard.getByLabel(`确认未计费 ${uncertainTaskId}`),
  ).toBeDisabled();
  await expect(
    uncertainCard.getByLabel(new RegExp(`付费重新生成 ${uncertainTaskId}`)),
  ).toHaveCount(0);

  await archiveCard
    .getByLabel(`处理原因 ${archiveTaskId}`)
    .fill("已恢复本地归档目录，只重试已付费结果的归档");
  await Promise.all([
    page.waitForResponse(
      (response) =>
        response.request().method() === "POST" &&
        response
          .url()
          .endsWith(`/api/generation-tasks/${archiveTaskId}/retry`) &&
        response.ok(),
    ),
    archiveCard.getByLabel(`重试归档 ${archiveTaskId}`).click(),
  ]);
  await runWorkerOnce({ label: "failure-archive-safe-retry" });
  const retriedArchiveCard = taskCardById(page, archiveTaskId);
  await expect(retriedArchiveCard.getByText("阶段：已归档")).toBeVisible({
    timeout: 15_000,
  });
  await expect(
    retriedArchiveCard.getByText(archiveProviderTail ?? ""),
  ).toBeVisible();
  await expect(
    retriedArchiveCard.getByText("尝试 1 次 · 归档重试 0 次"),
  ).toBeVisible();

  await uncertainCard
    .getByLabel(`处理原因 ${uncertainTaskId}`)
    .fill("已核对 Fake H3 未产生计费与 Provider 任务");
  await Promise.all([
    page.waitForResponse(
      (response) =>
        response.request().method() === "POST" &&
        response
          .url()
          .endsWith(
            `/api/generation-tasks/${uncertainTaskId}/confirm-not-charged`,
          ) &&
        response.ok(),
    ),
    uncertainCard.getByLabel(`确认未计费 ${uncertainTaskId}`).click(),
  ]);
  await runWorkerOnce({ label: "failure-uncertain-admin-requeue" });
  const recoveredUncertainCard = taskCardById(page, uncertainTaskId);
  await expect(recoveredUncertainCard.getByText("阶段：已归档")).toBeVisible({
    timeout: 15_000,
  });
  await expect(
    recoveredUncertainCard.getByText("尝试 2 次 · 归档重试 0 次"),
  ).toBeVisible();
  await expect(
    page.getByRole("progressbar", { name: "批次进度" }),
  ).toHaveAttribute("aria-valuenow", "100");
  await expect(page.getByText("需要处理 2", { exact: true })).toHaveCount(0);

  const output = {
    failed: { task_id: failedTaskId, required_action: "paid_regeneration" },
    archive_failed: {
      task_id: archiveTaskId,
      provider_tail_before_retry: archiveProviderTail,
      attempt_after_retry: 1,
      archive_retry_performed: true,
      failed_archive_retry_count: 0,
    },
    submission_uncertain: {
      task_id: uncertainTaskId,
      admin_confirmation_required: true,
      attempt_after_confirmation: 2,
    },
  };
  await writeFile(
    path.join(runDir, "logs", "failure-recovery-summary.json"),
    `${JSON.stringify(output, null, 2)}\n`,
    "utf8",
  );
  await page.screenshot({
    path: path.join(runDir, "screenshots", "1584x1024-failure-recovery.png"),
    fullPage: true,
  });
  await verifyCompactViewport(page, runDir);
}

async function verifyCompactViewport(page, runDir) {
  await page.setViewportSize({ width: 1024, height: 768 });
  await expect(
    page.getByRole("heading", { level: 1, name: "任务记录" }),
  ).toBeVisible();
  await expect(
    page.getByRole("progressbar", { name: "批次进度" }),
  ).toHaveAttribute("aria-valuenow", "100");
  await expect(page.locator("li.task-result-card")).toHaveCount(3);

  const metrics = await page.evaluate(() => ({
    viewport: {
      height: window.innerHeight,
      width: window.innerWidth,
    },
    document: {
      clientWidth: document.documentElement.clientWidth,
      scrollWidth: document.documentElement.scrollWidth,
    },
    body: {
      clientWidth: document.body.clientWidth,
      scrollWidth: document.body.scrollWidth,
    },
  }));
  expect(metrics.viewport).toEqual({ height: 768, width: 1024 });
  expect(metrics.document.scrollWidth).toBe(metrics.document.clientWidth);
  expect(metrics.body.scrollWidth).toBe(metrics.body.clientWidth);

  await writeFile(
    path.join(runDir, "logs", "1024x768-layout.json"),
    `${JSON.stringify({ ...metrics, horizontal_overflow: false }, null, 2)}\n`,
    "utf8",
  );
  await page.screenshot({
    path: path.join(runDir, "screenshots", "1024x768-failure-recovery.png"),
    fullPage: true,
  });
}

async function refreshActiveBatch(page) {
  const activeBatch = page.locator("button.batch-history-card--active");
  await expect(activeBatch).toHaveCount(1);
  await activeBatch.click();
}

function taskCardForStage(page, stage) {
  return page
    .locator("li.task-result-card")
    .filter({ hasText: `阶段：${stage}` });
}

function taskCardById(page, taskId) {
  return page.locator("li.task-result-card").filter({ hasText: taskId });
}

async function taskIdFromCard(card) {
  const value = (
    await card.locator(".task-result-heading strong").textContent()
  )?.trim();
  expect(value).toBeTruthy();
  return value;
}

function requiredEnvironmentPath(name) {
  const value = process.env[name]?.trim();
  if (!value) {
    throw new Error(`${name} is required`);
  }
  return value;
}
