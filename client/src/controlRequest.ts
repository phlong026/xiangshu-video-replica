/** 控制面请求骨架的共享工厂。
 *
 * 2026-10-31 前 api.ts 与 api.admin.ts 各持一份 requestControl，骨架相同
 * （CSRF 快照 → 超时 AbortController → JSON Content-Type → 带 base 前缀的
 * fetch → 401 通知 → 超时错误翻译），策略却在两处悄悄分叉：默认超时
 * 5s/10s、api.ts 对写方法自动补 X-Admin-CSRF、api.admin 对三条登录/换登/
 * 会话读路径豁免 401 通知、超时错误类型不同。收敛骨架、把策略变成显式
 * 配置，之后任何一侧行为变化都能在 diff 里直接看到。
 *
 * 会话函数（CSRF 读取/过期通知）由调用方注入：它们的实现住在 api.ts，
 * 本模块若直接 import 会形成 api.ts → controlRequest → api.ts 的环。
 */

export interface ControlRequestConfig {
  /** 未显式传 timeoutMs 时的默认超时（毫秒）。 */
  defaultTimeoutMs: number;
  /** API 基址解析（来自 apiBase.ts）。 */
  baseUrl: () => string;
  /** 请求开始前读取一次 CSRF（会话过期通知需要比对前后值）。 */
  csrfSnapshot: () => string | null;
  /** 401 时的会话过期通知。 */
  onUnauthorized: (csrfAtStart: string | null) => void;
  /** 返回 true 的路径不触发 401 通知（登录/换登表单自行处理 401）。 */
  unauthorizedExempt?: (path: string, init: RequestInit) => boolean;
  /** 写方法缺省时是否自动补 X-Admin-CSRF（api.ts 侧 true；api.admin 的
   * 写路径全部经 adminWrite 显式带头，保持 false 不引入隐式行为）。 */
  autoCsrfForWrites: boolean;
  /** 超时错误的构造器——两侧抛的错误类型不同，保持各自契约。 */
  timeoutError: (message: string) => Error;
}

export function createControlRequest(config: ControlRequestConfig) {
  return async function requestControl(
    path: string,
    init: RequestInit,
    timeoutMs: number = config.defaultTimeoutMs,
  ): Promise<Response> {
    const csrfAtStart = config.csrfSnapshot();
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), timeoutMs);
    const headers = new Headers(init.headers);
    if (init.body && !(init.body instanceof FormData)) {
      headers.set("Content-Type", "application/json");
    }
    if (
      config.autoCsrfForWrites &&
      init.method &&
      ["POST", "PUT", "PATCH", "DELETE"].includes(init.method.toUpperCase()) &&
      !headers.has("X-Admin-CSRF")
    ) {
      const csrf = config.csrfSnapshot();
      if (csrf) {
        headers.set("X-Admin-CSRF", csrf);
      }
    }
    try {
      const response = await fetch(`${config.baseUrl()}${path}`, {
        ...init,
        headers,
        credentials: "include",
        signal: controller.signal,
      });
      if (
        response.status === 401 &&
        !(config.unauthorizedExempt?.(path, init) ?? false)
      ) {
        config.onUnauthorized(csrfAtStart);
      }
      return response;
    } catch (error) {
      if (error instanceof DOMException && error.name === "AbortError") {
        throw config.timeoutError("请求超时，请重试");
      }
      throw error;
    } finally {
      window.clearTimeout(timeout);
    }
  };
}
