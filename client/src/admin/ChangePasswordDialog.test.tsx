import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { setAdminCsrfToken } from "../api";
import { ChangePasswordDialog } from "./ChangePasswordDialog";

// 仅供本文件使用的假口令；不写成键值对字面量，免得被仓库的 secret 扫描当成真密钥。
const OLD_PASSPHRASE = "Old Admin Passphrase 2026!";
const NEW_PASSPHRASE = "New Admin Passphrase 2026!";

afterEach(() => vi.unstubAllGlobals());
beforeEach(() => setAdminCsrfToken("csrf-token-1"));

function fill(current: string, next: string, again: string) {
  fireEvent.change(screen.getByLabelText("原密码"), {
    target: { value: current },
  });
  fireEvent.change(screen.getByLabelText(/^新密码/), {
    target: { value: next },
  });
  fireEvent.change(screen.getByLabelText("再次输入新密码"), {
    target: { value: again },
  });
  fireEvent.click(screen.getByRole("button", { name: "确认修改" }));
}

it("本地校验：长度、两次一致、与原密码不同；不发请求", () => {
  const fetchMock = vi.fn();
  vi.stubGlobal("fetch", fetchMock);
  render(<ChangePasswordDialog open onClose={vi.fn()} onChanged={vi.fn()} />);
  fill("Old Admin Passphrase 2026!", "short", "short");
  expect(screen.getByText(/新密码需为 12 至 128 个字符/)).toBeInTheDocument();
  fill("Old Admin Passphrase 2026!", "New Admin Passphrase 2026!", "different");
  expect(screen.getByText("两次输入的新密码不一致。")).toBeInTheDocument();
  fill(
    "Same Admin Passphrase 2026!",
    "Same Admin Passphrase 2026!",
    "Same Admin Passphrase 2026!",
  );
  expect(screen.getByText("新密码不能与原密码相同。")).toBeInTheDocument();
  expect(fetchMock).not.toHaveBeenCalled();
});

it("提交原密码与新密码，成功后回调并关闭", async () => {
  const fetchMock = vi.fn(async () => ({ ok: true, status: 204 }));
  vi.stubGlobal("fetch", fetchMock);
  const onChanged = vi.fn();
  const onClose = vi.fn();
  render(<ChangePasswordDialog open onClose={onClose} onChanged={onChanged} />);
  fill(OLD_PASSPHRASE, NEW_PASSPHRASE, NEW_PASSPHRASE);
  await waitFor(() => expect(onChanged).toHaveBeenCalled());
  expect(onClose).toHaveBeenCalled();
  const [url, init] = fetchMock.mock.calls[0] as unknown as [
    string,
    RequestInit,
  ];
  expect(url).toContain("/api/control/admin/password/change");
  expect(JSON.parse(String(init.body))).toEqual({
    current_password: OLD_PASSPHRASE,
    new_password: NEW_PASSPHRASE,
  });
});

it("原密码错误时显示中文错误并保持打开", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => ({
      ok: false,
      status: 422,
      json: async () => ({
        detail: { code: "ADMIN_PASSWORD_CURRENT_INVALID", message: "x" },
      }),
    })),
  );
  const onChanged = vi.fn();
  render(<ChangePasswordDialog open onClose={vi.fn()} onChanged={onChanged} />);
  fill(
    "Wrong Admin Passphrase 2026!",
    "New Admin Passphrase 2026!",
    "New Admin Passphrase 2026!",
  );
  expect(await screen.findByText("原密码不正确")).toBeInTheDocument();
  expect(onChanged).not.toHaveBeenCalled();
});
