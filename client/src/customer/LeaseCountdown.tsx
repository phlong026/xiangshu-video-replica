/** 会话有效期说明卡（FE-04 / T31 起源；2026-11-02 随 7 天滑动契约改造）。
 * 服务端在每次校验通过的请求上滑动续期，客户端不再有「立即续约」动作——
 * 到期只会在连续 7 天未使用后到来。
 */
export function LeaseCountdown({
  expiresAt,
}: {
  expiresAt: string;
}): React.JSX.Element {
  // 2026-11-04 r4 评审 P2：展示的是登录时的到期快照，而服务端对每个通过
  // 校验的请求都在滑动续期——快照跨过自身后，UI 若把它说成「当前有效期」
  // 甚至断言「已过期」都是误导（服务端会话实际有效）。在没有「每请求同步
  // 最新到期时间」的契约前，移除倒计时与三态断言；时间只作为「登录记录」
  // 呈现，续期语义交给下方说明文案（Codex B 级评审 P2：标签不得宣称
  // 「有效期至」这个现在时）。
  const formattedExpiry = new Date(expiresAt).toLocaleString("zh-CN", {
    month: "long",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });

  return (
    <div className="lease-countdown normal">
      <p className="label">
        登录时记录的到期时间：
        <time dateTime={expiresAt}>{formattedExpiry}</time>
      </p>
      <span className="status-text normal">
        持续使用自动续期；连续 7 天不使用后才需要重新登录
      </span>
    </div>
  );
}
