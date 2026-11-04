import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { LeaseCountdown } from "./LeaseCountdown";

describe("LeaseCountdown（7 天滑动会话有效期展示）", () => {
  const mockExpiresAt = new Date(
    Date.parse("2026-08-24T12:00:00Z") + 6 * 24 * 3600_000,
  ).toISOString(); // 登录快照：6 天后到期

  it("displays formatted expiration timestamp", () => {
    render(<LeaseCountdown expiresAt={mockExpiresAt} />);
    expect(screen.getByText(/会话有效期至/)).toBeInTheDocument();
  });

  it("explains server-side sliding renewal instead of a ticking countdown", () => {
    render(<LeaseCountdown expiresAt={mockExpiresAt} />);
    // r4 评审 P2：快照跨过自身后 UI 不得断言「已过期」——服务端持续滑动
    // 续期，展示改为静态说明（剩余时长/告警/过期三态全部移除）。
    expect(
      screen.getByText(/持续使用自动续期；连续 7 天不使用后才需要重新登录/),
    ).toBeInTheDocument();
    expect(screen.queryByText(/已过期/)).not.toBeInTheDocument();
    expect(screen.queryByText(/即将过期/)).not.toBeInTheDocument();
    expect(screen.queryByText(/剩余 \d+ 天/)).not.toBeInTheDocument();
  });

  it("stays a plain info card even when the login snapshot has passed", () => {
    // 快照已跨过：这正是 r4 发现的误导场景——组件必须保持说明形态。
    const staleSnapshot = new Date(
      Date.parse("2026-08-24T12:00:00Z") - 1000,
    ).toISOString();
    render(<LeaseCountdown expiresAt={staleSnapshot} />);
    expect(screen.queryByText(/已过期/)).not.toBeInTheDocument();
    expect(screen.getByText(/会话有效期至/)).toBeInTheDocument();
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
