import type { ShotCard } from "../api";
import type { ShotFacts } from "../shotFacts";

export function sampleShotFacts(): ShotFacts {
  return {
    schema_version: "storyboard.facts.v1",
    camera_shot_id: "camera-1",
    entry_boundary: "VIDEO_START",
    subjects: [
      {
        subject_id: "p1",
        label: "讲解者",
        kind: "PERSON",
        visibility: "VISIBLE",
      },
      {
        subject_id: "p2",
        label: "门旁的人",
        kind: "PERSON",
        visibility: "PARTIAL",
      },
    ],
    primary_subject_id: "p1",
    actions: [
      {
        subject_id: "p1",
        translation: "MOVING",
        gait: "WALKING",
        turning: "NONE",
        gesture: "GESTURING",
        direction: "lateral",
        path: "从院子走向门口",
        hand_detail: "抬手示意",
      },
    ],
    camera: { motion: "FOLLOW", detail: "摄影机跟随讲解者" },
    relative_motion: "人物与镜头同向移动",
    evidence: [
      {
        source: "VIDEO",
        fields: ["actions.p1.translation", "actions.p1.gait"],
        start_time: 0.125,
        end_time: 3.375,
        observation: "讲解者从院子移动到门口，双脚交替迈步",
        status: "CURRENT",
      },
      {
        source: "VIDEO",
        fields: ["camera.motion"],
        start_time: 0.125,
        end_time: 3.375,
        observation: "机位跟随主体",
        status: "CURRENT",
      },
    ],
  };
}

export function sampleFactsShot(): ShotCard & { facts: ShotFacts } {
  return {
    shot_id: "s1",
    start_time: 0.125,
    end_time: 3.375,
    shot_type: "中景",
    composition: "人物居中",
    camera_motion: "旧固定机位描述",
    subject: "旧主体描述",
    person_count: 1,
    action: "旧人物静止描述",
    scene: "院子",
    spoken_text: "",
    transition: "旧切镜描述",
    segment_kind: "SHOT_CUT",
    boundary_reason: "旧边界描述",
    motion: {
      subject_motion_state: "STATIC",
      subject_direction: "none",
      subject_displacement: "无位移",
      hand_action: "不动",
      camera_motion: "STATIC",
      relative_motion: "静止",
    },
    facts: sampleShotFacts(),
  };
}
