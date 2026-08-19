import { type FormEvent, useEffect, useRef, useState } from "react";

import {
  getSettings,
  type ProviderName,
  type ProviderSettings,
  type ProviderTestResult,
  type RuntimeSettings,
  type SettingsSnapshot,
  testProviderConnection,
  updateProviderSettings,
  updateRuntimeSettings,
} from "./api";

type ProviderField = {
  name: string;
  label: string;
  secret?: boolean;
  placeholder?: string;
};

type ProviderFormSpec = {
  title: string;
  note?: string;
  fields: ProviderField[];
};

// COS 区域固定为上海，界面不再显示 Region 输入框。
const COS_REGION = "ap-shanghai";

const PROVIDER_FORMS: Record<ProviderName, ProviderFormSpec> = {
  metaso: {
    title: "视频生成",
    fields: [{ name: "api_key", label: "API Key", secret: true }],
  },
  apilio: {
    title: "模型服务",
    fields: [
      { name: "api_key", label: "图像模型 API Key", secret: true },
      {
        name: "analysis_api_key",
        label: "视频分析 API Key（可选）",
        secret: true,
      },
    ],
  },
  cos: {
    title: "腾讯云存储",
    note: "区域固定为上海 · 测试连接会创建并删除一个临时对象",
    fields: [
      { name: "access_key_id", label: "SecretId", secret: true },
      { name: "secret_access_key", label: "SecretKey", secret: true },
      { name: "bucket", label: "Bucket" },
    ],
  },
  deepseek: {
    title: "AI 改写",
    note: "二创口播稿改写 · 默认 DeepSeek，只需 API Key",
    fields: [{ name: "api_key", label: "API Key", secret: true }],
  },
};

const PROVIDER_ORDER: ProviderName[] = ["metaso", "apilio", "cos", "deepseek"];

export function SettingsPanel() {
  const [settings, setSettings] = useState<SettingsSnapshot | null>(null);
  const [loadError, setLoadError] = useState("");

  useEffect(() => {
    let isMounted = true;
    getSettings()
      .then((snapshot) => {
        if (isMounted) {
          setSettings(snapshot);
          setLoadError("");
        }
      })
      .catch((error: unknown) => {
        if (isMounted) {
          setLoadError(
            error instanceof Error
              ? error.message
              : "无法读取设置。请确认本地服务已启动且当前身份具有管理员权限。",
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
    const finalConfig =
      provider === "cos" ? { ...config, region: COS_REGION } : config;
    const updated = await updateProviderSettings(provider, finalConfig);
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
    <section className="settings-page" aria-label="服务设置">
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
  const [visibleFields, setVisibleFields] = useState<Record<string, boolean>>(
    {},
  );
  const [status, setStatus] = useState("");
  const [statusTone, setStatusTone] = useState<"ok" | "error">("ok");
  const [isSaving, setIsSaving] = useState(false);
  const [isTesting, setIsTesting] = useState(false);

  useEffect(() => {
    setValues(initialValues(form.fields, settings.config));
  }, [form.fields, settings.config]);

  function toggleSecretVisibility(name: string) {
    setVisibleFields((current) => ({ ...current, [name]: !current[name] }));
  }

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (isSaving) {
      return;
    }
    setIsSaving(true);
    setStatus("");
    try {
      await onSave(provider, values);
      setValues((current) => clearSecretFields(current, form.fields));
      setVisibleFields({});
      setStatus("已保存");
      setStatusTone("ok");
    } catch {
      setStatus("保存失败，请检查必填项与管理员权限。");
      setStatusTone("error");
    } finally {
      setIsSaving(false);
    }
  }

  async function handleTest() {
    if (isTesting) {
      return;
    }
    setIsTesting(true);
    setStatus("");
    try {
      const result = await testProviderConnection(provider);
      setStatus(testResultLabel(result));
      setStatusTone(result.status === "not_configured" ? "error" : "ok");
    } catch (error) {
      setStatus(
        error instanceof Error
          ? error.message
          : "测试失败，请检查网络与管理员权限后重试。",
      );
      setStatusTone("error");
    } finally {
      setIsTesting(false);
    }
  }

  return (
    <form className="provider-card" data-provider={provider} onSubmit={submit}>
      <div>
        <h3>{form.title}</h3>
        {form.note ? <p>{form.note}</p> : null}
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
        {form.fields.map((field) => {
          const isVisible = Boolean(visibleFields[field.name]);
          return (
            <label key={field.name}>
              {field.label}
              <span className={field.secret ? "secret-field" : undefined}>
                <input
                  type={field.secret && !isVisible ? "password" : "text"}
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
                {field.secret ? (
                  <button
                    type="button"
                    className="secret-toggle"
                    aria-label={
                      isVisible ? `隐藏${field.label}` : `显示${field.label}`
                    }
                    aria-pressed={isVisible}
                    onClick={() => toggleSecretVisibility(field.name)}
                  >
                    <SecretToggleIcon visible={isVisible} />
                  </button>
                ) : null}
              </span>
            </label>
          );
        })}
      </div>
      <div className="form-actions">
        <button type="submit" disabled={isSaving || isTesting}>
          {isSaving ? "正在保存" : "保存"}
        </button>
        <button
          type="button"
          className="secondary-button"
          onClick={handleTest}
          disabled={isSaving || isTesting}
        >
          {isTesting ? "正在测试" : "测试连接"}
        </button>
        {status ? (
          <span
            role="status"
            className={
              statusTone === "error" ? "form-status--error" : undefined
            }
          >
            {status}
          </span>
        ) : null}
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
  const [isSaving, setIsSaving] = useState(false);
  const previousRuntimeRef = useRef(runtime);
  const isStorageChangePending =
    values.active_storage_provider !== runtime.active_storage_provider;

  useEffect(() => {
    if (previousRuntimeRef.current === runtime) {
      return;
    }
    previousRuntimeRef.current = runtime;
    setValues(runtime);
  }, [runtime]);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (isSaving) {
      return;
    }
    setStatus("");
    const limitsValid =
      Number.isInteger(values.max_generation_count_per_batch) &&
      values.max_generation_count_per_batch >= 1 &&
      Number.isInteger(values.max_concurrent_h3_tasks) &&
      values.max_concurrent_h3_tasks >= 1;
    if (!limitsValid) {
      setStatus("数量上限与并发数必须为 ≥1 的整数");
      return;
    }
    setIsSaving(true);
    try {
      await onSave(values);
      setStatus("已保存");
    } catch {
      setStatus("保存失败");
    } finally {
      setIsSaving(false);
    }
  }

  return (
    <form className="runtime-form" onSubmit={submit}>
      <h3>运行设置</h3>
      <div className="runtime-fields">
        <label>
          存储方式
          <select
            disabled={isSaving}
            value={values.active_storage_provider}
            onChange={(event) =>
              setValues((current) => ({
                ...current,
                active_storage_provider: event.target.value as "cos" | "local",
              }))
            }
          >
            <option value="cos">腾讯云存储</option>
            <option value="local">本地存储（仅开发）</option>
          </select>
        </label>
        <label>
          单次生成数量上限
          <input
            disabled={isSaving}
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
          视频生成并发数
          <input
            disabled={isSaving}
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
      </div>
      {values.active_storage_provider === "cos" ? (
        <p className="storage-provider-hint">
          请先在存储桶的跨域访问 CORS 设置中放行本应用的 PUT/GET/HEAD
          请求，否则上传会失败。
        </p>
      ) : null}
      <div className="form-actions">
        <button disabled={isSaving} type="submit">
          {isSaving ? "正在保存" : "保存"}
        </button>
        {isStorageChangePending ? (
          <span className="runtime-pending">存储方式修改尚未保存</span>
        ) : null}
        {status ? <span role="status">{status}</span> : null}
      </div>
    </form>
  );
}

function SecretToggleIcon({ visible }: { visible: boolean }) {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true">
      {visible ? (
        <>
          <path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94" />
          <path d="M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19" />
          <path d="M14.12 14.12a3 3 0 1 1-4.24-4.24" />
          <line x1="1" y1="1" x2="23" y2="23" />
        </>
      ) : (
        <>
          <path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z" />
          <circle cx="12" cy="12" r="3" />
        </>
      )}
    </svg>
  );
}

function testResultLabel(result: ProviderTestResult) {
  switch (result.status) {
    case "ok":
      return "连接测试通过";
    case "configured_only":
      return "参数已保存；测试不会发起外部调用";
    default:
      return "尚未保存该服务的必要参数";
  }
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
