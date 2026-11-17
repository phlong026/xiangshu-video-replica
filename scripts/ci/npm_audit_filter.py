"""npm audit 豁免过滤器（CI 依赖审计门配套）.

用法: `npm audit --audit-level=high --json | python3 scripts/ci/npm_audit_filter.py <waived-ids>`

入参为空格分隔的公告 ID（GHSA-…）。stdin 解析 npm audit --json 输出，提取全部
high/critical 公告 ID；全部命中白名单时输出 ``__ONLY_WAIVED__``，否则输出逗号
分隔的未豁免 ID。stdin 不是 JSON 时输出 ``__AUDIT_JSON_PARSE_FAIL__``（调用方
应保守判失败）。

白名单只允许收录「上游没有补丁版本」的公告（advisory Patched: None），并必须
注明跟踪链接与记录日期；上游发布补丁后立即移除并升级锁版本。
"""

from __future__ import annotations

import json
import sys


def _extract_ids(data: dict) -> set[str]:
    ids: set[str] = set()
    advisories = data.get("advisories")
    if isinstance(advisories, dict):
        # 旧版 npm audit --json：advisories 以公告 ID 为键。
        for key, adv in advisories.items():
            url = str((adv or {}).get("url", ""))
            ids.add(url.rsplit("/", 1)[-1] or str(key))
    for vuln in (data.get("vulnerabilities") or {}).values():
        for via in vuln.get("via", []) or []:
            if isinstance(via, dict):
                url = str(via.get("url", ""))
                source = str(via.get("source", ""))
                ids.add(url.rsplit("/", 1)[-1] or source)
    return {item for item in ids if item}


def main() -> int:
    waived = set(sys.argv[1].split()) if len(sys.argv) > 1 and sys.argv[1] else set()
    try:
        data = json.loads(sys.stdin.read())
    except Exception:
        print("__AUDIT_JSON_PARSE_FAIL__")
        return 0
    unwaived = sorted(_extract_ids(data) - waived)
    print(",".join(unwaived) if unwaived else "__ONLY_WAIVED__")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
