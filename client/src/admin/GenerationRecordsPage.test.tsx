import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import * as adminApi from "../api.admin";
import { GenerationRecordsPage } from "./GenerationRecordsPage";

vi.mock("../api.admin", () => ({
  getAdminGenerationRecords: vi.fn(),
}));

describe("GenerationRecordsPage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(adminApi.getAdminGenerationRecords).mockResolvedValue({
      items: [
        {
          record_id: "video-1",
          record_type: "VIDEO",
          operation: "I2V",
          user_id: "user-1",
          username: "customer-1",
          display_name: "客户一",
          project_id: "project-1",
          project_name: "演示项目",
          status: "RUNNING",
          provider: "minimax",
          model: "Hailuo-02",
          provider_cost: 1.25,
          provider_cost_status: "ESTIMATED",
          record_data_status: "VALID",
          charged_credits: 0,
          result_reference: null,
          provider_reference: null,
          error_code: null,
          error_message: null,
          created_at: "2026-09-02T11:00:00Z",
          completed_at: null,
        },
        {
          record_id: "first-frame-1",
          record_type: "FIRST_FRAME_IMAGE",
          operation: "GENERATE",
          user_id: "user-1",
          username: "customer-1",
          display_name: "客户一",
          project_id: "project-1",
          project_name: "演示项目",
          status: "SUCCEEDED",
          provider: "apilio",
          model: "gpt-image-2",
          provider_cost: null,
          provider_cost_status: "UNAVAILABLE",
          record_data_status: "VALID",
          charged_credits: 0,
          result_reference: "version-1",
          provider_reference: null,
          error_code: null,
          error_message: null,
          created_at: "2026-09-02T10:00:00Z",
          completed_at: "2026-09-02T10:01:00Z",
        },
        {
          record_id: "source-score-1",
          record_type: "SOURCE_FRAME_AI_SCORE",
          operation: "SCORE_CANDIDATES",
          user_id: "user-1",
          username: "customer-1",
          display_name: "客户一",
          project_id: "project-1",
          project_name: "演示项目",
          status: "SUCCEEDED",
          provider: "apilio_gemini",
          model: "gemini-2.5-flash",
          provider_cost: null,
          provider_cost_status: "UNAVAILABLE",
          record_data_status: "VALID",
          charged_credits: 0,
          result_reference: "version-2",
          provider_reference: null,
          error_code: null,
          error_message: null,
          created_at: "2026-09-02T09:00:00Z",
          completed_at: "2026-09-02T09:01:00Z",
        },
      ],
      total: 3,
      limit: 50,
      offset: 0,
    });
  });

  it("shows image generation and AI scoring with honest cost status", async () => {
    render(<GenerationRecordsPage />);

    expect(await screen.findByText("人物置换首帧")).toBeInTheDocument();
    expect(screen.getByText("源画面 AI 评分")).toBeInTheDocument();
    expect(screen.getAllByText("上游未回传")).toHaveLength(2);
    expect(screen.getByText("估算 1.25")).toBeInTheDocument();
    expect(screen.getByText("apilio / gpt-image-2")).toBeInTheDocument();
    expect(
      screen.getByText("apilio_gemini / gemini-2.5-flash"),
    ).toBeInTheDocument();
    expect(screen.getAllByText("customer-1")).toHaveLength(3);
  });

  it("reloads the current page on demand", async () => {
    render(<GenerationRecordsPage />);
    await screen.findByText("人物置换首帧");

    fireEvent.click(screen.getByRole("button", { name: "刷新记录" }));

    await waitFor(() => {
      expect(adminApi.getAdminGenerationRecords).toHaveBeenCalledTimes(2);
    });
  });

  it("opens failed records with the filter already applied", async () => {
    render(<GenerationRecordsPage initialStatus="FAILED" />);

    await waitFor(() => {
      expect(adminApi.getAdminGenerationRecords).toHaveBeenCalledWith(
        expect.objectContaining({ status: "FAILED" }),
      );
    });
    expect(screen.getByLabelText("生成状态")).toHaveValue("FAILED");
  });

  it("shows read-only oral failure details without a retry action", async () => {
    vi.mocked(adminApi.getAdminGenerationRecords).mockResolvedValue({
      items: [
        {
          record_id: "oral-1",
          record_type: "ORAL_VIDEO",
          operation: "TTS",
          user_id: "user-1",
          username: "customer-1",
          display_name: "客户一",
          project_id: null,
          project_name: null,
          status: "FAILED",
          provider: "hifly",
          model: null,
          provider_cost: null,
          provider_cost_status: "UNAVAILABLE",
          record_data_status: "VALID",
          charged_credits: 12,
          result_reference: null,
          provider_reference: "hifly-task-1",
          error_code: "ORAL_TASK_FAILED",
          error_message: "数字人服务生成失败",
          created_at: "2026-09-02T11:00:00Z",
          completed_at: "2026-09-02T11:01:00Z",
        },
      ],
      total: 1,
      limit: 50,
      offset: 0,
    });

    render(<GenerationRecordsPage initialStatus="FAILED" />);

    expect(await screen.findByText("口播视频")).toBeInTheDocument();
    fireEvent.click(screen.getByText("查看详情"));
    expect(screen.getByText("数字人服务生成失败")).toBeInTheDocument();
    expect(screen.getByText("hifly-task-1")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /重试/ })).toBeNull();
  });

  it("submits draft filters once instead of loading while typing", async () => {
    render(<GenerationRecordsPage />);
    await screen.findByText("人物置换首帧");

    fireEvent.change(screen.getByLabelText("生成账号"), {
      target: { value: "customer-2" },
    });
    fireEvent.change(screen.getByLabelText("生成状态"), {
      target: { value: "SUCCEEDED" },
    });
    await Promise.resolve();
    expect(adminApi.getAdminGenerationRecords).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByRole("button", { name: "查询" }));

    await waitFor(() => {
      expect(adminApi.getAdminGenerationRecords).toHaveBeenCalledTimes(2);
    });
    expect(adminApi.getAdminGenerationRecords).toHaveBeenLastCalledWith(
      expect.objectContaining({
        offset: 0,
        status: "SUCCEEDED",
        username: "customer-2",
      }),
    );
  });

  it("ignores an older response after a newer refresh finishes", async () => {
    let resolveFirst:
      | ((value: adminApi.AdminGenerationRecordPage) => void)
      | undefined;
    vi.mocked(adminApi.getAdminGenerationRecords)
      .mockReturnValueOnce(
        new Promise((resolve) => {
          resolveFirst = resolve;
        }),
      )
      .mockResolvedValueOnce({
        items: [],
        total: 0,
        limit: 50,
        offset: 0,
      });

    render(<GenerationRecordsPage />);
    fireEvent.click(await screen.findByRole("button", { name: "刷新中…" }));
    await waitFor(() =>
      expect(screen.getByText("暂无生成记录。")).toBeInTheDocument(),
    );

    resolveFirst?.({
      items: [
        {
          record_id: "stale-record",
          record_type: "VIDEO",
          operation: "I2V",
          user_id: "user-1",
          username: "stale-user",
          display_name: "旧数据",
          project_id: null,
          project_name: null,
          status: "SUCCEEDED",
          provider: "stale-provider",
          model: "stale-model",
          provider_cost: 1,
          provider_cost_status: "KNOWN",
          record_data_status: "VALID",
          charged_credits: 1,
          result_reference: null,
          provider_reference: null,
          error_code: null,
          error_message: null,
          created_at: "2026-09-01T00:00:00Z",
          completed_at: null,
        },
      ],
      total: 1,
      limit: 50,
      offset: 0,
    });

    await waitFor(() => expect(screen.queryByText("stale-user")).toBeNull());
  });
});
