import { type FormEvent, useLayoutEffect, useRef, useState } from "react";

import {
  type AdjustmentWriteResult,
  AdminActivationError,
  createCustomerAdjustment,
} from "../api.admin";
import { ConfirmDialog } from "./ui/ConfirmDialog";
import { PageBanner } from "./ui/PageBanner";
import { ADJUSTMENT_SOURCE_LABELS, labelFrom } from "./ui/vocabulary";

/** 与服务端 REVERSAL_SOURCE_DOCUMENT_TYPES 同集合：只有这两类来源允许负数调账。 */
const REFUND_SOURCES = ["REFUND_APPROVAL", "LEDGER_CORRECTION"] as const;

type PendingRefund = {
  userId: string;
  credits: number;
  sourceType: string;
  sourceRef: string;
  originalRechargeOrderNo: string;
  reason: string;
};

type RefundAttempt = {
  request: PendingRefund;
  key: string;
};

/**
 * 钱包可用余额只是前端总上限；真正可扣减范围由服务端逐笔核对原充值。
 * 审批单与原充值单分开记录，不能拿审批凭据代替积分来源证明。
 */
export function CustomerRefundSection({
  userId,
  availableCredits,
  readOnly,
  onRefunded,
}: {
  userId: string;
  availableCredits: number;
  readOnly: boolean;
  onRefunded: (result: AdjustmentWriteResult) => void;
}) {
  const [credits, setCredits] = useState("");
  const [sourceType, setSourceType] = useState<string>("REFUND_APPROVAL");
  const [sourceRef, setSourceRef] = useState("");
  const [originalRechargeOrderNo, setOriginalRechargeOrderNo] = useState("");
  const [reason, setReason] = useState("");
  const [formError, setFormError] = useState("");
  const [dialogError, setDialogError] = useState("");
  const [notice, setNotice] = useState("");
  const [pending, setPending] = useState<PendingRefund | null>(null);
  const [busy, setBusy] = useState(false);
  // 结果不确定时保留完整请求；关闭确认或切换客户不能把旧请求改成新扣减。
  const retries = useRef(new Map<string, RefundAttempt>());
  const inFlight = useRef(false);
  const currentUserId = useRef(userId);
  const mounted = useRef(true);
  currentUserId.current = userId;

  useLayoutEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  useLayoutEffect(() => {
    const unresolved = retries.current.get(userId)?.request;
    setCredits(unresolved ? String(unresolved.credits) : "");
    setSourceType(unresolved?.sourceType ?? "REFUND_APPROVAL");
    setSourceRef(unresolved?.sourceRef ?? "");
    setOriginalRechargeOrderNo(unresolved?.originalRechargeOrderNo ?? "");
    setReason(unresolved?.reason ?? "");
    setPending(null);
    setFormError("");
    setDialogError("");
    setNotice("");
  }, [userId]);

  if (readOnly) {
    return (
      <section aria-label="退款扣减" className="customer-detail-section">
        <h3>退款扣减</h3>
        <p className="admin-hint">审计员仅可查看，不能扣减积分。</p>
      </section>
    );
  }

  const unresolved = retries.current.has(userId);
  const frozen = busy || pending !== null || unresolved;

  function requestRefund(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (readOnly || inFlight.current || pending) return;
    const previous = retries.current.get(userId);
    if (previous) {
      setPending(previous.request);
      setDialogError("原扣减结果未确认，请先核对积分流水；重试将复用原请求。");
      return;
    }
    const amount = Number(credits);
    if (!Number.isSafeInteger(amount) || amount <= 0) {
      setFormError("扣减积分必须是大于 0 的整数");
      return;
    }
    if (amount > availableCredits) {
      setFormError(
        `超过当前可用余额，整体最多可扣减 ${availableCredits} 积分；原充值可退积分可能更少`,
      );
      return;
    }
    if (!originalRechargeOrderNo.trim()) {
      setFormError("请填写原充值订单号，用于核对该笔已付款充值的未用积分");
      return;
    }
    if (!sourceRef.trim()) {
      setFormError("请填写审批单号，用于和线下退款记录对齐");
      return;
    }
    if (!reason.trim()) {
      setFormError("请填写原因");
      return;
    }
    setFormError("");
    setDialogError("");
    setNotice("");
    setPending({
      userId,
      credits: amount,
      sourceType,
      sourceRef: sourceRef.trim(),
      originalRechargeOrderNo: originalRechargeOrderNo.trim(),
      reason: reason.trim(),
    });
  }

  async function submitRefund() {
    if (!pending || inFlight.current || readOnly || pending.userId !== userId) {
      return;
    }
    const request = pending;
    const previous = retries.current.get(request.userId);
    if (
      previous &&
      JSON.stringify(previous.request) !== JSON.stringify(request)
    ) {
      setDialogError(
        "原扣减结果未确认，不能修改原充值订单或其他扣减资料，请人工核对。",
      );
      return;
    }
    const attempt = previous ?? { request, key: crypto.randomUUID() };
    retries.current.set(request.userId, attempt);
    // state 更新前也要互斥，防止同一渲染周期重复确认。
    inFlight.current = true;
    setBusy(true);
    setDialogError("");
    const isCurrentCustomer = () =>
      mounted.current && currentUserId.current === request.userId;
    try {
      const result = await createCustomerAdjustment(
        request.userId,
        {
          sourceDocumentType: request.sourceType,
          sourceDocumentRef: request.sourceRef,
          originalRechargeOrderNo: request.originalRechargeOrderNo,
          credits: -request.credits,
        },
        request.reason,
        attempt.key,
      );
      if (
        result.original_recharge_order_no != null &&
        result.original_recharge_order_no !== request.originalRechargeOrderNo
      ) {
        throw new Error("返回的原充值订单号不一致，请人工核对");
      }
      retries.current.delete(request.userId);
      if (!isCurrentCustomer()) return;
      setNotice(
        `已从原充值订单 ${request.originalRechargeOrderNo} 扣减 ${request.credits} 积分，余额 ${result.wallet_balance_after} 积分。实际退款请线下办理，并用审批单号 ${request.sourceRef} 对齐。`,
      );
      setCredits("");
      setSourceRef("");
      setOriginalRechargeOrderNo("");
      setReason("");
      setPending(null);
      onRefunded(result);
    } catch (cause) {
      const message =
        cause instanceof Error && cause.message.trim()
          ? cause.message
          : "退款扣减失败";
      const deterministic =
        cause instanceof AdminActivationError &&
        cause.status !== undefined &&
        cause.status >= 400 &&
        cause.status < 500 &&
        ![408, 409, 429].includes(cause.status);
      if (deterministic) retries.current.delete(request.userId);
      if (!isCurrentCustomer()) return;
      if (deterministic) {
        setPending(null);
        setFormError(message);
      } else {
        setDialogError(
          `${message}。结果未确认：请先核对积分流水，再决定是否重试（重试将复用同一请求和幂等键）。`,
        );
      }
    } finally {
      inFlight.current = false;
      if (mounted.current) setBusy(false);
    }
  }

  return (
    <section
      aria-label="退款扣减"
      className="customer-detail-section"
      id="customer-refund"
    >
      <h3>退款扣减</h3>
      <p className="admin-hint">
        钱包可用余额仅是整体上限（当前 {availableCredits}{" "}
        积分），原充值可退积分由服务端核对。
        仅扣回对应已付款原充值中尚未使用的积分，赠送、预留、已消费及其他订单积分不能扣回。
        来源无法核对或原单余额不足时暂停扣减，交人工核对；含赠送的混合套餐也需人工核对。
        系统只记账本，实际退款请线下办理。
      </p>
      {notice ? <PageBanner tone="notice">{notice}</PageBanner> : null}
      {formError ? <PageBanner tone="error">{formError}</PageBanner> : null}
      {unresolved && !busy && !pending ? (
        <PageBanner tone="error">
          该客户有一笔扣减结果未确认，资料已锁定。请先核对积分流水，再提交原请求重试；需要更改时请先人工核对。
        </PageBanner>
      ) : null}
      {/* noValidate：超额、缺单号都由下方中文提示说明原因，不交给浏览器气泡。 */}
      <form className="admin-form" noValidate onSubmit={requestRefund}>
        <label>
          依据
          <select
            disabled={frozen}
            value={sourceType}
            onChange={(event) => setSourceType(event.target.value)}
          >
            {REFUND_SOURCES.map((value) => (
              <option key={value} value={value}>
                {labelFrom(ADJUSTMENT_SOURCE_LABELS, value)}
              </option>
            ))}
          </select>
        </label>
        <label>
          原充值订单号
          <input
            disabled={frozen}
            placeholder="填写充值订单列表中的订单号"
            value={originalRechargeOrderNo}
            onChange={(event) => setOriginalRechargeOrderNo(event.target.value)}
          />
        </label>
        <label>
          扣减积分
          <input
            disabled={frozen}
            max={availableCredits}
            min={1}
            step={1}
            type="number"
            value={credits}
            onChange={(event) => setCredits(event.target.value)}
          />
        </label>
        <label>
          审批单号
          <input
            disabled={frozen}
            placeholder="例如：RF-0928-01"
            value={sourceRef}
            onChange={(event) => setSourceRef(event.target.value)}
          />
        </label>
        <label>
          原因
          <input
            disabled={frozen}
            placeholder="例如：客户申请退还未用积分"
            value={reason}
            onChange={(event) => setReason(event.target.value)}
          />
        </label>
        <button disabled={busy || pending !== null} type="submit">
          提交退款扣减
        </button>
      </form>

      <ConfirmDialog
        busy={busy}
        confirmLabel="确认扣减"
        description={
          pending ? (
            <>
              即将从该客户的对应原充值中扣减 {pending.credits} 积分。
              <br />
              原充值订单号：{pending.originalRechargeOrderNo}
              <br />
              依据：{labelFrom(ADJUSTMENT_SOURCE_LABELS, pending.sourceType)}（
              {pending.sourceRef}）
              <br />
              原因：{pending.reason}
              <br />
              仅限原充值未用积分；服务端无法核对来源或原单余额不足时将拒绝扣减，需人工核对。
            </>
          ) : null
        }
        error={dialogError}
        level="standard"
        open={pending !== null && pending.userId === userId}
        title="确认退款扣减"
        onClose={() => {
          if (inFlight.current) return;
          setPending(null);
          setDialogError("");
        }}
        onConfirm={() => void submitRefund()}
      />
    </section>
  );
}
