/** 会话有效期展示（FE-04 / T31 起源；2026-11-02 随 7 天滑动契约改造）。
 * 服务端在每次校验通过的请求上滑动续期，客户端不再有「立即续约」动作——
 * 到期只会在连续 7 天未使用后到来，这里只负责把有效期与剩余时长说清楚。
 */
export function LeaseCountdown({
  expiresAt,
}: {
  expiresAt: string;
}): React.JSX.Element {
  // 2026-11-04 r4 评审 P2：展示的是登录时的到期快照，而服务端对每个通过
  // 校验的请求都在滑动续期——快照跨过自身后 UI 若断言「已过期」就是误导
  // （服务端会话实际有效）。在没有「每请求同步最新到期时间」的契约前，
  // 移除倒计时与过期/告警断言，改为纯说明卡。
  const formattedExpiry = new Date(expiresAt).toLocaleString("zh-CN", {
    month: "long",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });

  return (
    <div className="lease-countdown normal">
      <p className="label">
        会话有效期至：
        <time dateTime={expiresAt}>{formattedExpiry}</time>
      </p>
      <span className="status-text normal">
        持续使用自动续期；连续 7 天不使用后才需要重新登录
      </span>
    </div>
  );
}
