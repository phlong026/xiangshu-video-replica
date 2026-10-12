import { afterEach, expect, test, vi } from "vitest";

import { clearAdminCsrfToken, setAdminCsrfToken } from "./api";
import { createCustomerAdjustment } from "./api.admin";

const proof = {
  original_recharge_order_no: "PAY-001",
  original_recharge_order_id: "recharge-001",
  credit_lot_id: "lot-001",
  credits: 30,
  lot_remaining_before: 100,
  lot_remaining_after: 70,
};
const response = {
  original_recharge_order_no: "PAY-001",
  refund_funding: proof,
};
const refund = {
  sourceDocumentType: "REFUND_APPROVAL",
  sourceDocumentRef: "RF-001",
  originalRechargeOrderNo: "PAY-001",
  credits: -30,
};

function stubResponse(value: unknown, status = 200) {
  setAdminCsrfToken("test-admin-csrf");
  return vi
    .spyOn(globalThis, "fetch")
    .mockResolvedValue(new Response(JSON.stringify(value), { status }));
}

afterEach(() => {
  clearAdminCsrfToken();
});

test.each(["REFUND_APPROVAL", "LEDGER_CORRECTION"])(
  "%s 负数请求只走来源绑定端点，分别传递原充值与审批单且保留幂等键",
  async (sourceDocumentType) => {
    const fetchRequest = stubResponse(response);
    const result = await createCustomerAdjustment(
      "user-1",
      { ...refund, sourceDocumentType },
      "客户退款",
      "refund-stable-key",
    );
    expect(fetchRequest.mock.calls[0][0]).toBe(
      "http://127.0.0.1:8000/api/control/customers/user-1/source-refunds",
    );
    const request = fetchRequest.mock.calls[0][1];
    expect(JSON.parse(String(request?.body))).toEqual({
      source_document_type: sourceDocumentType,
      source_document_ref: "RF-001",
      original_recharge_order_no: "PAY-001",
      credits: -30,
      confirm: true,
      reason: "客户退款",
    });
    expect(new Headers(request?.headers).get("Idempotency-Key")).toBe(
      "refund-stable-key",
    );
    expect(result.original_recharge_order_no).toBe("PAY-001");
    expect(result.refund_funding).toEqual(proof);
  },
);

test("旧正数调账继续原端点且不额外发送原充值订单字段", async () => {
  const fetchRequest = stubResponse({});
  await createCustomerAdjustment(
    "user-1",
    {
      sourceDocumentType: "FREE_GRANT",
      sourceDocumentRef: "活动审批单",
      credits: 30,
    },
    "赠送积分",
    "legacy-stable-key",
  );
  expect(fetchRequest.mock.calls[0][0]).toBe(
    "http://127.0.0.1:8000/api/control/customers/user-1/adjustments",
  );
  expect(
    JSON.parse(String(fetchRequest.mock.calls[0][1]?.body)),
  ).not.toHaveProperty("original_recharge_order_no");
});

test("旧服务端没有来源端点时直接失败，不能回退到旧扣减接口", async () => {
  const fetchRequest = stubResponse({ detail: "Not Found" }, 404);
  await expect(
    createCustomerAdjustment("user-1", refund, "客户退款", "key-404"),
  ).rejects.toMatchObject({ status: 404 });
  expect(fetchRequest).toHaveBeenCalledTimes(1);
  expect(String(fetchRequest.mock.calls[0][0])).toMatch(/\/source-refunds$/);
});

test.each([
  ["缺少全部证明", {}],
  ["回包为空", null],
  ["只有原单号没有分配证明", { original_recharge_order_no: "PAY-001" }],
  ["顶层原单号不符", { ...response, original_recharge_order_no: "PAY-OTHER" }],
  [
    "证明原单号不符",
    {
      ...response,
      refund_funding: { ...proof, original_recharge_order_no: "PAY-OTHER" },
    },
  ],
  [
    "缺少充值 ID",
    {
      ...response,
      refund_funding: { ...proof, original_recharge_order_id: " " },
    },
  ],
  [
    "缺少来源批次",
    { ...response, refund_funding: { ...proof, credit_lot_id: "" } },
  ],
  ["扣减数量不符", { ...response, refund_funding: { ...proof, credits: 29 } }],
  [
    "扣减数量不是整数",
    { ...response, refund_funding: { ...proof, credits: "30" } },
  ],
  [
    "来源余额差额不符",
    { ...response, refund_funding: { ...proof, lot_remaining_after: 71 } },
  ],
  [
    "来源变为负余额",
    {
      ...response,
      refund_funding: {
        ...proof,
        lot_remaining_before: 20,
        lot_remaining_after: -10,
      },
    },
  ],
  [
    "来源余额不是安全整数",
    {
      ...response,
      refund_funding: {
        ...proof,
        lot_remaining_before: Number.MAX_SAFE_INTEGER + 1,
        lot_remaining_after: Number.MAX_SAFE_INTEGER - 29,
      },
    },
  ],
])("%s：不报告成功，保留不确定结果交人工核对", async (_name, body) => {
  const fetchRequest = stubResponse(body);
  await expect(
    createCustomerAdjustment("user-1", refund, "客户退款", "unchanged-key"),
  ).rejects.toMatchObject({
    code: "REFUND_RESPONSE_UNVERIFIED",
    status: undefined,
    message: expect.stringMatching(/结果未确认.*人工核对/),
  });
  expect(fetchRequest).toHaveBeenCalledTimes(1);
});

test("原充值来源校验 422 保留服务端明确拒绝代码", async () => {
  stubResponse(
    {
      detail: {
        code: "REFUND_SOURCE_BALANCE_INSUFFICIENT",
        message: "原充值可退积分不足，请人工核对",
      },
    },
    422,
  );
  await expect(
    createCustomerAdjustment("user-1", refund, "客户退款", "rejected-key"),
  ).rejects.toMatchObject({
    status: 422,
    code: "REFUND_SOURCE_BALANCE_INSUFFICIENT",
    message: expect.stringMatching(/原充值可退积分不足/),
  });
});
