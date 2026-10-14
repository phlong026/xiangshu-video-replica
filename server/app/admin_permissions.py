"""管理端权限点与角色模板（整改 T41）。

权限点分读、写两类：

- 读点（``*.read``）：目前决定菜单与页面是否出现；业务数据接口仍对所有管理角色可读，
  后续迭代再把服务端读侧按点收紧。
- 写点（``funds.write`` / ``settings.write`` / ``customer.write`` / ``content.write`` /
  ``export``）：服务端依赖按点拒绝，缺点即 403 ``PERMISSION_DENIED``。

模板 → 权限点的对应只在数据库 ``admin_role_templates`` 里维护（迁移 20261014T1200 预置
五个模板），这里不重复一份：会话加载时随用户一并读出，前后端用的是同一份数据。
"""

from __future__ import annotations

FUNDS_WRITE = "funds.write"
SETTINGS_WRITE = "settings.write"
CUSTOMER_WRITE = "customer.write"
CONTENT_WRITE = "content.write"
EXPORT = "export"

# 写点 → 面向操作者的中文名（403 文案用）。
POINT_LABELS = {
    FUNDS_WRITE: "资金操作",
    SETTINGS_WRITE: "系统设置修改",
    CUSTOMER_WRITE: "客户管理操作",
    CONTENT_WRITE: "内容运营操作",
    EXPORT: "数据导出",
}

# 团队新增 / 调整时可选的模板。超级管理员由 is_super_admin 标记决定，不在此列；
# 审计员由 role=auditor 决定。
ASSIGNABLE_ADMIN_TEMPLATES = ("operations", "finance", "content")

# 生效模板：角色与超管标记优先于 users.admin_template，避免出现「审计员却挂了财务模板」
# 这类自相矛盾的数据放大权限；NULL 沿用升级前语义（普通管理员 = 运营）。
EFFECTIVE_TEMPLATE_SQL = (
    "CASE WHEN u.role = 'auditor' THEN 'auditor' "
    "WHEN u.role = 'admin' AND u.is_super_admin = 1 THEN 'super_admin' "
    "ELSE COALESCE(u.admin_template, 'operations') END"
)
