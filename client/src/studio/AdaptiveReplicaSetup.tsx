import { type ReactNode, useState } from "react";
import { Button, Icon, StudioDialog } from "./ui";

const preferences = new Map<string, string>();

export function replicaPreference(accountId: string, field: string) {
  const key = JSON.stringify(["studio:adaptive-replica", accountId, field]);
  try {
    return window.localStorage.getItem(key) ?? preferences.get(key);
  } catch {
    return preferences.get(key) ?? null;
  }
}

export function saveReplicaPreference(
  accountId: string,
  field: string,
  value: string,
) {
  const key = JSON.stringify(["studio:adaptive-replica", accountId, field]);
  let persisted = false;
  try {
    window.localStorage.setItem(key, value);
    persisted = true;
  } catch {
    // 禁止本地存储时，同一会话仍只自动打开一次。
  }
  preferences.set(key, value);
  return persisted;
}

export function ReplicaDefaultSummary({
  label,
  name,
  imageUrl,
  loading = false,
  unavailable = false,
  disabled = false,
  emptyLabel = "选择人物",
  onChoose,
}: {
  label: string;
  name?: string;
  imageUrl?: string;
  loading?: boolean;
  unavailable?: boolean;
  disabled?: boolean;
  emptyLabel?: string;
  onChoose: () => void;
}) {
  return (
    <fieldset
      className="replica-default-summary"
      aria-label={label}
      aria-busy={loading}
    >
      <span className="replica-default-summary__avatar" aria-hidden="true">
        {imageUrl && name ? (
          <img src={imageUrl} alt="" />
        ) : (
          <Icon name="person" size={22} />
        )}
      </span>
      <div className="replica-default-summary__text">
        <span>{label}</span>
        <strong>
          {loading
            ? "正在读取…"
            : name || (unavailable ? "默认素材暂不可用" : "尚未设置")}
        </strong>
      </div>
      <Button
        className="replica-default-summary__action"
        variant="quiet"
        aria-label={name ? `更换${label}` : emptyLabel}
        disabled={disabled || loading}
        onClick={onChoose}
      >
        {name ? "更换" : emptyLabel}
      </Button>
    </fieldset>
  );
}

export function ReplicaDetailActions({
  children,
  secondaryAction,
  settings,
  feeNote,
}: {
  children: ReactNode;
  secondaryAction?: ReactNode;
  settings: ReactNode;
  feeNote: string;
}) {
  return (
    <div className="replica-detail-control">
      {settings}
      <div className="replica-detail-actions">
        {children}
        {secondaryAction}
      </div>
      <p className="replica-detail-fee">{feeNote}</p>
    </div>
  );
}

const guide = [
  [
    "一键识别时长",
    "4–30 秒（含 30 秒）走 AI 视频复刻；超过 30 秒走数字人口播。时长未知或不足 4 秒，可手动提取文案。",
  ],
  [
    "设置你的默认素材",
    "AI 视频选人物版本；数字人口播选已就绪的视频分身和已试听确认的声音。保存后，本设备上的同一账号下次直接复用；可以随时更换。",
  ],
  [
    "确认费用并查看进度",
    "拆解、首帧或转写可能产生积分。成片前会单独展示报价，确认后才生成。口播文案可在报价前编辑，生成后到任务中心查看。",
  ],
] as const;

export function AdaptiveReplicaGuide({
  onClose,
  onComplete,
}: {
  onClose: () => void;
  onComplete: () => void;
}) {
  const [step, setStep] = useState(0);
  const item = guide[step] ?? guide[0];
  const last = step === guide.length - 1;
  return (
    <StudioDialog title="一键复刻使用教程" onClose={onClose}>
      <p>
        第 {step + 1} / {guide.length} 步
      </p>
      <h3>{item[0]}</h3>
      <p>{item[1]}</p>
      <footer className="replica-pipeline-actions">
        {step > 0 && (
          <Button variant="outline" onClick={() => setStep(step - 1)}>
            上一步
          </Button>
        )}
        <Button
          variant="primary"
          onClick={last ? onComplete : () => setStep(step + 1)}
        >
          {last ? "知道了" : "下一步"}
        </Button>
      </footer>
    </StudioDialog>
  );
}
