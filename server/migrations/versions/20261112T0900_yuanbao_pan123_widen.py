"""provider_settings 白名单放宽：纳入 yuanbao（遗留修复）与 pan123（培训素材库）。

Revision ID: 20261112T0900_yuanbao_pan123_widen
Revises: 20261101T1000_training_materials

最后一次白名单放宽是 20260928T1200（+ses）。其后 ``yuanbao`` 进入
``settings.ProviderName``（视频号解析，凭据走 provider_settings）时漏改了
DB CHECK——元宝功能已合 main 未上生产，首次部署保存元宝配置即撞 CHECK。
本迁移同批把 ``yuanbao`` 与 ``pan123``（培训素材域唯一存储车道，
docs/培训素材123网盘存储方案-2026-11-12.md）纳入白名单。

downgrade 契约（fail-fast）：删除 pan123 配置行后重建旧约束；若库内仍有
旧白名单外的行（yuanbao），显式失败并给出受控清理步骤，绝不静默容忍不一致。
"""

from alembic import op

revision = "20261112T0900_yuanbao_pan123_widen"
down_revision = "20261101T1000_training_materials"
branch_labels = None
depends_on = None

_PREVIOUS_LIST = (
    "provider IN ('apilio', 'metaso', 'cos', 'deepseek', 'zpay', "
    "'hifly', 'tikhub', 'dashscope', 'douyidou', 'wechat_native', 'ses')"
)
_WIDENED_LIST = (
    "provider IN ('apilio', 'metaso', 'cos', 'deepseek', 'zpay', "
    "'hifly', 'tikhub', 'dashscope', 'douyidou', 'wechat_native', 'ses', "
    "'yuanbao', 'pan123')"
)


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(
        "ALTER TABLE provider_settings DROP CONSTRAINT ck_provider_settings_supported_provider"
    )
    op.execute(
        "ALTER TABLE provider_settings ADD CONSTRAINT ck_provider_settings_supported_provider "
        f"CHECK ({_WIDENED_LIST})"
    )


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("DELETE FROM provider_settings WHERE provider = 'pan123'")
    # fail-fast：旧白名单外的行（如 yuanbao）仍在时重建旧约束必然失败——
    # 显式报错并给出清理步骤，而不是让 CHECK 以一个含糊的 IntegrityError 爆掉。
    from sqlalchemy.sql import text

    bind = op.get_bind()
    leftover = bind.execute(
        text(
            "SELECT provider FROM provider_settings "
            "WHERE provider NOT IN ('apilio', 'metaso', 'cos', 'deepseek', 'zpay', "
            "'hifly', 'tikhub', 'dashscope', 'douyidou', 'wechat_native', 'ses')"
        )
    ).fetchall()
    if leftover:
        names = ", ".join(sorted(str(row[0]) for row in leftover))
        raise RuntimeError(
            "provider_settings 存在旧白名单之外的配置行，无法回退 CHECK 约束："
            f"{names}。受控清理：确认这些供应商不再使用后删除对应行，再重跑 downgrade。"
        )
    op.execute(
        "ALTER TABLE provider_settings DROP CONSTRAINT ck_provider_settings_supported_provider"
    )
    op.execute(
        "ALTER TABLE provider_settings ADD CONSTRAINT ck_provider_settings_supported_provider "
        f"CHECK ({_PREVIOUS_LIST})"
    )
