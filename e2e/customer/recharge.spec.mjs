import { expect, test } from "@playwright/test";

const CODE_RECHARGE = "XS04-1234567-89ABCDE-FGHJKMN-PQRSTVW";

test("customer wallet creates a recharge order under the customer session", async ({
  page,
}) => {
  // Guard: the wallet's payment form posts to the real ZPay gateway in a new
  // window (target=_blank). Never send the throwaway merchant fields there —
  // abort the gateway request and close any popup the submission opens.
  await page.context().route("**/*", (route) => {
    if (route.request().url().includes("zpayz.cn")) {
      return route.abort();
    }
    return route.continue();
  });

  await page.goto("/customer");
  await page.getByLabel("激活码").fill(CODE_RECHARGE);
  await page.getByLabel("设备名称").fill("E2E Recharge Device");
  await page.getByRole("button", { name: "激活并进入工作台" }).click();

  // The workspace lands; the wallet view lives in the sidebar user menu.
  await expect(page.getByRole("button", { name: "设备管理" })).toBeVisible({
    timeout: 20_000,
  });
  await page.getByRole("button", { name: "用户菜单" }).click();
  await page.getByRole("button", { name: "余额与充值" }).click();

  // The customer-lane wallet panel loads (task #7): balance + recharge form.
  await expect(page.getByRole("heading", { name: "充值条数" })).toBeVisible({
    timeout: 20_000,
  });
  await expect(page.getByRole("button", { name: "充值100元" })).toBeVisible();

  // Read the balance before the recharge: PENDING must not credit the wallet.
  const creditsCard = page.locator(".wallet-summary-card").nth(1);
  const creditsBefore = await creditsCard.locator("strong").textContent();

  // Clicking a preset posts POST /api/customer/recharge-orders and opens the
  // ZPay payment form in a popup (aborted above).
  const popupPromise = page
    .waitForEvent("popup", { timeout: 10_000 })
    .catch(() => null);
  await page.getByRole("button", { name: "充值100元" }).click();
  const popup = await popupPromise;
  if (popup) {
    await popup.close();
  }

  // The created order (待支付) appears in the recent-orders table.
  await expect(page.getByText("最近充值订单")).toBeVisible();
  await expect(page.getByText("待支付").first()).toBeVisible({
    timeout: 15_000,
  });

  // Credits are unchanged while the order is PENDING (BILL-01: the wallet is
  // credited only on the PAID callback).
  const creditsAfter = await creditsCard.locator("strong").textContent();
  expect(creditsAfter).toBe(creditsBefore);
});
