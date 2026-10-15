import { expect, test } from "@playwright/test";

// 走真实客户入口和完整样式；仅隔离会话、网络与桌面桥，不访问真实账号。
async function openCustomerCenter(page, displayName) {
  await page.addInitScript(() => {
    window.isTauri = true;
    window.__TAURI_INTERNALS__ = {
      invoke: async (command) => {
        if (command === "customer_load_credentials") {
          return {
            device_token: "device",
            session_token: "session",
            identity: { account_type: "MASTER" },
          };
        }
        if (command === "customer_save_credentials") return;
        if (command === "customer_device_instance_id") return "layout-fixture";
        if (command === "plugin:app|version") return "2.6.2";
        if (command === "list_local_publish_accounts") return [];
        throw new Error(`布局夹具不支持桌面操作：${command}`);
      },
    };
  });
  await page.route("**/api/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    const session = {
      user_id: "layout-fixture",
      username: "layout_fixture",
      device_token: "device",
      session_token: "session",
      session_epoch: 1,
      session_lease_expires_at: new Date(Date.now() + 3_600_000).toISOString(),
      account_type: "MASTER",
    };
    let body = [];
    if (path.includes("/sessions/")) body = session;
    else if (path.endsWith("/profile")) {
      body = {
        ...session,
        display_name: displayName,
        joined_at: "2026-10-12T00:00:00Z",
        device_slots_used: 1,
        device_slots_total: null,
      };
    } else if (path.endsWith("/center-summary") || path.endsWith("/wallet")) {
      body = {
        user_id: session.user_id,
        available_credits: 100,
        reserved_credits: 0,
        total_consumed_credits: 0,
        total_returned_credits: 0,
        active_tokens: 0,
      };
    } else if (
      path.includes("materials") ||
      path.includes("simple-characters/library") ||
      path === "/api/generation-batches" ||
      path === "/api/oral/tasks"
    ) {
      body = { items: [], next_cursor: null, total: 0, has_more: false };
    } else if (path.includes("capabilities")) body = { r2v_enabled: false };
    await route.fulfill({ json: body });
  });
  await page.goto("/#studio/profile");
  await page.getByRole("button", { name: "跳过", exact: true }).click();
  await page.getByRole("tab", { name: "发布账号", exact: true }).click();
  await expect(
    page.getByRole("navigation", { name: "账号导航" }),
  ).toBeVisible();
  await page.evaluate(() => document.fonts.ready);
}

for (const width of [390, 768, 1024, 1440]) {
  for (const displayName of [
    "验收账号",
    "超长账号名称用于验证窄屏导航显示与换行",
  ]) {
    test(`用户中心导航 ${width}px ${displayName}`, async ({
      page,
    }, testInfo) => {
      await page.setViewportSize({ width, height: 1000 });
      await openCustomerCenter(page, displayName);
      const nav = page.getByRole("navigation", { name: "账号导航" });
      const metrics = await nav.evaluate((element) => {
        const boxes = [...element.children].map((child) => {
          const rect = child.getBoundingClientRect();
          return {
            x: rect.x,
            right: rect.right,
            y: rect.y,
            bottom: rect.bottom,
          };
        });
        return {
          width: document.documentElement.clientWidth,
          scroll: document.documentElement.scrollWidth,
          boxes,
        };
      });
      expect(metrics.scroll).toBeLessThanOrEqual(metrics.width);
      for (const box of metrics.boxes) {
        expect(box.x).toBeGreaterThanOrEqual(0);
        expect(box.right).toBeLessThanOrEqual(metrics.width);
      }
      for (let i = 0; i < metrics.boxes.length; i++) {
        for (let j = i + 1; j < metrics.boxes.length; j++) {
          const a = metrics.boxes[i];
          const b = metrics.boxes[j];
          expect(
            a.right <= b.x ||
              b.right <= a.x ||
              a.bottom <= b.y ||
              b.bottom <= a.y,
          ).toBe(true);
        }
      }
      for (const button of await nav.getByRole("button").all()) {
        // 真实命中测试保证可点击；trial 不执行退出登录或检查更新。
        await button.click({ trial: true });
        const box = await button.boundingBox();
        expect(box.height).toBeLessThanOrEqual(48);
      }
      await page.screenshot({
        path: testInfo.outputPath("customer-navigation.png"),
        fullPage: true,
      });
    });
  }
}
