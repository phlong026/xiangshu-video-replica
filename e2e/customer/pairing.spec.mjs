import { expect, test } from "@playwright/test";
import {
  openCustomerDevices,
  waitForCustomerWorkspace,
} from "./workspace-navigation.mjs";

const CODE_C = "XS04-XYZ2345-6789ABC-DEFGHJK-MNPQRST";
const DEVICE_B_NAME = "E2E Device B";

test("second-device pairing: enroll waits, primary approves, polling completes binding", async ({
  browser,
}, testInfo) => {
  const baseURL = testInfo.project.use.baseURL;
  expect(baseURL, "customer E2E project must configure baseURL").toBeTruthy();
  const deviceA = await browser.newContext({ baseURL });
  const deviceB = await browser.newContext({ baseURL });
  const pageA = await deviceA.newPage();
  const pageB = await deviceB.newPage();

  // Device A activates a fresh code.
  await pageA.goto("/customer");
  await pageA.getByLabel("激活码").fill(CODE_C);
  await pageA.getByLabel("设备名称").fill("E2E Device A");
  await pageA.getByRole("button", { name: "激活并进入工作台" }).click();
  await waitForCustomerWorkspace(pageA);

  // Device B enrolls on the pairing entry with its own fingerprint.
  await pageB.goto("/customer/pairing");
  await pageB.getByLabel("激活码").fill(CODE_C);
  await pageB.getByLabel("设备名称").fill(DEVICE_B_NAME);
  await pageB.getByRole("button", { name: "提交配对申请" }).click();

  // Device B parks on the waiting screen.
  await expect(
    pageB.getByRole("heading", { name: "等待主设备审批" }),
  ).toBeVisible();

  // Device A approves the pending pairing in its device view.
  await openCustomerDevices(pageA);
  await expect(pageA.getByText("新的设备绑定请求")).toBeVisible({
    timeout: 20_000,
  });
  await pageA.getByRole("button", { name: "确认绑定" }).click();
  await expect(pageA.getByText("新的设备绑定请求")).toBeHidden({
    timeout: 20_000,
  });

  // Device B automatically consumes the approved request via its waiting
  // poll, then saves the 201 device credential without another form submit.
  await expect(pageB.getByRole("heading", { name: "配对成功" })).toBeVisible({
    timeout: 20_000,
  });
  // The saved device credential must be usable. Device A is still online,
  // so device B explicitly confirms the session switch before entering.
  await pageB.getByRole("button", { name: "进入客户工作区" }).click();
  const conflict = pageB.getByRole("dialog", { name: "检测到会话冲突" });
  await expect(conflict).toBeVisible();
  await conflict.getByRole("button", { name: "切换到本设备" }).click();
  await openCustomerDevices(pageB);
  await expect(
    pageB
      .getByRole("region", { name: "设备管理" })
      .getByText(DEVICE_B_NAME, { exact: true }),
  ).toBeVisible();

  await deviceA.close();
  await deviceB.close();
});
