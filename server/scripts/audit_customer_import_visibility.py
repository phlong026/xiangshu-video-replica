"""审计「链接导入」分类与发布状态的碰撞：识别历史旁路，禁止盲目批量回置.

2026-11 爆款链路改造（WP4）的一次性审计。背景：客户侧 ``/link-resolutions``
会把粘贴的视频以 ``category='链接导入'`` upsert 进内容池，而通用 upsert 对
既有行的分类是「非空即覆盖」——客户导入一条运营已发布的视频会把它改成
「链接导入」但发布位保留，形成**合法碰撞**；反之，历史上若存在「仅客户导入
来源却开着发布位」的行，才是真正的旁路。两者只凭 ``category`` 无法区分，
必须联合 ``viral_content_sources`` 的来源记录与 ``audit_logs`` 的策展审计
（viral_video.push 等）判定。

判定口径：
- ``LEGITIMATE``：有运营来源记录（admin_link_import / admin_search / collection）
  或有策展审计动作——运营确实发布过，**不得回置**；
- ``SUSPECT``：只有 customer_import 来源且无策展审计——旁路候选，``--apply``
  时回置发布位（collection_published=0, homepage_featured=0, pushed_at=NULL）；
- 在途任务提醒：``viral_refresh_tasks`` 里还有该视频的 publish/feature 待执行
  任务时，回置会被再次发布——即使 ``--apply`` 也跳过并逐条列出，先撤任务再跑。

默认只读预览；写库需要显式 ``--apply``。可重复执行，收敛到零输出即干净。

用法::

    python -m scripts.audit_customer_import_visibility             # 预览
    python -m scripts.audit_customer_import_visibility --apply     # 回置 SUSPECT
"""

from __future__ import annotations

import argparse

import psycopg

from app.db_pg import resolve_cli_pg_dsn

# 运营侧来源：管理端粘贴导入、管理端实时搜索采集、关键词采集（含回填）。
_OPS_SOURCE_KINDS = ("admin_link_import", "admin_search", "collection", "backfill")
# 策展审计动作：push 即「推送到前端」；feature 走 prepare+push 组合，push 必留痕。
_CURATION_ACTIONS = ("viral_video.push",)


def _fetch_candidates(conn: psycopg.Connection) -> list[dict[str, object]]:
    """发布位开着的「链接导入」行 + 逐行来源/策展/在途任务证据."""
    rows = conn.execute(
        """
        SELECT v.platform, v.video_id, v.title, v.collection_published,
               v.homepage_featured, v.pushed_at,
               EXISTS(SELECT 1 FROM viral_content_sources s
                      WHERE s.platform=v.platform AND s.video_id=v.video_id
                        AND s.source_kind = ANY(%s)) AS has_ops_source,
               EXISTS(SELECT 1 FROM audit_logs a
                      WHERE a.entity_type='viral_video'
                        AND a.entity_id=v.platform||':'||v.video_id
                        AND a.action = ANY(%s)) AS has_curation_audit,
               EXISTS(SELECT 1 FROM viral_refresh_tasks t
                      WHERE t.platform=v.platform
                        AND t.status IN ('PENDING','RUNNING')
                        AND t.collection_config_json LIKE '%'||v.video_id||'%') AS has_pending_task
        FROM viral_videos v
        WHERE v.deleted_at IS NULL AND v.category='链接导入'
          AND (v.collection_published=1 OR v.homepage_featured=1
               OR v.pushed_at IS NOT NULL)
        ORDER BY v.platform, v.video_id
        """,
        (_OPS_SOURCE_KINDS, _CURATION_ACTIONS),
    ).fetchall()
    return [dict(row._asdict()) for row in rows]  # noqa: SL001 - psycopg 行转字典


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Audit visibility collisions on customer-imported viral rows."
    )
    parser.add_argument("--apply", action="store_true", help="write; default is preview")
    args = parser.parse_args()
    dsn = resolve_cli_pg_dsn()

    with psycopg.connect(dsn) as conn:
        candidates = _fetch_candidates(conn)

        def is_legitimate(row: dict[str, object]) -> bool:
            return bool(row["has_ops_source"] or row["has_curation_audit"])

        legitimate = [row for row in candidates if is_legitimate(row)]
        suspects = [row for row in candidates if not is_legitimate(row)]
        blocked = [row for row in suspects if row["has_pending_task"]]
        actionable = [row for row in suspects if not row["has_pending_task"]]

        print(f"候选（链接导入 + 发布位开启）：{len(candidates)} 行")
        print(f"  LEGITIMATE（运营发布过的合法碰撞，不动）：{len(legitimate)} 行")
        print(f"  SUSPECT（旁路候选）：{len(suspects)} 行")
        for row in legitimate:
            print(f"    [LEGITIMATE] {row['platform']}/{row['video_id']} {row['title']!r}")
        for row in blocked:
            print(
                f"    [SUSPECT·有在途发布任务，先撤任务再回置] "
                f"{row['platform']}/{row['video_id']} {row['title']!r}"
            )
        for row in actionable:
            print(f"    [SUSPECT] {row['platform']}/{row['video_id']} {row['title']!r}")

        if not args.apply:
            print("预览模式：未写库。加 --apply 回置 SUSPECT 行的发布位。")
            return 0

        applied = 0
        for row in actionable:
            updated = conn.execute(
                """
                UPDATE viral_videos
                SET collection_published=0, homepage_featured=0, pushed_at=NULL,
                    updated_at=CURRENT_TIMESTAMP
                WHERE platform=%s AND video_id=%s AND deleted_at IS NULL
                  AND category='链接导入'
                  AND (collection_published=1 OR homepage_featured=1
                       OR pushed_at IS NOT NULL)
                """,
                (row["platform"], row["video_id"]),
            )
            applied += updated.rowcount
        conn.commit()
        print(f"已回置 {applied} 行（SUSPECT 且无在途任务）；LEGITIMATE 与在途任务行未动。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
