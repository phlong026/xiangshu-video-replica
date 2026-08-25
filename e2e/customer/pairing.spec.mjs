import { chromium, expect, test } from "@playwright/test";

const CODE_C = "XS04-XYZ2345-6789ABC-DEFGHJK-MNPQRST";
const DEVICE_B_FP = "E2E-DeviceB-Fingerprint";
const DEVICE_B_NAME = "E2E Device B";

test("second-device pairing: enroll parks on waiting, primary approves, re-enroll lands on 201", async () => {
  const browser = await chromium.launch();
  const deviceA = await browser.newContext();
  const deviceB = await browser.newContext();
  const pageA = await deviceA.newPage();
  const pageB = await deviceB.newPage();

  // Device A activates a fresh code.
  await pageA.goto("/customer");
  await pageA.getByLabel("激活码").fill(CODE_C);
  await pageA.getByLabel("设备名称").fill("E2E Device A");
  await pageA.getByRole("button", { name: "激活并进入工作台" }).click();
  await expect(pageA.getByRole("button", { name: "设备管理" })).toBeVisible({
    timeout: 20_000,
  });

  // Device B enrolls on the pairing entry with its own fingerprint.
  await pageB.goto("/customer/pairing");
  await pageB.getByLabel(/Activation Code/i).fill(CODE_C);
  await pageB.getByLabel(/Device Fingerprint/i).fill(DEVICE_B_FP);
  await pageB.getByLabel(/Device Name/i).fill(DEVICE_B_NAME);
  await pageB.getByRole("button", { name: /enroll device/i }).click();

  // Device B parks on the waiting screen.
  await expect(
    pageB.getByRole("heading", { name: "等待主设备审批" }),
  ).toBeVisible();

  // Device A approves the pending pairing in its device view.
  await pageA.getByRole("button", { name: "设备管理" }).click();
  await expect(pageA.getByText("待审批配对")).toBeVisible({ timeout: 20_000 });
  await pageA.getByRole("button", { name: /Approve pairing/i }).click();
  await expect(pageA.getByText("待审批配对")).toBeHidden({ timeout: 20_000 });

  // Device B re-checks: the button returns to the (reset) form, and the
  // approved request is idempotent server-side → 201 on re-submit.
  await pageB.getByRole("button", { name: "我已审批,重新配对" }).click();
  await pageB.getByLabel(/Activation Code/i).fill(CODE_C);
  await pageB.getByLabel(/Device Fingerprint/i).fill(DEVICE_B_FP);
  await pageB.getByLabel(/Device Name/i).fill(DEVICE_B_NAME);
  await pageB.getByRole("button", { name: /enroll device/i }).click();
  await expect(pageB.getByRole("heading", { name: "配对成功" })).toBeVisible({
    timeout: 20_000,
  });

  await browser.close();
});
