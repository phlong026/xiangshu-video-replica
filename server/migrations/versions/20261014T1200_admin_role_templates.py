"""管理端整改 T41：权限点与角色模板。

- ``admin_role_templates``：模板 → 权限点（JSONB 数组）。后端鉴权按权限点判断，模板内容只在
  这里维护，前端的菜单与按钮可见性读同一份（登录会话返回 ``permissions``）。
- ``users.admin_template``：管理端账号所属模板；NULL 表示沿用旧语义（超管 → super_admin，
  其它管理员 → operations，审计员 → auditor），所以升级前后已有账号的权限不变。

权限点（决策 D4 先分步覆盖资金、导出、设置，同时给内容与客户建立写点）：
读点  business.read funds.read customer.read generation.read content.read settings.read
      audit.read —— 目前控制菜单可见性；
写点  funds.write settings.write customer.write content.write export —— 服务端按点拒绝。

只追加表与列，不改已发布 revision。
"""

import json

from alembic import op

revision = "20261014T1200_admin_role_templates"
down_revision = "20261014T1100_customer_ops_params"
branch_labels = None
depends_on = None

_READS = [
    "business.read",
    "funds.read",
    "customer.read",
    "generation.read",
    "content.read",
    "settings.read",
    "audit.read",
]
_WRITES = ["funds.write", "settings.write", "customer.write", "content.write", "export"]

_TEMPLATES = [
    ("super_admin", "超级管理员", _READS + _WRITES),
    # 运营 = 升级前「普通管理员」的全部能力（不含团队与技术配置），所以存量账号权限不变。
    ("operations", "运营", _READS + _WRITES),
    (
        "finance",
        "财务",
        ["business.read", "funds.read", "customer.read", "settings.read"]
        + ["funds.write", "settings.write", "export"],
    ),
    (
        "content",
        "内容",
        ["generation.read", "content.read", "customer.read", "content.write"],
    ),
    ("auditor", "审计员", _READS + ["export"]),
]


def upgrade() -> None:
    # These additions belong to the PG runtime, not offline legacy source snapshots.
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(
        "CREATE TABLE admin_role_templates ("
        "template TEXT PRIMARY KEY, "
        "label TEXT NOT NULL, "
        "permissions JSONB NOT NULL DEFAULT '[]'::jsonb, "
        "CONSTRAINT ck_admin_role_templates_permissions "
        "CHECK (jsonb_typeof(permissions) = 'array'))"
    )
    for template, label, permissions in _TEMPLATES:
        op.execute(
            "INSERT INTO admin_role_templates (template, label, permissions) VALUES ("
            f"'{template}', '{label}', '{json.dumps(permissions)}'::jsonb)"
        )
    op.execute(
        "ALTER TABLE users ADD COLUMN admin_template TEXT REFERENCES admin_role_templates(template)"
    )


def downgrade() -> None:
    # These additions belong to the PG runtime, not offline legacy source snapshots.
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("ALTER TABLE users DROP COLUMN admin_template")
    op.execute("DROP TABLE admin_role_templates")
