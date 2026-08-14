import { type FormEvent, useEffect, useState } from "react";

import {
  getLatestProjectAnalysis,
  getLatestProjectShotCards,
  type Project,
  readAnalysisPayload,
  readShotCardPayload,
  type ShotCard,
  saveShotCards,
} from "./api";
import { CharacterSelection } from "./CharacterSelection";

const SHOT_TEXT_FIELDS: Array<{
  key: Exclude<keyof ShotCard, "start_time" | "end_time">;
  label: string;
}> = [
  { key: "shot_id", label: "镜头编号" },
  { key: "shot_type", label: "景别" },
  { key: "composition", label: "构图" },
  { key: "camera_motion", label: "运镜" },
  { key: "subject", label: "主体" },
  { key: "action", label: "动作" },
  { key: "scene", label: "场景" },
  { key: "spoken_text", label: "原口播" },
  { key: "transition", label: "转场" },
];

export function AnalysisWorkspace({
  onClose,
  project,
}: {
  onClose: () => void;
  project: Project;
}) {
  const [analysisId, setAnalysisId] = useState("");
  const [analysisSummary, setAnalysisSummary] = useState("");
  const [durationSeconds, setDurationSeconds] = useState(0);
  const [shots, setShots] = useState<ShotCard[]>([]);
  const [error, setError] = useState("");
  const [saveMessage, setSaveMessage] = useState("");
  const [isLoading, setIsLoading] = useState(true);
  const [isSaving, setIsSaving] = useState(false);

  useEffect(() => {
    let isActive = true;
    setIsLoading(true);
    setError("");
    setSaveMessage("");

    async function loadWorkspace() {
      try {
        const version = await getLatestProjectAnalysis(project.id);
        if (!isActive) {
          return;
        }
        const payload = readAnalysisPayload(version);
        if (!payload) {
          setError("视频拆解数据格式无效，请重新执行拆解。");
          return;
        }
        setAnalysisId(version.id);
        setAnalysisSummary(payload.summary);
        setDurationSeconds(payload.duration_seconds);
        setShots(payload.shots);
        const savedShotCardVersion = await getLatestProjectShotCards(
          project.id,
        );
        if (!isActive || !savedShotCardVersion) {
          return;
        }
        const savedShotCards = readShotCardPayload(savedShotCardVersion);
        if (savedShotCards?.source_analysis_version_id === version.id) {
          setShots(savedShotCards.shots);
        }
      } catch (requestError) {
        if (isActive) {
          setError(
            requestError instanceof Error
              ? requestError.message
              : "读取视频拆解失败。",
          );
        }
      } finally {
        if (isActive) {
          setIsLoading(false);
        }
      }
    }

    void loadWorkspace();

    return () => {
      isActive = false;
    };
  }, [project.id]);

  function updateShot(index: number, key: keyof ShotCard, value: string) {
    setShots((current) =>
      current.map((shot, shotIndex) => {
        if (shotIndex !== index) {
          return shot;
        }
        const nextValue =
          key === "start_time" || key === "end_time" ? Number(value) : value;
        return { ...shot, [key]: nextValue } as ShotCard;
      }),
    );
    setSaveMessage("");
  }

  async function handleSave(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!analysisId) {
      return;
    }
    setIsSaving(true);
    setError("");
    setSaveMessage("");
    try {
      const savedVersion = await saveShotCards(analysisId, shots);
      setSaveMessage(`镜头卡片已保存为版本 #${savedVersion.version_number}。`);
    } catch (requestError) {
      setError(
        requestError instanceof Error
          ? requestError.message
          : "保存镜头卡片失败。",
      );
    } finally {
      setIsSaving(false);
    }
  }

  return (
    <section className="analysis-workspace" aria-labelledby="analysis-title">
      <div className="section-heading">
        <div>
          <span className="eyebrow">VIDEO ANALYSIS</span>
          <h2 id="analysis-title">镜头卡片</h2>
          <p>
            “{project.name}”的自动拆解结果。保存后会创建独立的人工修订版本。
          </p>
        </div>
        <button className="secondary-button" onClick={onClose} type="button">
          返回项目
        </button>
      </div>
      {isLoading ? <p className="status-note">正在读取视频拆解</p> : null}
      {error ? <p className="settings-error">{error}</p> : null}
      {!isLoading && !error ? (
        <form className="analysis-form" onSubmit={handleSave}>
          <div className="analysis-summary">
            <strong>{analysisSummary}</strong>
            <span>参考时长：{durationSeconds.toFixed(1)} 秒</span>
          </div>
          <CharacterSelection projectId={project.id} />
          <div className="shot-card-list">
            {shots.map((shot, index) => (
              <fieldset className="shot-card" key={shot.shot_id}>
                <legend>镜头 {index + 1}</legend>
                <div className="shot-time-grid">
                  <ShotInput
                    label={`${shot.shot_id} 开始时间`}
                    onChange={(value) => updateShot(index, "start_time", value)}
                    type="number"
                    value={String(shot.start_time)}
                  />
                  <ShotInput
                    label={`${shot.shot_id} 结束时间`}
                    onChange={(value) => updateShot(index, "end_time", value)}
                    type="number"
                    value={String(shot.end_time)}
                  />
                </div>
                <div className="shot-field-grid">
                  {SHOT_TEXT_FIELDS.map(({ key, label }) => (
                    <ShotInput
                      key={key}
                      label={`${shot.shot_id} ${label}`}
                      onChange={(value) => updateShot(index, key, value)}
                      value={shot[key]}
                    />
                  ))}
                </div>
              </fieldset>
            ))}
          </div>
          {saveMessage ? <p className="setup-success">{saveMessage}</p> : null}
          <button disabled={isSaving || !analysisId} type="submit">
            {isSaving ? "正在保存" : "保存镜头卡片"}
          </button>
        </form>
      ) : null}
    </section>
  );
}

function ShotInput({
  label,
  onChange,
  type = "text",
  value,
}: {
  label: string;
  onChange: (value: string) => void;
  type?: "number" | "text";
  value: string;
}) {
  return (
    <label>
      {label}
      <input
        min={type === "number" ? 0 : undefined}
        onChange={(event) => onChange(event.target.value)}
        step={type === "number" ? "0.1" : undefined}
        type={type}
        value={value}
      />
    </label>
  );
}
