import { expect, test } from "@playwright/test";
import {
  openCustomerDevices,
  waitForCustomerWorkspace,
} from "./workspace-navigation.mjs";

const CODE_A = "XS04-ABCDEFG-HJKLMNP-QRSTVWX-YZ23456";
const CODE_B = "XS04-2345678-9ABCDEF-GHJKLMN-PQRSTVW";

test("customer activates a code and reaches the workspace", async ({
  page,
}) => {
  await page.goto("/customer");

  // First run shows the activation form.
  await expect(
    page.getByRole("heading", { name: "激活短视频复刻工作台" }),
  ).toBeVisible();

  await page.getByLabel("激活码").fill(CODE_A);
  await page.getByLabel("设备名称").fill("E2E Device A");
  await page.getByRole("button", { name: "激活并进入工作台" }).click();

  // The V1.4 workspace lands with the customer profile entry visible.
  await waitForCustomerWorkspace(page);
});

test("activated workspace shows the first device in slot one and a wallet", async ({
  page,
}) => {
  await page.goto("/customer");

  await page.getByLabel("激活码").fill(CODE_B);
  await page.getByLabel("设备名称").fill("E2E Device A");
  await page.getByRole("button", { name: "激活并进入工作台" }).click();

  const deviceManagement = await openCustomerDevices(page);

  // Two-slot device view: the first device occupies slot one, and the
  // recharge action is reachable without any second main-code entry.
  await expect(page.getByText("E2E Device A").first()).toBeVisible({
    timeout: 20_000,
  });
  await expect(
    deviceManagement.getByRole("button", { name: "充值条数" }),
  ).toBeVisible();

  // The pairing entry for a second device is present.
  await expect(
    deviceManagement.getByRole("link", { name: "绑定第二台设备" }),
  ).toBeVisible();
});
