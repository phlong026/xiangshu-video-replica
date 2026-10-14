import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { ViralCollectionPage } from "./ViralCollectionPage";

vi.mock("./ViralRuntimeSection", () => ({
  ViralRuntimeSection: () => <div data-testid="runtime">计划与预算内容</div>,
}));
vi.mock("./ViralKeywordsManager", () => ({
  ViralKeywordsManager: () => <div data-testid="keywords">关键词内容</div>,
}));
vi.mock("./ViralPlatformProbes", () => ({
  ViralPlatformProbes: () => <div data-testid="probes">平台状态内容</div>,
}));
vi.mock("./ViralCollectionRecords", () => ({
  ViralCollectionRecords: ({ initialBatchId }: { initialBatchId?: string }) => (
    <div data-testid="records">{`采集记录 ${initialBatchId ?? ""}`}</div>
  ),
}));

describe("ViralCollectionPage", () => {
  it("默认落在计划与预算；关键词、平台与记录各占一个页签，采集记录不与设置混排", () => {
    render(<ViralCollectionPage />);
    expect(screen.getByTestId("runtime")).toBeInTheDocument();
    expect(screen.queryByTestId("keywords")).toBeNull();
    expect(screen.queryByTestId("records")).toBeNull();

    fireEvent.click(screen.getByRole("tab", { name: "关键词" }));
    expect(screen.getByTestId("keywords")).toBeInTheDocument();
    expect(screen.queryByTestId("runtime")).toBeNull();
    expect(window.location.hash).toContain("sub=keywords");

    fireEvent.click(screen.getByRole("tab", { name: "平台与记录" }));
    expect(screen.getByTestId("probes")).toBeInTheDocument();
    expect(screen.getByTestId("records")).toBeInTheDocument();
  });

  it("带批次号进来直接落在平台与记录", () => {
    render(<ViralCollectionPage initialBatchId="batch-7" />);
    expect(screen.getByTestId("records")).toHaveTextContent("batch-7");
    expect(screen.queryByTestId("runtime")).toBeNull();
  });
});
