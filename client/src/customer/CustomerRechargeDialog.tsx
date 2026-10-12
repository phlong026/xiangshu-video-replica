import {
  type FormEvent,
  useCallback,
  useEffect,
  useRef,
  useState,
} from "react";

import {
  type CreatedRechargeOrder,
  CustomerApiError,
  type CustomerPaymentCode,
  type CustomerRechargePackage,
  customerCreateRechargeOrder,
  customerCreateRechargePaymentCode,
  customerGetRechargeOrder,
  customerGetWallet,
  customerListRechargePackages,
  type WalletSnapshot,
} from "../api";
import {
  matchingPackagesForAmount,
  packageBelowMinimum,
  packageBenefitLabel,
  packageBonusCredits,
  yuanInputToFen,
} from "../rechargePackageDisplay";
import { orderPollDelay, orderPollWithinWindow } from "./orderPolling";
import type { CustomerCredentialStore } from "./useCustomerSession";

/**
 * 轮询窗口（5 分钟）用尽后的口径，与钱包面板逐字一致。
 *
 * 窗口用尽后自动轮询就停了：此时若还把「系统会继续查询」留在弹窗上，客户会一直等
 * 一个不再发生的查询（同一次改动里钱包面板有明确提示，只有弹窗漏了）。
 */
const POLL_WINDOW_EXHAUSTED_NOTICE =
  "支付结果仍待确认，可稍后刷新页面继续查询。";

export function CustomerRechargeDialog({
  isOpen,
  onClose,
  onOrderCreated,
  onPaid,
  onSessionExpired,
  store,
  suggestedAmountYuan,
  suggestedPackageId,
}: {
  isOpen: boolean;
  onClose: () => void;
  onOrderCreated: () => void;
  onPaid: () => void;
  onSessionExpired: () => void;
  store: CustomerCredentialStore;
  suggestedAmountYuan?: number;
  /** 上游（如钱包套餐卡片）已选中的套餐：高亮提示，仍需用户再确认一次。 */
  suggestedPackageId?: string;
}) {
  const [wallet, setWallet] = useState<WalletSnapshot | null>(null);
  const [packages, setPackages] = useState<CustomerRechargePackage[]>([]);
  const [packageError, setPackageError] = useState("");
  const [amountYuan, setAmountYuan] = useState("");
  // 订单与码只能作为同一个视图替换，避免异步响应跨订单拼接。
  const [payment, setPayment] = useState<{
    order: CreatedRechargeOrder;
    code: CustomerPaymentCode | null;
    credentialToken: string;
  } | null>(null);
  const order = payment?.order ?? null;
  const paymentCode = payment?.code ?? null;
  const generation = useRef(0);
  const busy = useRef(false);
  const [paymentState, setPaymentState] = useState<
    "choosing" | "creating" | "waiting" | "paid"
  >("choosing");
  const [isQrLoaded, setIsQrLoaded] = useState(false);
  const [error, setError] = useState("");

  const loadCredential = useCallback(
    async (round = generation.current) => {
      const token = await store.loadSessionToken();
      if (round !== generation.current) return null;
      if (token === null) {
        onSessionExpired();
        return null;
      }
      return { kind: "session" as const, token };
    },
    [store, onSessionExpired],
  );

  const closeDialog = useCallback(() => {
    generation.current += 1;
    busy.current = false;
    onClose();
  }, [onClose]);

  const stillCurrent = useCallback(
    async (round: number, token: string) => {
      if (round !== generation.current) return false;
      const currentToken = await store.loadSessionToken();
      if (round !== generation.current) return false;
      if (currentToken !== token) {
        generation.current += 1;
        busy.current = false;
        setPayment(null);
        setWallet(null);
        setPackages([]);
        setPaymentState("choosing");
        setError("登录账号已变化，请关闭后重新打开充值窗口。");
        return false;
      }
      return true;
    },
    [store],
  );

  useEffect(() => {
    const round = ++generation.current;
    busy.current = false;
    if (!isOpen) {
      return;
    }
    setWallet(null);
    setAmountYuan(suggestedAmountYuan ? String(suggestedAmountYuan) : "");
    setPayment(null);
    setPaymentState("choosing");
    setIsQrLoaded(false);
    setError("");
    setPackages([]);
    setPackageError("");
    let active = true;
    void loadCredential()
      .then(async (credential) => {
        if (credential === null) {
          return;
        }
        // 钱包与套餐互不拖累：套餐加载失败时自定义金额仍可用。
        const [walletResult, packageResult] = await Promise.allSettled([
          customerGetWallet(credential),
          customerListRechargePackages(credential),
        ]);
        if (!active || !(await stillCurrent(round, credential.token))) {
          return;
        }
        if (walletResult.status === "fulfilled") {
          setWallet(walletResult.value);
        } else {
          setError(
            visibleError(walletResult.reason, "充值信息暂不可用，请稍后重试。"),
          );
        }
        if (packageResult.status === "fulfilled") {
          setPackages(packageResult.value);
        } else {
          setPackageError("充值套餐暂不可用，可使用自定义金额充值。");
        }
      })
      .catch((cause) => {
        if (active && round === generation.current) {
          setError(visibleError(cause, "充值信息暂不可用，请稍后重试。"));
        }
      });
    return () => {
      active = false;
      generation.current += 1;
      busy.current = false;
    };
  }, [isOpen, suggestedAmountYuan, loadCredential, stillCurrent]);

  useEffect(() => {
    if (!isOpen || paymentState !== "waiting" || payment === null) {
      return;
    }
    const polledOrder = payment.order;
    const paymentToken = payment.credentialToken;
    let active = true;
    const round = generation.current;
    let timer: number | undefined;
    // P2#20：与本页钱包面板同一个节奏（2s 起、翻倍、封顶 32s、5 分钟窗口）。
    let attempts = 0;
    const startedAt = Date.now();

    async function pollOrder() {
      const nextDelay = orderPollDelay(attempts);
      attempts += 1;
      try {
        if (!active || !(await stillCurrent(round, paymentToken))) return;
        const credential = { kind: "session" as const, token: paymentToken };
        const latest = await customerGetRechargeOrder(
          credential,
          polledOrder.order_no,
        );
        if (!active || !(await stillCurrent(round, credential.token))) {
          return;
        }
        if (
          latest.order_no !== polledOrder.order_no ||
          latest.amount_fen !== polledOrder.amount_fen ||
          latest.credits !== polledOrder.credits
        ) {
          throw new Error("支付结果与当前订单不一致，请稍后重试。");
        }
        if (latest.status === "PAID") {
          setPaymentState("paid");
          onPaid();
          return;
        }
        if (latest.status === "FAILED" || latest.status === "CLOSED") {
          setPayment(null);
          setError("该充值订单已结束，请重新创建订单。");
          setPaymentState("choosing");
          return;
        }
        if (orderPollWithinWindow(startedAt)) {
          timer = window.setTimeout(pollOrder, nextDelay);
        } else {
          // 窗口用尽即停：必须同时改文案——否则屏幕上留着「系统会继续查询」，
          // 客户会一直等一个已经不存在的轮询（钱包面板对同一情形就是这样做的）。
          setError(POLL_WINDOW_EXHAUSTED_NOTICE);
        }
      } catch (cause) {
        if (!active || round !== generation.current) {
          return;
        }
        if (orderPollWithinWindow(startedAt)) {
          setError(
            visibleError(cause, "暂时无法确认支付结果，系统会继续查询。"),
          );
          timer = window.setTimeout(pollOrder, nextDelay);
        } else {
          setError(POLL_WINDOW_EXHAUSTED_NOTICE);
        }
      }
    }

    void pollOrder();
    return () => {
      active = false;
      if (timer !== undefined) {
        window.clearTimeout(timer);
      }
    };
  }, [isOpen, paymentState, payment, onPaid, stillCurrent]);

  useEffect(() => {
    if (!isOpen) {
      return;
    }
    function closeOnEscape(event: KeyboardEvent) {
      if (event.key === "Escape") {
        closeDialog();
      }
    }
    document.addEventListener("keydown", closeOnEscape);
    return () => document.removeEventListener("keydown", closeOnEscape);
  }, [isOpen, closeDialog]);

  async function createOrder(input: { amountFen: number; packageId?: string }) {
    // 在读凭据前加锁，连续点击不能同时进入两个创建请求。
    if (busy.current || !isOpen) return;
    busy.current = true;
    const round = ++generation.current;
    setPayment(null);
    setPaymentState("creating");
    setError("");
    setIsQrLoaded(false);
    try {
      const credential = await loadCredential();
      if (credential === null || round !== generation.current) return;
      const created = await customerCreateRechargeOrder(
        credential,
        input.amountFen,
        { idempotencyKey: crypto.randomUUID(), packageId: input.packageId },
      );
      if (!(await stillCurrent(round, credential.token))) return;
      setPayment({
        order: created,
        code: null,
        credentialToken: credential.token,
      });
      // 订单已持久化；取码失败仍保留同一订单，供用户查询或重试。
      onOrderCreated();
      const code = await customerCreateRechargePaymentCode(
        credential,
        created.order_no,
      );
      if (!(await stillCurrent(round, credential.token))) return;
      assertPaymentMatches(created, code);
      setPayment({ order: created, code, credentialToken: credential.token });
      setPaymentState("waiting");
    } catch (cause) {
      if (round !== generation.current) return;
      setError(visibleError(cause, "支付二维码暂时无法生成，请稍后重试。"));
      setPaymentState("choosing");
    } finally {
      if (round === generation.current) {
        busy.current = false;
        setPaymentState((state) => (state === "creating" ? "choosing" : state));
      }
    }
  }

  async function createCustomPayment(amountYuanText: string) {
    if (!wallet) {
      return;
    }
    // 上线审查 P1-4：金额校验必须在「分」域内做精确整数判断。原实现
    // Number.isInteger(元) + 元*100 双重浮点路线，套餐引导预填的非整元
    // 金额（9998 分 → "99.98"）会必然撞上本校验，主流程被自己的
    // 推荐档位卡死。
    const amountFen = yuanInputToFen(amountYuanText);
    if (
      amountFen === null ||
      amountFen < wallet.min_recharge_fen ||
      amountFen % wallet.recharge_step_fen !== 0
    ) {
      setError(
        `充值金额须为${formatFen(wallet.min_recharge_fen)}起，并按${formatFen(wallet.recharge_step_fen)}递增。`,
      );
      return;
    }
    await createOrder({ amountFen });
  }

  async function retryPaymentCode() {
    if (payment === null || busy.current || !isOpen) return;
    busy.current = true;
    const round = ++generation.current;
    const currentOrder = payment.order;
    const paymentToken = payment.credentialToken;
    setPayment({
      order: currentOrder,
      code: null,
      credentialToken: paymentToken,
    });
    setPaymentState("creating");
    setError("");
    setIsQrLoaded(false);
    try {
      if (!(await stillCurrent(round, paymentToken))) return;
      const credential = { kind: "session" as const, token: paymentToken };
      const code = await customerCreateRechargePaymentCode(
        credential,
        currentOrder.order_no,
      );
      if (!(await stillCurrent(round, credential.token))) return;
      assertPaymentMatches(currentOrder, code);
      setPayment({ order: currentOrder, code, credentialToken: paymentToken });
      setPaymentState("waiting");
    } catch (cause) {
      if (round !== generation.current) return;
      setError(visibleError(cause, "支付二维码暂时无法生成，请稍后重试。"));
      setPaymentState("choosing");
    } finally {
      if (round === generation.current) {
        busy.current = false;
        setPaymentState((state) => (state === "creating" ? "choosing" : state));
      }
    }
  }

  function submitCustom(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    void createCustomPayment(amountYuan);
  }

  if (!isOpen) {
    return null;
  }

  // 输入串只解析一次：套餐命中提示与到账预估都必须用精确分值，
  // Number 路线的浮点尘埃会让 "99.98" 永远匹配不到 9998 分的套餐。
  const customAmountFen = yuanInputToFen(amountYuan);

  return (
    <div className="recharge-dialog">
      <section
        aria-labelledby="recharge-dialog-title"
        aria-modal="true"
        className="recharge-dialog__panel"
        role="dialog"
      >
        <header className="recharge-dialog__header">
          <div>
            <p className="eyebrow">余额充值</p>
            <h2 id="recharge-dialog-title">扫码充值</h2>
          </div>
          <button
            aria-label="关闭充值窗口"
            className="secondary-button"
            onClick={closeDialog}
            type="button"
          >
            关闭
          </button>
        </header>

        {paymentState === "paid" ? (
          <div className="recharge-dialog__success" role="status">
            <strong>充值成功</strong>
            <p>积分已经到账，可以继续创建视频。</p>
            <button onClick={closeDialog} type="button">
              完成
            </button>
          </div>
        ) : paymentCode && order ? (
          <div className="recharge-dialog__payment">
            <div className="recharge-dialog__qr">
              {!isQrLoaded ? <span>二维码加载中…</span> : null}
              <img
                key={`${order.order_no}:${paymentCode.qr_image_url}`}
                alt="充值支付二维码"
                onError={() => {
                  setIsQrLoaded(false);
                  setError("二维码图片加载失败，请点击重新获取。");
                }}
                onLoad={() => setIsQrLoaded(true)}
                src={paymentCode.qr_image_url}
              />
            </div>
            <div className="recharge-dialog__summary">
              <span>支付金额</span>
              <strong>{formatFen(order.amount_fen)}</strong>
              <p>到账 {order.credits} 积分 · 支付完成后自动到账</p>
              <span className="recharge-dialog__waiting" role="status">
                正在等待支付结果
              </span>
              {paymentCode.payment_url.startsWith("weixin://") ? (
                <p>请使用微信扫一扫完成支付。</p>
              ) : (
                <button
                  className="secondary-button"
                  onClick={() =>
                    window.open(
                      paymentCode.payment_url,
                      "_blank",
                      "noopener,noreferrer",
                    )
                  }
                  type="button"
                >
                  无法扫码？打开支付页面
                </button>
              )}
            </div>
          </div>
        ) : (
          <div className="recharge-dialog__chooser">
            <p>
              {wallet
                ? `当前可用 ${wallet.available_credits} 积分，${wallet.points_per_yuan ? `1 元 = ${wallet.points_per_yuan} 积分` : `兑换单价 ${formatFen(wallet.internal_unit_price_fen)}/积分`}。`
                : "正在读取充值信息…"}
            </p>
            {packages.length ? (
              <div className="recharge-dialog__packages">
                {packages.map((pkg) => {
                  const benefit = packageBenefitLabel(pkg);
                  const bonus = packageBonusCredits(wallet, pkg);
                  // 低于生效起充额的档位后端必 422：钱包快照就绪后置灰并说明原因。
                  const belowMinimum = packageBelowMinimum(pkg, wallet);
                  const suggested = pkg.id === suggestedPackageId;
                  return (
                    <button
                      className={
                        belowMinimum
                          ? "recharge-package-card recharge-package-card--locked"
                          : suggested
                            ? "recharge-package-card recharge-package-card--suggested"
                            : "recharge-package-card"
                      }
                      disabled={paymentState === "creating" || belowMinimum}
                      key={pkg.id}
                      onClick={() =>
                        void createOrder({
                          amountFen: pkg.amount_fen,
                          packageId: pkg.id,
                        })
                      }
                      type="button"
                    >
                      <span className="recharge-package-card__head">
                        <span className="recharge-package-card__tier">
                          {pkg.name}
                        </span>
                        {suggested ? (
                          <span className="recharge-package-card__badge">
                            已选套餐
                          </span>
                        ) : null}
                      </span>
                      <span className="recharge-package-card__price">
                        {formatFen(pkg.amount_fen)}
                      </span>
                      <span className="recharge-package-card__credits">
                        到账 {pkg.credits} 积分
                      </span>
                      {bonus !== null ? (
                        <span className="recharge-package-card__bonus">
                          含赠送 {bonus} 积分
                        </span>
                      ) : null}
                      {benefit ? (
                        <span
                          className="recharge-package-card__benefit"
                          title={benefit}
                        >
                          {benefit}
                        </span>
                      ) : null}
                      {belowMinimum && wallet ? (
                        <span className="recharge-package-card__note">
                          低于起充金额 {formatFen(wallet.min_recharge_fen)}
                          ，暂不可购
                        </span>
                      ) : null}
                    </button>
                  );
                })}
              </div>
            ) : packageError ? (
              <p role="status">{packageError}</p>
            ) : null}
            <form onSubmit={submitCustom}>
              <label>
                自定义金额（元）
                <input
                  inputMode="numeric"
                  min={wallet ? wallet.min_recharge_fen / 100 : undefined}
                  onChange={(event) => setAmountYuan(event.target.value)}
                  placeholder={
                    wallet ? `不低于 ${formatFen(wallet.min_recharge_fen)}` : ""
                  }
                  step={wallet ? wallet.recharge_step_fen / 100 : undefined}
                  type="number"
                  value={amountYuan}
                />
              </label>
              <button
                disabled={!wallet || paymentState === "creating"}
                type="submit"
              >
                {paymentState === "creating"
                  ? "正在生成二维码…"
                  : "生成支付二维码"}
              </button>
            </form>
            {wallet ? (
              <p className="recharge-dialog__custom-hint" role="note">
                自定义充值金额不低于 {formatFen(wallet.min_recharge_fen)}
                ，并按 {formatFen(wallet.recharge_step_fen)} 递增。
              </p>
            ) : null}
            {wallet &&
            customAmountFen !== null &&
            matchingPackagesForAmount(packages, customAmountFen).length ? (
              <p role="status">
                该金额有对应套餐（含赠送/折扣）；选择上方套餐可享受套餐到账与权益，
                自定义金额按基础汇率到账。
              </p>
            ) : null}
            {wallet && customAmountFen !== null && (
              <p role="status">
                预计到账{" "}
                {wallet.points_per_yuan
                  ? Math.floor((customAmountFen * wallet.points_per_yuan) / 100)
                  : Math.floor(
                      customAmountFen / wallet.internal_unit_price_fen,
                    )}{" "}
                积分，以创建订单时的兑换规则为准。
              </p>
            )}
            {order && !paymentCode ? (
              <button
                className="secondary-button"
                disabled={paymentState === "creating"}
                onClick={() => void retryPaymentCode()}
                type="button"
              >
                重新获取上一订单二维码
              </button>
            ) : null}
          </div>
        )}

        {error && paymentCode ? (
          <button
            className="secondary-button"
            disabled={paymentState === "creating"}
            onClick={() => void retryPaymentCode()}
            type="button"
          >
            重新获取本订单二维码
          </button>
        ) : null}
        {error ? (
          <p className="settings-error" role="alert">
            {error}
          </p>
        ) : null}
        <footer>请以本窗口显示的金额为准；如已扣款，请勿重复支付。</footer>
      </section>
    </div>
  );
}

function formatFen(amountFen: number): string {
  const yuan = amountFen / 100;
  return `${Number.isInteger(yuan) ? yuan : yuan.toFixed(2)}元`;
}

function visibleError(cause: unknown, fallback: string): string {
  if (cause instanceof CustomerApiError && cause.status === 401) {
    return "登录已失效，请重新进入工作台。";
  }
  return cause instanceof Error && cause.message.trim()
    ? cause.message
    : fallback;
}

function assertPaymentMatches(
  order: CreatedRechargeOrder,
  code: CustomerPaymentCode,
) {
  if (
    code.order_no !== order.order_no ||
    code.amount_fen !== order.amount_fen ||
    code.credits !== order.credits
  ) {
    throw new Error("支付二维码与当前订单不一致，请重新获取本订单二维码。");
  }
}
