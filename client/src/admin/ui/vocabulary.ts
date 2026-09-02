// 管理端统一词典 —— 状态、角色、金额与时间的唯一翻译来源。
//
// 规则（2026-09-02 管理端评估 §交互规范）：
// 1. 页面不得再写字面量状态映射表；新状态先在这里登记。
// 2. credits 统一叫"条数"，不再出现"积分/额度"。
// 3. 金额一律 `¥xx.xx`（分位保留）——此前的 `Math.floor` 会把 100.50 元
//    显示成 100 元，属数据失真，已修复。
// 4. REVOKED 按域区分动词：激活码"已撤销"、设备"已强制退出"（沿用操作
//    动词），客户沿用其激活码口径"已撤销"。

type LabelMap = Record<string, string>;

export const ACTIVATION_CODE_STATUS_LABELS: LabelMap = {
  GENERATED: "待启用",
  ISSUED: "可使用",
  ACTIVE: "使用中",
  SUSPENDED: "已暂停",
  REVOKED: "已撤销",
  EXPIRED: "已过期",
};

export const DEVICE_STATUS_LABELS: LabelMap = {
  BOUND: "已绑定",
  UNBOUND: "已解绑",
  REVOKED: "已强制退出",
};

export const CUSTOMER_STATUS_LABELS: LabelMap = {
  ACTIVE: "活跃",
  SUSPENDED: "已暂停",
  REVOKED: "已撤销",
};

export const RECHARGE_ORDER_STATUS_LABELS: LabelMap = {
  PENDING: "待支付",
  PAID: "已支付",
  FAILED: "失败",
  CLOSED: "已关闭",
};

export const ROLE_LABELS: LabelMap = {
  admin: "管理员",
  auditor: "审计员",
  customer: "客户",
  employee: "员工",
};

export const TRANSACTION_TYPE_LABELS: LabelMap = {
  CHARGE: "充值到账",
  RESERVE: "冻结",
  SETTLE: "结算",
  RELEASE: "释放",
};

export const ADJUSTMENT_SOURCE_LABELS: LabelMap = {
  CS_TICKET: "客服工单",
  REFUND_APPROVAL: "退款审批",
  COMPENSATION_APPROVAL: "补偿审批",
  LEDGER_CORRECTION: "账本更正",
  FREE_GRANT: "免费条数发放",
};

export const PLATFORM_LABELS: LabelMap = {
  windows: "Windows",
  macos: "macOS",
  ios: "iOS",
  android: "Android",
  linux: "Linux",
};

export const GENERATION_RECORD_TYPE_LABELS: LabelMap = {
  VIDEO: "视频生成",
  FIRST_FRAME_IMAGE: "人物置换首帧",
  CHARACTER_SHEET_IMAGE: "人物五视图",
  CHARACTER_VIEW_IMAGE: "人物单视图",
  SOURCE_FRAME_AI_SCORE: "源画面 AI 评分",
  SOURCE_FRAME_PROCESS: "源画面处理",
};

/** 查词典并回退到原始值——未知状态原样展示，便于发现新枚举。 */
export function labelFrom(labels: LabelMap, value: string): string {
  return labels[value] ?? value;
}

export function activationCodeStatusLabel(status: string): string {
  return labelFrom(ACTIVATION_CODE_STATUS_LABELS, status);
}

export function deviceStatusLabel(status: string): string {
  return labelFrom(DEVICE_STATUS_LABELS, status);
}

export function customerStatusLabel(status: string): string {
  return labelFrom(CUSTOMER_STATUS_LABELS, status.toUpperCase());
}

export function rechargeOrderStatusLabel(status: string): string {
  return labelFrom(RECHARGE_ORDER_STATUS_LABELS, status);
}

export function roleLabel(role: string): string {
  return labelFrom(ROLE_LABELS, role);
}

export function transactionTypeLabel(type: string): string {
  return labelFrom(TRANSACTION_TYPE_LABELS, type);
}

export function platformLabel(platform: string): string {
  return labelFrom(PLATFORM_LABELS, platform);
}

/** 精确的分为元展示：不丢分位（¥10050 → "¥100.50"）。 */
export function formatFen(fen: number): string {
  return `¥${formatYuanFromFen(fen)}`;
}

/** 分转数字元字符串，保留两位小数（供金额列与输入框回显使用）。 */
export function formatYuanFromFen(fen: number): string {
  return (fen / 100).toFixed(2);
}

/** 统一时间列格式；null/无法解析的值显示 "—" 而不是抛错。 */
export function formatDateTime(value: string | null | undefined): string {
  if (!value) {
    return "—";
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return "—";
  }
  return date.toLocaleString("zh-CN", { hour12: false });
}

/** 条数展示统一后缀。 */
export function formatCredits(count: number | null | undefined): string {
  return `${count ?? 0} 条`;
}
