import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { RESULT_URL_EXPIRED_MESSAGE } from "../api";
import { StudioResultPreview } from "./StudioResultPreview";

const api = vi.hoisted(() => ({
  batch: vi.fn(),
  direct: vi.fn(),
  archived: vi.fn(),
}));

vi.mock("../api", async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  getGenerationBatch: api.batch,
  createGenerationTaskPreviewUrl: api.direct,
  createGenerationResultPreviewUrl: api.archived,
}));

function mockDeliveredBatch(direct: boolean) {
  api.batch.mockResolvedValue({
    tasks: [
      {
        id: "delivered-task",
        status: "SUCCEEDED",
        direct_result_available: direct,
        result_asset_id: direct ? null : "result-asset",
      },
    ],
  });
}

describe("StudioResultPreview 媒体播放失败恢复", () => {
  beforeEach(() => {
    vi.resetAllMocks();
    api.direct.mockResolvedValue("https://cdn.example/first.mp4");
    api.archived.mockResolvedValue("https://cdn.example/first.mp4");
  });

  it.each(["direct", "archived"])(
    "%s 地址获取成功后媒体失败，可在当前面板重新获取并恢复播放",
    async (delivery) => {
      mockDeliveredBatch(delivery === "direct");
      render(<StudioResultPreview batchId="batch" onOpenTasks={vi.fn()} />);
      const video = await screen.findByLabelText("成片预览");
      expect(video).toHaveAttribute("src", "https://cdn.example/first.mp4");

      fireEvent.error(video);
      const retry = await screen.findByRole("button", { name: "重新获取" });
      expect(screen.getByText(/成片播放失败/)).toBeVisible();
      expect(api.batch).toHaveBeenCalledTimes(1);
      api.direct.mockResolvedValue("https://cdn.example/refreshed.mp4");
      api.archived.mockResolvedValue("https://cdn.example/refreshed.mp4");
      fireEvent.click(retry);

      await waitFor(() =>
        expect(screen.getByLabelText("成片预览")).toHaveAttribute(
          "src",
          "https://cdn.example/refreshed.mp4",
        ),
      );
      expect(screen.getByRole("button", { name: /下载成片/ })).toBeEnabled();

      // 服务端也可能返回同一个地址；显式重取必须重新加载已失败的媒体元素。
      const refreshed = screen.getByLabelText("成片预览");
      fireEvent.error(refreshed);
      fireEvent.click(await screen.findByRole("button", { name: "重新获取" }));
      await waitFor(() => {
        const recovered = screen.getByLabelText("成片预览");
        expect(recovered).not.toBe(refreshed);
        expect(recovered).toHaveAttribute(
          "src",
          "https://cdn.example/refreshed.mp4",
        );
      });
      expect(screen.queryByText(/成片播放失败/)).not.toBeInTheDocument();
    },
  );

  it("媒体失败后重新获取发现直链永久失效，展示真实失效状态", async () => {
    mockDeliveredBatch(true);
    render(<StudioResultPreview batchId="batch" onOpenTasks={vi.fn()} />);
    fireEvent.error(await screen.findByLabelText("成片预览"));
    const retry = await screen.findByRole("button", { name: "重新获取" });
    api.direct.mockRejectedValue(
      Object.assign(new Error("expired"), {
        status: 410,
        code: "RESULT_URL_EXPIRED",
      }),
    );
    fireEvent.click(retry);

    expect(await screen.findByText(RESULT_URL_EXPIRED_MESSAGE)).toBeVisible();
  });
});
