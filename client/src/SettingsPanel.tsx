import { type FormEvent, useEffect, useRef, useState } from "react";

import {
  type BillingSettings,
  getControlSettings,
  getSettings,
  type ProviderName,
  type ProviderSettings,
  type ProviderTestResult,
  type RuntimeSettings,
  revealProviderSecret,
  type SettingsSnapshot,
  testControlProviderConnection,
  testProviderConnection,
  updateBillingSettings,
  updateControlBillingSettings,
  updateControlProviderSettings,
  updateControlRuntimeSettings,
  updateProviderSettings,
  updateRuntimeSettings,
} from "./api";

function visibleErrorMessage(error: unknown, fallback: string): string {
  return error instanceof Error && error.message.trim()
    ? error.message
    : fallback;
}

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
  hifly: {
    title: "数字人口播",
    note: "Hifly · 只读检查账户余额，不会创建收费任务",
    fields: [{ name: "api_key", label: "API Key", secret: true }],
  },
  tikhub: {
    title: "爆款视频数据源",
    note: "抖音 / 视频号最近 7 天爆款参考库 · 只需 API Key",
    fields: [{ name: "api_key", label: "API Key", secret: true }],
  },
  dashscope: {
    title: "语音转写",
    note: "上传视频提取文案 · 只需 API Key",
    fields: [{ name: "api_key", label: "API Key", secret: true }],
  },
  douyidou: {
    title: "链接解析",
    note: "抖音 / 快手 / 小红书链接去水印与文案提取",
    fields: [
      { name: "app_id", label: "App ID" },
      { name: "app_secret", label: "App Secret", secret: true },
    ],
  },
};

const PROVIDER_ORDER: ProviderName[] = [
  "metaso",
  "apilio",
  "cos",
  "deepseek",
  "hifly",
  "tikhub",
  "dashscope",
  "douyidou",
];

export function SettingsPanel({
  source = "workspace",
  readOnly = false,
}: {
  source?: "workspace" | "control";
  readOnly?: boolean;
}) {
  const [settings, setSettings] = useState<SettingsSnapshot | null>(null);
  const [loadError, setLoadError] = useState("");

  useEffect(() => {
    let isMounted = true;
    (source === "control" ? getControlSettings() : getSettings())
      .then((snapshot) => {
        if (isMounted) {
          setSettings(snapshot);
          setLoadError("");
        }
      })
      .catch((error: unknown) => {
        if (isMounted) {
          setLoadError(
            visibleErrorMessage(
              error,
              "无法读取设置。请确认本地服务已启动且当前身份具有管理员权限。",
            ),
          );
        }
      });

    return () => {
      isMounted = false;
    };
  }, [source]);

  async function saveProvider(
    provider: ProviderName,
    config: Record<string, string>,
  ) {
    const finalConfig =
      provider === "cos" ? { ...config, region: COS_REGION } : config;
    const updated = await (source === "control"
      ? updateControlProviderSettings(provider, finalConfig)
      : updateProviderSettings(provider, finalConfig));
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
    const updated = await (source === "control"
      ? updateControlRuntimeSettings(runtime)
      : updateRuntimeSettings(runtime));
    setSettings((current) =>
      current ? { ...current, runtime: updated } : current,
    );
  }

  async function revealSavedSecret(provider: ProviderName, field: string) {
    if (source === "control") {
      throw new Error("控制台不支持显示已保存密钥");
    }
    return revealProviderSecret(provider, field);
  }

  async function saveBilling(billing: BillingSettings) {
    const payload = {
      internal_base_unit_price_fen: billing.internal_base_unit_price_fen,
      oral_unit_price_fen: billing.oral_unit_price_fen,
      min_recharge_fen: billing.min_recharge_fen,
      recharge_step_fen: billing.recharge_step_fen,
    };
    const updated = await (source === "control"
      ? updateControlBillingSettings(payload)
      : updateBillingSettings(payload));
    setSettings((current) =>
      current ? { ...current, billing: updated } : current,
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
        {PROVIDER_ORDER.map((provider) => {
          // 服务端快照可能落后于前端枚举（灰度/旧版本），缺失的 provider
          // 直接跳过，不让整个设置页白屏。
          const providerSettings = settings.providers[provider];
          if (!providerSettings) return null;
          return (
            <ProviderForm
              key={provider}
              provider={provider}
              readOnly={readOnly}
              settings={providerSettings}
              onSave={saveProvider}
              onReveal={source === "workspace" ? revealSavedSecret : undefined}
              onTest={
                source === "control"
                  ? testControlProviderConnection
                  : testProviderConnection
              }
            />
          );
        })}
      </div>

      <RuntimeForm
        readOnly={readOnly}
        runtime={settings.runtime}
        onSave={saveRuntime}
      />
      <OralPriceForm
        readOnly={readOnly}
        billing={settings.billing}
        onSave={saveBilling}
      />
    </section>
  );
}

function OralPriceForm({
  billing,
  readOnly,
  onSave,
}: {
  billing: BillingSettings;
  readOnly: boolean;
  onSave: (billing: BillingSettings) => Promise<void>;
}) {
  const [priceYuan, setPriceYuan] = useState(
    (billing.oral_unit_price_fen / 100).toString(),
  );
  const [status, setStatus] = useState("");
  const [isSaving, setIsSaving] = useState(false);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (isSaving) return;
    const numericPrice = Number(priceYuan);
    const oralUnitPriceFen = Math.round(numericPrice * 100);
    if (!Number.isFinite(numericPrice) || oralUnitPriceFen < 1) {
      setStatus("口播单价必须大于 0 元");
      return;
    }
    setIsSaving(true);
    setStatus("");
    try {
      await onSave({ ...billing, oral_unit_price_fen: oralUnitPriceFen });
      setStatus("口播价格已保存");
    } catch {
      setStatus("口播价格保存失败");
    } finally {
      setIsSaving(false);
    }
  }

  return (
    <form className="runtime-form" onSubmit={submit}>
      <h3>数字人口播价格</h3>
      <div className="runtime-fields">
        <label>
          数字人口播单价（元/条）
          <input
            disabled={readOnly || isSaving}
            type="number"
            min="0.01"
            step="0.01"
            value={priceYuan}
            onChange={(event) => setPriceYuan(event.target.value)}
          />
        </label>
      </div>
      <p className="storage-provider-hint">
        任务创建时冻结价格快照，后续改价不影响已创建任务。
      </p>
      <div className="form-actions">
        <button disabled={readOnly || isSaving} type="submit">
          {isSaving ? "正在保存" : "保存口播价格"}
        </button>
        {status ? <span role="status">{status}</span> : null}
      </div>
    </form>
  );
}

function ProviderForm({
  provider,
  readOnly,
  settings,
  onSave,
  onReveal,
  onTest,
}: {
  provider: ProviderName;
  readOnly: boolean;
  settings: ProviderSettings;
  onSave: (
    provider: ProviderName,
    config: Record<string, string>,
  ) => Promise<void>;
  onReveal?: (provider: ProviderName, field: string) => Promise<string>;
  onTest: (provider: ProviderName) => Promise<ProviderTestResult>;
}) {
  const form = PROVIDER_FORMS[provider];
  const [values, setValues] = useState<Record<string, string>>(() =>
    initialValues(form.fields, settings.config),
  );
  const [visibleFields, setVisibleFields] = useState<Record<string, boolean>>(
    {},
  );
  const [revealedFields, setRevealedFields] = useState<Record<string, boolean>>(
    {},
  );
  const [revealingFields, setRevealingFields] = useState<
    Record<string, boolean>
  >({});
  const [status, setStatus] = useState("");
  const [statusTone, setStatusTone] = useState<"ok" | "error">("ok");
  const [isSaving, setIsSaving] = useState(false);
  const [isTesting, setIsTesting] = useState(false);
  const previousConfigRef = useRef(settings.config);

  useEffect(() => {
    if (previousConfigRef.current === settings.config) {
      return;
    }
    previousConfigRef.current = settings.config;
    setValues(initialValues(form.fields, settings.config));
  }, [form.fields, settings.config]);

  async function toggleSecretVisibility(name: string) {
    if (visibleFields[name]) {
      setVisibleFields((current) => ({ ...current, [name]: false }));
      if (revealedFields[name]) {
        setValues((current) => ({ ...current, [name]: "" }));
        setRevealedFields((current) => ({ ...current, [name]: false }));
      }
      return;
    }

    if (values[name] || !settings.configured) {
      setVisibleFields((current) => ({ ...current, [name]: true }));
      return;
    }

    if (!onReveal || revealingFields[name]) {
      return;
    }

    setRevealingFields((current) => ({ ...current, [name]: true }));
    setStatus("");
    try {
      const value = await onReveal(provider, name);
      setValues((current) => ({ ...current, [name]: value }));
      setRevealedFields((current) => ({ ...current, [name]: true }));
      setVisibleFields((current) => ({ ...current, [name]: true }));
    } catch (error) {
      setStatus(visibleErrorMessage(error, "读取已保存密钥失败。"));
      setStatusTone("error");
    } finally {
      setRevealingFields((current) => ({ ...current, [name]: false }));
    }
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
      setRevealedFields({});
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
      const result = await onTest(provider);
      setStatus(testResultLabel(result));
      setStatusTone(testResultSucceeded(result) ? "ok" : "error");
    } catch (error) {
      setStatus(
        visibleErrorMessage(error, "测试失败，请检查网络与管理员权限后重试。"),
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
          const isRevealing = Boolean(revealingFields[field.name]);
          return (
            <label key={field.name}>
              {field.label}
              <span className={field.secret ? "secret-field" : undefined}>
                <input
                  disabled={readOnly || isRevealing}
                  type={field.secret && !isVisible ? "password" : "text"}
                  value={values[field.name] ?? ""}
                  placeholder={
                    field.secret && settings.configured
                      ? "已保存，留空不修改"
                      : field.placeholder
                  }
                  onChange={(event) => {
                    setValues((current) => ({
                      ...current,
                      [field.name]: event.target.value,
                    }));
                    setRevealedFields((current) => ({
                      ...current,
                      [field.name]: false,
                    }));
                  }}
                />
                {field.secret ? (
                  <button
                    disabled={readOnly || isRevealing}
                    type="button"
                    className="secret-toggle"
                    aria-label={
                      isRevealing
                        ? `正在读取${field.label}`
                        : isVisible
                          ? `隐藏${field.label}`
                          : `显示${field.label}`
                    }
                    aria-pressed={isVisible}
                    onClick={() => void toggleSecretVisibility(field.name)}
                  >
                    <span aria-hidden="true">✨</span>
                  </button>
                ) : null}
              </span>
            </label>
          );
        })}
      </div>
      <div className="form-actions">
        <button type="submit" disabled={readOnly || isSaving || isTesting}>
          {isSaving ? "正在保存" : "保存"}
        </button>
        <button
          type="button"
          className="secondary-button"
          onClick={handleTest}
          disabled={readOnly || isSaving || isTesting}
        >
          {isTesting
            ? provider === "hifly"
              ? "正在检查"
              : "正在测试"
            : provider === "hifly"
              ? "只读检查"
              : "测试连接"}
        </button>
        {status ? (
          <span
            role={statusTone === "error" ? "alert" : "status"}
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
  readOnly,
  onSave,
}: {
  runtime: RuntimeSettings;
  readOnly: boolean;
  onSave: (runtime: RuntimeSettings) => Promise<void>;
}) {
  const [values, setValues] = useState(runtime);
  const [status, setStatus] = useState("");
  const [isSaving, setIsSaving] = useState(false);
  const previousRuntimeRef = useRef(runtime);

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
          单次生成数量上限
          <input
            disabled={readOnly || isSaving}
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
            disabled={readOnly || isSaving}
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
      <p className="storage-provider-hint">
        人物图片、参考视频与首帧保存到腾讯云存储（需在桶 CORS 放行
        PUT/GET/HEAD，否则上传失败）；生成的成片仅保存在本机。
      </p>
      <div className="form-actions">
        <button disabled={readOnly || isSaving} type="submit">
          {isSaving ? "正在保存" : "保存"}
        </button>
        {status ? <span role="status">{status}</span> : null}
      </div>
    </form>
  );
}

function testResultLabel(result: ProviderTestResult) {
  switch (result.status) {
    case "ok":
      if (result.provider === "hifly") {
        if (hasValidHiflyCredit(result)) {
          return `只读账户检查通过，余额 ${result.account_credit} 积分；未创建收费任务`;
        }
        return "只读账户检查响应异常，请稍后重试。";
      }
      return "连接测试通过";
    case "configured_only":
      return "参数已保存；测试不会发起外部调用";
    default:
      return "尚未保存该服务的必要参数";
  }
}

function hasValidHiflyCredit(result: ProviderTestResult) {
  return (
    typeof result.account_credit === "number" &&
    Number.isSafeInteger(result.account_credit) &&
    result.account_credit >= 0
  );
}

function testResultSucceeded(result: ProviderTestResult) {
  return (
    result.status === "configured_only" ||
    (result.status === "ok" &&
      (result.provider !== "hifly" || hasValidHiflyCredit(result)))
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
