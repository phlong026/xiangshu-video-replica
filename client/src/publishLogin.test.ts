import { runInNewContext } from "node:vm";
import { expect, it, vi } from "vitest";
import script from "../src-tauri/src/publish_login.js?raw";

const qr = "data:image/png;base64,aGVsbG8=";
function environment(url: string, html: string, child = false) {
  const doc = document.implementation.createHTMLDocument();
  doc.body.innerHTML = html;
  for (const element of doc.querySelectorAll("*")) {
    element.getBoundingClientRect = () =>
      ({ width: 200, height: 200 }) as DOMRect;
    if (element.tagName === "IFRAME")
      Object.defineProperty(element, "contentWindow", {
        value: { postMessage: vi.fn() },
      });
  }
  const listeners = new Map<string, (event: unknown) => void>();
  const top = { postMessage: vi.fn() };
  const win: Record<string, unknown> = {
    addEventListener: (name: string, fn: (event: unknown) => void) =>
      listeners.set(name, fn),
  };
  win.top = child ? top : win;
  const timers: (() => unknown)[] = [];
  const location = new URL(url);
  runInNewContext(script, {
    window: win,
    document: doc,
    location,
    URL,
    Date,
    setTimeout: (fn: () => unknown) => timers.push(fn),
    getComputedStyle: () => ({ display: "block", visibility: "visible" }),
  });
  return {
    doc,
    win,
    top,
    listeners,
    location,
    tick: async () => {
      await timers.shift()?.();
      await Promise.resolve();
      return win.__xiangshuPublishLogin;
    },
  };
}

it("提取改版后的抖音二维码，并同步更新后的二维码", async () => {
  const env = environment(
    "https://creator.douyin.com/",
    `<div id="animate_qrcode_container"><img src="${qr}"></div>`,
  );
  expect(await env.tick()).toMatchObject({ phase: "qr_ready", image: qr });
  const updated = "data:image/png;base64,bmV3";
  env.doc.querySelector("img")?.setAttribute("src", updated);
  expect(await env.tick()).toMatchObject({ phase: "qr_ready", image: updated });
});

it("小红书只切换一次扫码面板，并提取扫一扫区域内的二维码", async () => {
  const env = environment(
    "https://creator.xiaohongshu.com/login",
    '<div class="login-box-container"><img class="css-wemwzq"></div>',
  );
  const click = vi.spyOn(
    env.doc.querySelector("img") as HTMLImageElement,
    "click",
  );
  await env.tick();
  await env.tick();
  expect(click).toHaveBeenCalledTimes(1);
  const box = env.doc.querySelector("div") as HTMLDivElement;
  box.innerHTML = `<div><span>APP扫一扫登录</span></div><div><img src="${qr}"></div>`;
  const img = box.querySelector("img") as HTMLImageElement;
  img.getBoundingClientRect = () => ({ width: 200, height: 200 }) as DOMRect;
  expect(await env.tick()).toMatchObject({ phase: "qr_ready", image: qr });
});

it("微信二维码只回传到视频号官方父页面", async () => {
  const env = environment(
    "https://open.weixin.qq.com/connect/qrconnect",
    `<img class="qrcode" src="${qr}">`,
    true,
  );
  await env.tick();
  expect(env.top.postMessage).toHaveBeenCalledWith(
    expect.objectContaining({
      type: "xiangshu-publish-qr",
      phase: "qr_ready",
      image: qr,
    }),
    "https://channels.weixin.qq.com",
  );
});

it("视频号跳过隐藏旧二维码，使用新版可见图片", async () => {
  const env = environment(
    "https://open.weixin.qq.com/connect/qrconnect",
    `<img class="qrcode" src="data:image/png;base64,b2xk"><img class="js_qrcode_img web_qrcode_img" src="${qr}">`,
    true,
  );
  const old = env.doc.querySelector(".qrcode") as HTMLImageElement;
  old.getBoundingClientRect = () => ({ width: 0, height: 0 }) as DOMRect;
  await env.tick();
  expect(env.top.postMessage).toHaveBeenLastCalledWith(
    expect.objectContaining({ phase: "qr_ready", image: qr }),
    "https://channels.weixin.qq.com",
  );
});

it("拒绝伪造来源的 iframe 消息，接受本页官方二维码 iframe", async () => {
  const env = environment(
    "https://channels.weixin.qq.com/",
    '<iframe src="https://open.weixin.qq.com/connect/qrconnect"></iframe>',
  );
  const receive = env.listeners.get("message");
  const payload = { type: "xiangshu-publish-qr", phase: "qr_ready", image: qr };
  receive?.({ origin: "https://evil.test", source: {}, data: payload });
  expect(await env.tick()).not.toMatchObject({ image: qr });
  receive?.({
    origin: "https://open.weixin.qq.com",
    source: env.doc.querySelector("iframe")?.contentWindow,
    data: payload,
  });
  expect(await env.tick()).toMatchObject({ phase: "qr_ready", image: qr });
});

it("二维码过期后不再显示旧码", async () => {
  const env = environment(
    "https://creator.douyin.com/",
    `<div id="animate_qrcode_container"><img src="${qr}"></div>`,
  );
  await env.tick();
  const notice = env.doc.createElement("p");
  notice.textContent = "二维码已过期";
  notice.getBoundingClientRect = () => ({ width: 200, height: 20 }) as DOMRect;
  env.doc.body.append(notice);
  expect(await env.tick()).toMatchObject({ phase: "expired", image: null });
});

it("后台窗口定时器暂停时，主动读取仍刷新二维码和过期状态", () => {
  const env = environment(
    "https://creator.douyin.com/",
    `<div id="animate_qrcode_container"><img src="${qr}"></div>`,
  );
  const read = env.win.__xiangshuReadPublishLogin as () => unknown;
  expect(read).toBeTypeOf("function");
  expect(read()).toMatchObject({ phase: "qr_ready", image: qr });
  const updated = "data:image/png;base64,bmV3";
  env.doc.querySelector("img")?.setAttribute("src", updated);
  expect(read()).toMatchObject({ phase: "qr_ready", image: updated });
  const notice = env.doc.createElement("p");
  notice.textContent = "二维码已过期";
  notice.getBoundingClientRect = () => ({ width: 200, height: 20 }) as DOMRect;
  env.doc.body.append(notice);
  expect(read()).toMatchObject({ phase: "expired", image: null });
});

it("后台视频号只向官方二维码框架请求刷新，子框架只接受官方顶层请求", () => {
  const parent = environment(
    "https://channels.weixin.qq.com/",
    '<iframe src="https://open.weixin.qq.com/connect/qrconnect"></iframe><iframe src="https://evil.test/connect/qrconnect"></iframe>',
  );
  (parent.win.__xiangshuReadPublishLogin as () => unknown)();
  const frames = parent.doc.querySelectorAll("iframe");
  expect(frames[0].contentWindow?.postMessage).toHaveBeenCalledWith(
    { type: "xiangshu-read-publish-qr" },
    "https://open.weixin.qq.com",
  );
  expect(frames[1].contentWindow?.postMessage).not.toHaveBeenCalled();
  const child = environment(
    "https://open.weixin.qq.com/connect/qrconnect",
    `<img class="qrcode" src="${qr}">`,
    true,
  );
  child.top.postMessage.mockClear();
  const receive = child.listeners.get("message");
  const data = { type: "xiangshu-read-publish-qr" };
  receive?.({ origin: "https://evil.test", source: child.top, data });
  receive?.({ origin: "https://channels.weixin.qq.com", source: {}, data });
  expect(child.top.postMessage).not.toHaveBeenCalled();
  receive?.({
    origin: "https://channels.weixin.qq.com",
    source: child.top,
    data,
  });
  expect(child.top.postMessage).toHaveBeenCalledWith(
    expect.objectContaining({ phase: "qr_ready", image: qr }),
    "https://channels.weixin.qq.com",
  );
});

it("非官方域名不注入，SVG 和远程地址不作为二维码回传", async () => {
  const evil = environment(
    "https://creator.douyin.com.evil.test/",
    `<img src="${qr}">`,
  );
  expect(await evil.tick()).toBeUndefined();
  const svg = environment(
    "https://creator.douyin.com/",
    '<div id="animate_qrcode_container"><img src="data:image/svg+xml;base64,PHN2Zz4="></div>',
  );
  expect(await svg.tick()).not.toMatchObject({ phase: "qr_ready" });
});

const kuaishouLogin = "https://passport.kuaishou.com/pc/account/login/";
const kuaishouQr = `<main id="login-form"><div class="qr-login"><img alt="qrcode" src="${qr}"></div></main>`;

it("快手从精确官方 passport 登录路径读取固定上游二维码", async () => {
  const env = environment(
    `${kuaishouLogin}?sid=kuaishou.web.cp.api`,
    kuaishouQr,
  );
  expect(await env.tick()).toMatchObject({ phase: "qr_ready", image: qr });
  expect(env.win.__xiangshuPublishIdentity).toBeUndefined();
  const updated = "data:image/png;base64,bmV3";
  env.doc.querySelector("img")?.setAttribute("src", updated);
  expect((env.win.__xiangshuReadPublishLogin as () => unknown)()).toMatchObject(
    {
      phase: "qr_ready",
      image: updated,
    },
  );
});

it("快手只点击可见 PC 切换按钮一次，避免切回密码模式", async () => {
  const env = environment(
    kuaishouLogin,
    '<main id="login-form"><div class="platform-switch platform-switch-mobile"></div><div class="platform-switch platform-switch-pc"></div></main>',
  );
  const mobile = vi.spyOn(
    env.doc.querySelector(".platform-switch-mobile") as HTMLElement,
    "click",
  );
  const pc = vi.spyOn(
    env.doc.querySelector(".platform-switch-pc") as HTMLElement,
    "click",
  );
  await env.tick();
  await env.tick();
  expect(pc).toHaveBeenCalledTimes(1);
  expect(mobile).not.toHaveBeenCalled();
});

it("快手已有二维码时不点击切换按钮", async () => {
  const env = environment(
    kuaishouLogin,
    kuaishouQr.replace(
      "</main>",
      '<div class="platform-switch platform-switch-pc"></div></main>',
    ),
  );
  const click = vi.spyOn(
    env.doc.querySelector(".platform-switch-pc") as HTMLElement,
    "click",
  );
  expect(await env.tick()).toMatchObject({ phase: "qr_ready", image: qr });
  expect(click).not.toHaveBeenCalled();
});

it.each([
  ["qrcode-status-timeout", "expired"],
  ["qrcode-status-confirm", "confirming"],
])("快手状态层 %s 隐藏旧码并上报 %s", async (status, phase) => {
  const env = environment(
    kuaishouLogin,
    kuaishouQr.replace(
      "</main>",
      `<div class="qrcode-status ${status}"></div></main>`,
    ),
  );
  expect(await env.tick()).toMatchObject({ phase, image: null });
});

it("快手隐藏的过期模板不会覆盖当前有效二维码", async () => {
  const env = environment(
    kuaishouLogin,
    kuaishouQr.replace(
      "</main>",
      '<div class="qrcode-status qrcode-status-timeout"></div></main>',
    ),
  );
  const expired = env.doc.querySelector(
    ".qrcode-status-timeout",
  ) as HTMLElement;
  expired.getBoundingClientRect = () => ({ width: 0, height: 0 }) as DOMRect;
  expect(await env.tick()).toMatchObject({ phase: "qr_ready", image: qr });
});

it.each([
  "http://passport.kuaishou.com/pc/account/login/",
  "https://passport.kuaishou.com.evil.test/pc/account/login/",
  "https://sub.passport.kuaishou.com/pc/account/login/",
  "https://passport.kuaishou.com/pc/account/login/other",
  "https://passport.kuaishou.com/other/pc/account/login/",
  "https://passport.kuaishou.com:8443/pc/account/login/",
])("快手拒绝未经证实的登录来源：%s", async (url) => {
  const env = environment(url, kuaishouQr);
  expect(await env.tick()).toBeUndefined();
  expect(env.win.__xiangshuReadPublishLogin).toBeUndefined();
});

it("快手创作者页仅接受当前官方 passport iframe 的二维码消息", async () => {
  const env = environment(
    "https://cp.kuaishou.com/article/publish/video",
    `<iframe src="${kuaishouLogin}"></iframe><iframe src="https://evil.test/pc/account/login/"></iframe>`,
  );
  const frames = env.doc.querySelectorAll("iframe");
  const receive = env.listeners.get("message");
  const data = { type: "xiangshu-publish-qr", phase: "qr_ready", image: qr };
  receive?.({
    origin: "https://evil.test",
    source: frames[1].contentWindow,
    data,
  });
  receive?.({ origin: "https://passport.kuaishou.com", source: {}, data });
  expect(await env.tick()).not.toMatchObject({ image: qr });
  receive?.({
    origin: "https://passport.kuaishou.com",
    source: frames[0].contentWindow,
    data,
  });
  expect(await env.tick()).toMatchObject({ phase: "qr_ready", image: qr });
  (env.win.__xiangshuReadPublishLogin as () => unknown)();
  expect(frames[0].contentWindow?.postMessage).toHaveBeenCalledWith(
    { type: "xiangshu-read-publish-qr" },
    "https://passport.kuaishou.com",
  );
  expect(frames[1].contentWindow?.postMessage).not.toHaveBeenCalled();
  frames[0].setAttribute("src", "https://passport.kuaishou.com/unrelated");
  expect(await env.tick()).not.toMatchObject({ image: qr });
});

it("快手 passport 子页面只向 cp 回传二维码并只接受 cp 顶层刷新", async () => {
  const env = environment(kuaishouLogin, kuaishouQr, true);
  await env.tick();
  expect(env.top.postMessage).toHaveBeenLastCalledWith(
    expect.objectContaining({
      type: "xiangshu-publish-qr",
      phase: "qr_ready",
      image: qr,
    }),
    "https://cp.kuaishou.com",
  );
  env.top.postMessage.mockClear();
  const receive = env.listeners.get("message");
  const data = { type: "xiangshu-read-publish-qr" };
  receive?.({
    origin: "https://channels.weixin.qq.com",
    source: env.top,
    data,
  });
  receive?.({ origin: "https://cp.kuaishou.com", source: {}, data });
  expect(env.top.postMessage).not.toHaveBeenCalled();
  receive?.({ origin: "https://cp.kuaishou.com", source: env.top, data });
  expect(env.top.postMessage).toHaveBeenCalledTimes(1);
});

it("快手登录页变成其他路径时主动读取也不回传旧码", async () => {
  const env = environment(kuaishouLogin, kuaishouQr);
  expect(await env.tick()).toMatchObject({ phase: "qr_ready" });
  env.location.pathname = "/unrelated";
  expect((env.win.__xiangshuReadPublishLogin as () => unknown)()).toMatchObject(
    {
      phase: "action_required",
      image: null,
    },
  );
});

it("快手上传按钮和创作者页面图片不产生登录身份或二维码", async () => {
  const env = environment(
    "https://cp.kuaishou.com/article/publish/video",
    `<button>上传视频</button>${kuaishouQr}`,
  );
  expect(await env.tick()).toMatchObject({ phase: "loading", image: null });
  expect(env.win.__xiangshuPublishIdentity).toBeUndefined();
});
