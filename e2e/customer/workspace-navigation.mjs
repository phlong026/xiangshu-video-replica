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
  // V1.4: the 设备管理 tab leaves the profile page and renders the dedicated
  // device-management page (a region labelled by its 设备管理 heading).
  await page.getByRole("tab", { name: "设备管理", exact: true }).click();
  const devices = page.getByRole("region", { name: "设备管理" });
  await expect(devices).toBeVisible({ timeout: 20_000 });
  return devices;
}

export async function openCustomerWallet(page) {
  const profileEntry = await waitForCustomerWorkspace(page);
  await profileEntry.click();
  // V1.4: the 使用记录 tab opens the wallet page (余额与充值) directly.
  await page.getByRole("tab", { name: "使用记录", exact: true }).click();
}
