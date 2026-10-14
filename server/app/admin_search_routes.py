"""管理端顶栏全局搜索（整改 T39）：粘贴客户名、订单号或任务号，一步到对应详情。

三类对象各取少量最佳匹配，按「编号精确命中优先、客户模糊匹配最后」排序：

- **订单**：``recharge_orders.merchant_order_no`` 或订单主键精确命中；
- **任务**：视频 / 口播 / 首帧 / 拆解任务主键精确命中（任务号都是系统生成的长随机串，
  不做模糊匹配，避免短串误命中一堆）；
- **客户**：用户名 / 公司名模糊匹配，或客户编号精确命中。

只读、不返回余额等业务指标，只给「是什么、去哪」；响应不带上游服务名或第三方任务号。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query

from app.admin_auth_routes import AdminReader
from app.admin_write_contract import http_error as _http
from app.db_pg import MissingDatabaseConfigError, pg_transaction

router = APIRouter(prefix="/api/control", tags=["admin-search"])

_MAX_PER_KIND = 5
_TASK_TABLES = (
    ("generation_tasks", "视频任务"),
    ("oral_tasks", "口播任务"),
    ("first_frame_tasks", "首帧任务"),
    ("analysis_tasks", "视频拆解任务"),
)


@router.get("/search")
def global_search(
    actor: AdminReader,
    q: Annotated[str, Query(min_length=1, max_length=200)],
) -> dict[str, object]:
    del actor
    term = q.strip()
    if len(term) < 2:
        return {"items": []}
    literal = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    items: list[dict[str, str]] = []
    try:
        with pg_transaction() as conn:
            for row in conn.execute(
                "SELECT ro.merchant_order_no, u.username, ro.status FROM recharge_orders ro "
                "JOIN users u ON u.id = ro.user_id "
                "WHERE ro.merchant_order_no = %s OR ro.id = %s LIMIT %s",
                (term, term, _MAX_PER_KIND),
            ).fetchall():
                items.append(
                    {
                        "type": "order",
                        "id": str(row[0]),
                        "label": f"订单 {row[0]}",
                        "detail": f"{row[1]} · {row[2]}",
                    }
                )
            for table, label in _TASK_TABLES:
                # 表名来自上面的常量元组，不接受外部输入。
                found = conn.execute(
                    f"SELECT id FROM {table} WHERE id = %s LIMIT 1",  # noqa: S608
                    (term,),
                ).fetchone()
                if found is not None:
                    items.append(
                        {
                            "type": "task",
                            "id": str(found[0]),
                            "label": f"{label} {found[0]}",
                            "detail": "生成记录",
                        }
                    )
            for row in conn.execute(
                "SELECT id, username, COALESCE(display_name, '') FROM users "
                "WHERE role = 'customer' AND (id = %s OR username ILIKE %s "
                "OR display_name ILIKE %s) "
                "ORDER BY (id = %s OR username = %s) DESC, username LIMIT %s",
                (term, f"%{literal}%", f"%{literal}%", term, term, _MAX_PER_KIND),
            ).fetchall():
                company = str(row[2]).strip()
                items.append(
                    {
                        "type": "customer",
                        "id": str(row[0]),
                        "label": company or str(row[1]),
                        "detail": f"客户 · {row[1]}" if company else "客户",
                    }
                )
    except (RuntimeError, MissingDatabaseConfigError) as exc:
        raise _http(503, "SEARCH_UNAVAILABLE", "全局搜索需要 PostgreSQL 运行时。") from exc
    return {"items": items}
