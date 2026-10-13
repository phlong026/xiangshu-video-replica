import type { CustomerStoredIdentity } from "./useCustomerSession";

/** 服务端把账号身份收敛为 MASTER/SUB/SUB_ADMIN。 */
export type AccountType = "MASTER" | "SUB" | "SUB_ADMIN";

/**
 * 身份解析：`profile.account_type` 是权威副本（服务端必返、默认 MASTER），
 * 凭据库里的缓存身份只在 profile 到达前兜底——重启恢复的先头帧可能拿不到身份。
 * 两者都没有时返回 null，界面保持沉默，而不是替用户猜一个「母账号」。
 */
export function resolveAccountType(
  profileType: string | null | undefined,
  identity: CustomerStoredIdentity | null | undefined,
): AccountType | null {
  const raw = profileType ?? identity?.accountType ?? null;
  if (!raw) return null;
  return raw === "SUB" || raw === "SUB_ADMIN" ? raw : "MASTER";
}

/** SUB_ADMIN 也归子账号：它有母账号、共用母账号钱包，同样不能充值。 */
export function isSubAccountType(type: AccountType | null): boolean {
  return type === "SUB" || type === "SUB_ADMIN";
}

/** 子账号与母账号共用积分，充值只能由母账号发起（服务端 MASTER_ACCOUNT_REQUIRED）。 */
export const SUB_ACCOUNT_RECHARGE_NOTICE =
  "子账号与母账号共用积分余额，充值需由母账号操作；积分不足时请联系母账号充值。";
