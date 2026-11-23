import { useRef, useState } from "react";
import {
  FACT_OPTIONS,
  factLabel,
  invalidateShotEvidence,
  readShotFacts,
  type ShotFacts,
  type ShotSubject,
  type ShotSubjectAction,
  unknownSubjectAction,
} from "./shotFacts";

/** 仅给损坏的已付费初稿一个修复出口，正常分镜仍使用分维度表单。 */
export function RawShotFactsRepair({
  raw,
  ariaPrefix,
  readOnly,
  onChange,
}: {
  raw: unknown;
  ariaPrefix: string;
  readOnly: boolean;
  onChange: (facts: ShotFacts) => void;
}) {
  const original = JSON.stringify(raw, null, 2) ?? "null";
  const [text, setText] = useState(original);
  const [error, setError] = useState("");
  const details = useRef<HTMLDetailsElement>(null);
  const apply = () => {
    if (readOnly) return;
    try {
      const facts = readShotFacts(JSON.parse(text));
      if (!facts) {
        setError(
          "结构化记录字段不完整或类型不正确，请保留主体、动作、机位和观察依据等字段。",
        );
        return;
      }
      onChange(invalidateShotEvidence(facts));
    } catch {
      setError("结构化记录不是有效 JSON，请检查括号、引号和逗号。");
    }
  };
  return (
    <div className="shot-facts-editor">
      <p role="status">结构化事实需修正，原始记录已保留。</p>
      <details ref={details}>
        <summary>修正结构化记录</summary>
        <p>
          高级修正仅作用于当前动作阶段。未确认项目保留
          UNKNOWN；采用后旧观察依据将标为待核对，保存时仍会检查字段关系。
        </p>
        <textarea
          aria-label={`${ariaPrefix} 结构化记录修正`}
          value={text}
          readOnly={readOnly}
          rows={10}
          onChange={(event) => {
            setText(event.target.value);
            setError("");
          }}
        />
        {error ? <p role="alert">{error}</p> : null}
        <div>
          <button type="button" disabled={readOnly} onClick={apply}>
            采用修正记录
          </button>
          <button
            type="button"
            onClick={() => {
              setText(original);
              setError("");
              if (details.current) details.current.open = false;
            }}
          >
            取消修正
          </button>
        </div>
      </details>
    </div>
  );
}

export function ShotFactsEditor({
  facts,
  ariaPrefix,
  readOnly = false,
  onChange,
}: {
  facts: ShotFacts;
  ariaPrefix: string;
  readOnly?: boolean;
  onChange: (facts: ShotFacts) => void;
}) {
  const [inspectedSubjectId, setInspectedSubjectId] = useState(
    facts.primary_subject_id,
  );
  const subject =
    facts.subjects.find((item) => item.subject_id === inspectedSubjectId) ??
    facts.subjects.find(
      (item) => item.subject_id === facts.primary_subject_id,
    ) ??
    facts.subjects[0];
  const action = subject
    ? (facts.actions.find((item) => item.subject_id === subject.subject_id) ??
      unknownSubjectAction(subject.subject_id))
    : null;
  const change = (next: ShotFacts, fields: string[]) => {
    if (JSON.stringify(next) === JSON.stringify(facts)) return;
    if (!readOnly) onChange(invalidateShotEvidence(next, fields));
  };
  const changeAction = (
    field: keyof Omit<ShotSubjectAction, "subject_id">,
    value: string,
  ) => {
    if (!action) return;
    const edited = { ...action, [field]: value } as ShotSubjectAction;
    change(
      {
        ...facts,
        actions: facts.actions.some(
          (item) => item.subject_id === action.subject_id,
        )
          ? facts.actions.map((item) =>
              item.subject_id === action.subject_id ? edited : item,
            )
          : [...facts.actions, edited],
      },
      [`actions.${action.subject_id}.${field}`],
    );
  };
  const changeSubject = (
    id: string,
    field: keyof Omit<ShotSubject, "subject_id">,
    value: string,
  ) => {
    change(
      {
        ...facts,
        subjects: facts.subjects.map((item) =>
          item.subject_id === id
            ? ({ ...item, [field]: value } as ShotSubject)
            : item,
        ),
      },
      field === "kind"
        ? [`subjects.${id}`, `actions.${id}`]
        : [`subjects.${id}.${field}`],
    );
  };
  const addSubject = () => {
    // 主体 ID 属于整条素材，不能让不同阶段新增的 subject-1 被误认成同一人。
    const id = `subject-${crypto.randomUUID()}`;
    change(
      {
        ...facts,
        subjects: [
          ...facts.subjects,
          {
            subject_id: id,
            label: "新主体",
            kind: "PERSON",
            visibility: "UNKNOWN",
          },
        ],
      },
      ["subjects"],
    );
  };

  return (
    <div className="shot-facts-editor">
      <small>动作、人数与运镜由这些事实同步展示。</small>
      <FactSelect
        label={`${ariaPrefix} 主要主体`}
        caption="主要主体"
        value={facts.primary_subject_id ?? ""}
        options={[
          ["", "主要主体待确认"],
          ...facts.subjects.map(
            (item) =>
              [
                item.subject_id,
                `${item.label || item.subject_id}（${item.subject_id}）`,
              ] as const,
          ),
        ]}
        disabled={readOnly}
        onChange={(value) => {
          setInspectedSubjectId(value || null);
          change({ ...facts, primary_subject_id: value || null }, [
            "primary_subject_id",
          ]);
        }}
      />
      <FactSelect
        label={`${ariaPrefix} 核对主体`}
        caption="核对主体"
        value={subject?.subject_id ?? ""}
        options={
          facts.subjects.length
            ? facts.subjects.map(
                (item) =>
                  [
                    item.subject_id,
                    `${item.label || item.subject_id}（${item.subject_id}）${item.subject_id === facts.primary_subject_id ? " · 主要主体" : ""}`,
                  ] as const,
              )
            : [["", "暂无主体"]]
        }
        disabled={facts.subjects.length === 0}
        onChange={setInspectedSubjectId}
      />
      <small>选择要查看或修改动作的对象，不改变主要主体。</small>
      {action ? (
        <>
          {(
            [
              ["translation", "位移"],
              ["gait", "步态"],
              ["turning", "转身"],
              ["gesture", "局部手势"],
              ["direction", "位移方向"],
            ] as const
          ).map(([field, label]) => (
            <FactSelect
              key={field}
              label={`${ariaPrefix} ${action.subject_id} ${label}`}
              caption={label}
              value={action[field]}
              options={FACT_OPTIONS[field]}
              disabled={readOnly}
              onChange={(value) => changeAction(field, value)}
            />
          ))}
          {(
            [
              ["path", "位移路径"],
              ["hand_detail", "手部细节"],
              ["description", "动作观察"],
            ] as const
          ).map(([field, label]) => (
            <FactInput
              key={field}
              label={`${ariaPrefix} ${action.subject_id} ${label}`}
              caption={label}
              value={action[field] ?? ""}
              disabled={readOnly}
              onChange={(value) => changeAction(field, value)}
            />
          ))}
        </>
      ) : (
        <small>选定主体后可核对其运动；其他主体的已有动作会保留。</small>
      )}
      <FactSelect
        label={`${ariaPrefix} 摄影机运动`}
        caption="摄影机运动"
        value={facts.camera.motion}
        options={FACT_OPTIONS.camera}
        disabled={readOnly}
        onChange={(value) =>
          change(
            {
              ...facts,
              camera: {
                ...facts.camera,
                motion: value as ShotFacts["camera"]["motion"],
              },
            },
            ["camera.motion"],
          )
        }
      />
      <FactInput
        label={`${ariaPrefix} 摄影机运动细节`}
        caption="运镜细节"
        value={facts.camera.detail}
        disabled={readOnly}
        onChange={(value) =>
          change({ ...facts, camera: { ...facts.camera, detail: value } }, [
            "camera.detail",
          ])
        }
      />
      <FactInput
        label={`${ariaPrefix} 主体与镜头关系`}
        caption="主体与镜头关系"
        value={facts.relative_motion}
        disabled={readOnly}
        onChange={(value) =>
          change({ ...facts, relative_motion: value }, ["relative_motion"])
        }
      />
      <details>
        <summary>主体与摄影镜头</summary>
        <FactInput
          label={`${ariaPrefix} 摄影镜头编号`}
          caption="所属摄影镜头"
          value={facts.camera_shot_id}
          disabled={readOnly}
          onChange={(value) =>
            change({ ...facts, camera_shot_id: value }, ["camera_shot_id"])
          }
        />
        <FactSelect
          label={`${ariaPrefix} 阶段进入方式`}
          caption="进入本动作阶段"
          value={facts.entry_boundary}
          options={FACT_OPTIONS.entry_boundary}
          disabled={readOnly}
          onChange={(value) =>
            change(
              {
                ...facts,
                entry_boundary: value as ShotFacts["entry_boundary"],
              },
              ["entry_boundary"],
            )
          }
        />
        <small>
          同一连续摄影镜头使用同一编号；本行时间范围就是当前动作阶段。
        </small>
        {facts.subjects.map((item) => (
          <div className="shot-facts-subject" key={item.subject_id}>
            <strong>{item.subject_id}</strong>
            <FactInput
              label={`${ariaPrefix} ${item.subject_id} 主体称呼`}
              caption="称呼"
              value={item.label}
              disabled={readOnly}
              onChange={(value) =>
                changeSubject(item.subject_id, "label", value)
              }
            />
            <FactSelect
              label={`${ariaPrefix} ${item.subject_id} 主体类型`}
              caption="类型"
              value={item.kind}
              options={FACT_OPTIONS.kind}
              disabled={readOnly}
              onChange={(value) =>
                changeSubject(item.subject_id, "kind", value)
              }
            />
            <FactSelect
              label={`${ariaPrefix} ${item.subject_id} 可见情况`}
              caption="本段可见情况"
              value={item.visibility}
              options={FACT_OPTIONS.visibility}
              disabled={readOnly}
              onChange={(value) =>
                changeSubject(item.subject_id, "visibility", value)
              }
            />
            <button
              type="button"
              disabled={readOnly}
              onClick={() =>
                change(
                  {
                    ...facts,
                    subjects: facts.subjects.filter(
                      (candidate) => candidate.subject_id !== item.subject_id,
                    ),
                    actions: facts.actions.filter(
                      (candidate) => candidate.subject_id !== item.subject_id,
                    ),
                    primary_subject_id:
                      facts.primary_subject_id === item.subject_id
                        ? null
                        : facts.primary_subject_id,
                  },
                  [
                    `subjects.${item.subject_id}`,
                    `actions.${item.subject_id}`,
                    ...(facts.primary_subject_id === item.subject_id
                      ? ["primary_subject_id"]
                      : []),
                  ],
                )
              }
            >
              移除 {item.subject_id}
            </button>
          </div>
        ))}
        <button type="button" onClick={addSubject} disabled={readOnly}>
          补充主体
        </button>
      </details>
      <ShotFactEvidence facts={facts} />
    </div>
  );
}

export function ShotFactEvidence({ facts }: { facts: ShotFacts }) {
  return (
    <details className="shot-facts-evidence">
      <summary>
        观察依据（{facts.evidence.length} 条）
        {facts.evidence.some((item) => item.status === "STALE")
          ? " · 有修改待核对"
          : ""}
      </summary>
      {facts.evidence.length ? (
        <ul>
          {facts.evidence.map((evidence) => (
            <li
              key={`${evidence.source}:${evidence.start_time}:${evidence.end_time}:${evidence.fields.join(",")}:${evidence.observation}`}
            >
              <strong>
                {evidence.status === "STALE"
                  ? "已修改，旧依据待核对"
                  : evidence.status && evidence.status !== "CURRENT"
                    ? "依据状态待确认"
                    : factLabel(
                        [
                          ["VIDEO", "画面观察"],
                          ["AUDIO", "声音观察"],
                          ["METADATA", "媒体信息"],
                          ["MANUAL", "人工观察"],
                        ],
                        evidence.source,
                      )}
              </strong>
              {` · ${evidence.start_time}–${evidence.end_time} 秒：${evidence.observation}`}
            </li>
          ))}
        </ul>
      ) : (
        <small>尚未记录观察依据，未知项保持待确认。</small>
      )}
    </details>
  );
}

function FactSelect({
  label,
  caption,
  value,
  options,
  disabled,
  onChange,
}: {
  label: string;
  caption: string;
  value: string;
  options: readonly (readonly [string, string])[];
  disabled: boolean;
  onChange: (value: string) => void;
}) {
  const known = options.some(([key]) => key === value);
  return (
    <label className="shot-fact-control">
      <span>{caption}</span>
      <select
        aria-label={label}
        className="shot-table-select"
        disabled={disabled}
        value={value}
        onChange={(event) => onChange(event.target.value)}
      >
        {options.map(([key, text]) => (
          <option key={key} value={key}>
            {text}
          </option>
        ))}
        {!known ? <option value={value}>待确认（未识别）</option> : null}
      </select>
    </label>
  );
}

function FactInput({
  label,
  caption,
  value,
  disabled,
  onChange,
}: {
  label: string;
  caption: string;
  value: string;
  disabled: boolean;
  onChange: (value: string) => void;
}) {
  return (
    <label className="shot-fact-control">
      <span>{caption}</span>
      <input
        aria-label={label}
        className="shot-table-input"
        value={value}
        disabled={disabled}
        onChange={(event) => onChange(event.target.value)}
      />
    </label>
  );
}
