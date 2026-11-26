import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  getSoftwareLicenses,
  issueSoftwareLicense,
  revealSoftwareLicense,
  updateSoftwareActivationPolicy,
} from "../api.admin";
import { SoftwareLicensesPage } from "./SoftwareLicensesPage";

vi.mock("../api.admin", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api.admin")>()),
  getSoftwareLicenses: vi.fn(),
  issueSoftwareLicense: vi.fn(),
  revealSoftwareLicense: vi.fn(),
  updateSoftwareActivationPolicy: vi.fn(),
}));

describe("软件授权管理", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(getSoftwareLicenses).mockResolvedValue({
      items: [],
      total: 0,
      auto_extend: false,
    });
  });

  it("发码时分别提交授权年限和北京时间对应的激活截止", async () => {
    vi.mocked(issueSoftwareLicense).mockResolvedValue({ id: "license-1" });
    render(<SoftwareLicensesPage readOnly={false} />);
    await screen.findByText("暂无软件授权");
    fireEvent.click(screen.getByRole("button", { name: "生成激活码" }));
    const dialog = within(screen.getByRole("dialog"));
    fireEvent.change(dialog.getByLabelText("软件授权年限"), {
      target: { value: "2" },
    });
    fireEvent.change(dialog.getByLabelText("激活码截止时间（北京时间）"), {
      target: { value: "2030-04-05T12:30" },
    });
    expect(dialog.queryByLabelText("操作原因")).not.toBeInTheDocument();
    fireEvent.click(dialog.getByRole("button", { name: "生成激活码" }));
    await waitFor(() =>
      expect(issueSoftwareLicense).toHaveBeenCalledWith(
        {
          authorization_years: 2,
          activation_expires_at: "2030-04-05T04:30:00.000Z",
        },
        expect.any(String),
      ),
    );
  });

  it("一键复制软件激活码，不弹出确认表单", async () => {
    const sampleCode = "example-code-for-test";
    const writeText = vi.fn().mockResolvedValue(undefined);
    const previousClipboard = Object.getOwnPropertyDescriptor(
      navigator,
      "clipboard",
    );
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText },
    });
    vi.mocked(getSoftwareLicenses).mockResolvedValue({
      items: [
        {
          id: "license-1",
          masked_code: "example-****",
          activation_expires_at: "2030-04-05T04:30:00.000Z",
          authorization_years: 1,
          username: null,
          authorized_until: null,
          expired: false,
          can_reveal: true,
        },
      ],
      total: 1,
      auto_extend: false,
    });
    vi.mocked(revealSoftwareLicense).mockResolvedValue({
      activation_code: sampleCode,
    });
    try {
      render(<SoftwareLicensesPage readOnly={false} />);
      fireEvent.click(await screen.findByRole("button", { name: "复制" }));
      expect(await screen.findByText("激活码已复制。")).toBeVisible();
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
      expect(revealSoftwareLicense).toHaveBeenCalledExactlyOnceWith(
        "license-1",
        expect.any(String),
      );
      expect(writeText).toHaveBeenCalledExactlyOnceWith(sampleCode);
    } finally {
      if (previousClipboard) {
        Object.defineProperty(navigator, "clipboard", previousClipboard);
      } else {
        Reflect.deleteProperty(navigator, "clipboard");
      }
    }
  });

  it("开启自动延期后回显服务端保存的开关状态", async () => {
    vi.mocked(updateSoftwareActivationPolicy).mockImplementation(async () => {
      vi.mocked(getSoftwareLicenses).mockResolvedValue({
        items: [],
        total: 0,
        auto_extend: true,
      });
      return { auto_extend: true };
    });
    render(<SoftwareLicensesPage readOnly={false} />);
    const toggle = screen.getByRole("switch", { name: "自动延长未使用激活码" });
    await waitFor(() => expect(toggle).toBeEnabled());
    expect(toggle).not.toBeChecked();
    fireEvent.click(toggle);
    const dialog = within(screen.getByRole("dialog"));
    fireEvent.change(dialog.getByRole("textbox"), {
      target: { value: "延长未使用码的交付窗口" },
    });
    fireEvent.click(dialog.getByRole("button", { name: "确认执行" }));
    await waitFor(() => expect(toggle).toBeChecked());
    expect(updateSoftwareActivationPolicy).toHaveBeenCalledWith(
      true,
      "延长未使用码的交付窗口",
      expect.any(String),
    );
  });
});
