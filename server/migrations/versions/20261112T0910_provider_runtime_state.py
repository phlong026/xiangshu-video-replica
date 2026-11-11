"""供应商运行态共享状态表：123网盘 accessToken 跨进程共享。

Revision ID: 20261112T0910_provider_runtime_state
Revises: 20261112T0900_yuanbao_pan123_widen

123网盘官方限制同一 client_id 同时最多 3 个 token 在用、多次颁发踢旧 token。
部署为多进程（2 个 API 端口实例 + 4 个独立 worker），进程各自颁发必然超限互
踢——token 必须经本表全局共享：advisory lock 单飞颁发 + 各进程缓存，全局同时
只活跃一个 token（docs/培训素材123网盘存储方案-2026-11-12.md §3.3）。

值存 Fernet 加密 JSON（client_id/access_token/expires_at），token 与颁发它的
client_id 绑定存证：切换账号后旧 token 因 client_id 不匹配被弃用，配置保存
护栏同事务原子清除本表 pan123 行。
"""

from alembic import op

revision = "20261112T0910_provider_runtime_state"
down_revision = "20261112T0900_yuanbao_pan123_widen"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("""CREATE TABLE provider_runtime_state (
        provider TEXT NOT NULL,
        state_key TEXT NOT NULL,
        encrypted_value TEXT NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (provider, state_key)
    )""")


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("DROP TABLE provider_runtime_state")
