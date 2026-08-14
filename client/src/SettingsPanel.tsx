import { type FormEvent, useEffect, useState } from "react";

import {
  type DiagnosticProviderResult,
  downloadDiagnosticReport,
  getSettings,
  type ProviderName,
  type ProviderSettings,
  type RuntimeSettings,
  runSettingsDiagnostic,
  type SettingsDiagnosticReport,
  type SettingsSnapshot,
  updateProviderSettings,
  updateRuntimeSettings,
} from "./api";

type ProviderField = {
  name: string;
  label: string;
  secret?: boolean;
  placeholder?: string;
};

const PROVIDER_FORMS: Record<
  ProviderName,
  { title: string; description: string; fields: ProviderField[] }
> = {
  metaso: {
    title: "H3 视频生成",
    description: "用于 MiniMax H3 图生视频任务。默认接口地址由应用维护。",
    fields: [{ name: "api_key", label: "H3 API Key", secret: true }],
  },
  apilio: {
    title: "模型服务（Apilio）",
    description:
      "默认 Key 用于图像模型；如 Gemini 使用独立令牌，可单独填写视频分析 Key。",
    fields: [
      { name: "api_key", label: "图像模型 API Key", secret: true },
      {
        name: "analysis_api_key",
        label: "Gemini 视频分析 API Key（可选）",
        secret: true,
      },
    ],
  },
  cos: {
    title: "腾讯云 COS",
    description:
      "默认对象存储。保持私有 Bucket，由系统签发临时上传和读取地址。",
    fields: [
      { name: "access_key_id", label: "SecretId", secret: true },
      { name: "secret_access_key", label: "SecretKey", secret: true },
      { name: "bucket", label: "Bucket" },
      { name: "region", label: "Region", placeholder: "例如 ap-shanghai" },
    ],
  },
  oss: {
    title: "阿里云 OSS",
    description: "备用对象存储。只在运行设置中切换为当前存储后用于新素材。",
    fields: [
      { name: "access_key_id", label: "AccessKey ID", secret: true },
      { name: "secret_access_key", label: "AccessKey Secret", secret: true },
      { name: "bucket", label: "Bucket" },
      { name: "endpoint", label: "Endpoint", placeholder: "https://oss-…" },
    ],
  },
};

const PROVIDER_ORDER: ProviderName[] = ["metaso", "apilio", "cos", "oss"];

export function SettingsPanel() {
  const [settings, setSettings] = useState<SettingsSnapshot | null>(null);
  const [loadError, setLoadError] = useState("");
  const [diagnostic, setDiagnostic] = useState<SettingsDiagnosticReport | null>(
    null,
  );
  const [diagnosticError, setDiagnosticError] = useState("");
  const [isTesting, setIsTesting] = useState(false);
  const [isDownloading, setIsDownloading] = useState(false);

  useEffect(() => {
    let isMounted = true;
    getSettings()
      .then((snapshot) => {
        if (isMounted) {
          setSettings(snapshot);
          setLoadError("");
        }
      })
      .catch(() => {
        if (isMounted) {
          setLoadError(
            "无法读取设置。请确认本地服务已启动且当前身份具有管理员权限。",
          );
        }
      });

    return () => {
      isMounted = false;
    };
  }, []);

  async function saveProvider(
    provider: ProviderName,
    config: Record<string, string>,
  ) {
    const updated = await updateProviderSettings(provider, config);
    setSettings((current) =>
      current
        ? {
            ...current,
            providers: { ...current.providers, [provider]: updated },
          }
        : current,
    );
  }

  async function saveRuntime(runtime: RuntimeSettings) {
    const updated = await updateRuntimeSettings(runtime);
    setSettings((current) =>
      current ? { ...current, runtime: updated } : current,
    );
  }

  async function testSettings() {
    setIsTesting(true);
    setDiagnosticError("");
    try {
      setDiagnostic(await runSettingsDiagnostic());
    } catch {
      setDiagnosticError(
        "测试设置失败。请下载本地服务日志或检查网络、权限与配置后重试。",
      );
    } finally {
      setIsTesting(false);
    }
  }

  async function downloadDiagnostic() {
    if (!diagnostic) {
      return;
    }
    setIsDownloading(true);
    setDiagnosticError("");
    try {
      await downloadDiagnosticReport(diagnostic.download_url, diagnostic.id);
    } catch (error) {
      setDiagnosticError(
        error instanceof Error ? error.message : "下载诊断日志失败。",
      );
    } finally {
      setIsDownloading(false);
    }
  }

  if (loadError) {
    return (
      <section className="settings-error" role="alert">
        {loadError}
      </section>
    );
  }

  if (!settings) {
    return <p className="status-note">正在读取服务设置</p>;
  }

  return (
    <section className="settings-page" aria-labelledby="settings-title">
      <div className="section-heading">
        <div>
          <span className="eyebrow">SERVICE SETTINGS</span>
          <h2 id="settings-title">服务设置</h2>
        </div>
        <span className="settings-hint">密钥加密保存，页面不会回显原值</span>
      </div>

      <div className="provider-grid">
        {PROVIDER_ORDER.map((provider) => (
          <ProviderForm
            key={provider}
            provider={provider}
            settings={settings.providers[provider]}
            onSave={saveProvider}
          />
        ))}
      </div>

      <RuntimeForm runtime={settings.runtime} onSave={saveRuntime} />

      <section className="diagnostic-panel" aria-labelledby="diagnostic-title">
        <div>
          <span className="eyebrow">TEST MODE</span>
          <h3 id="diagnostic-title">测试设置</h3>
          <p>
            逐项检查已保存的服务参数并生成脱敏日志。已配置的 COS/OSS
            会创建并删除一个小测试对象，可能产生云存储请求费用；该操作不会提交
            H3、视频或图片生成任务。H3
            与模型服务的检测只确认配置，不发起计费调用。
          </p>
        </div>
        <button type="button" onClick={testSettings} disabled={isTesting}>
          {isTesting ? "正在测试" : "测试设置"}
        </button>

        {diagnostic ? (
          <DiagnosticResult
            report={diagnostic}
            isDownloading={isDownloading}
            onDownload={downloadDiagnostic}
          />
        ) : null}
        {diagnosticError ? (
          <p className="settings-error" role="alert">
            {diagnosticError}
          </p>
        ) : null}
      </section>
    </section>
  );
}

function ProviderForm({
  provider,
  settings,
  onSave,
}: {
  provider: ProviderName;
  settings: ProviderSettings;
  onSave: (
    provider: ProviderName,
    config: Record<string, string>,
  ) => Promise<void>;
}) {
  const form = PROVIDER_FORMS[provider];
  const [values, setValues] = useState<Record<string, string>>(() =>
    initialValues(form.fields, settings.config),
  );
  const [status, setStatus] = useState("");
  const [isSaving, setIsSaving] = useState(false);

  useEffect(() => {
    setValues(initialValues(form.fields, settings.config));
  }, [form.fields, settings.config]);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setIsSaving(true);
    setStatus("");
    try {
      await onSave(provider, values);
      setValues((current) => clearSecretFields(current, form.fields));
      setStatus("已保存");
    } catch {
      setStatus("保存失败，请检查必填项与管理员权限。");
    } finally {
      setIsSaving(false);
    }
  }

  return (
    <form className="provider-card" onSubmit={submit}>
      <div>
        <h3>{form.title}</h3>
        <p>{form.description}</p>
      </div>
      <span
        className={
          settings.configured
            ? "config-state config-state--ready"
            : "config-state"
        }
      >
        {settings.configured ? "已配置" : "未配置"}
      </span>
      <div className="field-stack">
        {form.fields.map((field) => (
          <label key={field.name}>
            {field.label}
            <input
              type={field.secret ? "password" : "text"}
              value={values[field.name] ?? ""}
              placeholder={
                field.secret && settings.configured
                  ? "已保存，留空不修改"
                  : field.placeholder
              }
              onChange={(event) =>
                setValues((current) => ({
                  ...current,
                  [field.name]: event.target.value,
                }))
              }
            />
          </label>
        ))}
      </div>
      <div className="form-actions">
        <button type="submit" disabled={isSaving}>
          {isSaving ? "正在保存" : "保存"}
        </button>
        {status ? <span role="status">{status}</span> : null}
      </div>
    </form>
  );
}

function RuntimeForm({
  runtime,
  onSave,
}: {
  runtime: RuntimeSettings;
  onSave: (runtime: RuntimeSettings) => Promise<void>;
}) {
  const [values, setValues] = useState(runtime);
  const [status, setStatus] = useState("");
  const isStorageChangePending =
    values.active_storage_provider !== runtime.active_storage_provider;

  useEffect(() => setValues(runtime), [runtime]);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setStatus("");
    try {
      await onSave(values);
      setStatus("运行设置已保存");
    } catch {
      setStatus("运行设置保存失败");
    }
  }

  return (
    <form className="runtime-form" onSubmit={submit}>
      <div>
        <span className="eyebrow">RUNTIME</span>
        <h3>运行设置</h3>
      </div>
      <label>
        当前对象存储
        <select
          value={values.active_storage_provider}
          onChange={(event) =>
            setValues((current) => ({
              ...current,
              active_storage_provider: event.target.value as "cos" | "oss",
            }))
          }
        >
          <option value="cos">腾讯云 COS</option>
          <option value="oss">阿里云 OSS</option>
        </select>
      </label>
      <label>
        单次生成数量上限
        <input
          type="number"
          min="1"
          value={values.max_generation_count_per_batch}
          onChange={(event) =>
            setValues((current) => ({
              ...current,
              max_generation_count_per_batch: Number(event.target.value),
            }))
          }
        />
      </label>
      <label>
        H3 最大并发数
        <input
          type="number"
          min="1"
          value={values.max_concurrent_h3_tasks}
          onChange={(event) =>
            setValues((current) => ({
              ...current,
              max_concurrent_h3_tasks: Number(event.target.value),
            }))
          }
        />
      </label>
      <div className="form-actions">
        <button type="submit">保存运行设置</button>
        <span>
          {isStorageChangePending
            ? `尚未保存：将切换为${storageProviderLabel(values.active_storage_provider)}`
            : `当前已保存：${storageProviderLabel(runtime.active_storage_provider)}`}
        </span>
        {status ? <span role="status">{status}</span> : null}
      </div>
    </form>
  );
}

function DiagnosticResult({
  report,
  isDownloading,
  onDownload,
}: {
  report: SettingsDiagnosticReport;
  isDownloading: boolean;
  onDownload: () => Promise<void>;
}) {
  return (
    <div className="diagnostic-result">
      <strong>
        {report.status === "ok" ? "设置检查通过" : "检测到需要处理的配置项"}
      </strong>
      <ul>
        {report.providers.map((provider) => (
          <li key={provider.provider}>
            <span>{PROVIDER_FORMS[provider.provider].title}</span>
            <span
              className={`diagnostic-status diagnostic-status--${provider.status}`}
            >
              {diagnosticStatusLabel(provider.status)}
            </span>
            <p>{provider.message}</p>
          </li>
        ))}
      </ul>
      <button
        type="button"
        className="secondary-button"
        onClick={onDownload}
        disabled={isDownloading}
      >
        {isDownloading ? "正在下载" : "下载诊断日志"}
      </button>
    </div>
  );
}

function initialValues(
  fields: ProviderField[],
  config: Record<string, string>,
) {
  return Object.fromEntries(
    fields.map((field) => [
      field.name,
      field.secret ? "" : (config[field.name] ?? ""),
    ]),
  );
}

function clearSecretFields(
  values: Record<string, string>,
  fields: ProviderField[],
) {
  const next = { ...values };
  for (const field of fields) {
    if (field.secret) {
      next[field.name] = "";
    }
  }
  return next;
}

function diagnosticStatusLabel(status: DiagnosticProviderResult["status"]) {
  return {
    ok: "通过",
    not_configured: "未配置",
    configured_only: "已配置（不调用）",
    error: "失败",
  }[status];
}

function storageProviderLabel(
  provider: RuntimeSettings["active_storage_provider"],
) {
  return provider === "cos" ? "腾讯云 COS" : "阿里云 OSS";
}
