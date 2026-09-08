import { expect } from "@playwright/test";

export async function waitForCustomerWorkspace(page) {
  const profileEntry = page.getByRole("button", {
    name: "用户档案",
    exact: true,
  });
  await expect(profileEntry).toBeVisible({ timeout: 20_000 });
  return profileEntry;
}

export async function openCustomerDevices(page) {
  const profileEntry = await waitForCustomerWorkspace(page);
  await profileEntry.click();
  await page.getByRole("tab", { name: "设备管理", exact: true }).click();
  const profile = page.getByRole("region", { name: "个人中心" });
  await expect(profile).toBeVisible();
  // Pending requests append a count badge to the device button's accessible name.
  await profile
    .getByRole("navigation", { name: "个人中心功能" })
    .getByRole("button", { name: "设备管理" })
    .click();
  return profile.getByRole("region", { name: "设备管理" });
}

export async function openCustomerWallet(page) {
  const profileEntry = await waitForCustomerWorkspace(page);
  await profileEntry.click();
  await page.getByRole("tab", { name: "使用记录", exact: true }).click();
}
