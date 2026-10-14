import { createContext, type ReactNode, useContext } from "react";

/**
 * 权限点判断（整改 T41）：取代各页面零散的 readOnly 布尔。
 *
 * - 读点（``*.read``）决定菜单与页面是否出现；
 * - 写点（``funds.write`` 等）与导出点（``export``）决定按钮是否可用，服务端同样按点拒绝，
 *   这里只负责不把用不了的按钮摆出来。
 *
 * 没有 Provider（单测里单独渲染某个页面）时放行一切：页面自身的 readOnly 与服务端
 * 仍然是真正的闸门，Provider 缺席不应让既有页面测试全部失效。
 */
export type CanFn = (point: string) => boolean;

const allowAll: CanFn = () => true;

const PermissionContext = createContext<CanFn>(allowAll);

export function AdminPermissionsProvider({
  can,
  children,
}: {
  can: CanFn;
  children: ReactNode;
}) {
  return (
    <PermissionContext.Provider value={can}>
      {children}
    </PermissionContext.Provider>
  );
}

export function useCan(): CanFn {
  return useContext(PermissionContext);
}

/**
 * 会话里没有 permissions 字段（旧夹具 / 旧服务端）时的回退：管理员全部放行，
 * 审计员只有读点与导出——与升级前的角色语义一致。
 */
export function canFromActor(actor: {
  role: string;
  permissions?: string[];
}): CanFn {
  const permissions = actor.permissions;
  if (permissions) {
    const granted = new Set(permissions);
    return (point) => granted.has(point);
  }
  return (point) =>
    actor.role === "admin" || point === "export" || point.endsWith(".read");
}
