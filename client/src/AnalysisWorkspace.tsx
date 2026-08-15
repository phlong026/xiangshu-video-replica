import { type FormEvent, useEffect, useState } from "react";

import {
  type AnalysisProvider,
  getLatestProjectAnalysis,
  getLatestProjectShotCards,
  type Project,
  readAnalysisPayload,
  readAnalysisProvider,
  readShotCardPayload,
  type ShotCard,
  saveShotCards,
} from "./api";
import { CharacterSelection } from "./CharacterSelection";
import { FirstFrameSelection } from "./FirstFrameSelection";
import { SourceFrameSelection } from "./SourceFrameSelection";

function toNonNegativeTime(value: string): number {
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed >= 0 ? parsed : 0;
}

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
  const [analysisProvider, setAnalysisProvider] =
    useState<AnalysisProvider | null>(null);
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
    setAnalysisProvider(null);

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
        setAnalysisProvider(readAnalysisProvider(version));
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
          key === "start_time" || key === "end_time"
            ? toNonNegativeTime(value)
            : value;
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
    if (shots.some((shot) => shot.end_time < shot.start_time)) {
      setError("镜头时间无效：结束时间不能早于开始时间。");
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
      <WorkflowSteps />
      {isLoading ? <p className="status-note">正在读取视频拆解</p> : null}
      {error ? <p className="settings-error">{error}</p> : null}
      {!isLoading && !error ? (
        <form className="analysis-form" onSubmit={handleSave}>
          <div className="analysis-summary">
            <strong>{analysisSummary}</strong>
            <div className="analysis-meta">
              <span>参考时长：{durationSeconds.toFixed(1)} 秒</span>
              {analysisProvider ? (
                <span>拆解来源：{providerLabel(analysisProvider)}</span>
              ) : null}
            </div>
          </div>
          {analysisProvider === "fake_gemini" ? (
            <p className="status-note">
              当前显示的是内置模拟结果。请在设置中保存 Gemini 视频分析 API
              Key，并配置可用的 COS 或 OSS 存储后重新拆解。
            </p>
          ) : null}
          <div className="analysis-workspace-grid">
            <div className="analysis-primary">
              <SourceFrameSelection
                projectId={project.id}
                referenceAssetId={project.reference_asset_id}
              />
              <FirstFrameSelection projectId={project.id} />
              <div className="shot-card-list">
                {shots.map((shot, index) => (
                  <fieldset className="shot-card" key={shot.shot_id}>
                    <legend>镜头 {index + 1}</legend>
                    <div className="shot-time-grid">
                      <ShotInput
                        label={`${shot.shot_id} 开始时间`}
                        onChange={(value) =>
                          updateShot(index, "start_time", value)
                        }
                        type="number"
                        value={String(shot.start_time)}
                      />
                      <ShotInput
                        label={`${shot.shot_id} 结束时间`}
                        onChange={(value) =>
                          updateShot(index, "end_time", value)
                        }
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
              {saveMessage ? (
                <p className="setup-success">{saveMessage}</p>
              ) : null}
              <button disabled={isSaving || !analysisId} type="submit">
                {isSaving ? "正在保存" : "保存镜头卡片"}
              </button>
            </div>
            <aside className="analysis-sidebar" aria-label="当前人物设定">
              <CharacterSelection projectId={project.id} />
            </aside>
          </div>
        </form>
      ) : null}
    </section>
  );
}

function WorkflowSteps() {
  const steps = [
    "上传参考视频",
    "视频拆解",
    "选择源画面",
    "首帧生成",
    "生成结果",
  ];

  return (
    <ol className="workflow-steps" aria-label="复刻流程">
      {steps.map((step, index) => {
        const isComplete = index < 2;
        const isCurrent = index === 2;
        return (
          <li
            className={
              isCurrent
                ? "workflow-step workflow-step--current"
                : isComplete
                  ? "workflow-step workflow-step--complete"
                  : "workflow-step"
            }
            key={step}
          >
            <span aria-hidden="true">{isComplete ? "✓" : index + 1}</span>
            <strong aria-current={isCurrent ? "step" : undefined}>
              {step}
            </strong>
          </li>
        );
      })}
    </ol>
  );
}

function providerLabel(provider: AnalysisProvider) {
  return provider === "apilio_gemini"
    ? "Gemini 3.1 Pro（Apilio）"
    : "内置模拟拆解（尚未调用 Gemini）";
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
