import type { ShotCard, ShotMotion } from "./api";

export type ShotSubject = {
  subject_id: string;
  label: string;
  kind: "PERSON" | "OBJECT";
  visibility: "VISIBLE" | "PARTIAL" | "OFFSCREEN" | "UNKNOWN";
};

export type ShotSubjectAction = {
  subject_id: string;
  translation: "STATIONARY" | "MOVING" | "UNKNOWN";
  gait: "NONE" | "WALKING" | "RUNNING" | "UNKNOWN" | "NOT_APPLICABLE";
  turning: "NONE" | "TURNING" | "UNKNOWN";
  gesture: "NONE" | "GESTURING" | "UNKNOWN" | "NOT_APPLICABLE";
  direction: ShotMotion["subject_direction"];
  path: string;
  hand_detail: string;
  description?: string;
};

export type ShotFacts = {
  schema_version: "storyboard.facts.v1";
  camera_shot_id: string;
  entry_boundary: "VIDEO_START" | "CUT" | "DISSOLVE" | "CONTINUOUS" | "UNKNOWN";
  subjects: ShotSubject[];
  primary_subject_id: string | null;
  actions: ShotSubjectAction[];
  camera: { motion: ShotMotion["camera_motion"]; detail: string };
  relative_motion: string;
  evidence: Array<{
    source: "VIDEO" | "AUDIO" | "METADATA" | "MANUAL";
    fields: string[];
    start_time: number;
    end_time: number;
    observation: string;
    status?: "CURRENT" | "STALE";
  }>;
};

export const FACT_OPTIONS = {
  translation: [
    ["UNKNOWN", "待确认"],
    ["STATIONARY", "无空间位移"],
    ["MOVING", "有空间位移"],
  ],
  gait: [
    ["UNKNOWN", "待确认"],
    ["NONE", "未迈步"],
    ["WALKING", "行走"],
    ["RUNNING", "跑动"],
    ["NOT_APPLICABLE", "不适用"],
  ],
  turning: [
    ["UNKNOWN", "待确认"],
    ["NONE", "未转身"],
    ["TURNING", "转身"],
  ],
  gesture: [
    ["UNKNOWN", "待确认"],
    ["NONE", "无手势"],
    ["GESTURING", "做手势"],
    ["NOT_APPLICABLE", "不适用"],
  ],
  direction: [
    ["unknown", "待确认"],
    ["toward_camera", "向镜头"],
    ["away_from_camera", "背离镜头"],
    ["left", "向画面左"],
    ["right", "向画面右"],
    ["lateral", "横向移动"],
    ["in_place", "原地"],
    ["none", "无"],
  ],
  camera: [
    ["UNKNOWN", "待确认"],
    ["STATIC", "固定机位"],
    ["PUSH_IN", "推近"],
    ["PULL_BACK", "拉远"],
    ["HANDHELD_TRACKING", "手持跟拍"],
    ["PAN", "横摇"],
    ["TILT", "纵摇"],
    ["FOLLOW", "跟随主体"],
    ["ORBIT", "环绕"],
    ["CRANE_UP", "升高"],
    ["CRANE_DOWN", "降低"],
    ["ZOOM_IN", "变焦拉近"],
    ["ZOOM_OUT", "变焦拉远"],
  ],
  entry_boundary: [
    ["UNKNOWN", "进入方式待确认"],
    ["VIDEO_START", "视频开始"],
    ["CUT", "硬切"],
    ["DISSOLVE", "叠化"],
    ["CONTINUOUS", "同镜头连续"],
  ],
  kind: [
    ["PERSON", "人物"],
    ["OBJECT", "物体"],
  ],
  visibility: [
    ["UNKNOWN", "可见情况待确认"],
    ["VISIBLE", "可见"],
    ["PARTIAL", "部分可见"],
    ["OFFSCREEN", "画外"],
  ],
} as const;

export function factLabel(
  options: readonly (readonly [string, string])[],
  value: string,
): string {
  return options.find(([key]) => key === value)?.[1] ?? "待确认（未识别）";
}

function isObject(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function hasStrings(
  value: unknown,
  fields: string[],
): value is Record<string, unknown> {
  return (
    isObject(value) && fields.every((field) => typeof value[field] === "string")
  );
}

/** 只解释当前契约；保留原对象中的附加证据，未知枚举由控件显式显示待确认。 */
export function readShotFacts(value: unknown): ShotFacts | null {
  if (
    !hasStrings(value, [
      "camera_shot_id",
      "entry_boundary",
      "relative_motion",
    ]) ||
    value.schema_version !== "storyboard.facts.v1" ||
    !(
      value.primary_subject_id === null ||
      typeof value.primary_subject_id === "string"
    ) ||
    !Array.isArray(value.subjects) ||
    !value.subjects.every((subject) =>
      hasStrings(subject, ["subject_id", "label", "kind", "visibility"]),
    ) ||
    !Array.isArray(value.actions) ||
    !value.actions.every(
      (action) =>
        hasStrings(action, [
          "subject_id",
          "translation",
          "gait",
          "turning",
          "gesture",
          "direction",
          "path",
          "hand_detail",
        ]) &&
        (action.description === undefined ||
          typeof action.description === "string"),
    ) ||
    !hasStrings(value.camera, ["motion", "detail"]) ||
    !Array.isArray(value.evidence) ||
    !value.evidence.every(
      (evidence) =>
        isObject(evidence) &&
        hasStrings(evidence, ["source", "observation"]) &&
        Array.isArray(evidence.fields) &&
        evidence.fields.every((field) => typeof field === "string") &&
        typeof evidence.start_time === "number" &&
        Number.isFinite(evidence.start_time) &&
        typeof evidence.end_time === "number" &&
        Number.isFinite(evidence.end_time) &&
        (evidence.status === undefined || typeof evidence.status === "string"),
    )
  )
    return null;
  return value as unknown as ShotFacts;
}

export function unknownSubjectAction(subjectId: string): ShotSubjectAction {
  return {
    subject_id: subjectId,
    translation: "UNKNOWN",
    gait: "UNKNOWN",
    turning: "UNKNOWN",
    gesture: "UNKNOWN",
    direction: "unknown",
    path: "",
    hand_detail: "",
  };
}

/** 旧观察仍保留可回看，但已修改字段不能继续借用它宣称经过视频确认。 */
export function invalidateShotEvidence(
  facts: ShotFacts,
  fields?: string[],
): ShotFacts {
  const overlaps = (left: string, right: string) =>
    left === right ||
    left.startsWith(`${right}.`) ||
    right.startsWith(`${left}.`);
  return {
    ...facts,
    evidence: facts.evidence.map((evidence) =>
      !fields ||
      evidence.fields.some((observed) =>
        fields.some((changed) => overlaps(observed, changed)),
      )
        ? { ...evidence, status: "STALE" }
        : evidence,
    ),
  };
}

export function formatSubjectAction(action: ShotSubjectAction): string {
  return [
    `位移：${factLabel(FACT_OPTIONS.translation, action.translation)}`,
    `步态：${factLabel(FACT_OPTIONS.gait, action.gait)}`,
    `转身：${factLabel(FACT_OPTIONS.turning, action.turning)}`,
    `手势：${factLabel(FACT_OPTIONS.gesture, action.gesture)}`,
    `方向：${factLabel(FACT_OPTIONS.direction, action.direction)}`,
    action.path,
    action.hand_detail,
    action.description,
  ]
    .filter(Boolean)
    .join("；");
}

export function formatShotFacts(facts: ShotFacts): string {
  const subject = facts.subjects.find(
    (item) => item.subject_id === facts.primary_subject_id,
  );
  return [
    subject
      ? `${subject.label || subject.subject_id}（${subject.subject_id}）`
      : "主要主体待确认",
    formatShotActions(facts),
    `镜头：${factLabel(FACT_OPTIONS.camera, facts.camera.motion)}`,
    facts.camera.detail,
    facts.relative_motion,
  ]
    .filter(Boolean)
    .join("；");
}

function formatShotActions(facts: ShotFacts): string {
  return (
    facts.subjects
      .map((subject) => {
        const action = facts.actions.find(
          (item) => item.subject_id === subject.subject_id,
        );
        return `${subject.label || subject.subject_id}（${subject.subject_id}）：${action ? formatSubjectAction(action) : "动作待确认"}`;
      })
      .join("；") || "无人物出镜"
  );
}

/** 这是界面预览，不把旧派生文字重新解释成事实；服务端保存时统一重算正式文字。 */
export function shotFactsView<T extends Partial<ShotCard>>(shot: T): T {
  const facts = readShotFacts(shot.facts);
  if (!facts)
    return shot.facts == null
      ? shot
      : {
          ...shot,
          subject: "主体待确认",
          person_count: null,
          action: "结构化事实待确认",
          camera_motion: "待确认",
          transition: "待确认",
          segment_kind: null,
          boundary_reason: "事实格式暂不支持，已保留原始内容",
        };
  const people = facts.subjects.filter((subject) => subject.kind === "PERSON");
  const count =
    facts.subjects.some(
      (subject) => !["PERSON", "OBJECT"].includes(subject.kind),
    ) ||
    people.some(
      (subject) =>
        !["VISIBLE", "PARTIAL", "OFFSCREEN"].includes(subject.visibility),
    )
      ? null
      : new Set(
          people
            .filter(
              (subject) =>
                subject.visibility === "VISIBLE" ||
                subject.visibility === "PARTIAL",
            )
            .map((subject) => subject.subject_id),
        ).size;
  const subject = facts.subjects.find(
    (item) => item.subject_id === facts.primary_subject_id,
  );
  const boundary = factLabel(FACT_OPTIONS.entry_boundary, facts.entry_boundary);
  return {
    ...shot,
    subject: subject
      ? `${subject.label || subject.subject_id}（${subject.subject_id}）`
      : count === 0
        ? "无人物出镜"
        : "主要主体待确认",
    person_count: count,
    action: formatShotActions(facts),
    camera_motion: [
      factLabel(FACT_OPTIONS.camera, facts.camera.motion),
      facts.camera.detail,
    ]
      .filter(Boolean)
      .join("；"),
    transition: boundary,
    segment_kind:
      facts.entry_boundary === "CUT" || facts.entry_boundary === "DISSOLVE"
        ? "SHOT_CUT"
        : facts.entry_boundary === "VIDEO_START" ||
            facts.entry_boundary === "CONTINUOUS"
          ? "ACTION_BEAT"
          : null,
    boundary_reason: `${facts.camera_shot_id || "摄影镜头待确认"} · ${boundary}`,
  };
}
