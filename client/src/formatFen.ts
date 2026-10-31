/** 客户/工作台侧金额（分）展示的唯一实现。
 *
 * 2026-10-31 前 WalletPanel / CustomerRechargeDialog / CustomerWalletPanel
 * 各持一份逐字相同的 formatFen，收敛于此。注意 admin/ui/vocabulary.ts 里
 * 还有一份管理端口径的 formatFen（¥ 前缀、不足一分显示「< ¥0.01」），
 * 面向运营报表，两套口径有意分开，不要合并。
 */
export function formatFen(amountFen: number): string {
  const yuan = amountFen / 100;
  return `${Number.isInteger(yuan) ? yuan : yuan.toFixed(2)}元`;
}
