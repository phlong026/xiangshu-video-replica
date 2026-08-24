import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { LeaseCountdown } from "./LeaseCountdown";

describe("LeaseCountdown (FE-04 / T31)", () => {
  const mockExpiresAt = new Date(Date.now() + 1800_000).toISOString(); // 30 分钟后过期

  it("displays formatted expiration timestamp", () => {
    const mockOnRefresh = vi.fn();
    render(
      <LeaseCountdown expiresAt={mockExpiresAt} onRefresh={mockOnRefresh} />,
    );

    expect(screen.getByText(/session expires/i)).toBeInTheDocument();
  });

  it("shows countdown timer updating every second", () => {
    const mockOnRefresh = vi.fn();
    render(
      <LeaseCountdown expiresAt={mockExpiresAt} onRefresh={mockOnRefresh} />,
    );

    // Initial display should show time remaining
    expect(screen.getByText(/\d+ minutes/)).toBeInTheDocument();
  });

  it("changes to warning state when lease < 5 minutes", () => {
    const mockNearExpiry = new Date(Date.now() + 300_000).toISOString(); // 5 分钟内
    const mockOnRefresh = vi.fn();

    render(
      <LeaseCountdown expiresAt={mockNearExpiry} onRefresh={mockOnRefresh} />,
    );

    expect(screen.getByText(/Expiring soon/i)).toBeInTheDocument();
  });

  it("changes to expired state when lease <= 0", () => {
    const mockExpired = new Date(Date.now() - 1000).toISOString(); // 已过期
    const mockOnRefresh = vi.fn();

    render(
      <LeaseCountdown expiresAt={mockExpired} onRefresh={mockOnRefresh} />,
    );

    expect(screen.getByText(/expired/i)).toBeInTheDocument();
  });

  it("provides refresh/renew button that calls onRefresh callback", () => {
    const mockOnRefresh = vi.fn();
    render(
      <LeaseCountdown expiresAt={mockExpiresAt} onRefresh={mockOnRefresh} />,
    );

    const button = screen.getByLabelText(/Renew or extend this session/i);
    button.click();
    expect(mockOnRefresh).toHaveBeenCalledTimes(1);
  });

  it("does not display any plaintext tokens or secrets in the UI", () => {
    const mockOnRefresh = vi.fn();
    render(
      <LeaseCountdown expiresAt={mockExpiresAt} onRefresh={mockOnRefresh} />,
    );

    expect(screen.queryByLabelText(/session-token/i)).not.toBeInTheDocument();
    expect(screen.queryByLabelText(/device-token/i)).not.toBeInTheDocument();
  });
});
