import { expect, test } from "@playwright/test";

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

  // The workspace lands with the device-management entry visible.
  await expect(page.getByRole("button", { name: "设备管理" })).toBeVisible({
    timeout: 20_000,
  });
});

test("activated workspace shows the first device in slot one and a wallet", async ({
  page,
}) => {
  await page.goto("/customer");

  await page.getByLabel("激活码").fill(CODE_B);
  await page.getByLabel("设备名称").fill("E2E Device A");
  await page.getByRole("button", { name: "激活并进入工作台" }).click();

  await page.getByRole("button", { name: "设备管理" }).click();

  // Two-slot device view: the first device occupies slot one, and the
  // recharge action is reachable without any second main-code entry.
  await expect(page.getByText("E2E Device A").first()).toBeVisible({
    timeout: 20_000,
  });
  await expect(page.getByRole("button", { name: "Recharge" })).toBeVisible();

  // The pairing entry for a second device is present.
  await expect(
    page.getByRole("link", { name: /Register second device/i }),
  ).toBeVisible();
});
