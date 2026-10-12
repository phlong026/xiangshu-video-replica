import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import * as api from "../api";
import { CustomerRechargeDialog } from "./CustomerRechargeDialog";
import type { CustomerCredentialStore } from "./useCustomerSession";

vi.mock("../api", () => ({
  CustomerApiError: class extends Error {},
  customerCreateRechargeOrder: vi.fn(),
  customerCreateRechargePaymentCode: vi.fn(),
  customerGetRechargeOrder: vi.fn(),
  customerGetWallet: vi.fn(),
  customerListRechargePackages: vi.fn(),
}));

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}
function order(order_no: string, amount_fen = 1000) {
  return {
    order_no,
    amount_fen,
    credits: amount_fen,
    status: "PENDING",
  } as api.CreatedRechargeOrder;
}
function code(order_no: string, amount_fen = 1000): api.CustomerPaymentCode {
  return {
    order_no,
    amount_fen,
    credits: amount_fen,
    qr_image_url: `https://test.invalid/${order_no}.png`,
    payment_url: `weixin://${order_no}`,
  };
}
const props = () => ({
  isOpen: true,
  onClose: vi.fn(),
  onOrderCreated: vi.fn(),
  onPaid: vi.fn(),
  onSessionExpired: vi.fn(),
  store: {
    loadSessionToken: vi.fn().mockResolvedValue("fake-a"),
  } as unknown as CustomerCredentialStore,
});
async function choose() {
  fireEvent.click(await screen.findByRole("button", { name: /套餐A/ }));
}

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(api.customerGetWallet).mockResolvedValue({
    min_recharge_fen: 100,
    recharge_step_fen: 100,
    available_credits: 0,
    points_per_yuan: 100,
  } as api.WalletSnapshot);
  vi.mocked(api.customerListRechargePackages).mockResolvedValue([
    { id: "a", name: "套餐A", amount_fen: 1000, credits: 1000 },
  ] as api.CustomerRechargePackage[]);
  vi.mocked(api.customerGetRechargeOrder).mockImplementation(
    () => new Promise(() => {}),
  );
});
afterEach(cleanup);

describe("充值订单与二维码围栏", () => {
  it("关闭重开后旧二维码不能覆盖新订单金额与积分", async () => {
    const oldCode = deferred<api.CustomerPaymentCode>();
    vi.mocked(api.customerCreateRechargeOrder)
      .mockResolvedValueOnce(order("A"))
      .mockResolvedValueOnce(order("B", 2000));
    vi.mocked(api.customerCreateRechargePaymentCode)
      .mockReturnValueOnce(oldCode.promise)
      .mockResolvedValueOnce(code("B", 2000));
    const p = props();
    const view = render(<CustomerRechargeDialog {...p} />);
    await choose();
    await waitFor(() =>
      expect(api.customerCreateRechargePaymentCode).toHaveBeenCalledTimes(1),
    );
    view.rerender(<CustomerRechargeDialog {...p} isOpen={false} />);
    view.rerender(<CustomerRechargeDialog {...p} />);
    await choose();
    await waitFor(() =>
      expect(
        screen.getByAltText("充值支付二维码").getAttribute("src"),
      ).toContain("B.png"),
    );
    await act(async () => {
      oldCode.resolve(code("A"));
    });
    expect(screen.getByAltText("充值支付二维码").getAttribute("src")).toContain(
      "B.png",
    );
    expect(screen.getByText("20元")).toBeTruthy();
  });

  it("旧创建订单在关闭后返回不再生成二维码或触发新窗口回调", async () => {
    const oldOrder = deferred<api.CreatedRechargeOrder>();
    vi.mocked(api.customerCreateRechargeOrder).mockReturnValueOnce(
      oldOrder.promise,
    );
    const p = props();
    const view = render(<CustomerRechargeDialog {...p} />);
    await choose();
    await waitFor(() =>
      expect(api.customerCreateRechargeOrder).toHaveBeenCalledTimes(1),
    );
    view.rerender(<CustomerRechargeDialog {...p} isOpen={false} />);
    await act(async () => {
      oldOrder.resolve(order("A"));
    });
    expect(api.customerCreateRechargePaymentCode).not.toHaveBeenCalled();
    expect(p.onOrderCreated).not.toHaveBeenCalled();
  });

  it("服务端返回不匹配的订单二维码时拒绝显示", async () => {
    vi.mocked(api.customerCreateRechargeOrder).mockResolvedValue(order("A"));
    vi.mocked(api.customerCreateRechargePaymentCode).mockResolvedValue(
      code("B"),
    );
    render(<CustomerRechargeDialog {...props()} />);
    await choose();
    await waitFor(() =>
      expect(api.customerCreateRechargePaymentCode).toHaveBeenCalled(),
    );
    await waitFor(() =>
      expect(screen.queryByAltText("充值支付二维码")).toBeNull(),
    );
    expect(await screen.findByRole("alert")).toBeTruthy();
  });

  it("账号令牌在请求中切换后不得显示旧账号订单", async () => {
    const pending = deferred<api.CustomerPaymentCode>();
    vi.mocked(api.customerCreateRechargeOrder).mockResolvedValue(order("A"));
    vi.mocked(api.customerCreateRechargePaymentCode).mockReturnValue(
      pending.promise,
    );
    const p = props();
    render(<CustomerRechargeDialog {...p} />);
    await choose();
    await waitFor(() =>
      expect(api.customerCreateRechargePaymentCode).toHaveBeenCalled(),
    );
    vi.mocked(p.store.loadSessionToken).mockResolvedValue("fake-b");
    await act(async () => {
      pending.resolve(code("A"));
    });
    expect(screen.queryByAltText("充值支付二维码")).toBeNull();
  });

  it("二维码失败重试仍绑定原订单并拒绝金额不符的响应", async () => {
    vi.mocked(api.customerCreateRechargeOrder).mockResolvedValue(order("A"));
    vi.mocked(api.customerCreateRechargePaymentCode)
      .mockRejectedValueOnce(new Error("暂不可用"))
      .mockResolvedValueOnce(code("A", 2000));
    render(<CustomerRechargeDialog {...props()} />);
    await choose();
    fireEvent.click(
      await screen.findByRole("button", { name: "重新获取上一订单二维码" }),
    );
    await waitFor(() =>
      expect(api.customerCreateRechargePaymentCode).toHaveBeenCalledTimes(2),
    );
    await waitFor(() =>
      expect(screen.queryByAltText("充值支付二维码")).toBeNull(),
    );
    expect(api.customerCreateRechargeOrder).toHaveBeenCalledTimes(1);
  });
  it("读凭据未完成时连续点击只创建一次订单", async () => {
    const credential = deferred<string>();
    vi.mocked(api.customerCreateRechargeOrder).mockResolvedValue(order("A"));
    vi.mocked(api.customerCreateRechargePaymentCode).mockResolvedValue(
      code("A"),
    );
    const p = props();
    render(<CustomerRechargeDialog {...p} />);
    const button = await screen.findByRole("button", { name: /套餐A/ });
    vi.mocked(p.store.loadSessionToken).mockReturnValue(credential.promise);
    fireEvent.click(button);
    fireEvent.click(button);
    await act(async () => {
      credential.resolve("fake-a");
    });
    await waitFor(() =>
      expect(api.customerCreateRechargeOrder).toHaveBeenCalledTimes(1),
    );
  });

  it("Esc关闭立即作废未完成请求，即使父组件尚未重绘", async () => {
    const pending = deferred<api.CustomerPaymentCode>();
    vi.mocked(api.customerCreateRechargeOrder).mockResolvedValue(order("A"));
    vi.mocked(api.customerCreateRechargePaymentCode).mockReturnValue(
      pending.promise,
    );
    const p = props();
    render(<CustomerRechargeDialog {...p} />);
    await choose();
    await waitFor(() =>
      expect(api.customerCreateRechargePaymentCode).toHaveBeenCalled(),
    );
    fireEvent.keyDown(document, { key: "Escape" });
    await act(async () => {
      pending.resolve(code("A"));
    });
    expect(p.onClose).toHaveBeenCalledTimes(1);
    expect(screen.queryByAltText("充值支付二维码")).toBeNull();
  });

  it("旧订单轮询成功不能将重开后的订单标成已支付", async () => {
    const oldPoll =
      deferred<Awaited<ReturnType<typeof api.customerGetRechargeOrder>>>();
    vi.mocked(api.customerCreateRechargeOrder)
      .mockResolvedValueOnce(order("A"))
      .mockResolvedValueOnce(order("B", 2000));
    vi.mocked(api.customerCreateRechargePaymentCode)
      .mockResolvedValueOnce(code("A"))
      .mockResolvedValueOnce(code("B", 2000));
    vi.mocked(api.customerGetRechargeOrder)
      .mockReturnValueOnce(oldPoll.promise)
      .mockImplementation(() => new Promise(() => {}));
    const p = props();
    const view = render(<CustomerRechargeDialog {...p} />);
    await choose();
    await waitFor(() =>
      expect(api.customerGetRechargeOrder).toHaveBeenCalledTimes(1),
    );
    view.rerender(<CustomerRechargeDialog {...p} isOpen={false} />);
    view.rerender(<CustomerRechargeDialog {...p} />);
    await choose();
    await waitFor(() =>
      expect(api.customerGetRechargeOrder).toHaveBeenCalledTimes(2),
    );
    await act(async () => {
      oldPoll.resolve({
        order_no: "A",
        amount_fen: 1000,
        credits: 1000,
        status: "PAID",
        channel: "TEST",
        created_at: "2026-10-12T00:00:00Z",
        paid_at: "2026-10-12T00:01:00Z",
      });
    });
    expect(p.onPaid).not.toHaveBeenCalled();
    expect(screen.getByAltText("充值支付二维码").getAttribute("src")).toContain(
      "B.png",
    );
  });

  it("换凭据仓库后旧请求不能污染新用户窗口", async () => {
    const pending = deferred<api.CustomerPaymentCode>();
    vi.mocked(api.customerCreateRechargeOrder).mockResolvedValue(order("A"));
    vi.mocked(api.customerCreateRechargePaymentCode).mockReturnValue(
      pending.promise,
    );
    const p = props();
    const view = render(<CustomerRechargeDialog {...p} />);
    await choose();
    await waitFor(() =>
      expect(api.customerCreateRechargePaymentCode).toHaveBeenCalled(),
    );
    const nextStore = {
      loadSessionToken: vi.fn().mockResolvedValue("fake-b"),
    } as unknown as CustomerCredentialStore;
    view.rerender(<CustomerRechargeDialog {...p} store={nextStore} />);
    await act(async () => {
      pending.resolve(code("A"));
    });
    expect(screen.queryByAltText("充值支付二维码")).toBeNull();
    expect(await screen.findByRole("button", { name: /套餐A/ })).toBeTruthy();
  });

  it("图片失败可重取同一订单二维码，不另建付款订单", async () => {
    vi.mocked(api.customerCreateRechargeOrder).mockResolvedValue(order("A"));
    vi.mocked(api.customerCreateRechargePaymentCode).mockResolvedValue(
      code("A"),
    );
    render(<CustomerRechargeDialog {...props()} />);
    await choose();
    fireEvent.error(await screen.findByAltText("充值支付二维码"));
    fireEvent.click(
      await screen.findByRole("button", { name: "重新获取本订单二维码" }),
    );
    await waitFor(() =>
      expect(api.customerCreateRechargePaymentCode).toHaveBeenCalledTimes(2),
    );
    expect(api.customerCreateRechargeOrder).toHaveBeenCalledTimes(1);
    expect(api.customerCreateRechargePaymentCode).toHaveBeenLastCalledWith(
      { kind: "session", token: "fake-a" },
      "A",
    );
  });

  it("已显示二维码后切换同一仓库中的账号，下次轮询不得携新凭据查旧订单", async () => {
    vi.mocked(api.customerCreateRechargeOrder).mockResolvedValue(order("A"));
    vi.mocked(api.customerCreateRechargePaymentCode).mockResolvedValue(
      code("A"),
    );
    vi.mocked(api.customerGetRechargeOrder).mockResolvedValue({
      order_no: "A",
      status: "PENDING",
      amount_fen: 1000,
      credits: 1000,
      channel: "TEST",
      created_at: "2026-10-12T00:00:00Z",
      paid_at: null,
    });
    const p = props();
    render(<CustomerRechargeDialog {...p} />);
    await choose();
    await screen.findByAltText("充值支付二维码");
    await waitFor(() =>
      expect(api.customerGetRechargeOrder).toHaveBeenCalledTimes(1),
    );
    vi.mocked(p.store.loadSessionToken).mockResolvedValue("fake-b");
    await waitFor(
      () => expect(screen.queryByAltText("充值支付二维码")).toBeNull(),
      { timeout: 3500 },
    );
    expect(api.customerGetRechargeOrder).toHaveBeenCalledTimes(1);
  });

  it("已显示二维码后换账号，重取不得携新凭据请求旧订单", async () => {
    vi.mocked(api.customerCreateRechargeOrder).mockResolvedValue(order("A"));
    vi.mocked(api.customerCreateRechargePaymentCode).mockResolvedValue(
      code("A"),
    );
    const p = props();
    render(<CustomerRechargeDialog {...p} />);
    await choose();
    fireEvent.error(await screen.findByAltText("充值支付二维码"));
    vi.mocked(p.store.loadSessionToken).mockResolvedValue("fake-b");
    fireEvent.click(
      await screen.findByRole("button", { name: "重新获取本订单二维码" }),
    );
    await waitFor(() =>
      expect(screen.getByRole("alert").textContent).toContain("登录账号已变化"),
    );
    expect(api.customerCreateRechargePaymentCode).toHaveBeenCalledTimes(1);
    expect(screen.queryByAltText("充值支付二维码")).toBeNull();
  });
});
