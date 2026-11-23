"""分镜共享语义检查：只报告矛盾，不根据关键词改写原片事实。"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping, Sequence
from typing import Any

from app.storyboard_facts import (
    SUBJECT_MOTION_STATES as SUBJECT_MOTION_STATES,
)
from app.storyboard_facts import (
    parse_subject_motion_states as parse_subject_motion_states,
)
from app.storyboard_facts import storyboard_facts_issues


class StoryboardConsistencyError(ValueError):
    """保存入口需要向调用者返回可定位的问题，而不是不可恢复的服务端错误。"""

    def __init__(self, issues: list[dict[str, str]]) -> None:
        self.issues = issues
        first = issues[0]
        super().__init__(f"分镜 {first['shot_id'] or '全片'} 待确认：{first['message']}")


_UNCERTAIN_OR_QUOTED = re.compile(
    r"准备|打算|即将|将要|计划|想要|假如|如果|可能|似乎|疑似|看不清|无法判断|"
    r"尚未|旁白|台词|口播内容|说[道：:]|讲述|提到|回忆|说到|字幕|[‘“\"]"
)
_NEGATED = re.compile(
    r"没有|没|未|并非|不是|不再|不曾|不(?:走|跑|动|抬|挥|转|硬切|切|推|拉|摇|跟)|不能|不要|禁止|无需|无须"
)
_LOCOMOTION = re.compile(
    r"走向|走到|走近|走进|走出|走过|走来|走去|行走|迈步|边走|跑向|跑到|跑动|奔跑|转身|转过身"
)
_GESTURE = re.compile(r"抬手|抬起(?:左|右|双)?手|挥手|招手|摆手|挥动(?:左|右|双)?手")
_HUMAN = re.compile(r"讲解者|主讲人|人物|男子|女子|男人|女人|女士|男士|男孩|女孩")
_NO_PERSON = {"无人物出镜", "没有人物", "无人出镜", "无人物", "无人"}
_CONTINUOUS = re.compile(r"无切(?:镜|换)|同(?:一)?镜头|连续镜头|连续拍摄")
_CUT = re.compile(r"硬切|跳切|叠化|淡入|淡出|剪辑切换|切到|切换到")


def _observed_clauses(text: str) -> list[str]:
    # 只做保守的矛盾探测；不完整主语、否定与转述不能成为自动补造动作的依据。
    if _UNCERTAIN_OR_QUOTED.search(text):
        return []
    return [
        clause
        for clause in re.split(r"[，,。；;]|但是|但", text)
        if clause.strip() and not _NEGATED.search(clause)
    ]


def _observed_person_action(
    action: str, person_count: Any, subject: str, pattern: re.Pattern[str]
) -> bool:
    if person_count is None:
        # 旧镜头未记人数时，只认一个明确的人物主体；不能用配角动作覆盖主讲人。
        if (
            len(_HUMAN.findall(action)) != 1
            or not _HUMAN.search(subject)
            or re.search(r"多人|两人|两名|另一|其他|路人|分别|有人", action)
        ):
            return False
    elif person_count != 1 and not (
        isinstance(person_count, int) and not isinstance(person_count, bool) and person_count > 1
    ):
        return False
    clauses = _observed_clauses(action)
    if isinstance(person_count, int) and person_count > 1:
        # 多人旧数据没有主体 ID，只核对明确点名主讲人的子句；旁人的动作不借此归给他。
        aliases = [name for name in ("主讲人", "讲解者", "主持人", "主要人物") if name in subject]
        if len(subject.strip()) >= 4 and subject.strip() not in aliases:
            aliases.append(subject.strip())
        clauses = [
            clause
            for clause in clauses
            if any(clause.strip().startswith(alias) for alias in aliases)
            and not re.search(r"路人|配角|另一人|其他人", clause)
        ]
    return any(
        pattern.search(clause) is not None
        and not re.match(r"\s*(?:镜头|摄影机|相机|摄像机|机位|画面)", clause)
        and not re.search(r"路人|另一人|其他人", clause)
        for clause in clauses
    )


def _camera_conflicts(text: str, mode: str) -> bool:
    observed = "；".join(_observed_clauses(text))
    if not observed:
        return False
    descriptions = {
        "PUSH_IN": r"推近|前移|向前移动",
        "PULL_BACK": r"拉远|后移|向后移动",
        "HANDHELD_TRACKING": r"手持跟拍",
        "FOLLOW": r"跟随|跟拍",
        "PAN": r"横摇|水平摇|向左摇|向右摇",
        "TILT": r"纵摇|上下摇|向上摇|向下摇",
        "ORBIT": r"环绕",
        "CRANE_UP": r"升镜|升高",
        "CRANE_DOWN": r"降镜|降低",
        "ZOOM_IN": r"变焦推近|变焦放大",
        "ZOOM_OUT": r"变焦拉远|变焦缩小",
    }
    moving = {key for key, pattern in descriptions.items() if re.search(pattern, observed)}
    stationary = re.search(r"固定机位|机位固定|镜头静止|摄影机静止|无运镜", observed)
    if mode == "STATIC":
        return bool(moving)
    if mode == "UNKNOWN":
        return False
    # 固定机位仍可变焦；复合运镜不靠一个词推断完整轨迹。
    if stationary and not moving and mode not in {"ZOOM_IN", "ZOOM_OUT"}:
        return True
    opposite = {
        "PUSH_IN": "PULL_BACK",
        "PULL_BACK": "PUSH_IN",
        "ZOOM_IN": "ZOOM_OUT",
        "ZOOM_OUT": "ZOOM_IN",
    }
    return mode in opposite and opposite[mode] in moving and mode not in moving


def _spoken_content(text: str) -> str:
    """标点风格不应打断复刻；数字的小数点、正负号和单位仍是话语内容。"""
    normalized = unicodedata.normalize("NFKC", text)
    chars: list[str] = []
    for index, char in enumerate(normalized):
        if char.isspace():
            continue
        if unicodedata.category(char).startswith("P"):
            before = index > 0 and normalized[index - 1].isdigit()
            after = index + 1 < len(normalized) and normalized[index + 1].isdigit()
            numeric = (
                (char in ".:/" and before and after)
                or (char == "-" and after)
                or (char in "%‰" and before)
            )
            if not numeric:
                continue
        chars.append(char)
    return "".join(chars)


def _entry_transition_kind(text: str) -> str | None:
    """只取进入本段的明确证据，否定和切入后的连续拍摄不能反转入口含义。"""
    for clause in re.split(r"[，,。；;]", text):
        if re.match(r"\s*(?:随后|然后|之后|接着|继而|最后|此后)", clause):
            break
        if _UNCERTAIN_OR_QUOTED.search(clause) or _NEGATED.search(clause):
            continue
        if _CUT.search(clause):
            return "SHOT_CUT"
        if _CONTINUOUS.search(clause):
            return "ACTION_BEAT"
    return None


def storyboard_consistency_issues(
    shots: Sequence[Mapping[str, Any]], original_script: str | None = None
) -> list[dict[str, str]]:
    """所有入口共享的门禁；旧数据缺 motion 时不伪造静止，也不破坏历史读取。"""
    issues: list[dict[str, str]] = storyboard_facts_issues(shots)
    seen: set[str] = set()

    def add(code: str, shot_id: str, message: str) -> None:
        issues.append({"code": code, "shot_id": shot_id, "message": message})

    for shot in shots:
        shot_id = str(shot.get("shot_id") or "").strip()
        if shot_id and shot_id in seen:
            add("DUPLICATE_SHOT_ID", shot_id, "分镜编号重复，会导致台词或素材对应错误。")
        seen.add(shot_id)
        if shot.get("facts") is not None:
            # 新事实已按主体和时间校验；陈旧的兼容投影不能否决用户刚确认的事实。
            # 不合法 facts 同样在上方形成硬问题，不能通过跳到旧字段消掉错误。
            continue
        count = shot.get("person_count")
        subject = str(shot.get("subject") or "").strip()
        if isinstance(count, int) and not isinstance(count, bool):
            if count > 0 and subject in _NO_PERSON:
                add("PERSON_COUNT_SUBJECT_CONFLICT", shot_id, "人数大于零，但主体描述为无人物。")
            elif (
                count == 0
                and _HUMAN.search(subject)
                and not re.search(r"无|没有|未见|照片|海报|屏幕|画像|雕像|非真人", subject)
            ):
                add("PERSON_COUNT_SUBJECT_CONFLICT", shot_id, "人数为零，但主体描述包含可见人物。")
        kind = shot.get("segment_kind")
        transition = str(shot.get("transition") or "")
        observed_kind = _entry_transition_kind(transition)
        if kind in {"ACTION_BEAT", "SHOT_CUT"} and observed_kind and observed_kind != kind:
            add("SEGMENT_TRANSITION_CONFLICT", shot_id, "段落类型与进入本段的转场描述不一致。")
        motion = shot.get("motion")
        if not isinstance(motion, Mapping):
            continue
        try:
            states = set(parse_subject_motion_states(str(motion.get("subject_motion_state") or "")))
        except ValueError as exc:
            add("MOTION_STATE_CONFLICT", shot_id, str(exc))
            states = set()
        person_states = {"STATIC", "WALKING", "RUNNING", "MOVING", "TURNING", "GESTURING_ONLY"}
        if (count == 0 and states.intersection(person_states)) or (
            isinstance(count, int) and count > 0 and "NO_PERSON" in states
        ):
            add("PERSON_COUNT_MOTION_CONFLICT", shot_id, "可见人数与人物运动状态不一致。")
        action = str(shot.get("action") or "")
        if (
            states.intersection({"STATIC", "GESTURING_ONLY"})
            and not states.intersection({"WALKING", "RUNNING", "MOVING", "TURNING"})
            and any(
                _observed_person_action(text, count, subject, _LOCOMOTION)
                for text in (action, str(motion.get("subject_displacement") or ""))
            )
        ):
            add(
                "SUBJECT_ACTION_MOTION_CONFLICT",
                shot_id,
                "动作描述包含人物行走、跑动或转身，但运动状态表示原地不动。",
            )
        if states == {"STATIC"} and any(
            _observed_person_action(text, count, subject, _GESTURE)
            for text in (action, str(motion.get("hand_action") or ""))
        ):
            add(
                "SUBJECT_ACTION_MOTION_CONFLICT",
                shot_id,
                "人物有明确手势动作，但运动状态标为静止。",
            )
        direction = motion.get("subject_direction")
        if (
            states.intersection({"STATIC", "GESTURING_ONLY", "NO_PERSON"})
            and not states.intersection({"WALKING", "RUNNING", "MOVING", "TURNING"})
            and direction in {"toward_camera", "away_from_camera", "left", "right", "lateral"}
        ):
            add(
                "SUBJECT_DIRECTION_CONFLICT",
                shot_id,
                "人物状态与人物位移方向不一致；摄影机运动应单独记录。",
            )
        if (
            states.intersection({"WALKING", "RUNNING", "MOVING"})
            and direction in {"none", "in_place"}
            and not re.search(
                r"原地|踏步|跑步机|传送带", action + str(motion.get("subject_displacement") or "")
            )
        ):
            add(
                "SUBJECT_DIRECTION_CONFLICT",
                shot_id,
                "人物被标记行走或跑动，但未记录适用的位移方向。",
            )
        if _camera_conflicts(
            str(shot.get("camera_motion") or ""), str(motion.get("camera_motion") or "UNKNOWN")
        ):
            add("CAMERA_MOTION_CONFLICT", shot_id, "摄影机运动的文字描述与运动类别不一致。")
    if original_script is not None:
        joined = "".join(str(shot.get("spoken_text") or "") for shot in shots)
        if _spoken_content(joined) != _spoken_content(original_script):
            add(
                "SPOKEN_SCRIPT_CONFLICT",
                "",
                "全片文案与按时间拼接的分段台词不一致，请核对漏句、重复或串段。",
            )
    return issues
