/* Platform selectors adapted from dreammis/social-auto-upload (MIT).
 * Full license shipped in frontend assets: /licenses/social-auto-upload-MIT.txt
 * Only QR images/status leave an official frame; credentials stay in WebView2.
 */
(() => {
  const channels = "https://channels.weixin.qq.com";
  const wechat = "https://open.weixin.qq.com";
  const kuaishou = "https://cp.kuaishou.com";
  const passport = "https://passport.kuaishou.com";
  const origin = location.origin;
  const child = window.top !== window;
  const official = ["https://creator.douyin.com", channels, "https://creator.xiaohongshu.com", kuaishou];
  const kuaishouLogin = (url) => url.origin === passport && ["/pc/account/login", "/pc/account/login/"].includes(url.pathname);
  const qrTarget = (url) => {
    if (kuaishouLogin(url)) return kuaishou;
    if ((url.origin === wechat && url.pathname === "/connect/qrconnect") ||
      (url.origin === channels && url.pathname.includes("login-for-iframe"))) return channels;
    return null;
  };
  const target = child ? qrTarget(location) : null;
  if (child ? !target : !(official.includes(origin) || kuaishouLogin(location))) return;
  const trustedCurrent = () => location.origin === origin && (origin !== passport || kuaishouLogin(location));
  const frameUrl = (frame) => {
    try {
      const url = new URL(frame.getAttribute("src"), location.href);
      return qrTarget(url) === origin ? url : null;
    } catch { return null; }
  };

  const validImage = (value) => typeof value === "string" && value.length <= 600000 &&
    /^data:image\/(png|jpeg|webp);base64,[A-Za-z0-9+/]+=*$/.test(value);
  const visible = (element) => {
    if (!element) return false;
    const rect = element.getBoundingClientRect();
    const style = getComputedStyle(element);
    return rect.width > 0 && rect.height > 0 && style.display !== "none" && style.visibility !== "hidden";
  };
  const first = (selectors, root = document) => {
    for (const selector of selectors) {
      const element = [...root.querySelectorAll(selector)].find(visible);
      if (element) return element;
    }
    return null;
  };
  const report = (phase, image = null) => {
    if (!trustedCurrent()) return;
    const value = { phase, image: validImage(image) ? image : null, observed_at: Date.now() };
    if (child) window.top.postMessage({ type: "xiangshu-publish-qr", ...value }, target);
    else window.__xiangshuPublishLogin = value;
  };
  let bridge = null;
  if (!child && [channels, kuaishou].includes(origin)) {
    window.addEventListener("message", (event) => {
      if (!trustedCurrent() || !event.source) return;
      const frame = [...document.querySelectorAll("iframe")].find((element) => {
        const url = frameUrl(element);
        return element.contentWindow === event.source && url?.origin === event.origin;
      });
      if (!frame || event.data?.type !== "xiangshu-publish-qr") return;
      const { phase, image } = event.data;
      if (!["loading", "qr_ready", "expired", "confirming", "action_required"].includes(phase)) return;
      if (phase === "qr_ready" && !validImage(image)) return;
      bridge = { frame, href: frameUrl(frame).href, phase, image: phase === "qr_ready" ? image : null, at: Date.now() };
    });
  }
  const readBridge = () => {
    if (bridge && document.contains(bridge.frame) && visible(bridge.frame) &&
      frameUrl(bridge.frame)?.href === bridge.href && Date.now() - bridge.at < 4000) {
      report(bridge.phase, bridge.image);
      return true;
    }
    return false;
  };

  let switched = false;
  let cachedSource = "";
  let cachedImage = null;
  function readImage(image) {
    const source = image.getAttribute("src") || "";
    if (validImage(source)) return source;
    // 只读取官方页面已经渲染的图像，不额外请求图片、Cookie 或任意资源。
    if (source === cachedSource && cachedImage) return cachedImage;
    if (!image.complete || !image.naturalWidth || image.naturalWidth > 2048 || image.naturalHeight > 2048) return null;
    try {
      const canvas = document.createElement("canvas");
      canvas.width = image.naturalWidth;
      canvas.height = image.naturalHeight;
      canvas.getContext("2d").drawImage(image, 0, 0);
      const data = canvas.toDataURL("image/png");
      if (!validImage(data)) return null;
      cachedSource = source;
      cachedImage = data;
      return data;
    } catch { return null; }
  }

  function scan() {
    if (!trustedCurrent() || !document.body) return;
    // 只看可见叶子文本，避免隐藏的旧过期模板覆盖当前二维码。
    const text = [...document.querySelectorAll("p,span,div")]
      .filter((element) => !element.children.length && visible(element))
      .map((element) => element.textContent.trim()).join(" ");
    if (origin === passport && first(["div.qrcode-status.qrcode-status-timeout"])) return report("expired");
    if (origin === passport && first(["div.qrcode-status.qrcode-status-confirm"])) return report("confirming");
    if (/二维码已(?:过期|失效)|二维码过期|二维码失效/.test(text)) return report("expired");
    if (/扫码成功|请在手机上确认|请在手机端确认/.test(text)) return report("confirming");
    if (/安全验证|拖动滑块|短信验证|身份验证/.test(text)) return report("action_required");

    let image;
    if (origin === "https://creator.douyin.com") {
      image = first([
        'div#animate_qrcode_container img[src^="data:image"]',
        'div[class*="animate_qrcode_container"] img[src^="data:image"]',
        'div[class*="scan_qrcode_login_content"] img[src^="data:image"]',
        'img[aria-label="二维码"]',
      ]);
      if (!image && !switched) {
        const tab = [...document.querySelectorAll("button,div,span")].find(
          (element) => !element.children.length && element.textContent.trim() === "扫码登录" && visible(element),
        );
        if (tab) { switched = true; tab.click(); }
      }
    } else if (origin === "https://creator.xiaohongshu.com") {
      const box = first(["div[class*='login-box']"]);
      if (box) {
        const label = [...box.querySelectorAll("span,div,p")].find(
          (element) => !element.children.length && /APP扫一扫登录|扫一扫/.test(element.textContent),
        );
        if (label) {
          for (const node of [label, label.parentElement]) {
            let sibling = node?.nextElementSibling;
            while (sibling && !image) {
              image = first(["img:not(.css-wemwzq)"], sibling);
              sibling = sibling.nextElementSibling;
            }
          }
          image ||= first(['img[src^="data:image"]', 'img[class*="qrcode"]'], box);
        } else if (!switched) {
          const toggle = first(["img.css-wemwzq"], box);
          if (toggle) { switched = true; toggle.click(); }
        }
      }
    } else if (origin === passport) {
      const box = first(["main#login-form"]);
      if (box) {
        image = first(['div.qr-login img[alt="qrcode"]'], box);
        if (!image && !switched) {
          const toggle = first(["div.platform-switch.platform-switch-pc"], box);
          if (toggle) { switched = true; toggle.click(); }
        }
      }
    } else if (origin === kuaishou) {
      if (readBridge()) return;
    } else {
      image = first(["div.login-qrcode-wrap img.qrcode", "div.qrcode-wrap img.qrcode", "img.qrcode", "img.js_qrcode_img"]);
      if (!image && readBridge()) return;
    }
    if (image) {
      const data = readImage(image);
      return report(data ? "qr_ready" : "action_required", data);
    }
    report("loading");
  }
  const read = () => {
    if (!trustedCurrent()) {
      if (!child) window.__xiangshuPublishLogin = { phase: "action_required", image: null, observed_at: Date.now() };
      return window.__xiangshuPublishLogin;
    }
    try { scan(); } catch { report("action_required"); }
    return window.__xiangshuPublishLogin;
  };
  // 后台窗口可能暂停定时器，主窗口轮询时也向已验证的官方 iframe 请求刷新。
  if (!child) window.__xiangshuReadPublishLogin = () => {
    if (trustedCurrent() && [channels, kuaishou].includes(origin)) {
      for (const frame of document.querySelectorAll("iframe")) {
        const url = frameUrl(frame);
        if (url) frame.contentWindow?.postMessage({ type: "xiangshu-read-publish-qr" }, url.origin);
      }
    }
    return read();
  };
  else window.addEventListener("message", (event) => {
    if (event.origin === target && event.source === window.top && event.data?.type === "xiangshu-read-publish-qr") read();
  });
  const tick = () => {
    read();
    setTimeout(tick, 1000);
  };
  report("loading");
  setTimeout(tick, 0);
})();
