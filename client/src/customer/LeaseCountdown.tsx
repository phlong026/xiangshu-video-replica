import { useEffect, useState } from "react";

/** 会话有效期展示（FE-04 / T31 起源；2026-11-02 随 7 天滑动契约改造）。
 * 服务端在每次校验通过的请求上滑动续期，客户端不再有「立即续约」动作——
 * 到期只会在连续 7 天未使用后到来，这里只负责把有效期与剩余时长说清楚。
 */
export function LeaseCountdown({
  expiresAt,
}: {
  expiresAt: string;
}): React.JSX.Element {
  // 天级倒计时用分钟粒度的滴答即可；保留周期重算以免「剩余」停住不动。
  const [nowMs, setNowMs] = useState(() => Date.now());
  useEffect(() => {
    const timer = window.setInterval(() => setNowMs(Date.now()), 30_000);
    return () => window.clearInterval(timer);
  }, []);

  const now = new Date(nowMs);
  const expiry = new Date(expiresAt);
  const diffMs = expiry.getTime() - now.getTime();

  let status: "normal" | "warning" | "expired" = "normal";
  if (diffMs <= 0) {
    status = "expired";
  } else if (diffMs < 60 * 60_000) {
    // Less than 1 hour: 提示用户再不使用就要重新登录了。
    status = "warning";
  }
  const minutesRemaining = Math.max(0, Math.ceil(diffMs / 60_000));
  const daysRemaining = Math.floor(minutesRemaining / (60 * 24));
  const hoursRemaining = Math.floor((minutesRemaining % (60 * 24)) / 60);
  const formattedExpiry = expiry.toLocaleString("zh-CN", {
    month: "long",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });

  return (
    <div className={`lease-countdown ${status}`}>
      <p className="label">
        本次会话有效至：
        <time dateTime={expiresAt}>{formattedExpiry}</time>
      </p>

      {status === "expired" && (
        <span className="status-text expired">⚠️ 已过期——请重新登录</span>
      )}

      {status === "warning" && (
        <span className="status-text warning">
          ⏰ 即将过期（剩余 {minutesRemaining} 分钟），继续使用会自动续期
        </span>
      )}

      {status === "normal" && (
        <span className="status-text normal">
          剩余 {daysRemaining} 天 {hoursRemaining} 小时，使用中自动续期
        </span>
      )}
    </div>
  );
}
