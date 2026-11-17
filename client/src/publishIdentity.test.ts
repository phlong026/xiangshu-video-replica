import { runInNewContext } from "node:vm";
import { expect, it, vi } from "vitest";

import script from "../src-tauri/src/publish_identity.js?raw";

const privateFixture = ["synthetic", "do-not-forward"].join("-");

function environment(
  origin: string,
  path: string,
  body: unknown,
  options: {
    timers?: boolean;
    responseUrl?: string;
    requestUrl?: string;
    method?: string;
    status?: number;
    redirected?: boolean;
    json?: () => Promise<unknown>;
    child?: boolean;
  } = {},
) {
  const location = new URL(origin);
  let responseBody = body;
  const clone = vi.fn(() => ({
    json: options.json ?? (async () => responseBody),
  }));
  const requested: string[] = [];
  const pageFetch = vi.fn(async (input?: unknown, _init?: unknown) => {
    if (typeof input === "string") requested.push(input);
    const status = options.status ?? 200;
    return {
      url: options.responseUrl ?? origin + path,
      status,
      ok: status >= 200 && status < 300,
      redirected: options.redirected ?? false,
      clone,
    };
  });
  const listeners = new Map<string, () => void>();
  const history = {
    pushState: (_state: unknown, _title: string, url: string) => {
      location.href = new URL(url, location.href).href;
    },
    replaceState: (_state: unknown, _title: string, url: string) => {
      location.href = new URL(url, location.href).href;
    },
  };
  const win: Record<string, unknown> = {
    fetch: pageFetch,
    history,
    addEventListener: (event: string, handler: () => void) =>
      listeners.set(event, handler),
  };
  win.top = options.child ? {} : win;
  let scheduled: (() => void) | undefined;
  if (options.timers) {
    win.setInterval = (handler: () => void) => {
      scheduled = handler;
      return 1;
    };
    win.clearInterval = () => {
      scheduled = undefined;
    };
  }
  class XHR {
    status = options.status ?? 200;
    responseURL = options.responseUrl ?? origin + path;
    responseType = "json";
    response = body;
    responseText = JSON.stringify(body);
    handlers = new Map<string, () => void>();
    open(_method: string, _url: string) {}
    addEventListener(event: string, handler: () => void) {
      this.handlers.set(event, handler);
    }
    load() {
      this.handlers.get("load")?.();
    }
  }
  runInNewContext(script, { window: win, location, URL, XMLHttpRequest: XHR });
  const settle = async () => {
    await Promise.resolve();
    await Promise.resolve();
    await Promise.resolve();
  };
  return {
    win,
    clone,
    requested,
    pageFetch,
    history,
    location,
    listeners,
    XHR,
    settle,
    setBody: (next: unknown) => {
      responseBody = next;
    },
    identity: () =>
      (win.__xiangshuReadPublishIdentity as (() => unknown) | undefined)?.(),
    tick: async () => {
      scheduled?.();
      await settle();
      return win.__xiangshuPublishIdentity;
    },
    scheduled: () => scheduled,
    read: async () => {
      await (win.fetch as (...args: unknown[]) => Promise<unknown>)(
        options.requestUrl ?? origin + path,
        {
          method:
            options.method ??
            (origin === "https://cp.kuaishou.com" ? "POST" : "GET"),
        },
      );
      await settle();
      return win.__xiangshuPublishIdentity;
    },
  };
}

it.each([
  [
    "https://creator.douyin.com",
    "/web/api/media/user/info/",
    {
      status_code: 0,
      user: {
        uid: "u1",
        nickname: "抖音用户",
        cookie: privateFixture,
      },
    },
    "抖音用户",
  ],
  [
    "https://channels.weixin.qq.com",
    "/cgi-bin/mmfinderassistant-bin/auth/auth_data",
    {
      errCode: 0,
      data: {
        finderUser: { finderUsername: "u1", nickname: "视频号用户" },
        authKey: privateFixture,
      },
    },
    "视频号用户",
  ],
  [
    "https://creator.xiaohongshu.com",
    "/api/galaxy/user/info",
    {
      success: true,
      data: {
        userId: "u1",
        userName: "小红书用户",
        token: privateFixture,
      },
    },
    "小红书用户",
  ],
])(
  "官方账号响应只提取 UID 和用户名：%s",
  async (origin, path, body, username) => {
    const result = await environment(origin, path, body).read();
    expect(result).toEqual({ platform_user_id: "u1", username });
    expect(JSON.stringify(result)).not.toContain(privateFixture);
  },
);

it("不读取其他用户资料接口或失败登录响应", async () => {
  const other = environment("https://creator.douyin.com", "/api/other-user", {
    status_code: 0,
    user: { uid: "u1", nickname: "其他人" },
  });
  expect(await other.read()).toBeUndefined();
  expect(other.clone).not.toHaveBeenCalled();
  expect(
    await environment(
      "https://creator.douyin.com",
      "/web/api/media/user/info/",
      { status_code: 8, user: { uid: "u1", nickname: "未登录" } },
    ).read(),
  ).toBeUndefined();
});

it("抖音在页面自身不发身份请求时主动复核并保存账号", async () => {
  const douyin = environment(
    "https://creator.douyin.com",
    "/web/api/media/user/info/",
    { status_code: 0, user: { uid: "u1", nickname: "抖音用户" } },
    { timers: true },
  );
  // 扫码后的跳转使被动拦截落空：页面没有发起过任何请求。
  expect(douyin.win.__xiangshuPublishIdentity).toBeUndefined();
  expect(await douyin.tick()).toEqual({
    platform_user_id: "u1",
    username: "抖音用户",
  });
  expect(douyin.requested).toEqual(["/web/api/media/user/info/?aid=1128"]);
});

it("抖音识别成功后停止主动复核", async () => {
  const douyin = environment(
    "https://creator.douyin.com",
    "/web/api/media/user/info/",
    { status_code: 0, user: { uid: "u1", nickname: "抖音用户" } },
    { timers: true },
  );
  await douyin.tick();
  await douyin.tick();
  expect(douyin.scheduled()).toBeUndefined();
  expect(douyin.requested).toHaveLength(1);
});

it("只有抖音启动主动复核", async () => {
  for (const [origin, path] of [
    ["https://channels.weixin.qq.com", "/auth/auth_data"],
    ["https://creator.xiaohongshu.com", "/api/galaxy/user/info"],
  ]) {
    const other = environment(origin, path, {}, { timers: true });
    expect(other.scheduled()).toBeUndefined();
    expect(other.requested).toEqual([]);
  }
});

it.each([
  [
    { url_list: ["https://p26.douyinpic.com/large.jpeg"] },
    undefined,
    undefined,
    "https://p26.douyinpic.com/large.jpeg",
  ],
  [
    undefined,
    { url_list: ["https://p26.douyinpic.com/thumb.jpeg"] },
    undefined,
    "https://p26.douyinpic.com/thumb.jpeg",
  ],
  [
    undefined,
    undefined,
    "https://p26.douyinpic.com/flat.jpeg",
    "https://p26.douyinpic.com/flat.jpeg",
  ],
])(
  "抖音从身份响应中提取头像，按 larger → thumb → url 回退",
  async (avatar_larger, avatar_thumb, avatar_url, expected) => {
    const result = await environment(
      "https://creator.douyin.com",
      "/web/api/media/user/info/",
      {
        status_code: 0,
        user: {
          uid: "u1",
          nickname: "抖音用户",
          avatar_larger,
          avatar_thumb,
          avatar_url,
        },
      },
    ).read();
    expect(result).toEqual({
      platform_user_id: "u1",
      username: "抖音用户",
      avatar_url: expected,
    });
  },
);

it("视频号从身份响应中提取头像", async () => {
  const result = await environment(
    "https://channels.weixin.qq.com",
    "/auth/auth_data",
    {
      errCode: 0,
      data: {
        finderUser: {
          finderUsername: "u1",
          nickname: "视频号用户",
          headImgUrl: "https://wx.qlogo.cn/finderhead/abc/0",
        },
      },
    },
  ).read();
  expect(result).toEqual({
    platform_user_id: "u1",
    username: "视频号用户",
    avatar_url: "https://wx.qlogo.cn/finderhead/abc/0",
  });
});

it.each([
  ["http://p26.douyinpic.com/insecure.jpeg"],
  ["data:image/png;base64,iVBORw0KGgo="],
  ["javascript:alert(1)"],
  [`https://p26.douyinpic.com/${"a".repeat(1200)}.jpeg`],
])("拒绝非 https 或超长的头像地址：%s", async (avatar_url) => {
  const result = await environment(
    "https://creator.douyin.com",
    "/web/api/media/user/info/",
    { status_code: 0, user: { uid: "u1", nickname: "抖音用户", avatar_url } },
  ).read();
  expect(result).toEqual({ platform_user_id: "u1", username: "抖音用户" });
});

it("不在相似域名注入或读取账号", async () => {
  const other = environment(
    "https://creator.douyin.com.evil.test",
    "/web/api/media/user/info/",
    { status_code: 0, user: { uid: "u1", nickname: "假响应" } },
  );
  expect(await other.read()).toBeUndefined();
  expect(other.clone).not.toHaveBeenCalled();
});

const kuaishouOrigin = "https://cp.kuaishou.com";
const kuaishouEndpoint = "/rest/v2/creator/pc/authority/account/current";
const kuaishouBody = {
  result: 1,
  data: {
    userId: 123456,
    userName: "快手昵称",
    userKwaiId: "不是内部UID",
    userAvatar: "https://p3.a.kwimgs.com/synthetic-avatar.jpeg",
    logined: true,
    session: privateFixture,
  },
};

it("快手只从官方 bootstrap POST 提取真实 userId、userName 与公开头像", async () => {
  const env = environment(kuaishouOrigin, kuaishouEndpoint, kuaishouBody, {
    timers: true,
  });
  const expected = {
    platform_user_id: "123456",
    username: "快手昵称",
    avatar_url: "https://p3.a.kwimgs.com/synthetic-avatar.jpeg",
  };
  expect(await env.read()).toEqual(expected);
  expect(env.identity()).toEqual(expected);
  expect(env.pageFetch).toHaveBeenCalledWith(
    kuaishouOrigin + kuaishouEndpoint,
    {
      method: "POST",
      redirect: "error",
    },
  );
  expect(JSON.stringify(env.identity())).not.toContain(privateFixture);
  expect(env.scheduled()).toBeUndefined();
});

it.each([
  { result: "1", data: kuaishouBody.data },
  { result: true, data: kuaishouBody.data },
  { result: 109, data: kuaishouBody.data },
  { result: 1, data: { ...kuaishouBody.data, logined: false } },
  { result: 1, data: { ...kuaishouBody.data, userId: 0 } },
  { result: 1, data: { ...kuaishouBody.data, userId: "0" } },
  { result: 1, data: { ...kuaishouBody.data, userId: false } },
  { result: 1, data: { ...kuaishouBody.data, userId: 9007199254740992 } },
  { result: 1, data: { ...kuaishouBody.data, userId: " " } },
  { result: 1, data: { ...kuaishouBody.data, userName: " " } },
  { result: 1, data: { ...kuaishouBody.data, userName: "a".repeat(257) } },
  { result: 1, data: { userKwaiId: "假UID", nickname: "假昵称" } },
  { result: 1, data: null },
])("快手拒绝默认身份、失败与未知响应 %#", async (body) => {
  const env = environment(kuaishouOrigin, kuaishouEndpoint, body);
  await env.read();
  expect(env.identity()).toBeNull();
});

it.each([
  "/rest/cp/works/v2/common/pc/current/user",
  "/rest/cp/creator/pc/home/infoV2",
  `/prefix${kuaishouEndpoint}`,
  `${kuaishouEndpoint}/`,
])("快手其他接口即使携带 userId 也不产生身份：%s", async (path) => {
  const env = environment(kuaishouOrigin, path, kuaishouBody);
  expect(await env.read()).toBeUndefined();
  expect(env.clone).not.toHaveBeenCalled();
  expect(env.identity()).toBeNull();
});

it.each([
  { method: "GET" },
  { responseUrl: `https://evil.test${kuaishouEndpoint}` },
  { requestUrl: `https://evil.test${kuaishouEndpoint}` },
  { requestUrl: `https://cp.kuaishou.com.evil.test${kuaishouEndpoint}` },
  { responseUrl: `https://passport.kuaishou.com${kuaishouEndpoint}` },
  { redirected: true },
  { status: 302 },
  { status: 401 },
  { status: 429 },
  { status: 503 },
])("快手验证原请求、最终响应来源和 HTTP 状态 %#", async (options) => {
  const env = environment(
    kuaishouOrigin,
    kuaishouEndpoint,
    kuaishouBody,
    options,
  );
  expect(await env.read()).toBeUndefined();
  expect(env.clone).not.toHaveBeenCalled();
  expect(env.identity()).toBeNull();
});

it.each([
  "https://passport.kuaishou.com",
  "https://cp.kuaishou.com.evil.test",
  "http://cp.kuaishou.com",
  "https://sub.cp.kuaishou.com",
])("登录域或近似域不能确认快手身份：%s", async (origin) => {
  const env = environment(origin, kuaishouEndpoint, kuaishouBody);
  expect(await env.read()).toBeUndefined();
  expect(env.clone).not.toHaveBeenCalled();
  expect(env.identity()).toBeUndefined();
});

it("快手只在顶层创作者文档安装身份捕获", async () => {
  const env = environment(kuaishouOrigin, kuaishouEndpoint, kuaishouBody, {
    child: true,
  });
  expect(await env.read()).toBeUndefined();
  expect(env.clone).not.toHaveBeenCalled();
  expect(env.identity()).toBeUndefined();
});

it("快手 JSON 迟到回执在导航后失效，返回同 URL 也不能复用", async () => {
  let resolve: (value: unknown) => void = () => {};
  const pending = new Promise((done) => {
    resolve = done;
  });
  const env = environment(kuaishouOrigin, kuaishouEndpoint, kuaishouBody, {
    json: () => pending,
  });
  await env.read();
  env.history.pushState(null, "", "/article/other");
  env.history.replaceState(null, "", "/");
  resolve(kuaishouBody);
  await env.settle();
  expect(env.identity()).toBeNull();
});

it("快手清除明确未登录身份，且不接受页面伪造或改写 UID", async () => {
  const env = environment(kuaishouOrigin, kuaishouEndpoint, kuaishouBody);
  env.win.__xiangshuPublishIdentity = {
    platform_user_id: "forged",
    username: "伪造账号",
  };
  expect(env.identity()).toBeNull();
  await env.read();
  const copy = env.identity() as { platform_user_id: string };
  copy.platform_user_id = "modified";
  env.win.__xiangshuPublishIdentity = copy;
  expect(env.identity()).toMatchObject({ platform_user_id: "123456" });
  env.setBody({ result: 1, data: { logined: false } });
  await env.read();
  expect(env.identity()).toBeNull();
});

it("快手 XHR 同时核实原始 POST 和最终回执来源", () => {
  const good = environment(kuaishouOrigin, kuaishouEndpoint, kuaishouBody);
  const xhr = new good.XHR();
  xhr.open("POST", kuaishouEndpoint);
  xhr.load();
  expect(good.identity()).toMatchObject({ platform_user_id: "123456" });
  for (const requestUrl of [
    `https://evil.test${kuaishouEndpoint}`,
    "/rest/cp/works/v2/common/pc/current/user",
  ]) {
    const env = environment(kuaishouOrigin, kuaishouEndpoint, kuaishouBody);
    const request = new env.XHR();
    request.open("POST", requestUrl);
    request.load();
    expect(env.identity()).toBeNull();
  }
  const redirected = environment(
    kuaishouOrigin,
    kuaishouEndpoint,
    kuaishouBody,
    {
      responseUrl: `https://passport.kuaishou.com${kuaishouEndpoint}`,
    },
  );
  const request = new redirected.XHR();
  request.open("POST", kuaishouEndpoint);
  request.load();
  expect(redirected.identity()).toBeNull();
});

it("快手 XHR 加载中导航使旧回执失效", () => {
  const env = environment(kuaishouOrigin, kuaishouEndpoint, kuaishouBody);
  const request = new env.XHR();
  request.open("POST", kuaishouEndpoint);
  env.history.pushState(null, "", "/article/publish/video");
  request.load();
  expect(env.identity()).toBeNull();
});
