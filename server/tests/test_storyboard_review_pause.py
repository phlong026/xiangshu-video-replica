from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from app import replica_pipeline as rp
from app import replica_pipeline_stages as stages
from app.generation import map_script_to_shots


def test_conflicting_analysis_waits_then_resumes_with_corrected_card(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(UTC)
    row: dict[str, Any] = {
        "analysis_task_id": "analysis-task",
        "stage_detail": {},
        "status": rp.STATUS_ANALYZING,
        "created_at": now - timedelta(minutes=1),
        "suspended_at": None,
        "suspended_seconds_total": 0,
    }
    lease = SimpleNamespace(id="pipeline", project_id="project")
    current_card = {"id": "card-1"}
    conn = SimpleNamespace(execute=lambda *args: SimpleNamespace(fetchone=lambda: current_card))
    shot = {
        "shot_id": "s1",
        "person_count": 1,
        "action": "人物从院子走向门口",
        "motion": {"subject_motion_state": "STATIC", "subject_direction": "none"},
    }
    monkeypatch.setattr(
        stages, "_sub_task_status", lambda *args: ("SUCCEEDED", {"result_version_id": "analysis"})
    )
    monkeypatch.setattr(stages, "_version_payload", lambda *args: {"shots": [shot]})
    monkeypatch.setattr(
        stages,
        "_save_stage_detail",
        lambda conn, pid, detail: row.update(stage_detail=dict(detail)),
    )
    advanced: list[Any] = []
    monkeypatch.setattr(rp, "advance_status", lambda *args, **kwargs: advanced.append(kwargs))

    assert stages._stage_poll_analysis(conn, lease, row).kind == "waiting"
    assert advanced == []
    review = row["stage_detail"]["storyboardReview"]
    assert review["shotCardVersionId"] == "card-1"
    assert review["issues"][0]["code"] == "SUBJECT_ACTION_MOTION_CONFLICT"
    assert rp.check_deadlines(conn, row, now=now + timedelta(days=3)) is False

    # 用户等待不应耗尽机器执行预算；新的分镜版本保留原拆解任务引用。
    row["created_at"] -= timedelta(days=3)
    current_card["id"] = "card-2"
    shot["motion"] = {"subject_motion_state": "WALKING", "subject_direction": "away_from_camera"}
    assert stages._stage_poll_analysis(conn, lease, row).kind == "advanced"
    assert len(advanced) == 1
    assert row["stage_detail"]["shotCardVersionId"] == "card-2"
    assert "storyboardReview" not in row["stage_detail"]
    assert rp.check_deadlines(conn, row, now=datetime.now(UTC)) is False


def test_corrected_storyboard_script_is_the_pipeline_script(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = {"stage_detail": {"analysisVersionId": "analysis", "shotCardVersionId": "card"}}
    lease = SimpleNamespace(id="pipeline", project_id="project", owner_user_id="user")
    payloads = {
        "card": {"original_script": "用户核对后的台词"},
        "analysis": {"analysis": {"original_script": "旧全文"}},
    }
    monkeypatch.setattr(stages, "_version_payload", lambda conn, vid: payloads[vid])
    monkeypatch.setattr(stages, "_pipeline_actor", lambda *args: object())
    monkeypatch.setattr(stages, "_save_stage_detail", lambda *args: None)
    monkeypatch.setattr(rp, "advance_status", lambda *args, **kwargs: None)
    captured: list[Any] = []

    def save_script(*args: Any, **kwargs: Any) -> dict[str, str]:
        captured.append(kwargs["request"])
        return {"id": "script"}

    monkeypatch.setattr(stages, "create_script_version", save_script)
    assert stages._stage_bridge_script(object(), lease, row).kind == "advanced"
    assert captured[0].text == "用户核对后的台词"


def test_original_speech_keeps_its_observed_timing_even_across_a_sentence() -> None:
    shots = [
        {"shot_id": "s1", "start_time": 0, "end_time": 2, "spoken_text": "大家好，"},
        {"shot_id": "s2", "start_time": 2, "end_time": 5, "spoken_text": "今天带你看院子。"},
    ]
    mapped = map_script_to_shots("大家好，今天带你看院子。", shots)
    assert [(item["text"], item["start_time"], item["end_time"]) for item in mapped] == [
        ("大家好，", 0, 2),
        ("今天带你看院子。", 2, 5),
    ]
