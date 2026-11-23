import type { ShotCard, ShotConsistencyIssue, ShotMotion } from "./api";
import { RawShotFactsRepair, ShotFactsEditor } from "./ShotFactsEditor";
import {
  formatShotFacts,
  invalidateShotEvidence,
  readShotFacts,
  shotFactsView,
} from "./shotFacts";

const SUBJECT_MOTION_STATE_OPTIONS: Array<{
  value: ShotMotion["subject_motion_state"];
  label: string;
}> = [
  { value: "STATIC", label: "静止" },
  { value: "WALKING", label: "行走" },
  { value: "MOVING", label: "有位移，步态待确认" },
  { value: "RUNNING", label: "跑动" },
  { value: "TURNING", label: "转身" },
  { value: "GESTURING_ONLY", label: "手势" },
  { value: "OBJECT_MOTION", label: "物体运动" },
  { value: "NO_PERSON", label: "无人物" },
  { value: "UNKNOWN", label: "待确认" },
];

const SUBJECT_DIRECTION_OPTIONS: Array<{
  value: ShotMotion["subject_direction"];
  label: string;
}> = [
  { value: "toward_camera", label: "向镜头" },
  { value: "away_from_camera", label: "背离镜头" },
  { value: "left", label: "向画面左" },
  { value: "right", label: "向画面右" },
  { value: "lateral", label: "横向移动" },
  { value: "in_place", label: "原地" },
  { value: "none", label: "无" },
  { value: "unknown", label: "待确认" },
];

const MOTION_CAMERA_OPTIONS: Array<{
  value: ShotMotion["camera_motion"];
  label: string;
}> = [
  { value: "STATIC", label: "固定机位" },
  { value: "PUSH_IN", label: "推近" },
  { value: "PULL_BACK", label: "拉远" },
  { value: "HANDHELD_TRACKING", label: "手持跟拍" },
  { value: "PAN", label: "横摇" },
  { value: "TILT", label: "纵摇" },
  { value: "FOLLOW", label: "跟随主体" },
  { value: "ORBIT", label: "环绕" },
  { value: "CRANE_UP", label: "升高" },
  { value: "CRANE_DOWN", label: "降低" },
  { value: "ZOOM_IN", label: "变焦拉近" },
  { value: "ZOOM_OUT", label: "变焦拉远" },
  { value: "UNKNOWN", label: "待确认" },
];

const DEFAULT_SHOT_MOTION: ShotMotion = {
  subject_motion_state: "UNKNOWN",
  subject_direction: "unknown",
  subject_displacement: "待确认",
  hand_action: "待确认",
  camera_motion: "UNKNOWN",
  relative_motion: "待确认",
};

const SHOT_TEXT_FIELDS = [
  { key: "shot_id", label: "镜头编号" },
  { key: "shot_type", label: "景别" },
  { key: "composition", label: "构图" },
  { key: "camera_motion", label: "运镜" },
  { key: "subject", label: "主体" },
  { key: "action", label: "动作" },
  { key: "scene", label: "场景" },
  { key: "spoken_text", label: "原口播" },
  { key: "transition", label: "转场" },
] as const;

const CLIPBOARD_HEADERS = [
  "镜头编号",
  "开始(秒)",
  "结束(秒)",
  "景别",
  "构图",
  "运镜",
  "主体",
  "动作",
  "人物与镜头运动",
  "场景",
  "原口播",
  "转场",
  "分段类型",
  "分段原因",
  "人数",
  "场景陈设",
  "光线",
  "服装与姿态",
  "环境音",
  "配乐风格",
];

function clipboardCell(value: unknown): string {
  return String(value ?? "")
    .replace(/[\t\r\n]+/g, " ")
    .trim();
}

function optionLabel<T extends string>(
  options: Array<{ value: T; label: string }>,
  value: T,
): string {
  return options.find((option) => option.value === value)?.label ?? "待确认";
}

export function formatShotMotion(
  motion: ShotMotion | null | undefined,
): string {
  if (!motion) return "";
  return [
    `人物：${motion.subject_motion_state
      .split("+")
      .map((state) =>
        optionLabel(
          SUBJECT_MOTION_STATE_OPTIONS,
          state as ShotMotion["subject_motion_state"],
        ),
      )
      .join("＋")}`,
    `方向：${optionLabel(SUBJECT_DIRECTION_OPTIONS, motion.subject_direction)}`,
    `位移：${motion.subject_displacement}`,
    `手部：${motion.hand_action}`,
    `镜头：${optionLabel(MOTION_CAMERA_OPTIONS, motion.camera_motion)}`,
    `相对运动：${motion.relative_motion}`,
  ].join("；");
}

/** 旧版拆解（2026-10 之前）没有 motion 字段：这些镜头编译后只会得到
 * action 文本拼接，成片人物容易僵立原地。编辑器据此显式提示（方案 T11）。
 * 序号取原数组下标：先过滤后编号会把「第 2 镜」错报成「第 1 镜」（评审 #8）。 */
export function missingMotionShotIds(shots: ShotCard[]): string[] {
  return shots
    .map((shot, index) =>
      shot.motion == null && shot.facts == null
        ? shot.shot_id?.trim()
          ? shot.shot_id
          : `第 ${index + 1} 镜`
        : "",
    )
    .filter((label) => label !== "");
}

/** 制表符格式可直接粘贴到 Excel、飞书表格和常见文档表格。 */
export function formatShotCardsForClipboard(
  shots: Partial<ShotCard>[],
  context: Record<string, unknown> = {},
): string {
  const hasFacts = shots.some((shot) => shot.facts != null);
  const rows = shots.map((sourceShot) => {
    const shot = shotFactsView(sourceShot);
    const facts = readShotFacts(shot.facts);
    return [
      shot.shot_id,
      shot.start_time,
      shot.end_time,
      shot.shot_type,
      shot.composition,
      shot.camera_motion,
      shot.subject,
      shot.action,
      facts
        ? formatShotFacts(facts)
        : shot.facts != null
          ? "结构化事实待确认"
          : formatShotMotion(shot.motion),
      shot.scene,
      shot.spoken_text,
      shot.transition,
      shot.segment_kind,
      shot.boundary_reason,
      shot.person_count,
      shot.scene_dressing,
      shot.scene_lighting,
      shot.wardrobe_pose_detail,
      shot.ambient_sound,
      shot.music_style_hint,
      ...(hasFacts
        ? [shot.facts == null ? "" : JSON.stringify(shot.facts)]
        : []),
    ];
  });
  const headers = [
    ...CLIPBOARD_HEADERS,
    ...(hasFacts ? ["结构化事实(JSON)"] : []),
  ];
  const table = [headers, ...rows]
    .map((row) => row.map(clipboardCell).join("\t"))
    .join("\n");
  const source =
    context.analysis &&
    typeof context.analysis === "object" &&
    !Array.isArray(context.analysis)
      ? (context.analysis as Record<string, unknown>)
      : context;
  const metadata = [
    ["theme", "主题"],
    ["visual_style", "视觉风格"],
    ["pace", "节奏"],
    ["camera_language", "镜头语言"],
    ["color_tone", "色调"],
    ["duration_seconds", "全片时长(秒)"],
    ["aspect_ratio", "画幅"],
    ["resolution", "源片分辨率"],
    ["fps", "源片帧率"],
  ].flatMap(([key, label]) => {
    const value = source[key];
    return (typeof value === "string" && value.trim()) ||
      (typeof value === "number" && Number.isFinite(value))
      ? [`${label}\t${clipboardCell(value)}`]
      : [];
  });
  const sourceReview = Object.hasOwn(context, "source_review")
    ? context.source_review
    : source.source_review;
  if (sourceReview && typeof sourceReview === "object") {
    metadata.push(`原片校对(JSON)\t${JSON.stringify(sourceReview)}`);
  }
  return metadata.length
    ? `${table}\n\n全片信息\n${metadata.join("\n")}`
    : table;
}

export type ShotTableImportResult =
  | { ok: true; promptText: string }
  | { ok: false; error: string };

export function promptTextFromShotTableClipboard(
  clipboardText: string,
): ShotTableImportResult {
  const normalized = clipboardText.replace(/^\uFEFF/, "").trim();
  if (!normalized) return { ok: false, error: "剪贴板中没有可导入的分镜表。" };
  const lines = normalized.split(/\r?\n/);
  const firstLine = lines[0] ?? "";
  const headers = firstLine.split("\t").map((value) => value.trim());
  const required = ["镜头编号", "开始(秒)", "结束(秒)"];
  if (!required.every((header) => headers.includes(header))) {
    return {
      ok: false,
      error: "未识别到分镜表表头，请先使用拆解页面的“复制分镜表”按钮。",
    };
  }
  if (!lines.slice(1).some((line) => line.trim())) {
    return { ok: false, error: "分镜表中没有可导入的镜头数据。" };
  }
  const promptText = [
    "以下内容是拆解后的分镜表。请保留镜头顺序、精确时间、人物与镜头运动、场景光线、服饰姿态、原口播和声音信息，并转换为当前视频模式要求的 H3 提示词格式。SHOT_CUT 表示真实切镜；ACTION_BEAT 表示同一镜头内的连续动作阶段，不得改成切镜。若含结构化事实(JSON)，以其中的主体引用和独立运动维度为准，不得把一个主体的动作移给另一主体，STALE 依据不能当作当前确认。缺失字段保持未确认，不得补成静止或固定机位：",
    normalized,
  ].join("\n\n");
  if (Array.from(promptText).length > 7000) {
    return {
      ok: false,
      error: "分镜表超过 7000 字，请删减镜头描述后再导入。",
    };
  }
  return { ok: true, promptText };
}

export function ShotCardEditor({
  shots,
  readOnly = false,
  onChange,
  issues = [],
}: {
  shots: ShotCard[];
  readOnly?: boolean;
  onChange: (index: number, shot: ShotCard) => void;
  issues?: ShotConsistencyIssue[];
}) {
  const update = (
    index: number,
    key: Exclude<keyof ShotCard, "motion" | "facts">,
    value: string,
  ) => {
    const shot = shots[index];
    if (!shot || readOnly) return;
    const nextValue =
      key === "start_time" || key === "end_time"
        ? toNonNegativeTime(value)
        : value;
    if (shot[key] === nextValue) return;
    const facts = readShotFacts(shot.facts);
    onChange(index, {
      ...shot,
      [key]: nextValue,
      ...(facts
        ? {
            facts: invalidateShotEvidence(
              facts,
              key === "start_time" || key === "end_time" ? undefined : [key],
            ),
          }
        : {}),
    } as ShotCard);
  };

  const updateMotion = (index: number, patch: Partial<ShotMotion>) => {
    const shot = shots[index];
    if (!shot || readOnly) return;
    if (shot.motion == null && patch.subject_motion_state === undefined) return;
    onChange(index, {
      ...shot,
      motion: { ...(shot.motion ?? DEFAULT_SHOT_MOTION), ...patch },
    });
  };

  // 旧拆解结果没有 motion，编译只能退回 action 文本——整条成片会偏静止且
  // 用户毫不知情（服务端只记日志）。在这里把缺运动的镜头显式摆出来（T11）。
  const missingMotionIds = missingMotionShotIds(shots);

  return (
    <div className="shot-table-wrap">
      {issues.length ? (
        <div
          className="shot-table-review"
          role="status"
          aria-label="分镜待确认事项"
        >
          <strong>分镜待确认 · {issues.length} 项</strong>
          <ul>
            {issues.map((issue) => (
              <li key={`${issue.code}:${issue.shot_id ?? ""}:${issue.message}`}>
                {issue.shot_id ? `${issue.shot_id}：` : ""}
                {issue.message}
              </li>
            ))}
          </ul>
        </div>
      ) : null}
      {missingMotionIds.length ? (
        <p className="shot-table__motion-missing" role="status">
          {missingMotionIds.join("、")}{" "}
          尚未识别运动信息，建议核对，或在「运动」列补充。
        </p>
      ) : null}
      <table className="shot-table">
        <thead>
          <tr>
            <th scope="col">时间段</th>
            <th scope="col">开始(秒)</th>
            <th scope="col">结束(秒)</th>
            {SHOT_TEXT_FIELDS.filter(({ key }) => key !== "shot_id").flatMap(
              ({ key, label }) => [
                <th key={key} scope="col">
                  {label}
                </th>,
                ...(key === "action"
                  ? [
                      <th key="motion" scope="col">
                        运动
                      </th>,
                    ]
                  : []),
              ],
            )}
          </tr>
        </thead>
        <tbody>
          {shots.map((sourceShot, index) => {
            const shot = shotFactsView(sourceShot);
            const facts = readShotFacts(sourceShot.facts);
            const hasFacts = sourceShot.facts != null;
            return (
              <tr key={shot.shot_id}>
                <td className="shot-table__id">
                  {hasFacts ? <small>动作阶段</small> : null}
                  <ShotCellInput
                    ariaLabel={`${shot.shot_id} ${hasFacts ? "动作阶段编号" : "镜头编号"}`}
                    onChange={(value) => update(index, "shot_id", value)}
                    readOnly={readOnly}
                    value={shot.shot_id}
                  />
                  <select
                    aria-label={`${shot.shot_id} 分段类型`}
                    className="shot-table-select"
                    disabled={readOnly || hasFacts}
                    value={shot.segment_kind ?? ""}
                    onChange={(event) =>
                      onChange(index, {
                        ...shot,
                        segment_kind:
                          (event.target.value as ShotCard["segment_kind"]) ||
                          null,
                      })
                    }
                  >
                    <MotionOptions
                      options={[
                        { value: "", label: "未标注分段" },
                        { value: "ACTION_BEAT", label: "连续动作段" },
                        { value: "SHOT_CUT", label: "真实切镜" },
                      ]}
                      values={[shot.segment_kind ?? ""]}
                    />
                  </select>
                  <ShotCellInput
                    ariaLabel={`${shot.shot_id} 分段原因`}
                    onChange={(value) =>
                      onChange(index, {
                        ...shot,
                        boundary_reason: value || null,
                      })
                    }
                    readOnly={readOnly || hasFacts}
                    value={shot.boundary_reason ?? ""}
                  />
                </td>
                <td className="shot-table__time">
                  <ShotCellInput
                    ariaLabel={`${shot.shot_id} 开始时间`}
                    onChange={(value) => update(index, "start_time", value)}
                    readOnly={readOnly}
                    type="number"
                    value={String(shot.start_time)}
                  />
                </td>
                <td className="shot-table__time">
                  <ShotCellInput
                    ariaLabel={`${shot.shot_id} 结束时间`}
                    onChange={(value) => update(index, "end_time", value)}
                    readOnly={readOnly}
                    type="number"
                    value={String(shot.end_time)}
                  />
                </td>
                {SHOT_TEXT_FIELDS.filter(
                  ({ key }) => key !== "shot_id",
                ).flatMap(({ key, label }) => [
                  <td
                    key={key}
                    className={
                      key === "spoken_text" ? "shot-table__spoken" : undefined
                    }
                  >
                    <ShotCellInput
                      ariaLabel={`${shot.shot_id} ${label}`}
                      onChange={(value) => update(index, key, value)}
                      readOnly={
                        readOnly ||
                        (hasFacts &&
                          [
                            "action",
                            "subject",
                            "camera_motion",
                            "transition",
                          ].includes(key))
                      }
                      value={shot[key]}
                    />
                    {key === "subject" ? (
                      <input
                        aria-label={`${shot.shot_id} 人数`}
                        className="shot-table-input"
                        type="number"
                        min={0}
                        step={1}
                        placeholder="人数待确认"
                        readOnly={readOnly}
                        disabled={readOnly || hasFacts}
                        value={shot.person_count ?? ""}
                        onChange={(event) => {
                          const value = event.target.value;
                          const count = value === "" ? null : Number(value);
                          if (
                            count === null ||
                            (Number.isInteger(count) && count >= 0)
                          )
                            onChange(index, { ...shot, person_count: count });
                        }}
                      />
                    ) : null}
                  </td>,
                  ...(key === "action"
                    ? [
                        <td key="motion" className="shot-table__motion">
                          {facts ? (
                            <ShotFactsEditor
                              ariaPrefix={shot.shot_id}
                              facts={facts}
                              readOnly={readOnly}
                              onChange={(next) =>
                                onChange(index, { ...sourceShot, facts: next })
                              }
                            />
                          ) : hasFacts ? (
                            <RawShotFactsRepair
                              key={JSON.stringify(sourceShot.facts)}
                              ariaPrefix={shot.shot_id}
                              raw={sourceShot.facts}
                              readOnly={readOnly}
                              onChange={(next) =>
                                onChange(index, { ...sourceShot, facts: next })
                              }
                            />
                          ) : (
                            <ShotMotionCell
                              ariaPrefix={shot.shot_id}
                              motion={shot.motion ?? null}
                              onChange={(patch) => updateMotion(index, patch)}
                              readOnly={readOnly}
                            />
                          )}
                        </td>,
                      ]
                    : []),
                ])}
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function toNonNegativeTime(value: string) {
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed >= 0 ? parsed : 0;
}

function ShotCellInput({
  ariaLabel,
  onChange,
  readOnly = false,
  type = "text",
  value,
}: {
  ariaLabel: string;
  onChange: (value: string) => void;
  readOnly?: boolean;
  type?: "number" | "text";
  value: string;
}) {
  return (
    <input
      aria-label={ariaLabel}
      className="shot-table-input"
      disabled={readOnly}
      min={type === "number" ? 0 : undefined}
      onChange={(event) => onChange(event.target.value)}
      step={type === "number" ? "any" : undefined}
      type={type}
      value={value}
    />
  );
}

function ShotMotionCell({
  ariaPrefix,
  motion,
  onChange,
  readOnly = false,
}: {
  ariaPrefix: string;
  motion: ShotMotion | null;
  onChange: (patch: Partial<ShotMotion>) => void;
  readOnly?: boolean;
}) {
  if (motion == null) {
    return (
      <select
        aria-label={`${ariaPrefix} 人物运动`}
        className="shot-table-select"
        disabled={readOnly}
        onChange={(event) => {
          const next = event.target.value as ShotMotion["subject_motion_state"];
          if (next) onChange({ subject_motion_state: next });
        }}
        value=""
      >
        <option value="">未标注</option>
        {SUBJECT_MOTION_STATE_OPTIONS.map((option) => (
          <option key={option.value} value={option.value}>
            {option.label}
          </option>
        ))}
      </select>
    );
  }
  const states = motion.subject_motion_state
    .split("+")
    .map((state) => state.trim());
  return (
    <div className="shot-motion-cell">
      <select
        aria-label={`${ariaPrefix} 人物运动`}
        className="shot-table-select"
        disabled={readOnly}
        multiple
        size={4}
        title="可多选；按住 Ctrl 或 Command 添加伴随动作"
        onChange={(event) => {
          const selected = Array.from(
            event.target.selectedOptions,
            (option) => option.value,
          );
          const added = selected.filter((state) => !states.includes(state));
          const exclusive = new Set([
            "STATIC",
            "NO_PERSON",
            "UNKNOWN",
            "OBJECT_MOTION",
          ]);
          const single = added.find((state) => exclusive.has(state));
          const locomotion = added.find((state) =>
            ["WALKING", "RUNNING", "MOVING"].includes(state),
          );
          const next = single
            ? [single]
            : added.length
              ? selected.filter(
                  (state) =>
                    !exclusive.has(state) &&
                    (!locomotion ||
                      !["WALKING", "RUNNING", "MOVING"].includes(state) ||
                      state === locomotion),
                )
              : selected;
          onChange({
            subject_motion_state: (next.join("+") ||
              "UNKNOWN") as ShotMotion["subject_motion_state"],
          });
        }}
        value={states}
      >
        <MotionOptions options={SUBJECT_MOTION_STATE_OPTIONS} values={states} />
      </select>
      <select
        aria-label={`${ariaPrefix} 位移方向`}
        className="shot-table-select"
        disabled={readOnly}
        onChange={(event) =>
          onChange({
            subject_direction: event.target
              .value as ShotMotion["subject_direction"],
          })
        }
        value={motion.subject_direction}
      >
        <MotionOptions
          options={SUBJECT_DIRECTION_OPTIONS}
          values={[motion.subject_direction]}
        />
      </select>
      <select
        aria-label={`${ariaPrefix} 运镜方式`}
        className="shot-table-select"
        disabled={readOnly}
        onChange={(event) =>
          onChange({
            camera_motion: event.target.value as ShotMotion["camera_motion"],
          })
        }
        value={motion.camera_motion}
      >
        <MotionOptions
          options={MOTION_CAMERA_OPTIONS}
          values={[motion.camera_motion]}
        />
      </select>
      <ShotCellInput
        ariaLabel={`${ariaPrefix} 位移幅度`}
        onChange={(value) => onChange({ subject_displacement: value })}
        readOnly={readOnly}
        value={motion.subject_displacement}
      />
      <ShotCellInput
        ariaLabel={`${ariaPrefix} 手部动作`}
        onChange={(value) => onChange({ hand_action: value })}
        readOnly={readOnly}
        value={motion.hand_action}
      />
      <ShotCellInput
        ariaLabel={`${ariaPrefix} 相对运动`}
        onChange={(value) => onChange({ relative_motion: value })}
        readOnly={readOnly}
        value={motion.relative_motion}
      />
    </div>
  );
}

function MotionOptions({
  options,
  values,
}: {
  options: Array<{ value: string; label: string }>;
  values: string[];
}) {
  // 历史或未来枚举必须保留可见占位，不能让 select 默默选中第一项。
  const unknown = [...new Set(values)].filter(
    (value) => !options.some((option) => option.value === value),
  );
  return (
    <>
      {options.map((option) => (
        <option key={option.value} value={option.value}>
          {option.label}
        </option>
      ))}
      {unknown.map((value) => (
        <option key={value} value={value}>
          待确认（未识别）
        </option>
      ))}
    </>
  );
}
