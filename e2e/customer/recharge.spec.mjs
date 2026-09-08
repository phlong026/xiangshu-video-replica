import { expect, test } from "@playwright/test";
import { openCustomerWallet } from "./workspace-navigation.mjs";

const CODE_RECHARGE = "XS04-1234567-89ABCDE-FGHJKMN-PQRSTVW";

test("customer wallet creates a recharge order under the customer session", async ({
  page,
}) => {
  // The provider QR exchange is an external chain and must not reach real
  // ZPay during local integration.  Keep the owned order creation real, then
  // supply only the display-only QR response at the browser boundary.
  await page.route(
    "**/api/customer/recharge-orders/*/payment-code",
    async (route) => {
      const orderNo = new URL(route.request().url()).pathname.split("/").at(-2);
      await route.fulfill({
        contentType: "application/json",
        json: {
          order_no: orderNo,
          amount_fen: 10_000,
          credits: 10,
          qr_image_url:
            "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg'/%3E",
          payment_url: "https://payment.invalid/e2e",
        },
        status: 200,
      });
    },
  );

  await page.goto("/customer");
  await page.getByLabel("激活码").fill(CODE_RECHARGE);
  await page.getByLabel("设备名称").fill("E2E Recharge Device");
  await page.getByRole("button", { name: "激活并进入工作台" }).click();

  // The V1.4 workspace reaches the customer wallet through 用户档案 > 使用记录.
  await openCustomerWallet(page);

  // The customer-lane wallet panel loads (task #7): balance + recharge form.
  // V1.4 bills by seconds: the recharge section heading is 充值秒数额度.
  await expect(page.getByRole("heading", { name: "充值秒数额度" })).toBeVisible(
    {
      timeout: 20_000,
    },
  );
  await expect(page.getByRole("button", { name: "充值100元" })).toBeVisible();

  // Read the balance before the recharge: PENDING must not credit the wallet.
  // The first summary card shows 可用额度; its value is synchronous, unlike
  // the second card whose price quotes load asynchronously.
  const balanceCard = page.locator(".wallet-summary-card").first();
  const creditsBefore = await balanceCard.locator("strong").textContent();

  // Clicking a preset opens the confirmation dialog.  Generating the QR then
  // posts POST /api/customer/recharge-orders; only the provider QR response
  // is stubbed above.
  await page.getByRole("button", { name: "充值100元" }).click();
  const rechargeDialog = page.getByRole("dialog", { name: "扫码充值" });
  await expect(rechargeDialog).toBeVisible();
  await rechargeDialog.getByRole("button", { name: "生成支付二维码" }).click();
  await expect(
    rechargeDialog.getByRole("img", { name: "充值支付二维码" }),
  ).toBeVisible();
  await rechargeDialog.getByRole("button", { name: "关闭充值窗口" }).click();

  // The created order (待支付) appears in the recent-orders table.
  await expect(page.getByText("最近充值订单")).toBeVisible();
  await expect(page.getByText("待支付").first()).toBeVisible({
    timeout: 15_000,
  });

  // Credits are unchanged while the order is PENDING (BILL-01: the wallet is
  // credited only on the PAID callback).
  const creditsAfter = await balanceCard.locator("strong").textContent();
  expect(creditsAfter).toBe(creditsBefore);
});
