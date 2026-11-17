"""llm_json 宽容解析链的单测：每种已知畸变形态一条回归。

形态清单来自生产实测累积（EB1CA38A/T15/各系模型），任何一条回归都意味着
网关或模型行为又变了，需要在解析链上补对应策略——而不是把畸形形态再报
给用户。
"""

from __future__ import annotations

import pytest

from app.llm_json import parse_llm_json


def test_raw_valid_json_uses_raw_strategy() -> None:
    value, strategy = parse_llm_json('{"a": 1, "list": [1, 2]}')
    assert value == {"a": 1, "list": [1, 2]}
    assert strategy == "raw"


def test_raw_newline_injection_is_stripped() -> None:
    # EB1CA38A 生产实测：裸换行注进键名与数字字面量中间。
    broken = '{"schema_version\n": "v1", "resolution": "1080x192\n0"}'
    value, strategy = parse_llm_json(broken)
    assert value == {"schema_version": "v1", "resolution": "1080x1920"}
    assert strategy == "strip_newlines"


def test_markdown_fence_is_stripped() -> None:
    fenced = '```json\n{"ok": true, "n": 1.5}\n```'
    value, strategy = parse_llm_json(fenced)
    assert value == {"ok": True, "n": 1.5}
    assert strategy == "strip_code_fence"


def test_fence_with_injected_newlines_is_stripped() -> None:
    fenced = '```json\n{"ok": tru\ne, "n": 1.5}\n```'
    value, strategy = parse_llm_json(fenced)
    assert value == {"ok": True, "n": 1.5}
    assert strategy == "strip_code_fence_and_newlines"


def test_surrounding_prose_is_extracted() -> None:
    noisy = '好的，以下是结果：\n{"a": 1}\n如有问题请告诉我。'
    value, strategy = parse_llm_json(noisy)
    assert value == {"a": 1}
    assert strategy == "extract_outermost_json"


def test_trailing_comma_is_fixed() -> None:
    trailing = '{"a": [1, 2,], "b": {"c": 3,},}'
    value, strategy = parse_llm_json(trailing)
    assert value == {"a": [1, 2], "b": {"c": 3}}
    assert strategy == "trailing_comma_fix"


def test_escaped_newline_in_values_survives_stripping() -> None:
    # 字符串值内部由 \n 转义承载的换行是内容本身，剥离原始换行不得影响它。
    legit = '{"script": "第一句\\n第二句"}'
    value, strategy = parse_llm_json(legit)
    assert value == {"script": "第一句\n第二句"}
    assert strategy == "raw"


def test_genuinely_broken_json_still_raises() -> None:
    with pytest.raises(Exception):
        parse_llm_json("这不是 JSON，也不含任何对象结构")


def test_escalation_order_prefers_least_destructive() -> None:
    # 既带围栏又带裸换行：优先走围栏剥离（信息无损），而不是直接上尾逗号修复。
    both = '```json\n{"a": 1}\n```'
    value, strategy = parse_llm_json(both)
    assert value == {"a": 1}
    assert strategy == "strip_code_fence"
