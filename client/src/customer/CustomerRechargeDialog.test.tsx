import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import type { ComponentProps } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { CustomerRechargeDialog } from "./CustomerRechargeDialog";
import { orderPollWithinWindow } from "./orderPolling";
import type { CustomerCredentialStore } from "./useCustomerSession";

// 轮询窗口默认按真实实现判定（5 分钟内继续轮询）；只有专门验证「窗口用尽」的用例
// 才把它临时压成 false，避免真的等五分钟。
vi.mock("./orderPolling", async (importOriginal) => ({
  ...(await importOriginal<typeof import("./orderPolling")>()),
  orderPollWithinWindow: vi.fn(() => true),
}));

const sessionCredentialText = "recharge-dialog-session-credential";

function fakeStore(): CustomerCredentialStore {
  return {
    loadDeviceCredentialToken: vi.fn().mockResolvedValue(null),
    loadSessionToken: vi.fn().mockResolvedValue(sessionCredentialText),
    saveActivation: vi.fn().mockResolvedValue(undefined),
    saveSessionToken: vi.fn().mockResolvedValue(undefined),
    clearSessionToken: vi.fn().mockResolvedValue(undefined),
    clearAllCredentials: vi.fn().mockResolvedValue(undefined),
    deviceInstanceId: vi.fn().mockResolvedValue("test-instance-id"),
    devicePlatform: () => "windows",
    // CW-062：身份缓存不参与这些用例的断言，给出满足接口的最小桩。
    loadIdentity: async () => null,
    // 「记住密码」在这些用例里不参与断言，给出满足接口的最小桩。
    loadRememberedLogin: async () => null,
    saveRememberedLogin: async () => {},
    clearRememberedLogin: async () => {},
  };
}

function jsonResponse(payload: unknown, status = 200) {
  return Promise.resolve({
    ok: status >= 200 && status < 300,
    status,
    json: async () => payload,
  });
}

// 管理端配置的档位（100 元 → 10 积分，无权益）：客户端不再内置金额列表。
const rechargePackagePayload = {
  items: [
    {
      id: "pkg-basic",
      name: "标准档",
      amount_fen: 10000,
      credits: 10,
      discount_rate: null,
      discount_interfaces: [],
      sort_order: 0,
      is_active: true,
      version: 1,
      created_at: "2026-09-22 10:00:00",
      updated_at: "2026-09-22 10:00:00",
    },
  ],
};

// 需求原文场景：1998 元到账 2000 积分 + 视频生成 9 折。
const discountPackagePayload = {
  items: [
    {
      id: "pkg-premium",
      name: "尊享档",
      amount_fen: 199800,
      credits: 2000,
      discount_rate: "0.9000",
      discount_interfaces: ["video_generation"],
      sort_order: 1,
      is_active: true,
      version: 2,
      created_at: "2026-09-22 10:00:00",
      updated_at: "2026-09-22 10:00:00",
    },
  ],
};

const walletPayload = {
  available_credits: 12,
  reserved_credits: 0,
  internal_unit_price_fen: 1000,
  min_recharge_fen: 10000,
  recharge_step_fen: 1000,
};

/**
 * 下单 → 出码 → 轮询这条链路的 fetch 桩：订单保持 PENDING，好让轮询一直跑到
 * 窗口用尽或断言结束。默认付款链接是站外 URL。
 */
function paymentFlowFetchMock(paymentUrl = "https://payment.example/pay") {
  return vi.fn((url: string, options?: RequestInit) => {
    if (url.endsWith("/api/customer/recharge-packages")) {
      return jsonResponse(rechargePackagePayload);
    }
    if (url.endsWith("/api/customer/wallet")) {
      return jsonResponse({
        available_credits: 12,
        reserved_credits: 0,
        internal_unit_price_fen: 1000,
        min_recharge_fen: 10000,
        recharge_step_fen: 1000,
      });
    }
    if (
      url.endsWith("/api/customer/recharge-orders") &&
      options?.method === "POST"
    ) {
      return jsonResponse(
        {
          order_no: "202608270001",
          status: "PENDING",
          amount_fen: 10000,
          credits: 10,
          gateway_url: "https://payment.example/submit",
          method: "POST",
          form_fields: {},
        },
        201,
      );
    }
    if (url.endsWith("/payment-code")) {
      return jsonResponse({
        order_no: "202608270001",
        amount_fen: 10000,
        credits: 10,
        qr_image_url: "https://payment.example/qr.png",
        payment_url: paymentUrl,
      });
    }
    if (url.endsWith("/api/customer/recharge-orders/202608270001")) {
      return jsonResponse({
        order_no: "202608270001",
        status: "PENDING",
        amount_fen: 10000,
        credits: 10,
        channel: "wechat",
        created_at: "2026-08-27T00:00:00Z",
        paid_at: null,
      });
    }
    throw new Error(`unexpected request: ${url}`);
  });
}

function renderDialog(
  props: Partial<ComponentProps<typeof CustomerRechargeDialog>> = {},
) {
  return render(
    <CustomerRechargeDialog
      isOpen
      onClose={vi.fn()}
      onOrderCreated={vi.fn()}
      onPaid={vi.fn()}
      onSessionExpired={vi.fn()}
      store={fakeStore()}
      {...props}
    />,
  );
}

function configuredRechargeFetchMock() {
  const items = [
    { id: "basic", name: "基础会员", amount_fen: 198000, credits: 20000 },
    { id: "silver", name: "白银会员", amount_fen: 298000, credits: 29800 },
    { id: "gold", name: "黄金会员", amount_fen: 398000, credits: 39800 },
  ];
  let order = { order_no: "configured-order", amount_fen: 0, credits: 0 };
  return vi.fn((url: string, options?: RequestInit) => {
    if (url.endsWith("/api/customer/wallet")) {
      return jsonResponse({
        ...walletPayload,
        points_per_yuan: 10,
        min_recharge_fen: 50000,
        recharge_step_fen: 10000,
      });
    }
    if (url.endsWith("/api/customer/recharge-packages")) {
      return jsonResponse({
        items: items.map((item) => ({
          ...rechargePackagePayload.items[0],
          ...item,
        })),
      });
    }
    if (
      url.endsWith("/api/customer/recharge-orders") &&
      options?.method === "POST"
    ) {
      const payload = JSON.parse(String(options.body));
      const pkg = items.find((item) => item.id === payload.package_id);
      order = {
        ...order,
        amount_fen: payload.amount_fen,
        credits: pkg?.credits ?? payload.amount_fen / 10,
      };
      return jsonResponse({ ...order, status: "PENDING" }, 201);
    }
    if (url.endsWith("/payment-code")) {
      return jsonResponse({
        ...order,
        qr_image_url: "https://payment.example/qr.png",
        payment_url: "weixin://wxpay/bizpayurl?pr=configured-test",
      });
    }
    if (url.endsWith("/api/customer/recharge-orders/configured-order")) {
      return jsonResponse({ ...order, status: "PENDING" });
    }
    throw new Error(`unexpected request: ${url}`);
  });
}

describe("CustomerRechargeDialog", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("选择套餐只更新选择，确认充值后才创建带套餐编号的订单", async () => {
    const fetchMock = paymentFlowFetchMock();
    vi.stubGlobal("fetch", fetchMock);
    renderDialog();

    const card = await screen.findByRole("button", { name: /标准档/ });
    fireEvent.click(card);
    expect(card).toHaveAttribute("aria-pressed", "true");
    expect(
      fetchMock.mock.calls.some(([, options]) => options?.method === "POST"),
    ).toBe(false);

    fireEvent.click(screen.getByRole("button", { name: "确认充值" }));
    await screen.findByRole("img", { name: "充值支付二维码" });
    const createCall = fetchMock.mock.calls.find(
      ([url, options]) =>
        String(url).endsWith("/api/customer/recharge-orders") &&
        options?.method === "POST",
    );
    expect(createCall?.[1]?.body).toBe(
      JSON.stringify({ amount_fen: 10000, package_id: "pkg-basic" }),
    );
  });

  it("键盘经过自定义输入框时保留已选套餐，实际输入才切换充值方式", async () => {
    vi.stubGlobal("fetch", configuredRechargeFetchMock());
    renderDialog();
    const card = await screen.findByRole("button", { name: /基础会员/ });
    fireEvent.click(card);
    const input = screen.getByLabelText("自定义金额（元）");
    fireEvent.focus(input);
    expect(card).toHaveAttribute("aria-pressed", "true");
    fireEvent.change(input, { target: { value: "600" } });
    expect(card).toHaveAttribute("aria-pressed", "false");
    expect(screen.getByRole("radio", { name: "自定义充值" })).toBeChecked();
  });

  it.each([
    "https://payment.example/pay",
    "weixin://wxpay/bizpayurl?pr=local-test",
  ])(
    "creates a payment code and renders its QR image inside the app",
    async (paymentUrl) => {
      const onOrderCreated = vi.fn();
      const fetchMock = paymentFlowFetchMock(paymentUrl);
      vi.stubGlobal("fetch", fetchMock);

      renderDialog({ onOrderCreated });

      // 档位来自管理端配置，选中后统一确认，金额与套餐编号一起提交。
      fireEvent.click(await screen.findByRole("button", { name: /100元/ }));
      fireEvent.click(screen.getByRole("button", { name: "确认充值" }));

      const qr = await screen.findByRole("img", { name: "充值支付二维码" });
      expect(qr).toHaveAttribute("src", "https://payment.example/qr.png");
      expect(screen.getByText("正在等待支付结果")).toBeInTheDocument();
      expect(screen.getByText(/到账 10 积分/)).toBeInTheDocument();
      expect(onOrderCreated).toHaveBeenCalledOnce();
      const createCall = fetchMock.mock.calls.find(
        ([url, options]) =>
          String(url).endsWith("/api/customer/recharge-orders") &&
          options?.method === "POST",
      );
      expect(createCall?.[1]?.body).toBe(
        JSON.stringify({ amount_fen: 10000, package_id: "pkg-basic" }),
      );
      if (paymentUrl.startsWith("weixin://")) {
        expect(
          screen.getByText("请使用微信扫一扫完成支付。"),
        ).toBeInTheDocument();
        expect(
          screen.queryByRole("button", { name: "无法扫码？打开支付页面" }),
        ).not.toBeInTheDocument();
      } else {
        expect(
          screen.getByRole("button", { name: "无法扫码？打开支付页面" }),
        ).toBeInTheDocument();
      }
      expect(screen.queryByText(/zpay|微信支付商户|支付宝商户/i)).toBeNull();
      await waitFor(() =>
        expect(
          fetchMock.mock.calls.some(([url]) =>
            String(url).endsWith("/payment-code"),
          ),
        ).toBe(true),
      );
    },
  );

  it("closes on Escape", () => {
    const onClose = vi.fn();
    vi.stubGlobal(
      "fetch",
      vi.fn((url: string) => {
        if (String(url).endsWith("/api/customer/recharge-packages")) {
          return jsonResponse({ items: [] });
        }
        return jsonResponse(walletPayload);
      }),
    );
    renderDialog({ onClose });

    fireEvent.keyDown(document, { key: "Escape" });
    expect(onClose).toHaveBeenCalledOnce();
  });

  it("disables packages priced below the effective minimum recharge amount", async () => {
    // 低于起充额的档位（50 元 < 100 元）后端下单必 422，对话框不得放行点击。
    const belowMinimum = {
      ...rechargePackagePayload.items[0],
      id: "pkg-50",
      name: "五十元档",
      amount_fen: 5000,
      credits: 5,
    };
    vi.stubGlobal(
      "fetch",
      vi.fn((url: string) => {
        if (String(url).endsWith("/api/customer/recharge-packages")) {
          return jsonResponse({
            items: [belowMinimum, ...rechargePackagePayload.items],
          });
        }
        return jsonResponse(walletPayload);
      }),
    );
    renderDialog();
    const lowCard = await screen.findByRole("button", { name: /五十元档/ });
    expect(lowCard).toBeDisabled();
    expect(lowCard).toHaveTextContent("低于起充金额");
    const okCard = screen.getByRole("button", { name: /标准档/ });
    expect(okCard).toBeEnabled();
  });

  it("keeps the custom-amount path when packages are unavailable", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn((url: string) => {
        if (String(url).endsWith("/api/customer/recharge-packages")) {
          return Promise.reject(new Error("套餐接口不可用"));
        }
        return jsonResponse(walletPayload);
      }),
    );
    renderDialog();

    expect(
      await screen.findByText("充值套餐暂不可用，可使用自定义金额充值。"),
    ).toBeInTheDocument();
    expect(screen.queryByText("标准档")).toBeNull();
    expect(screen.getByLabelText("自定义金额（元）")).toBeInTheDocument();
  });

  it("hints at a matching package but still orders the raw custom amount", async () => {
    const fetchMock = vi.fn((url: string, options?: RequestInit) => {
      if (String(url).endsWith("/api/customer/recharge-packages")) {
        return jsonResponse(rechargePackagePayload);
      }
      if (String(url).endsWith("/api/customer/wallet")) {
        return jsonResponse(walletPayload);
      }
      if (
        String(url).endsWith("/api/customer/recharge-orders") &&
        options?.method === "POST"
      ) {
        return jsonResponse(
          {
            order_no: "202608270001",
            status: "PENDING",
            amount_fen: 10000,
            credits: 10,
            gateway_url: "https://payment.example/submit",
            method: "POST",
            form_fields: {},
          },
          201,
        );
      }
      if (String(url).endsWith("/payment-code")) {
        return jsonResponse({
          order_no: "202608270001",
          amount_fen: 10000,
          credits: 10,
          qr_image_url: "https://payment.example/qr.png",
          payment_url: "https://payment.example/pay",
        });
      }
      throw new Error(`unexpected request: ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    renderDialog();

    await screen.findByRole("button", { name: /标准档/ });
    fireEvent.change(screen.getByLabelText("自定义金额（元）"), {
      target: { value: "100" },
    });
    expect(await screen.findByText(/该金额有对应套餐/)).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "确认充值" }));
    await waitFor(() =>
      expect(
        fetchMock.mock.calls.some(
          ([url, options]) =>
            String(url).endsWith("/api/customer/recharge-orders") &&
            options?.method === "POST",
        ),
      ).toBe(true),
    );
    const createCall = fetchMock.mock.calls.find(
      ([url, options]) =>
        String(url).endsWith("/api/customer/recharge-orders") &&
        options?.method === "POST",
    );
    // 自定义金额不带 package_id：按基础汇率到账，不享受套餐赠送/权益。
    expect(createCall?.[1]?.body).toBe(JSON.stringify({ amount_fen: 10000 }));
  });

  it("marks the package recommended by the wallet entry as selected", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn((url: string) => {
        if (String(url).endsWith("/api/customer/recharge-packages")) {
          return jsonResponse(discountPackagePayload);
        }
        if (String(url).endsWith("/api/customer/wallet")) {
          return jsonResponse(walletPayload);
        }
        return jsonResponse({ items: [], total: 0, limit: 20, offset: 0 });
      }),
    );
    renderDialog({
      suggestedAmountYuan: 1998,
      suggestedPackageId: "pkg-premium",
    });

    const card = await screen.findByRole("button", { name: /尊享档/ });
    expect(card).toHaveClass("recharge-package-card--suggested");
    expect(card).toHaveTextContent("已选套餐");
    expect(card).toHaveTextContent("视频生成 9折");
    expect(card).toHaveTextContent("含赠送 1801 积分");
    // 套餐价不能污染自定义输入；两个入口适用不同的步长规则。
    expect(screen.getByLabelText("自定义金额（元）")).toHaveValue(null);
    expect(screen.getByText(/最低 100元/)).toBeInTheDocument();
    expect(screen.getByLabelText("自定义金额（元）")).toHaveAttribute(
      "min",
      "100",
    );
  });

  it.each(["", "0", "499", "550", "500.01", "22000000"])(
    "自定义金额 %s 不符合500元起100元步长时，不创建支付订单",
    async (amount) => {
      const fetchMock = configuredRechargeFetchMock();
      vi.stubGlobal("fetch", fetchMock);
      renderDialog();
      await screen.findByRole("button", { name: /基础会员/ });

      fireEvent.click(screen.getByRole("radio", { name: "自定义充值" }));
      fireEvent.change(screen.getByLabelText("自定义金额（元）"), {
        target: { value: amount },
      });
      fireEvent.click(screen.getByRole("button", { name: "确认充值" }));

      expect(await screen.findByRole("alert")).toHaveTextContent(
        "充值金额须为500元起，并按100元递增。",
      );
      expect(
        fetchMock.mock.calls.some(([, options]) => options?.method === "POST"),
      ).toBe(false);
    },
  );

  it.each([500, 600])(
    "自定义充值%s元显示基础汇率预估并提交不带套餐编号的精确金额",
    async (amount) => {
      const fetchMock = configuredRechargeFetchMock();
      vi.stubGlobal("fetch", fetchMock);
      renderDialog();
      await screen.findByRole("button", { name: /基础会员/ });
      const input = screen.getByLabelText("自定义金额（元）");
      expect(input).toHaveAttribute("min", "500");
      expect(input).toHaveAttribute("step", "100");
      fireEvent.change(input, { target: { value: String(amount) } });
      expect(screen.getByRole("radio", { name: "自定义充值" })).toBeChecked();
      expect(
        screen.getByText(
          `预计到账 ${(amount * 10).toLocaleString("zh-CN")} 积分`,
        ),
      ).toBeInTheDocument();
      fireEvent.click(screen.getByRole("button", { name: "确认充值" }));
      await screen.findByRole("img", { name: "充值支付二维码" });
      const createCall = fetchMock.mock.calls.find(
        ([url, options]) =>
          url.endsWith("/api/customer/recharge-orders") &&
          options?.method === "POST",
      );
      expect(JSON.parse(String(createCall?.[1]?.body))).toEqual({
        amount_fen: amount * 100,
      });
    },
  );

  it.each([
    ["基础会员", "basic", 198000],
    ["白银会员", "silver", 298000],
    ["黄金会员", "gold", 398000],
  ] as const)(
    "%s按后台原价提交，不受自定义100元步长限制",
    async (name, id, amountFen) => {
      const fetchMock = configuredRechargeFetchMock();
      vi.stubGlobal("fetch", fetchMock);
      renderDialog();
      fireEvent.click(
        await screen.findByRole("button", { name: new RegExp(name) }),
      );
      fireEvent.click(screen.getByRole("button", { name: "确认充值" }));
      await screen.findByRole("img", { name: "充值支付二维码" });
      const createCall = fetchMock.mock.calls.find(
        ([url, options]) =>
          url.endsWith("/api/customer/recharge-orders") &&
          options?.method === "POST",
      );
      expect(JSON.parse(String(createCall?.[1]?.body))).toEqual({
        amount_fen: amountFen,
        package_id: id,
      });
    },
  );

  it("保留后台自定义套餐名称和完整折扣范围文字", async () => {
    const name = "运营配置的套餐名称与完整说明：用于持续创作和团队日常视频制作";
    const fetchMock = configuredRechargeFetchMock();
    vi.stubGlobal("fetch", (url: string, options?: RequestInit) => {
      if (url.endsWith("/api/customer/recharge-packages")) {
        return jsonResponse({
          items: [
            {
              ...discountPackagePayload.items[0],
              name,
              discount_interfaces: [
                "video_generation",
                "video_analysis",
                "first_frame",
                "character",
                "script_rewrite",
                "oral",
                "asr",
                "viral_extract",
                "link_resolution",
                "prompt_optimize",
                "后台保留的自定义范围文字",
              ],
            },
          ],
        });
      }
      return fetchMock(url, options);
    });
    renderDialog();
    const card = await screen.findByRole("button", { name: new RegExp(name) });
    expect(within(card).getByText(name)).toBeInTheDocument();
    expect(card).toHaveTextContent(
      "视频生成、视频分析、首帧生成、人物形象、文案改写、数字人口播、语音转写、爆款提取、链接解析、提示词优化、后台保留的自定义范围文字 9折",
    );
  });

  it("自定义预填的非整元金额按生效步长精确下单，不被浮点尘埃误拒", async () => {
    const fetchMock = vi.fn((url: string, options?: RequestInit) => {
      if (String(url).endsWith("/api/customer/recharge-packages")) {
        return jsonResponse({
          items: [
            {
              id: "pkg-9998",
              name: "尝鲜档",
              amount_fen: 9998,
              credits: 10,
              discount_rate: null,
              discount_interfaces: [],
              sort_order: 0,
              is_active: true,
              version: 1,
              created_at: null,
              updated_at: null,
            },
          ],
        });
      }
      if (String(url).endsWith("/api/customer/wallet")) {
        // step 收窄到 1 分：隔离浮点问题本身——原实现
        // Number.isInteger(99.98) 恒为 false，套餐引导的预填金额必被拒。
        return jsonResponse({
          available_credits: 12,
          reserved_credits: 0,
          internal_unit_price_fen: 1000,
          min_recharge_fen: 1000,
          recharge_step_fen: 1,
        });
      }
      if (String(url).endsWith("/api/customer/recharge-orders")) {
        if (options?.method === "POST") {
          return jsonResponse({
            order_no: "202609240001",
            amount_fen: 9998,
            credits: 10,
            status: "PENDING",
          });
        }
        return jsonResponse({
          order_no: "202609240001",
          amount_fen: 9998,
          credits: 10,
          status: "PENDING",
        });
      }
      if (String(url).endsWith("/payment-code")) {
        return jsonResponse({
          qr_image_url: "https://qr.example/pay/1",
          payment_url: "https://pay.example/1",
        });
      }
      return jsonResponse({ items: [], total: 0, limit: 20, offset: 0 });
    });
    vi.stubGlobal("fetch", fetchMock);
    renderDialog({
      suggestedAmountYuan: 99.98,
    });

    expect(await screen.findByLabelText("自定义金额（元）")).toHaveValue(99.98);
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "确认充值" })).toBeEnabled(),
    );
    fireEvent.click(screen.getByRole("button", { name: "确认充值" }));
    await waitFor(() =>
      expect(
        fetchMock.mock.calls.some(
          ([url, options]) =>
            String(url).endsWith("/api/customer/recharge-orders") &&
            options?.method === "POST",
        ),
      ).toBe(true),
    );
    const createCall = fetchMock.mock.calls.find(
      ([url, options]) =>
        String(url).endsWith("/api/customer/recharge-orders") &&
        options?.method === "POST",
    );
    // 精确落在 9998 分：字符串拼分路线不受 99.98*100=9998.000…002 影响。
    expect(createCall?.[1]?.body).toBe(JSON.stringify({ amount_fen: 9998 }));
  });

  // 刻意放在最后：本用例把 orderPollWithinWindow 压成一次 false，虽然只是
  // `mockReturnValueOnce`（只吃一次调用），但把它摆在末尾就不必依赖
  // afterEach 的 restoreAllMocks 是否会把默认实现还原——后续用例的轮询行为
  // 不受任何残留影响。
  it("轮询窗口用尽后停止自动查询并改口径，不留下「系统会继续查询」（评审 #10）", async () => {
    // 第一次轮询就判定窗口已用尽：订单仍是 PENDING，但不能再自动查询下去。
    vi.mocked(orderPollWithinWindow).mockReturnValueOnce(false);
    vi.stubGlobal("fetch", paymentFlowFetchMock());
    renderDialog();

    fireEvent.click(await screen.findByRole("button", { name: /100元/ }));
    fireEvent.click(screen.getByRole("button", { name: "确认充值" }));
    await screen.findByRole("img", { name: "充值支付二维码" });

    // 此前这里只是不再 setTimeout，屏幕上「系统会继续查询」会一直留着，
    // 客户以为系统还在查、继续等。
    expect(
      await screen.findByText("支付结果仍待确认，可稍后刷新页面继续查询。"),
    ).toBeVisible();
    expect(screen.queryByText(/系统会继续查询/)).toBeNull();
  });
});
