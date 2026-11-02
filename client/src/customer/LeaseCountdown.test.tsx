import { render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { LeaseCountdown } from "./LeaseCountdown";

describe("LeaseCountdown（7 天滑动会话有效期展示）", () => {
  // A fixed clock keeps the warning/expired boundaries deterministic — the
  // expiry math is relative to Date.now(), and a drifting wall clock (or a
  // shared fake-timer pool) would otherwise flake at the <1h edge.
  const FIXED_NOW = new Date("2026-08-24T12:00:00Z").getTime();
  const mockExpiresAt = new Date(FIXED_NOW + 6 * 24 * 3600_000).toISOString(); // 6 天后过期

  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(FIXED_NOW);
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("displays formatted expiration timestamp", () => {
    render(<LeaseCountdown expiresAt={mockExpiresAt} />);
    expect(screen.getByText(/本次会话有效至/)).toBeInTheDocument();
  });

  it("shows the day-scale remaining window with auto-renewal hint", () => {
    render(<LeaseCountdown expiresAt={mockExpiresAt} />);
    // 6 天剩余：天级文案 + 「使用中自动续期」说明（7 天滑动契约无手动续约）。
    expect(
      screen.getByText(/剩余 6 天 \d+ 小时，使用中自动续期/),
    ).toBeInTheDocument();
  });

  it("changes to warning state when expiry < 1 hour", () => {
    const mockNearExpiry = new Date(FIXED_NOW + 59 * 60_000).toISOString(); // 59 分钟 — 严格小于 1 小时边界
    render(<LeaseCountdown expiresAt={mockNearExpiry} />);
    expect(screen.getByText(/即将过期/)).toBeInTheDocument();
  });

  it("changes to expired state when expiry <= 0", () => {
    const mockExpired = new Date(FIXED_NOW - 1000).toISOString(); // 已过期
    render(<LeaseCountdown expiresAt={mockExpired} />);
    expect(screen.getByText(/已过期/)).toBeInTheDocument();
  });

  it("renders no manual renewal button (renewal is server-side)", () => {
    render(<LeaseCountdown expiresAt={mockExpiresAt} />);
    expect(screen.queryByLabelText(/续约会话/)).not.toBeInTheDocument();
  });

  it("does not display any plaintext tokens or secrets in the UI", () => {
    render(<LeaseCountdown expiresAt={mockExpiresAt} />);
    expect(screen.queryByLabelText(/session-token/i)).not.toBeInTheDocument();
    expect(screen.queryByLabelText(/device-token/i)).not.toBeInTheDocument();
  });
});
