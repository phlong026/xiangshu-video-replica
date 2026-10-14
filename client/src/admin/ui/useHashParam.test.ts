import { act, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { useHashParam, writeHashParams } from "./useHashParam";

const TABS = ["overview", "orders", "ledger"] as const;

afterEach(() => window.history.replaceState(null, "", "/"));

describe("useHashParam", () => {
  it("从 URL 恢复合法值，脏值退回默认", () => {
    window.history.replaceState(null, "", "/admin#admin/funds?sub=orders");
    const { result } = renderHook(() => useHashParam("sub", "overview", TABS));
    expect(result.current[0]).toBe("orders");

    window.history.replaceState(null, "", "/admin#admin/funds?sub=hack");
    const dirty = renderHook(() => useHashParam("sub", "overview", TABS));
    expect(dirty.result.current[0]).toBe("overview");
  });

  it("外部意图优先于 URL", () => {
    window.history.replaceState(null, "", "/admin#admin/funds?sub=orders");
    const { result } = renderHook(() =>
      useHashParam("sub", "overview", TABS, "ledger"),
    );
    expect(result.current[0]).toBe("ledger");
  });

  it("切换写入 URL、保留其它参数、去掉意图；默认值不留参数", () => {
    window.history.replaceState(
      null,
      "",
      "/admin#admin/funds?intent=recon&userId=u1",
    );
    const { result } = renderHook(() => useHashParam("sub", "overview", TABS));
    act(() => result.current[1]("orders"));
    expect(result.current[0]).toBe("orders");
    expect(window.location.hash).toBe("#admin/funds?userId=u1&sub=orders");
    act(() => result.current[1]("overview"));
    expect(window.location.hash).toBe("#admin/funds?userId=u1");
  });

  it("writeHashParams 可保留意图", () => {
    window.history.replaceState(null, "", "/admin#admin/funds?intent=recon");
    writeHashParams({ month: "2026-10" }, { dropIntent: false });
    expect(window.location.hash).toBe(
      "#admin/funds?intent=recon&month=2026-10",
    );
  });
});
