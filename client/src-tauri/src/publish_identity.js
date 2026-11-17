// 只在官方创作者顶层文档捕获本人身份，禁止把登录页和其他资料接口当作账号凭据。
(() => {
  if (window.top !== window) return;
  const allowed = {
    "https://creator.douyin.com": "/web/api/media/user/info/",
    "https://channels.weixin.qq.com": "/auth/auth_data",
    "https://creator.xiaohongshu.com": "/api/galaxy/user/info",
    "https://cp.kuaishou.com": "/rest/v2/creator/pc/authority/account/current",
  };
  const origin = location.origin;
  const endpoint = allowed[origin];
  if (!endpoint) return;
  const kuaishou = origin === "https://cp.kuaishou.com";
  let generation = 0;
  let verified = null;
  let verifiedAt = null;
  const stamp = () => ({ generation, href: location.href });
  const current = (source) => source && source.generation === generation && source.href === location.href && location.origin === origin;
  const invalidate = () => {
    generation += 1;
    verified = null;
    verifiedAt = null;
    window.__xiangshuPublishIdentity = null;
  };
  // 同文档跳转也要作废在途回执；完整导航由新文档重新安装此脚本。
  if (kuaishou) {
    for (const name of ["pagehide", "popstate", "hashchange"]) window.addEventListener?.(name, invalidate);
    for (const name of ["pushState", "replaceState"]) {
      const original = window.history?.[name];
      if (typeof original === "function") window.history[name] = function (...args) {
        const result = original.apply(this, args);
        invalidate();
        return result;
      };
    }
  }
  const documentId = window.crypto?.randomUUID?.() ?? `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
  Object.defineProperty(window, "__xiangshuPublishDocumentId", { value: documentId, writable: false, configurable: false });
  // Rust 读取闭包中的回执，不接受页面任意改写同名全局对象伪造 UID。
  Object.defineProperty(window, "__xiangshuReadPublishIdentity", {
    value: () => current(verifiedAt) && verified ? { ...verified } : null,
    writable: false,
    configurable: false,
  });
  const matches = (url) => {
    try {
      const parsed = new URL(url, location.href);
      return parsed.origin === origin && (kuaishou ? parsed.pathname === endpoint : parsed.pathname.endsWith(endpoint));
    } catch { return false; }
  };
  const validAvatar = (value) =>
    typeof value === "string" && value.trim().length <= 1024 && /^https:\/\/\S+$/.test(value.trim());
  const capture = (requestUrl, responseUrl, body, source) => {
    try {
      if (!current(source) || !matches(requestUrl) || !matches(responseUrl) || !body || typeof body !== "object") return;
      let uid, name, avatar;
      if (origin === "https://creator.douyin.com" && Number(body.status_code) === 0) {
        uid = body.user?.uid ?? body.user?.user_id;
        name = body.user?.nickname;
        avatar = body.user?.avatar_larger?.url_list?.[0] ?? body.user?.avatar_thumb?.url_list?.[0] ?? body.user?.avatar_url;
      } else if (origin === "https://channels.weixin.qq.com" && Number(body.errCode ?? body.errcode) === 0) {
        const user = body.data?.finderUser ?? body.data?.userAttr ?? body.data?.finderList?.[0];
        uid = user?.finderUsername;
        name = user?.nickname ?? user?.nickName;
        avatar = user?.headImgUrl ?? user?.headImage;
      } else if (origin === "https://creator.xiaohongshu.com" && (body.success === true || (body.success === undefined && body.code === 0))) {
        uid = body.data?.userId;
        name = body.data?.userName;
      } else if (kuaishou) {
        if (body.result === 109 || (body.result === 1 && body.data?.logined === false)) {
          verified = null;
          verifiedAt = null;
          window.__xiangshuPublishIdentity = null;
          return;
        }
        if (body.result !== 1) return;
        uid = body.data?.userId;
        name = body.data?.userName;
        avatar = body.data?.userAvatar;
        if (uid === 0 || (typeof uid === "string" && uid.trim() === "0")) return;
      }
      if ((typeof uid === "string" || (typeof uid === "number" && Number.isSafeInteger(uid))) && typeof name === "string" && String(uid).trim() && String(uid).length <= 256 && name.trim() && (!kuaishou || name.length <= 256)) {
        const identity = { platform_user_id: String(uid).trim(), username: name.trim().slice(0, 256) };
        if (validAvatar(avatar)) identity.avatar_url = avatar.trim();
        verified = identity;
        verifiedAt = source;
        window.__xiangshuPublishIdentity = { ...identity };
      }
    } catch { /* 不读取非 JSON 与其他接口的数据。 */ }
  };
  const fetchOriginal = window.fetch;
  window.fetch = async function (...args) {
    const requestUrl = typeof args[0] === "string" ? args[0] : args[0]?.url ?? args[0]?.href;
    const method = String(args[1]?.method ?? args[0]?.method ?? "GET").toUpperCase();
    const source = stamp();
    const eligible = current(source) && matches(requestUrl) && (!kuaishou || method === "POST");
    // 官方 bootstrap 不需要重定向；禁止身份请求经其他站点再返回。
    if (kuaishou && eligible) args[1] = { ...args[1], redirect: "error" };
    const response = await fetchOriginal.apply(this, args);
    const responseUrl = response.url;
    if (eligible && current(source) && matches(responseUrl) && (!kuaishou || (response.ok && response.status >= 200 && response.status < 300 && !response.redirected))) {
      void response.clone().json().then(body => capture(requestUrl, responseUrl, body, source)).catch(() => {});
    }
    return response;
  };
  const openOriginal = XMLHttpRequest.prototype.open;
  XMLHttpRequest.prototype.open = function (...args) {
    const [method, requestUrl] = args;
    const source = stamp();
    const eligible = current(source) && matches(requestUrl) && (!kuaishou || String(method).toUpperCase() === "POST");
    this.addEventListener("load", () => {
      try {
        if (eligible && current(source) && matches(this.responseURL) && (!kuaishou || (this.status >= 200 && this.status < 300))) {
          capture(requestUrl, this.responseURL, this.responseType === "json" ? this.response : JSON.parse(this.responseText), source);
        }
      } catch { /* 跳转、非 JSON 与其他接口不产生身份。 */ }
    }, { once: true });
    return openOriginal.apply(this, args);
  };

  // 抖音可能跨扫码导航丢失被动回执；仅复核已知的官方本人接口。
  // 快手 bootstrap 自带官方签名与上下文，只监听真实 POST，不猜测请求。
  if (origin === "https://creator.douyin.com" && typeof window.setInterval === "function") {
    let attempts = 0;
    const timer = window.setInterval(() => {
      if (verified || ++attempts > 100) {
        window.clearInterval?.(timer);
        return;
      }
      try {
        void Promise.resolve(window.fetch("/web/api/media/user/info/?aid=1128", { credentials: "include" })).catch(() => {});
      } catch { /* 一次失败不阻塞下次复核。 */ }
    }, 3000);
  }
})();
