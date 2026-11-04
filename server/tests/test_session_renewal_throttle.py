"""会话续期双节流与登录限流占位返还的单元测试（2026-11-03 会话 P1 批）。

不依赖 PG 夹具：用记录式假连接验证 `renew_session_expiry` 的节流决策
矩阵与 `refund_rate_limit` 的返还语义。真库行为（在线谓词联动、字段级
不写断言）在 `test_customer_sessions.py` 的 PG 用例中覆盖。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

from app.customer_session_service import (
    SESSION_ACTIVITY_WRITE_INTERVAL_SECONDS,
    SESSION_RENEWAL_WRITE_INTERVAL_SECONDS,
    SESSION_VALIDITY_SECONDS,
    renew_session_expiry,
)


class RecordingConn:
    """捕获 execute 调用的最小假连接（不做任何数据库语义）。"""

    def __init__(self) -> None:
        self.statements: list[tuple[str, tuple[Any, ...]]] = []
        # RETURNING 路径取回的行；默认 7 天候选值，供续期用例断言。
        self.returned_row: tuple[Any, ...] = (_now() + timedelta(seconds=SESSION_VALIDITY_SECONDS),)

    def execute(self, sql: str, params: Any = None) -> Any:
        self.statements.append((" ".join(sql.split()), tuple(params or ())))
        return SimpleNamespace(rowcount=1, fetchone=lambda: self.returned_row)


def _now() -> datetime:
    return datetime(2026, 11, 3, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# S1：活动节流与续期节流分离
# ---------------------------------------------------------------------------


def test_fresh_session_writes_nothing() -> None:
    """租约新鲜且活动时间在窗口内：零写入（逐请求零额外写负担不变）。"""
    conn = RecordingConn()
    result = renew_session_expiry(
        conn,
        session_id="s1",
        lease_until=_now() + timedelta(days=7),
        last_seen_at=_now() - timedelta(seconds=30),
        now=_now(),
    )
    assert result is None
    assert conn.statements == []


def test_stale_activity_writes_seen_without_renewal() -> None:
    """S1 核心：未到续期阈值但活动时间超过 5 分钟 → 只写活动，租约不动。"""
    conn = RecordingConn()
    result = renew_session_expiry(
        conn,
        session_id="s1",
        lease_until=_now() + timedelta(days=7),
        last_seen_at=_now() - timedelta(seconds=SESSION_ACTIVITY_WRITE_INTERVAL_SECONDS + 1),
        now=_now(),
    )
    assert result is None
    assert len(conn.statements) == 1
    statement, _ = conn.statements[0]
    # 活动写不得触碰 lease_until：续期与活动是两个独立节流维度。
    assert "lease_until" not in statement.split("SET ", 1)[1].split(" WHERE")[0]
    assert "last_seen_at" in statement


def test_null_last_seen_counts_as_stale_activity() -> None:
    """历史行 last_seen_at 为 NULL：视为需要活动写（初始化该列）。"""
    conn = RecordingConn()
    result = renew_session_expiry(
        conn,
        session_id="s1",
        lease_until=_now() + timedelta(days=7),
        last_seen_at=None,
        now=_now(),
    )
    assert result is None
    assert len(conn.statements) == 1
    statement, _ = conn.statements[0]
    assert "last_seen_at" in statement


def test_due_renewal_writes_lease_and_activity() -> None:
    """租约落后超过（7 天 - 1 小时）阈值：续期写同时刷新活动时间。"""
    conn = RecordingConn()
    aged = _now() + timedelta(
        seconds=SESSION_VALIDITY_SECONDS - SESSION_RENEWAL_WRITE_INTERVAL_SECONDS - 60
    )
    result = renew_session_expiry(
        conn,
        session_id="s1",
        lease_until=aged,
        last_seen_at=_now() - timedelta(seconds=30),
        now=_now(),
    )
    assert result is not None
    expected = (
        (_now() + timedelta(seconds=SESSION_VALIDITY_SECONDS)).replace(microsecond=0).isoformat()
    )
    assert result == expected
    assert len(conn.statements) == 1
    statement, params = conn.statements[0]
    assert "lease_until" in statement and "GREATEST" in statement
    assert params[0] == expected


# ---------------------------------------------------------------------------
# S2：租约防回缩
# ---------------------------------------------------------------------------


def test_renewal_keeps_longer_existing_lease() -> None:
    """现值租约长于 now+7d（时钟回拨场景）：不触发续期写，不缩短。"""
    conn = RecordingConn()
    result = renew_session_expiry(
        conn,
        session_id="s1",
        lease_until=_now() + timedelta(days=8),
        last_seen_at=_now() - timedelta(seconds=30),
        now=_now(),
    )
    assert result is None
    assert conn.statements == []


def test_login_and_heartbeat_paths_share_the_anti_shrink_expression() -> None:
    """login/heartbeat/renew 三处写点的防回缩由同一表达式保证（源码级钉住）。

    三处 UPDATE 均为 lease_until = GREATEST(...) 形态：单元层无法驱动完整
    登录/心跳状态机（PG 用例覆盖行为面），此处把静态契约钉住，防止后续
    改动悄悄退回裸写。
    """
    import inspect
    import re

    from app import customer_session_service

    source = inspect.getsource(customer_session_service)
    greatease_writes = re.findall(
        r"SET lease_until = GREATEST\(lease_until::timestamptz, %s::timestamptz\)",
        source,
    )
    assert len(greatease_writes) == 3, (
        "login 续租 / heartbeat / renew_session_expiry 三处租约写都必须是 "
        f"GREATEST 防回缩形态，实际 {len(greatease_writes)} 处"
    )


# ---------------------------------------------------------------------------
# S3：refund_rate_limit 返还语义
# ---------------------------------------------------------------------------


def test_refund_decrements_only_live_windows() -> None:
    from app.security_rate_limit import refund_rate_limit

    conn = RecordingConn()
    assert refund_rate_limit(
        conn,
        dimension="login:account",
        identifier="acct-1",
        window_start="2026-11-03T12:00:00+00:00",
        now=_now(),
    )
    statement, params = conn.statements[0]
    assert "hit_count - 1" in statement
    # 返还精确匹配占位所属窗口（window_start =），跨窗口不串扰。
    assert "window_start = %s" in statement and "hit_count > 0" in statement
    # 参数序：(updated_at, bucket_key, window_start)。
    assert len(params) == 3
    assert params[2] == "2026-11-03T12:00:00+00:00"


def test_refund_rejects_unknown_dimension() -> None:
    import pytest

    from app.security_rate_limit import refund_rate_limit

    conn = RecordingConn()
    with pytest.raises(ValueError):
        refund_rate_limit(
            conn,
            dimension="bogus:dimension",
            identifier="x",
            window_start="2026-11-03T12:00:00+00:00",
            now=_now(),
        )
