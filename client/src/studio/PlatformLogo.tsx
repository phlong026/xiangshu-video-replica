import { useState } from "react";
import douyinLogo from "../assets/platforms/douyin.ico?inline";
import wechatChannelsLogo from "../assets/platforms/wechat_channels.ico?inline";
import xiaohongshuLogo from "../assets/platforms/xiaohongshu.ico?inline";

// 官方图标内联到构建产物，避免桌面端或子路径部署漏拷公共目录时显示破图。
// 原始文件与来源记录保留在 public/platforms，供旧链接兼容和资源校验。
const platforms = {
  douyin: { name: "抖音", logo: douyinLogo },
  wechat_channels: { name: "视频号", logo: wechatChannelsLogo },
  xiaohongshu: { name: "小红书", logo: xiaohongshuLogo },
};

export type PublishPlatform = keyof typeof platforms;
export const publishPlatformNames = Object.fromEntries(
  Object.entries(platforms).map(([key, value]) => [key, value.name]),
) as Record<PublishPlatform, string>;

export function PlatformLogo({
  platform,
  size = 24,
}: {
  platform: string;
  size?: number;
}) {
  const item =
    platforms[platform as PublishPlatform] ??
    Object.values(platforms).find((value) => value.name === platform);
  if (!item) return null;
  return (
    <img
      src={item.logo}
      alt=""
      aria-hidden="true"
      width={size}
      height={size}
      style={{
        width: size,
        height: size,
        objectFit: "contain",
        verticalAlign: "middle",
        flexShrink: 0,
      }}
    />
  );
}

/** The account's picture, falling back to the platform mark when it cannot load. */
export function AccountAvatar({
  account,
  size = 32,
}: {
  account: { platform: string; username: string; avatar_url?: string | null };
  size?: number;
}) {
  const [failed, setFailed] = useState(false);
  if (!account.avatar_url || failed)
    return <PlatformLogo platform={account.platform} size={size} />;
  return (
    <img
      className="publish-account-avatar"
      src={account.avatar_url}
      alt={`${account.username} 的头像`}
      width={size}
      height={size}
      // Platform CDNs expire links and check referrers; the mark stands in for a
      // picture that will not load, so a row never renders as a broken image.
      referrerPolicy="no-referrer"
      onError={() => setFailed(true)}
    />
  );
}
