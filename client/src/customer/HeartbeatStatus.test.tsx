import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { HeartbeatStatus } from "./HeartbeatStatus";

describe("HeartbeatStatus (FE-04 / T31)", () => {
  const mockLastHeartbeat = new Date(Date.now() - 15_000).toISOString(); // 15 秒前

  it("displays formatted heartbeat timestamp", () => {
    const mockOnRefresh = vi.fn();
    render(
      <HeartbeatStatus
        lastHeartbeatAt={mockLastHeartbeat}
        onRefresh={mockOnRefresh}
      />,
    );

    expect(screen.getByText(/last heartbeat/i)).toBeInTheDocument();
  });

  it("shows connection healthy status when within 80% interval", () => {
    const mockOnRefresh = vi.fn();
    render(
      <HeartbeatStatus
        lastHeartbeatAt={mockLastHeartbeat}
        intervalSeconds={30}
        onRefresh={mockOnRefresh}
      />,
    );

    expect(screen.getByText(/connection healthy/i)).toBeInTheDocument();
  });

  it("shows warning when heartbeat is overdue (> interval)", () => {
    const mockOverdue = new Date(Date.now() - 40_000).toISOString(); // 40 秒前
    const mockOnRefresh = vi.fn();

    render(
      <HeartbeatStatus
        lastHeartbeatAt={mockOverdue}
        intervalSeconds={30}
        onRefresh={mockOnRefresh}
      />,
    );

    expect(screen.getByText(/overdue/i)).toBeInTheDocument();
  });

  it("shows expired when heartbeat is > 2x interval", () => {
    const mockExpired = new Date(Date.now() - 70_000).toISOString(); // 70 秒前
    const mockOnRefresh = vi.fn();

    render(
      <HeartbeatStatus
        lastHeartbeatAt={mockExpired}
        intervalSeconds={30}
        onRefresh={mockOnRefresh}
      />,
    );

    expect(screen.getByText(/expired/i)).toBeInTheDocument();
  });

  it("provides refresh button that calls onRefresh callback", () => {
    const mockOnRefresh = vi.fn();
    render(
      <HeartbeatStatus
        lastHeartbeatAt={mockLastHeartbeat}
        onRefresh={mockOnRefresh}
      />,
    );

    const button = screen.getByLabelText(
      /Manually trigger a heartbeat refresh/i,
    );
    button.click();
    expect(mockOnRefresh).toHaveBeenCalledTimes(1);
  });

  it("does not display any plaintext tokens or secrets in the UI", () => {
    const mockOnRefresh = vi.fn();
    render(
      <HeartbeatStatus
        lastHeartbeatAt={mockLastHeartbeat}
        onRefresh={mockOnRefresh}
      />,
    );

    expect(screen.queryByLabelText(/session-token/i)).not.toBeInTheDocument();
    expect(screen.queryByLabelText(/device-token/i)).not.toBeInTheDocument();
  });
});
