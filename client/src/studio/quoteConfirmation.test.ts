import { afterEach, describe, expect, it, vi } from "vitest";
import {
  isCurrentQuote,
  isQuoteRejection,
  watchQuoteExpiry,
} from "./quoteConfirmation";

const quote = {
  quote_token: "fake-q",
  quote_expires_at: 2000,
  max_credits: 12,
  estimated_credits: 12,
};

describe("报价确认边界", () => {
  it("只接受有效期内且与显示金额相同的合同", () => {
    expect(isCurrentQuote(quote, 1_000_000)).toBe(true);
    expect(
      isCurrentQuote(
        { ...quote, max_credits: 0, estimated_credits: 0 },
        1_000_000,
      ),
    ).toBe(true);
  });

  it.each([
    { quote_token: "" },
    { quote_expires_at: 1000 },
    { quote_expires_at: Number.NaN },
    { max_credits: -1 },
    { max_credits: 12.1 },
    { max_credits: 13 },
    { estimated_credits: undefined },
  ])("拒绝失效、缺失或金额不一致的报价 %j", (change) => {
    expect(isCurrentQuote({ ...quote, ...change }, 1_000_000)).toBe(false);
  });

  it("旧价格响应不能误当成已验证合同", () => {
    expect(isCurrentQuote(null)).toBe(false);
    expect(isCurrentQuote({ estimated_credits: 12 })).toBe(false);
  });

  it("明确报价拒绝可重新报价，网络未知结果保留原幂等键", () => {
    expect(
      isQuoteRejection(
        Object.assign(new Error("changed"), { code: "QUOTE_CHANGED" }),
      ),
    ).toBe(true);
    expect(isQuoteRejection(new Error("network"))).toBe(false);
    expect(isQuoteRejection({ code: "QUOTE_CHANGED" })).toBe(false);
  });
});

describe("报价过期提示", () => {
  afterEach(() => vi.useRealTimers());

  it("到期显示重新确认入口，不触发自动提交", () => {
    vi.useFakeTimers();
    vi.setSystemTime(1_999_000);
    const expire = vi.fn();
    watchQuoteExpiry(quote, expire);
    vi.advanceTimersByTime(999);
    expect(expire).not.toHaveBeenCalled();
    vi.advanceTimersByTime(1);
    expect(expire).toHaveBeenCalledOnce();
  });

  it("取消弹窗或替换报价时取消旧定时器", () => {
    vi.useFakeTimers();
    vi.setSystemTime(1_999_000);
    const expire = vi.fn();
    const cancel = watchQuoteExpiry(quote, expire);
    cancel();
    vi.advanceTimersByTime(10_000);
    expect(expire).not.toHaveBeenCalled();
  });

  it("缺少报价时不留下定时器", () => {
    vi.useFakeTimers();
    const expire = vi.fn();
    watchQuoteExpiry(null, expire)();
    expect(vi.getTimerCount()).toBe(0);
  });
});
