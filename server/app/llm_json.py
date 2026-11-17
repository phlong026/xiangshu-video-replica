"""LLM 结构化输出的宽容解析链（统一入口，零成本纯本地）。

大模型经网关返回的 JSON 有多种已知畸变形态（生产实测累积）：

- 裸换行注入：键名/数字字面量中间出现原始 LF（gemini-3.8/4 网关分块缺陷，
  EB1CA38A/T15 同源）；
- markdown 代码围栏：```` ```json … ``` ```` 包裹（各系模型偶发）；
- 前后杂文本：模型无视「只输出 JSON」的指令，附解说词或思考标签；
- 尾随逗号：``[1, 2,]``（少数模型小概率）。

解析策略按「破坏性从低到高」逐级尝试，任一级成功即返回；全失败抛最后
一次异常。剔除原始换行与修尾逗号都只应在原样解析失败后启用——它们可能
改动字符串值内部内容（值里的裸换行被合并、``,]`` 被删），但解析失败本就
意味着这份数据即将被丢弃，恢复出主体结构比整包拒绝好。

``parse_llm_json`` 返回 ``(value, strategy)``：strategy 供调用方留痕
（``raw`` 表示一次成功，其余值代表走了哪级恢复，运维按此统计注入率）。
"""

from __future__ import annotations

import json
import re
from typing import Any

# markdown 围栏：```json … ``` / ``` … ```（围栏语言标记可有可无）。
_FENCE_PATTERN = re.compile(r"^\s*```[a-zA-Z0-9_-]*\s*\n(?P<body>.*?)\n?\s*```\s*$", re.S)

# 尾随逗号：``,,]``、``, }``（仅在原样解析失败后使用，见模块 docstring）。
_TRAILING_COMMA_PATTERN = re.compile(r",\s*(?=[\]}])")

_STRATEGY_RAW = "raw"
_STRATEGY_STRIP_NEWLINES = "strip_newlines"
_STRATEGY_UNFENCE = "strip_code_fence"
_STRATEGY_UNFENCE_STRIP = "strip_code_fence_and_newlines"
_STRATEGY_EXTRACT = "extract_outermost_json"
_STRATEGY_EXTRACT_STRIP = "extract_outermost_json_strip_newlines"
_STRATEGY_TRAILING = "trailing_comma_fix"
_STRATEGY_TRAILING_STRIP = "trailing_comma_fix_strip_newlines"


def strip_code_fence(text: str) -> str:
    """剥掉一层 markdown 代码围栏；没有围栏时原样返回。"""
    match = _FENCE_PATTERN.match(text)
    if match:
        return match.group("body")
    return text


def extract_outermost_json(text: str) -> str | None:
    """截取首个 ``{`` 到最后一个 ``}`` 之间的片段；不存在时返回 None。

    处理「模型在 JSON 前后附了解说词」的形态。对象数组（首个有效字符是
    ``[``）同样支持。只切一刀，不做任何内容改写。
    """
    for open_char, close_char in (("{", "}"), ("[", "]")):
        start = text.find(open_char)
        end = text.rfind(close_char)
        if start >= 0 and end > start:
            return text[start : end + 1]
    return None


def _attempt_loads(text: str) -> Any:
    return json.loads(text)


def parse_llm_json(text: str) -> tuple[Any, str]:
    """逐级宽容解析，返回 ``(value, strategy)``；全失败抛 JSONDecodeError。

    strategy 取值见模块常量；``raw`` 表示文本未经任何改写一次解析成功。
    """
    body = text.strip()
    candidates: list[tuple[str, str]] = [
        (body, _STRATEGY_RAW),
        # 裸换行剥离（EB1CA38A/T15）：同款处理已长期运行于 replica_prompt 链路。
        (body.replace("\r", "").replace("\n", ""), _STRATEGY_STRIP_NEWLINES),
    ]
    unfenced = strip_code_fence(body)
    if unfenced != body:
        candidates.append((unfenced, _STRATEGY_UNFENCE))
        candidates.append((unfenced.replace("\r", "").replace("\n", ""), _STRATEGY_UNFENCE_STRIP))
    extracted = extract_outermost_json(body)
    if extracted is not None and extracted != body:
        candidates.append((extracted, _STRATEGY_EXTRACT))
        candidates.append((extracted.replace("\r", "").replace("\n", ""), _STRATEGY_EXTRACT_STRIP))
        extracted_unfenced = strip_code_fence(extracted)
        if extracted_unfenced != extracted:
            candidates.append(
                (extracted_unfenced.replace("\r", "").replace("\n", ""), _STRATEGY_TRAILING)
            )
    # 尾逗号修复放最后：正则可能误伤字符串值内部的 ``,:]`` 形态，
    # 只有前面全军覆没时才值得冒险。纯 trailing 修复先于 trailing+strip
    # （少一层内容改写）。
    trailing_body = _TRAILING_COMMA_PATTERN.sub("", body).replace("\r", "").replace("\n", "")
    candidates.append((trailing_body, _STRATEGY_TRAILING))
    if extracted is not None:
        candidates.append(
            (
                _TRAILING_COMMA_PATTERN.sub("", extracted).replace("\r", "").replace("\n", ""),
                _STRATEGY_TRAILING_STRIP,
            )
        )
    last_error: json.JSONDecodeError | None = None
    seen: set[str] = set()
    for candidate, strategy in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        try:
            return _attempt_loads(candidate), strategy
        except json.JSONDecodeError as exc:
            last_error = exc
    assert last_error is not None  # candidates 至少含 raw 一项
    raise last_error
