"""客户运营参数（整改 T29）：低余额阈值与健康分预警线的读取。

客户列表「余额不足」与客户运营「健康分」共用同一份参数，存在 runtime_settings 单行
里；读取不依赖任何路由模块，客户列表与生命周期路由都可直接引用，避免循环依赖。
"""

from __future__ import annotations

from typing import TypedDict

import psycopg

from app.db_pg import MissingDatabaseConfigError, pg_transaction

DEFAULT_LOW_BALANCE_THRESHOLD = 100
DEFAULT_HEALTH_ALERT_SCORE = 40
LOW_BALANCE_THRESHOLD_MAX = 1_000_000


class CustomerOpsParams(TypedDict):
    low_balance_threshold: int
    health_alert_score: int


def read_customer_ops_params(conn: psycopg.Connection) -> CustomerOpsParams:
    """单行表可能尚未写入（全新库）：缺行按默认值，与迁移列默认一致。"""
    row = conn.execute(
        "SELECT customer_low_balance_threshold, customer_health_alert_score "
        "FROM runtime_settings WHERE id=1"
    ).fetchone()
    if row is None:
        return {
            "low_balance_threshold": DEFAULT_LOW_BALANCE_THRESHOLD,
            "health_alert_score": DEFAULT_HEALTH_ALERT_SCORE,
        }
    return {"low_balance_threshold": int(row[0]), "health_alert_score": int(row[1])}


def effective_low_balance_threshold(override: int | None) -> int:
    """显式传入的阈值优先（临时试算），否则取持久化参数。

    数据库不可用时回落默认值：调用方随后的主查询会按自己的口径报 503，
    这里不重复抛错。
    """
    if override is not None:
        return override
    try:
        with pg_transaction() as conn:
            return read_customer_ops_params(conn)["low_balance_threshold"]
    except (RuntimeError, MissingDatabaseConfigError):
        return DEFAULT_LOW_BALANCE_THRESHOLD
