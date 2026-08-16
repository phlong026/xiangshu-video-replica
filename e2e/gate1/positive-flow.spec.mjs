import { mkdir, readFile, stat, writeFile } from "node:fs/promises";
import path from "node:path";

import { expect, test } from "@playwright/test";

import { observePage, requiredRunDir } from "./evidence.mjs";
import { restartApi, withBrokenGenerationArchive } from "./runtime.mjs";
import { runWorkerOnce } from "./worker.mjs";

const VIEW_NAMES = [
  "正面头像",
  "正面半身",
  "正面全身",
  "左 45°",
  "右 45°",
  "左侧面",
  "右侧面",
];

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
        "1280x720-published-character.png",
      ),
      fullPage: true,
    });

    await createProjectBatch(page);
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
      path: path.join(runDir, "screenshots", "1280x720-positive-results.png"),
      fullPage: true,
    });

    await context.close();
    await restartApi();
    recoveryContext = await browser.newContext({
      acceptDownloads: true,
      baseURL: requiredEnvironmentPath("GATE1_WEB_URL"),
      recordVideo: { dir: path.join(runDir, "browser", "recovery-video") },
      viewport: { width: 1280, height: 720 },
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
    expect(evidence.networkFailures).toEqual([]);
  }
});

async function enterWorkspace(page) {
  await page.goto("/");
  await page.getByRole("button", { name: "进入工作台" }).click();
  await expect(page.getByRole("status")).toHaveText("本地服务已连接");
}

async function createAndPublishCharacter(page) {
  const mediaDir = requiredEnvironmentPath("GATE1_MEDIA_DIR");
  await page.getByRole("button", { name: "人物库" }).click();
  await page.getByRole("button", { name: "创建人物身份" }).click();
  await page.getByLabel("人物显示名").fill("Gate 1 林夏");
  await page.getByLabel("授权使用范围").fill("内部短视频");
  await page.getByLabel("授权到期日").fill("2035-12-31");
  await page
    .getByLabel("肖像授权文件")
    .setInputFiles(path.join(mediaDir, "authorization.png"));
  await page.getByRole("button", { name: "创建并上传授权" }).click();

  await page
    .getByLabel("真人源图", { exact: true })
    .setInputFiles(path.join(mediaDir, "source.png"));
  await page.getByRole("button", { name: "上传并检查源图" }).click();
  await expect(
    page.getByRole("heading", { name: "源图质量通过" }),
  ).toBeVisible();
  await page.getByRole("button", { name: "确认质检结果" }).click();
  await page.getByRole("button", { name: "完成并返回人物库" }).click();
  await expect(
    page.getByText("人物身份已激活，可继续创建人设。"),
  ).toBeVisible();

  await page.getByRole("button", { name: "新建人设" }).click();
  await page.getByLabel("人设名称").fill("乡墅项目管理专家");
  await page.getByLabel("职业定位").fill("乡墅短视频主理人");
  await page.getByLabel("使用场景").fill("夏日咖啡馆口播");
  await page.getByLabel("服装描述").fill("白色休闲上衣");
  await page.getByLabel("默认背景").fill("明亮咖啡馆");
  await page.getByLabel("使用范围").fill("内部短视频、测试");
  await page.getByLabel("正向提示词").fill("自然、真实、稳定一致");
  await page.getByLabel("负向提示词").fill("留影、畸形、多人");
  await page.getByRole("button", { name: "保存人设" }).click();
  await expect(page.getByText("人设已创建。")).toBeVisible();

  await page.getByRole("button", { name: "创建 DRAFT 版本" }).click();
  await expect(page.getByText("已创建 V1 DRAFT 版本。")).toBeVisible();
  await page.getByRole("button", { name: "开始生成 7 类视角" }).click();
  await runWorkerOnce({ label: "positive-character-seven-views", maxTasks: 7 });
  await expect(page.getByText("成功 7", { exact: true })).toBeVisible({
    timeout: 15_000,
  });

  const frontSlot = viewSlot(page, "正面头像");
  const originalFront = candidate(frontSlot, 1);
  await originalFront.getByLabel("审核说明").fill("表情不符合项目要求");
  await originalFront.getByRole("button", { name: "驳回" }).click();
  await expect(originalFront.getByText("人工审核：已驳回")).toBeVisible();
  await originalFront.getByRole("button", { name: "重新生成正面头像" }).click();
  await runWorkerOnce({
    label: "positive-character-front-regeneration",
    maxTasks: 1,
  });
  await expect(candidate(frontSlot, 2)).toBeVisible({ timeout: 15_000 });

  for (const viewName of VIEW_NAMES) {
    const slot = viewSlot(page, viewName);
    const approvedCandidate = candidate(slot, viewName === "正面头像" ? 2 : 1);
    await approvedCandidate.getByRole("button", { name: "批准" }).click();
    await expect(approvedCandidate.getByText("人工审核：已批准")).toBeVisible();
    await approvedCandidate
      .getByRole("radio", { name: "选为发布资产" })
      .check();
  }

  page.once("dialog", async (dialog) => {
    expect(dialog.message()).toBe(
      "发布后角色版本与七类资产选择不可修改。确认发布？",
    );
    await dialog.accept();
  });
  await page.getByRole("button", { name: "发布角色版本" }).click();
  await expect(
    page.getByText("版本已发布，内容不可修改").first(),
  ).toBeVisible();
}

async function createProjectBatch(page) {
  const mediaDir = requiredEnvironmentPath("GATE1_MEDIA_DIR");
  await page.getByRole("button", { name: "新建复刻" }).click();
  await page.getByLabel("项目名称").fill("Gate 1 夏日咖啡馆口播");
  await page
    .getByLabel("参考视频")
    .setInputFiles(path.join(mediaDir, "reference.mp4"));
  await page.getByRole("button", { name: "创建并上传" }).click();
  await expect(page.getByRole("heading", { name: "镜头卡片" })).toBeVisible({
    timeout: 30_000,
  });
  await expect(page.getByText("FakeGemini video analysis")).toBeVisible();

  await page.getByRole("button", { name: "选择角色版本" }).click();
  await page
    .getByRole("radio", { name: /Gate 1 林夏.*乡墅项目管理专家.*V1/ })
    .check();
  await page.getByRole("button", { name: "确认角色版本" }).click();
  await expect(
    page.getByText("已选择角色“Gate 1 林夏 · 乡墅项目管理专家 V1”。"),
  ).toBeVisible();

  await page.getByRole("button", { name: "重新提取候选" }).click();
  await expect(page.getByAltText("候选源画面 1")).toBeVisible({
    timeout: 20_000,
  });
  await page.getByLabel("人物朝向").selectOption("FRONT");
  await page.getByLabel("人物景别").selectOption("HALF_BODY");
  await page.getByLabel("面部可见性").selectOption("VISIBLE");
  await page.getByLabel("身体完整度").selectOption("UPPER_BODY");
  await page.getByRole("radio", { name: /候选 1/ }).check();
  await page.getByRole("button", { name: "确认源画面" }).click();
  await expect(page.getByText("已确认候选源画面 1。")).toBeVisible();

  const confirmReferences = page.getByRole("button", { name: "确认人物参考" });
  await expect(confirmReferences).toBeEnabled({ timeout: 15_000 });
  await confirmReferences.click();
  await expect(page.getByText("当前人物参考图已确认。")).toBeVisible();

  await page.getByLabel("候选数量").fill("1");
  await page.getByRole("button", { name: "重新生成候选首帧" }).click();
  await expect(page.getByAltText("首帧候选 1")).toBeVisible({
    timeout: 15_000,
  });
  await page.getByRole("radio", { name: /首帧候选 1/ }).check();
  await page.getByRole("button", { name: "确认用于 H3 的首帧" }).click();
  await expect(page.getByText(/已确认首帧候选 1/)).toBeVisible();

  await page
    .getByLabel("S01 原口播")
    .fill("夏日咖啡馆的好项目，要从真实需求出发。");
  await page.getByRole("button", { name: "保存镜头卡片" }).click();
  await expect(page.getByText(/镜头卡片已保存为版本 #/)).toBeVisible();

  await page.getByRole("radio", { name: "自定义稿" }).check();
  await page
    .getByLabel("口播稿内容")
    .fill("夏日咖啡馆的好项目，要从真实需求出发。");
  await page.getByRole("button", { name: "保存口播稿" }).click();
  await expect(page.getByText(/口播稿已保存为版本 #/)).toBeVisible();
  await page.getByRole("button", { name: "编译 H3 Prompt" }).click();
  await expect(page.getByText(/H3 Prompt 已编译为版本 #/)).toBeVisible();
  const prompt = page.getByLabel("H3 Prompt 内容");
  await prompt.fill(
    `${await prompt.inputValue()}\nGate 1 人工修订：保持自然眼神与稳定运镜。`,
  );
  await page.getByRole("button", { name: "另存 Prompt 新版本" }).click();
  await expect(page.getByText(/Prompt 已另存为版本 #/)).toBeVisible();
  await page.getByRole("button", { name: "锁定 Prompt" }).click();
  await expect(page.getByText(/Prompt 版本 #.*已锁定/)).toBeVisible();

  await page.getByLabel("生成数量").fill("3");
  await page.getByRole("button", { name: "创建 3 个生成任务" }).click();
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
  await expect(page.getByRole("heading", { name: "项目列表" })).toBeVisible();
  await expect(page.getByText("Gate 1 夏日咖啡馆口播")).toBeVisible();

  await page.getByRole("button", { name: "人物库" }).click();
  await expect(
    page.getByRole("heading", { name: "Gate 1 林夏", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByText("版本已发布，内容不可修改").first(),
  ).toBeVisible();

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
    path: path.join(runDir, "screenshots", "1280x720-recovered-results.png"),
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
    path: path.join(runDir, "screenshots", "1280x720-failure-recovery.png"),
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

function viewSlot(page, name) {
  return page.locator("article.view-slot").filter({
    has: page.getByRole("heading", { name, exact: true }),
  });
}

function candidate(slot, number) {
  return slot.locator(".view-candidate").filter({
    hasText: `候选 ${number}`,
  });
}

function requiredEnvironmentPath(name) {
  const value = process.env[name]?.trim();
  if (!value) {
    throw new Error(`${name} is required`);
  }
  return value;
}
