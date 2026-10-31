import "./brand-identity.css";

import zhongshuLogoMark from "./assets/brand/zhongshu-logo-mark.svg";

// 品牌锁定版式：logo 在左垂直居中，右侧两行文案左缘对齐——
// 主行是品牌名，副行是平台定位语（副行收进组件内，避免各使用处自行拼 small）。
export function BrandIdentity() {
  return (
    <span className="brand-identity">
      <img src={zhongshuLogoMark} alt="众墅之家" width={50.4} height={43.2} />
      <span className="brand-identity__text">
        <strong>众墅之家 AI 即创</strong>
        <small>乡墅爆款视频创作平台</small>
      </span>
    </span>
  );
}
