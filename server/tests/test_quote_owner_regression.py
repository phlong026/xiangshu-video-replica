"""直接验证既有口播报价入口，避免母子账号报价和受理折扣分叉。"""

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

from cryptography.fernet import Fernet


def test_oral_quote_prices_the_paying_wallet(monkeypatch: Any) -> None:
    from app import billing_catalog, oral_routes

    calls: list[str | None] = []

    def snapshot(
        conn: Any, service: str, units: Any, *, user_id: str | None = None
    ) -> dict[str, Any]:
        calls.append(user_id)
        return {
            "service": service,
            "version": 1,
            "unit": "second",
            "units": str(units),
            "enabled": True,
            "unit_credits": "2",
            "unit_rounding": "ceil",
            "credits": int(units) if user_id == "owner" else int(units) * 2,
            "discount_basis_points": 5000 if user_id == "owner" else 10000,
            "discount_rate": "0.5000" if user_id == "owner" else None,
            "discount_source": "manual",
            "consumption_rounding": "ceil",
            "points_per_yuan": 100,
            "free_reason": None,
        }

    class Conn:
        def execute(self, query: str, params: Any = None) -> Any:
            row = (datetime.now(UTC),) if "clock_timestamp" in query else ("owner",)
            return SimpleNamespace(fetchone=lambda: row)

    monkeypatch.setattr(billing_catalog, "retail_snapshot", snapshot)
    # 新合同模块存在时替换其导入别名；旧基线仍可直接运行本回归。
    try:
        from app import quote_contracts
    except ImportError:
        pass
    else:
        monkeypatch.setattr(quote_contracts, "retail_snapshot", snapshot)
    monkeypatch.setenv("VIDEO_REPLICA_SETTINGS_KEY", Fernet.generate_key().decode())
    result = oral_routes.read_oral_budget(
        oral_routes.OralBudgetRequest(script_text="一二三四五六七八"),
        Conn(),
        SimpleNamespace(id="child"),
    )
    assert result["estimated_credits"] == 4
    assert result["unit_credits"] == 1
    assert set(calls) == {"owner"}
