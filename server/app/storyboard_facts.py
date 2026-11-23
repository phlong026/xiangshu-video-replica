"""绑定主体、时间和证据的分镜事实；旧字段仅由这一份事实派生。"""

from __future__ import annotations

import copy
import math
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, ValidationError, field_validator

SubjectDirection = Literal[
    "toward_camera", "away_from_camera", "left", "right", "lateral", "in_place", "none", "unknown"
]
CameraMotion = Literal[
    "STATIC",
    "PUSH_IN",
    "PULL_BACK",
    "HANDHELD_TRACKING",
    "PAN",
    "TILT",
    "FOLLOW",
    "ORBIT",
    "CRANE_UP",
    "CRANE_DOWN",
    "ZOOM_IN",
    "ZOOM_OUT",
    "UNKNOWN",
]
SUBJECT_MOTION_STATES = (
    "STATIC",
    "WALKING",
    "RUNNING",
    "MOVING",
    "TURNING",
    "GESTURING_ONLY",
    "OBJECT_MOTION",
    "NO_PERSON",
    "UNKNOWN",
)
SUBJECT_MOTION_LABELS = {
    "STATIC": "保持静止",
    "WALKING": "行走",
    "RUNNING": "奔跑",
    "MOVING": "移动",
    "TURNING": "转身",
    "GESTURING_ONLY": "仅做手势",
    "OBJECT_MOTION": "物体位移",
    "NO_PERSON": "无人物出镜",
    "UNKNOWN": "运动状态无法判断",
}
CAMERA_MOTION_LABELS = {
    "STATIC": "固定机位",
    "PUSH_IN": "推近",
    "PULL_BACK": "拉远",
    "HANDHELD_TRACKING": "手持跟拍",
    "PAN": "横摇",
    "TILT": "纵摇",
    "FOLLOW": "跟随",
    "ORBIT": "环绕",
    "CRANE_UP": "升镜",
    "CRANE_DOWN": "降镜",
    "ZOOM_IN": "变焦推近",
    "ZOOM_OUT": "变焦拉远",
    "UNKNOWN": "机位运动无法判断",
}
_DIRECTIONS = {
    "toward_camera": "朝镜头方向",
    "away_from_camera": "背离镜头方向",
    "left": "向画面左",
    "right": "向画面右",
    "lateral": "横向",
    "in_place": "原地",
}
_ENTRY = {
    "VIDEO_START": ("ACTION_BEAT", "视频开始"),
    "CUT": ("SHOT_CUT", "硬切"),
    "DISSOLVE": ("SHOT_CUT", "叠化"),
    "CONTINUOUS": ("ACTION_BEAT", "同镜头连续，无切镜"),
    "UNKNOWN": (None, "进入方式无法判断"),
}
_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$"
_TOLERANCE = 0.05


class _FactsModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SubjectFact(_FactsModel):
    subject_id: str = Field(pattern=_ID_PATTERN)
    label: str = Field(min_length=1)
    kind: Literal["PERSON", "OBJECT"]
    # 指本段是否曾实际可见，不代表每帧同时在场；首帧人数必须另看首帧。
    visibility: Literal["VISIBLE", "PARTIAL", "OFFSCREEN", "UNKNOWN"]


class SubjectActionFact(_FactsModel):
    subject_id: str = Field(pattern=_ID_PATTERN)
    translation: Literal["STATIONARY", "MOVING", "UNKNOWN"]
    gait: Literal["NONE", "WALKING", "RUNNING", "UNKNOWN", "NOT_APPLICABLE"]
    turning: Literal["NONE", "TURNING", "UNKNOWN"]
    gesture: Literal["NONE", "GESTURING", "UNKNOWN", "NOT_APPLICABLE"]
    direction: SubjectDirection
    path: str
    hand_detail: str
    description: str = ""


class CameraFact(_FactsModel):
    motion: CameraMotion
    detail: str


class FactEvidence(_FactsModel):
    source: Literal["VIDEO", "AUDIO", "METADATA", "MANUAL"]
    fields: list[str] = Field(min_length=1)
    start_time: FiniteFloat = Field(ge=0)
    end_time: FiniteFloat = Field(ge=0)
    observation: str = Field(min_length=1)
    # 人工改事实后保留旧观察供追溯，但它不能继续证明新事实已经复核。
    status: Literal["CURRENT", "STALE"] = "CURRENT"

    @field_validator("start_time", "end_time", mode="before")
    @classmethod
    def real_seconds(cls, value: Any) -> Any:
        if isinstance(value, bool):
            raise ValueError("证据时间不能是布尔值")
        return value


class ShotFacts(_FactsModel):
    schema_version: Literal["storyboard.facts.v1"]
    camera_shot_id: str = Field(pattern=_ID_PATTERN)
    entry_boundary: Literal["VIDEO_START", "CUT", "DISSOLVE", "CONTINUOUS", "UNKNOWN"]
    subjects: list[SubjectFact]
    primary_subject_id: str | None
    actions: list[SubjectActionFact]
    camera: CameraFact
    relative_motion: str
    evidence: list[FactEvidence] = Field(default_factory=list)


def parse_subject_motion_states(value: str) -> tuple[str, ...]:
    """兼容旧并列状态；位移方式只能有一种，局部动作可以同时发生。"""
    parts = tuple(part.strip() for part in value.split("+"))
    if not parts or any(part not in SUBJECT_MOTION_STATES for part in parts):
        raise ValueError("人物运动状态包含未知取值")
    if len(set(parts)) != len(parts):
        raise ValueError("人物运动状态不能重复")
    exclusive = {"STATIC", "NO_PERSON", "UNKNOWN", "OBJECT_MOTION"}
    if (len(parts) > 1 and exclusive.intersection(parts)) or len(
        {"WALKING", "RUNNING", "MOVING"}.intersection(parts)
    ) > 1:
        raise ValueError("同一动作阶段的人物运动状态相互矛盾，请按真实阶段拆分")
    return parts


def subject_motion_phrase(motion: Mapping[str, Any]) -> str:
    """所有编译器共用状态语义；返回不含主体名称的运动短语。"""
    raw = str(motion.get("subject_motion_state") or "").strip()
    states = parse_subject_motion_states(raw) if raw else ()
    direction = _DIRECTIONS.get(str(motion.get("subject_direction") or ""), "")
    direction_state = next((s for s in states if s in {"WALKING", "RUNNING", "MOVING"}), "TURNING")
    clauses: list[str] = []
    for state in states:
        phrase = SUBJECT_MOTION_LABELS[state]
        if state == "GESTURING_ONLY" and len(states) > 1:
            phrase = "做手势"
        if state == direction_state and direction:
            phrase = direction + phrase
        clauses.append(phrase)
    return "，同时".join(clauses)


def _read_facts(shot: Mapping[str, Any]) -> ShotFacts | None:
    raw = shot.get("facts")
    if raw is None:
        return None
    return ShotFacts.model_validate(raw)


def _visible_people(facts: ShotFacts) -> int | None:
    people = [s for s in facts.subjects if s.kind == "PERSON"]
    if any(s.visibility == "UNKNOWN" for s in people):
        return None
    return len({s.subject_id for s in people if s.visibility in {"VISIBLE", "PARTIAL"}})


def _primary(facts: ShotFacts) -> tuple[SubjectFact | None, SubjectActionFact | None]:
    subject = next((s for s in facts.subjects if s.subject_id == facts.primary_subject_id), None)
    action = next((a for a in facts.actions if a.subject_id == facts.primary_subject_id), None)
    return subject, action


def _legacy_state(
    facts: ShotFacts, subject: SubjectFact | None, action: SubjectActionFact | None
) -> str:
    if subject is None:
        return "NO_PERSON" if _visible_people(facts) == 0 else "UNKNOWN"
    if action is None or subject.visibility not in {"VISIBLE", "PARTIAL"}:
        return "UNKNOWN"
    if subject.kind == "OBJECT":
        return "OBJECT_MOTION" if action.translation == "MOVING" else "UNKNOWN"
    states: list[str] = []
    if action.gait in {"WALKING", "RUNNING"}:
        states.append(action.gait)
    elif action.translation == "MOVING":
        states.append("MOVING")
    if action.turning == "TURNING":
        states.append("TURNING")
    if action.gesture == "GESTURING":
        # 旧单态暗含原地；未知位移不能借一次手势变成“原地手势”。
        if states or action.translation == "STATIONARY":
            states.append("GESTURING_ONLY")
    if states:
        return "+".join(states)
    if (
        action.translation == "STATIONARY"
        and action.gait == "NONE"
        and action.turning == "NONE"
        and action.gesture == "NONE"
    ):
        return "STATIC"
    return "UNKNOWN"


def _action_description(
    subject: SubjectFact, action: SubjectActionFact, *, label: str | None = None
) -> str:
    parts: list[str] = []
    if action.description.strip():
        parts.append(action.description.strip())
    movement = {
        "STATIONARY": "场景内站位不变" if subject.kind == "PERSON" else "场景内位置不变",
        "MOVING": "在场景内移动",
        "UNKNOWN": "场景内位移无法判断",
    }[action.translation]
    parts.append(movement)
    if action.gait in {"WALKING", "RUNNING"}:
        parts.append(
            ("原地" if action.translation == "STATIONARY" else "")
            + SUBJECT_MOTION_LABELS[action.gait]
        )
    elif action.gait == "UNKNOWN" and subject.kind == "PERSON":
        parts.append("步态无法判断")
    if action.turning == "TURNING":
        parts.append("转身")
    elif action.turning == "UNKNOWN":
        parts.append("转身情况无法判断")
    if action.gesture == "GESTURING":
        parts.append("做手势")
    elif action.gesture == "UNKNOWN" and subject.kind == "PERSON":
        parts.append("手势情况无法判断")
    if action.direction in _DIRECTIONS and action.direction != "in_place":
        parts.append(_DIRECTIONS[action.direction])
    if action.path.strip():
        parts.append(action.path.strip())
    if action.hand_detail.strip():
        parts.append(action.hand_detail.strip())
    return f"{label or subject.label}：" + "，".join(parts)


def _actions_description(facts: ShotFacts) -> str:
    subjects = {s.subject_id: s for s in facts.subjects}
    counts = Counter(s.label for s in facts.subjects)
    # “两名路人”仍是两个主体；同名时把稳定编号带到指令里，避免渲染后再次混同。
    labels = {
        s.subject_id: f"{s.label}（{s.subject_id}）" if counts[s.label] > 1 else s.label
        for s in facts.subjects
    }
    descriptions = [
        _action_description(subjects[a.subject_id], a, label=labels[a.subject_id])
        for a in facts.actions
        if a.subject_id in subjects
    ]
    primary, primary_action = _primary(facts)
    if primary is not None and primary_action is None:
        descriptions.insert(0, f"{labels[primary.subject_id]}的动作尚无法判断")
    if not descriptions:
        descriptions.append("无人物出镜" if _visible_people(facts) == 0 else "主体动作尚无法判断")
    return "；".join(descriptions)


def _preserve_primary_observation(facts: ShotFacts, raw_action: Any) -> None:
    primary, action = _primary(facts)
    if primary is None or action is None or not isinstance(raw_action, str):
        return
    # 已经过投影的文本不是新增的视频观察，重复解析不能反向把它写回证据。
    if raw_action.strip() == _actions_description(facts):
        return
    others = [s.label for s in facts.subjects if s.subject_id != primary.subject_id]
    aliases = [primary.label]
    aliases.extend(
        alias for alias in ("主讲人", "讲解者", "主持人", "主要人物") if alias in primary.label
    )
    # 一名真人不等于唯一动作主体：宠物、车辆等也可以移动。
    solo = (
        primary.kind == "PERSON"
        and primary.visibility in {"VISIBLE", "PARTIAL"}
        and all(
            s.subject_id == primary.subject_id or s.visibility == "OFFSCREEN"
            for s in facts.subjects
        )
    )
    clauses = [action.description.strip()] if action.description.strip() else []
    for clause in re.split(r"[，,。；;]", raw_action):
        clause = clause.strip()
        if not clause or any(label and label in clause for label in others):
            continue
        if re.match(r"^(?:镜头|摄影机|相机|画面|路人|另一|其他人)", clause):
            continue
        explicitly_bound = any(
            clause.startswith(alias)
            for alias in aliases
            if alias not in {"人物", "人", "男子", "女子"}
        )
        if (explicitly_bound or solo) and not any(clause in existing for existing in clauses):
            clauses.append(clause)
    action.description = "；".join(clauses)


def normalize_storyboard_shots(
    shots: Sequence[Mapping[str, Any]], *, preserve_observations: bool = False
) -> list[dict[str, Any]]:
    """新事实严格解析并派生兼容视图；旧数据不补造事实，坏 facts 不降级。"""
    normalized: list[dict[str, Any]] = []
    for shot in shots:
        copied = copy.deepcopy(dict(shot))
        facts = _read_facts(shot)
        if facts is None:
            normalized.append(copied)
            continue
        facts = facts.model_copy(deep=True)
        if preserve_observations:
            _preserve_primary_observation(facts, shot.get("action"))
        primary, action = _primary(facts)
        count = _visible_people(facts)
        kind, transition = _ENTRY[facts.entry_boundary]
        camera = CAMERA_MOTION_LABELS[facts.camera.motion]
        copied.update(
            facts=facts.model_dump(mode="json"),
            subject=primary.label
            if primary is not None
            else "无人物出镜"
            if count == 0
            else "主要主体待确认",
            person_count=count,
            action=_actions_description(facts),
            camera_motion=camera
            + (f"；{facts.camera.detail}" if facts.camera.detail.strip() else ""),
            segment_kind=kind,
            transition=transition,
            boundary_reason=transition,
            motion={
                "subject_motion_state": _legacy_state(facts, primary, action),
                "subject_direction": action.direction if action is not None else "unknown",
                "subject_displacement": action.path
                or {"MOVING": "在场景内移动", "STATIONARY": "无位移", "UNKNOWN": "无法判断"}[
                    action.translation
                ]
                if action is not None
                else "无法判断",
                "hand_action": action.hand_detail
                or (
                    "做手势"
                    if action.gesture == "GESTURING"
                    else "无法判断"
                    if action.gesture == "UNKNOWN"
                    else "无明显手势"
                )
                if action is not None
                else "无法判断",
                "camera_motion": facts.camera.motion,
                "relative_motion": facts.relative_motion or "相对运动无法判断",
            },
        )
        normalized.append(copied)
    return normalized


def render_storyboard_motion(shot: Mapping[str, Any]) -> str | None:
    """新事实只经过这一套中文语义渲染；返回 None 让旧资料走兼容模板。"""
    facts = _read_facts(shot)
    if facts is None:
        return None
    parts = [_actions_description(facts), "摄影机：" + CAMERA_MOTION_LABELS[facts.camera.motion]]
    if facts.camera.detail.strip():
        parts.append(facts.camera.detail.strip())
    if facts.relative_motion.strip():
        parts.append("画面相对变化：" + facts.relative_motion.strip())
    return "；".join(parts)


_UNCERTAIN = re.compile(
    r"准备|打算|将要|即将|计划|如果|假如|可能|疑似|似乎|无法判断|看不清|旁白|台词|字幕|[“‘\"]"
)
_NEGATIVE = re.compile(
    r"没有|未|不(?:再|曾|会|要|能|得|是|走|跑|动|抬|挥|转|推|拉|摇|跟)|并非|无(?:须|需|位移|移动)"
)
_TRANSLATION = re.compile(
    r"走向|走到|走近|走进|走出|走过|跑向|跑到|移向|移到|移近|离开原位|(?:向前|向后|横向|向左|向右)移动"
)
_GAIT = re.compile(r"行走|迈步|边走|走向|走到|走近|跑向|跑动|奔跑")
_TURN = re.compile(r"转身|转过身")
_GESTURE = re.compile(r"抬手|抬起(?:左|右|双)?手|挥手|招手|摆手|挥动(?:左|右|双)?手|做手势")
_STATIONARY = re.compile(r"站(?:立)?在原地|站位不变|位置不变|没有(?:发生)?位移|无位移|保持静止")


def _observed_clauses(value: str) -> list[str]:
    if _UNCERTAIN.search(value):
        return []
    return [
        clause for clause in re.split(r"[，,。；;]|但是|但", value) if not _NEGATIVE.search(clause)
    ]


def _observed_stationary(value: str) -> bool:
    for clause in re.split(r"[，,。；;]", value):
        if _UNCERTAIN.search(clause):
            continue
        for match in _STATIONARY.finditer(clause):
            prefix = clause[max(0, match.start() - 8) : match.start()]
            if not re.search(r"(?:并非|不是|不再|没有|未|不)(?:一直|始终)?$", prefix):
                return True
    return False


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    try:
        result = float(value)
    except OverflowError:
        return None
    return result if math.isfinite(result) else None


def _issue(code: str, shot_id: str, message: str) -> dict[str, str]:
    return {"code": code, "shot_id": shot_id, "message": message}


def _evidence_field_exists(field: str, facts: ShotFacts) -> bool:
    parts = field.split(".")
    roots = {
        "camera_shot_id",
        "entry_boundary",
        "primary_subject_id",
        "relative_motion",
        "start_time",
        "end_time",
        "spoken_text",
    }
    if parts[0] in roots:
        return len(parts) == 1
    if parts[0] == "camera":
        return len(parts) == 1 or (len(parts) == 2 and parts[1] in CameraFact.model_fields)
    if parts[0] in {"subjects", "actions"}:
        if len(parts) == 1:
            return True
        rows = facts.subjects if parts[0] == "subjects" else facts.actions
        model = SubjectFact if parts[0] == "subjects" else SubjectActionFact
        return any(row.subject_id == parts[1] for row in rows) and (
            len(parts) == 2 or (len(parts) == 3 and parts[2] in model.model_fields)
        )
    return False


def storyboard_facts_issues(shots: Sequence[Mapping[str, Any]]) -> list[dict[str, str]]:
    """只报告可确定的矛盾；未知/缺证据由 review_gaps 单独表达。"""
    issues: list[dict[str, str]] = []
    prior: tuple[ShotFacts, float | None] | None = None
    used_camera_ids: set[str] = set()
    subject_kinds: dict[str, str] = {}
    duration = max((_number(s.get("end_time")) or 0 for s in shots), default=0)
    for index, shot in enumerate(shots):
        shot_id = str(shot.get("shot_id") or f"s{index + 1}")
        try:
            facts = _read_facts(shot)
        except ValidationError as exc:
            location = ".".join(map(str, exc.errors()[0]["loc"]))
            issues.append(
                _issue(
                    "FACTS_SCHEMA_INVALID",
                    shot_id,
                    f"规范化事实格式不合法：{location or 'facts'}。",
                )
            )
            prior = None
            continue
        if facts is None:
            prior = None
            continue

        def add(code: str, message: str) -> None:
            issues.append(_issue(code, shot_id, message))

        subject_ids = [s.subject_id for s in facts.subjects]
        action_ids = [a.subject_id for a in facts.actions]
        if len(set(subject_ids)) != len(subject_ids):
            add("FACTS_SUBJECT_ID_DUPLICATE", "同一分镜的主体编号重复，人数和动作归属无法确定。")
        if len(set(action_ids)) != len(action_ids):
            add("FACTS_ACTION_DUPLICATE", "同一主体有多条阶段动作；先后变化应拆分连续动作阶段。")
        if facts.primary_subject_id is not None and facts.primary_subject_id not in subject_ids:
            add("FACTS_PRIMARY_SUBJECT_INVALID", "主要主体编号没有对应的主体记录。")
        subjects = {s.subject_id: s for s in facts.subjects}
        for subject in facts.subjects:
            previous_kind = subject_kinds.setdefault(subject.subject_id, subject.kind)
            if previous_kind != subject.kind:
                add(
                    "FACTS_SUBJECT_IDENTITY_CONFLICT",
                    f"主体 {subject.subject_id} 在不同分镜被同时标记为人物和物体。",
                )
        for action in facts.actions:
            if action.subject_id not in subjects:
                add("FACTS_ACTION_SUBJECT_INVALID", f"动作引用了不存在的主体 {action.subject_id}。")
                continue
            subject = subjects[action.subject_id]
            # 手部未知不能遮掉已经清楚写出的位移，三个观察字段分别取证。
            clauses = [
                clause
                for value in (action.description, action.path, action.hand_detail)
                for clause in _observed_clauses(value)
            ]
            moving = any(_TRANSLATION.search(c) for c in clauses)
            if (
                (action.translation == "STATIONARY" and moving)
                or (action.gait == "NONE" and any(_GAIT.search(c) for c in clauses))
                or (action.turning == "NONE" and any(_TURN.search(c) for c in clauses))
                or (action.gesture == "NONE" and any(_GESTURE.search(c) for c in clauses))
            ):
                add(
                    "FACTS_ACTION_CONFLICT",
                    f"主体 {subject.label} 的动作观察与结构化位移、步态、转身或手势不一致。",
                )
            if action.translation == "MOVING" and _observed_stationary(
                action.description + "；" + action.path
            ):
                add(
                    "FACTS_ACTION_CONFLICT",
                    f"主体 {subject.label} 被标记有位移，但观察说明站位不变。",
                )
            if (
                action.translation == "STATIONARY"
                and action.direction
                in {"toward_camera", "away_from_camera", "left", "right", "lateral"}
            ) or (action.translation == "MOVING" and action.direction in {"none", "in_place"}):
                add(
                    "FACTS_DIRECTION_CONFLICT",
                    f"主体 {subject.label} 的场景位移与位移方向相互矛盾。",
                )
            if subject.visibility == "OFFSCREEN" and (
                action.translation != "UNKNOWN"
                or action.gait in {"WALKING", "RUNNING"}
                or action.turning == "TURNING"
                or action.gesture == "GESTURING"
            ):
                add(
                    "FACTS_VISIBILITY_CONFLICT",
                    f"主体 {subject.label} 全段未出镜，不能把其运动写成已观察事实。",
                )

        # 这里只比较原始机位说明；先拼接枚举标签会掩盖说明本身的反向结论。
        camera_text = "；".join(_observed_clauses(facts.camera.detail))
        camera_moves = bool(
            re.search(r"推近|拉远|前移|后移|跟拍|跟随|横摇|纵摇|环绕|升镜|降镜|变焦", camera_text)
        )
        if facts.camera.motion == "STATIC" and camera_moves:
            add("FACTS_CAMERA_CONFLICT", "摄影机标记为静止，但机位观察包含明确运镜。")
        elif (
            facts.camera.motion not in {"STATIC", "UNKNOWN", "ZOOM_IN", "ZOOM_OUT"}
            and re.search(r"固定机位|机位固定|无运镜", camera_text)
            and not camera_moves
        ):
            add("FACTS_CAMERA_CONFLICT", "摄影机运动类别与固定机位的观察不一致。")
        opposites = {
            "PUSH_IN": r"拉远|后移",
            "PULL_BACK": r"推近|前移",
            "ZOOM_IN": r"变焦拉远|变焦缩小",
            "ZOOM_OUT": r"变焦推近|变焦放大",
        }
        if facts.camera.motion in opposites and re.search(
            opposites[facts.camera.motion], camera_text
        ):
            add("FACTS_CAMERA_CONFLICT", "摄影机运动方向与机位观察相反。")

        start, end = _number(shot.get("start_time")), _number(shot.get("end_time"))
        if start is None or end is None or start < 0 or end <= start:
            add("FACTS_TIME_INVALID", "事实所属动作阶段的起止时间不合法。")
        if index == 0 and start is not None and abs(start) > _TOLERANCE:
            add("FACTS_TIME_DISCONTINUITY", "第一段事实必须从源视频零秒开始，不能省略开场。")
        if (
            (index == 0 and facts.entry_boundary not in {"VIDEO_START", "UNKNOWN"})
            or (index > 0 and facts.entry_boundary == "VIDEO_START")
            or (
                facts.entry_boundary == "VIDEO_START"
                and start is not None
                and abs(start) > _TOLERANCE
            )
        ):
            add("FACTS_ENTRY_BOUNDARY_CONFLICT", "视频开始只能对应全片从零秒开始的第一段。")
        if prior is not None:
            previous, previous_end = prior
            if (
                start is not None
                and previous_end is not None
                and abs(start - previous_end) > _TOLERANCE
            ):
                add("FACTS_TIME_DISCONTINUITY", "相邻事实阶段的时间不连续或相互重叠。")
            if (
                facts.entry_boundary == "CONTINUOUS"
                and facts.camera_shot_id != previous.camera_shot_id
            ):
                add("FACTS_CAMERA_SHOT_CONFLICT", "连续动作阶段必须属于同一摄影镜头。")
            if (
                facts.entry_boundary in {"CUT", "DISSOLVE"}
                and facts.camera_shot_id == previous.camera_shot_id
            ):
                add("FACTS_CAMERA_SHOT_CONFLICT", "真实剪辑切换必须开始新的摄影镜头。")
            if (
                facts.camera_shot_id != previous.camera_shot_id
                and facts.camera_shot_id in used_camera_ids
            ):
                add(
                    "FACTS_CAMERA_SHOT_CONFLICT",
                    "摄影镜头编号在中断后被复用；回切也属于新的连续摄影镜头。",
                )
        used_camera_ids.add(facts.camera_shot_id)
        prior = (facts, end)
        for evidence in facts.evidence:
            if evidence.status == "STALE":
                continue
            if any(not _evidence_field_exists(field, facts) for field in evidence.fields):
                add("FACTS_EVIDENCE_FIELD_INVALID", "当前证据引用了不存在的事实字段或主体。")
            if evidence.end_time < evidence.start_time or evidence.end_time > duration + _TOLERANCE:
                add("FACTS_EVIDENCE_TIME_INVALID", "当前证据时间倒置或超出源视频时间范围。")
                continue
            if start is None or end is None:
                continue
            boundary_fields = {"entry_boundary", "camera_shot_id", "start_time"}
            if (
                any(field in boundary_fields for field in evidence.fields)
                and not evidence.start_time - _TOLERANCE <= start <= evidence.end_time + _TOLERANCE
            ):
                add("FACTS_ENTRY_EVIDENCE_INVALID", "入口边界的当前证据没有覆盖本段开始时点。")
            if any(field not in boundary_fields for field in evidence.fields) and (
                evidence.start_time < start - _TOLERANCE or evidence.end_time > end + _TOLERANCE
            ):
                add("FACTS_EVIDENCE_TIME_INVALID", "动作或画面的当前证据不在所属动作阶段内。")
    return issues


_UNOBSERVED_VIDEO = re.compile(
    r"(?:未(?:曾|能)?|没有|并未|无法|不能)(?:实际|直接|亲自)?"
    r"(?:观察|观看|看过|获取|访问|加载|读取|打开)(?:到|过)?"
    r"(?:实际|生成的|这段|该)?(?:视频|成片|画面)|"
    r"(?:仅|只)(?:根据|按|凭)[^。；！？\n]{0,32}(?:推测|推断|猜测|估计)|"
    r"\b(?:not|haven't|hasn't|didn't|cannot|can't|unable to)\s+(?:actually\s+)?"
    r"(?:watch(?:ed)?|view(?:ed)?|see(?:n)?|observe(?:d)?|access(?:ed)?)"
    r"(?:\s+\w+){0,3}\s+(?:video|footage|clip)\b|"
    r"^\s*(?:待确认|未知|无法判断|未观察|unknown|not observed)\s*[。.!]?\s*$",
    re.IGNORECASE,
)


def _video_evidence_covers(evidence: FactEvidence, field: str) -> bool:
    if evidence.source != "VIDEO" or _UNOBSERVED_VIDEO.search(evidence.observation):
        return False
    # 单帧可支持主体外观/可见性；动作过程、运镜和切镜不能据静态截图宣称已核实。
    if field != "subjects" and not field.startswith("subjects."):
        if evidence.end_time <= evidence.start_time:
            return False
    return any(field == parent or field.startswith(parent + ".") for parent in evidence.fields)


def storyboard_facts_review_gaps(shots: Sequence[Mapping[str, Any]]) -> list[dict[str, str]]:
    """完整性提醒不等于事实冲突；不得据未知项补造静止或触发付费重试。"""
    gaps: list[dict[str, str]] = []
    for index, shot in enumerate(shots):
        shot_id = str(shot.get("shot_id") or f"s{index + 1}")
        try:
            facts = _read_facts(shot)
        except ValidationError:
            continue
        if facts is None:
            gaps.append(
                _issue("FACTS_MISSING", shot_id, "缺少规范化主体与运动事实，尚未完成源片复核。")
            )
            continue
        primary, action = _primary(facts)
        unknown = facts.entry_boundary == "UNKNOWN" or facts.camera.motion == "UNKNOWN"
        unknown = unknown or any(s.visibility == "UNKNOWN" for s in facts.subjects)
        unknown = unknown or (
            primary is None and any(s.visibility in {"VISIBLE", "PARTIAL"} for s in facts.subjects)
        )
        actions = {a.subject_id: a for a in facts.actions}
        for subject in facts.subjects:
            if subject.visibility in {"VISIBLE", "PARTIAL"} and subject.subject_id not in actions:
                code = (
                    "FACTS_PRIMARY_ACTION_MISSING"
                    if subject.subject_id == facts.primary_subject_id
                    else "FACTS_SUBJECT_ACTION_MISSING"
                )
                gaps.append(_issue(code, shot_id, f"主体 {subject.label} 尚无可核对的动作事实。"))
        unknown = unknown or any(
            "UNKNOWN" in (a.translation, a.gait, a.turning, a.gesture) or a.direction == "unknown"
            for a in facts.actions
        )
        if unknown:
            gaps.append(_issue("FACTS_UNKNOWN", shot_id, "仍有无法判断的主体、边界或运动字段。"))
        current = [
            e
            for e in facts.evidence
            if e.status == "CURRENT" and e.source in {"VIDEO", "AUDIO"} and e.observation.strip()
        ]
        required = {"camera.motion", "entry_boundary", "relative_motion"}
        if facts.camera.detail.strip():
            required.add("camera.detail")
        if not facts.subjects:
            required.add("subjects")
        for subject in facts.subjects:
            required.update(
                f"subjects.{subject.subject_id}.{field}"
                for field in ("kind", "visibility", "label")
            )
            subject_action = actions.get(subject.subject_id)
            if subject.visibility in {"VISIBLE", "PARTIAL"} and subject_action is not None:
                fields = {"translation", "gait", "turning", "gesture", "direction"}
                fields.update(
                    field
                    for field in ("path", "hand_detail", "description")
                    if getattr(subject_action, field).strip()
                )
                required.update(f"actions.{subject.subject_id}.{field}" for field in fields)
        if not all(
            any(_video_evidence_covers(evidence, field) for evidence in current)
            for field in required
        ):
            gaps.append(
                _issue(
                    "FACTS_EVIDENCE_MISSING", shot_id, "人物、运动或边界缺少当前源视频观察证据。"
                )
            )
        if str(shot.get("spoken_text") or "").strip() and not any(
            e.source == "AUDIO" and "spoken_text" in e.fields for e in current
        ):
            gaps.append(
                _issue("FACTS_AUDIO_EVIDENCE_MISSING", shot_id, "分段台词缺少当前音轨观察证据。")
            )
    return gaps
