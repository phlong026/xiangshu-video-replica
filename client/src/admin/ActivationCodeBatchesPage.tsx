import { type FormEvent, useState } from "react";

import {
  type ActivationBatchResult,
  type ActivationDownloadResult,
  type ActivationGenerateResult,
  AdminActivationError,
  adminActivationErrorMessage,
  createActivationCodeBatch,
  createIdempotencyKey,
  downloadActivationCodeExport,
  generateActivationCodes,
} from "../api.admin";

const FIXED_AMOUNTS_YUAN = [100, 200, 500, 1000] as const;
const MAX_CODES_PER_REQUEST = 100;

type AmountChoice = (typeof FIXED_AMOUNTS_YUAN)[number] | "custom";
type GenerationPhase = "idle" | "creating" | "generating" | "retrieving";

type CompletedGeneration = {
  amountYuan: number;
  quantity: number;
  credits: number;
  result: ActivationDownloadResult;
};

/**
 * A simple operator surface over the existing audited batch APIs.
 *
 * Batches remain the durable server-side audit boundary, but they are an
 * implementation detail here: one submission creates the hidden batch,
 * generates its codes and retrieves the one-time plaintext export.
 */
export function ActivationCodeBatchesPage({
  unitPriceFen,
  readOnly = false,
  onSessionExpired,
}: {
  unitPriceFen: number | null;
  readOnly?: boolean;
  onSessionExpired?: () => void;
}) {
  const [amountChoice, setAmountChoice] = useState<AmountChoice>(100);
  const [customAmountYuan, setCustomAmountYuan] = useState("");
  const [quantity, setQuantity] = useState("1");
  const [phase, setPhase] = useState<GenerationPhase>("idle");
  const [error, setError] = useState("");
  const [copyNotice, setCopyNotice] = useState("");
  const [completed, setCompleted] = useState<CompletedGeneration | null>(null);

  // Preserve each write key and intermediate result across uncertain network
  // failures. A retry continues from the last confirmed stage instead of
  // minting another hidden batch or another set of codes.
  const [pendingBatch, setPendingBatch] =
    useState<ActivationBatchResult | null>(null);
  const [pendingExport, setPendingExport] =
    useState<ActivationGenerateResult | null>(null);
  const [createKey, setCreateKey] = useState<string | null>(null);
  const [generateKey, setGenerateKey] = useState<string | null>(null);
  const [downloadKey, setDownloadKey] = useState<string | null>(null);

  const amountYuan =
    amountChoice === "custom" ? Number(customAmountYuan) : amountChoice;
  const parsedQuantity = Number(quantity);
  const amountLabel =
    Number.isFinite(amountYuan) && amountYuan > 0
      ? `¥${formatNumber(amountYuan)}`
      : "";
  const actionLabel = phaseLabel(phase, parsedQuantity, amountLabel);
  const credits = calculateCredits(amountYuan, unitPriceFen);
  const selectionLocked =
    phase !== "idle" ||
    pendingBatch !== null ||
    pendingExport !== null ||
    createKey !== null ||
    generateKey !== null ||
    downloadKey !== null;

  function handleFailure(cause: unknown, fallback: string) {
    if (cause instanceof AdminActivationError && cause.status === 401) {
      setError("会话已失效，请重新登录");
      onSessionExpired?.();
      return;
    }
    setError(adminActivationErrorMessage(cause, fallback));
  }

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError("");
    setCopyNotice("");

    const validationError = validateGeneration(
      amountYuan,
      parsedQuantity,
      unitPriceFen,
    );
    if (validationError) {
      setError(validationError);
      return;
    }

    // Validation above proves all three values are positive integers and the
    // amount is exactly divisible by the configured unit price.
    const priceFen = unitPriceFen as number;
    const creditCount = (amountYuan * 100) / priceFen;
    const reason = `后台直接生成：${parsedQuantity} 个 ${amountLabel} 激活码`;
    let batch = pendingBatch;
    let generated = pendingExport;
    let currentPhase: GenerationPhase = "creating";

    try {
      if (!batch) {
        setPhase("creating");
        const key = createKey ?? createIdempotencyKey();
        setCreateKey(key);
        batch = await createActivationCodeBatch(
          {
            name: createAutomaticBatchName(amountYuan),
            // The current server contract stores the charged per-video price
            // in this frozen field. Total code value is credits × unit price.
            face_value_fen: priceFen,
            credits: creditCount,
            quantity: parsedQuantity,
            activation_expires_at: oneYearFromNow(),
            reason,
          },
          key,
        );
        setPendingBatch(batch);
        setCreateKey(null);
      }

      currentPhase = "generating";
      if (!generated) {
        setPhase("generating");
        const key = generateKey ?? createIdempotencyKey();
        setGenerateKey(key);
        generated = await generateActivationCodes(
          batch.batch_id,
          parsedQuantity,
          reason,
          key,
          true,
        );
        setPendingExport(generated);
        setGenerateKey(null);
      }

      currentPhase = "retrieving";
      setPhase("retrieving");
      const key = downloadKey ?? createIdempotencyKey();
      setDownloadKey(key);
      const result = await downloadActivationCodeExport(
        generated.export_id,
        reason,
        key,
      );

      setCompleted({
        amountYuan,
        quantity: parsedQuantity,
        credits: creditCount,
        result,
      });
      setPendingBatch(null);
      setPendingExport(null);
      setCreateKey(null);
      setGenerateKey(null);
      setDownloadKey(null);
    } catch (cause) {
      handleFailure(cause, phaseErrorFallback(currentPhase));
      if (cause instanceof AdminActivationError && cause.status !== undefined) {
        // A server response is definitive. Clear the pipeline so a corrected
        // selection starts cleanly; only network/timeout failures retain the
        // intermediate state and idempotency keys for a safe retry.
        setPendingBatch(null);
        setPendingExport(null);
        setCreateKey(null);
        setGenerateKey(null);
        setDownloadKey(null);
      }
    } finally {
      setPhase("idle");
    }
  }

  async function copyAllCodes() {
    if (!completed) {
      return;
    }
    try {
      await navigator.clipboard.writeText(completed.result.codes.join("\n"));
      setCopyNotice("全部激活码已复制");
    } catch {
      setCopyNotice("复制失败，请手动选择激活码");
    }
  }

  return (
    <section className="activation-generator-page" aria-label="生成激活码">
      {readOnly ? (
        <p className="wallet-notice" role="status">
          当前为只读模式，不能生成激活码。
        </p>
      ) : null}
      {error ? (
        <p className="settings-error" role="alert">
          {error}
        </p>
      ) : null}

      <form className="activation-generator" onSubmit={submit}>
        <header className="activation-generator__header">
          <div>
            <p className="activation-generator__eyebrow">快速发码</p>
            <h2>直接生成激活码</h2>
          </div>
          <p>选择金额和数量即可。批次、有效期和审计信息由系统自动处理。</p>
        </header>

        <fieldset className="activation-amount-picker">
          <legend>每个激活码的金额</legend>
          <div className="activation-amount-grid">
            {FIXED_AMOUNTS_YUAN.map((amount) => (
              <button
                aria-pressed={amountChoice === amount}
                className={
                  amountChoice === amount
                    ? "activation-amount-option is-active"
                    : "activation-amount-option"
                }
                disabled={selectionLocked}
                key={amount}
                type="button"
                onClick={() => setAmountChoice(amount)}
              >
                ¥{amount}
              </button>
            ))}
            <button
              aria-pressed={amountChoice === "custom"}
              className={
                amountChoice === "custom"
                  ? "activation-amount-option is-active"
                  : "activation-amount-option"
              }
              disabled={selectionLocked}
              type="button"
              onClick={() => setAmountChoice("custom")}
            >
              自定义金额
            </button>
          </div>
        </fieldset>

        {amountChoice === "custom" ? (
          <div className="activation-generator__field">
            <label htmlFor="activation-custom-amount">自定义金额（元）</label>
            <input
              aria-describedby="activation-custom-amount-hint"
              id="activation-custom-amount"
              inputMode="numeric"
              min="1"
              step="1"
              type="number"
              value={customAmountYuan}
              disabled={selectionLocked}
              onChange={(event) => setCustomAmountYuan(event.target.value)}
            />
            {unitPriceFen ? (
              <span id="activation-custom-amount-hint">
                金额需为 {formatFenAsYuan(unitPriceFen)} 元的整数倍
              </span>
            ) : null}
          </div>
        ) : null}

        <div className="activation-generator__field">
          <label htmlFor="activation-code-quantity">生成数量</label>
          <input
            aria-describedby="activation-code-quantity-hint"
            id="activation-code-quantity"
            inputMode="numeric"
            max={MAX_CODES_PER_REQUEST}
            min="1"
            step="1"
            type="number"
            value={quantity}
            disabled={selectionLocked}
            onChange={(event) => setQuantity(event.target.value)}
          />
          <span id="activation-code-quantity-hint">
            单次最多生成 {MAX_CODES_PER_REQUEST} 个
          </span>
        </div>

        <div className="activation-generator__summary" aria-live="polite">
          {unitPriceFen === null ? (
            <span>正在读取当前价格…</span>
          ) : credits ? (
            <>
              <span>每个激活码</span>
              <strong>{amountLabel}</strong>
              <span>激活后可生成 {credits} 个视频 · 领取有效期 1 年</span>
            </>
          ) : (
            <span>选择金额后显示到账次数</span>
          )}
        </div>

        <button
          className="activation-generator__submit"
          disabled={readOnly || phase !== "idle" || unitPriceFen === null}
          type="submit"
        >
          {actionLabel}
        </button>
      </form>

      {completed ? (
        <section className="activation-code-result" aria-label="新生成的激活码">
          <div className="activation-code-result__header">
            <div>
              <p className="activation-generator__eyebrow">生成成功</p>
              <h3>
                已生成 {completed.quantity} 个 ¥
                {formatNumber(completed.amountYuan)} 激活码
              </h3>
            </div>
            <button type="button" onClick={copyAllCodes}>
              复制全部
            </button>
          </div>
          <p className="activation-code-result__warning">
            明文激活码仅在本页显示这一次，请立即复制并安全保存；列表页仅保留脱敏码。
          </p>
          <ul className="activation-code-result__list">
            {completed.result.codes.map((code) => (
              <li key={code}>
                <code>{code}</code>
              </li>
            ))}
          </ul>
          {copyNotice ? (
            <p className="wallet-notice" role="status">
              {copyNotice}
            </p>
          ) : null}
        </section>
      ) : null}
    </section>
  );
}

function calculateCredits(
  amountYuan: number,
  unitPriceFen: number | null,
): number | null {
  if (
    !unitPriceFen ||
    !Number.isInteger(unitPriceFen) ||
    unitPriceFen <= 0 ||
    !Number.isInteger(amountYuan) ||
    amountYuan <= 0
  ) {
    return null;
  }
  const amountFen = amountYuan * 100;
  if (amountFen % unitPriceFen !== 0) {
    return null;
  }
  return amountFen / unitPriceFen;
}

function validateGeneration(
  amountYuan: number,
  quantity: number,
  unitPriceFen: number | null,
): string | null {
  if (!unitPriceFen || !Number.isInteger(unitPriceFen) || unitPriceFen <= 0) {
    return "当前价格尚未加载，请稍后重试";
  }
  if (!Number.isInteger(amountYuan) || amountYuan <= 0) {
    return "请输入正整数金额";
  }
  if ((amountYuan * 100) % unitPriceFen !== 0) {
    return `金额需为 ${formatFenAsYuan(unitPriceFen)} 元的整数倍`;
  }
  if (
    !Number.isInteger(quantity) ||
    quantity < 1 ||
    quantity > MAX_CODES_PER_REQUEST
  ) {
    return `生成数量必须是 1 到 ${MAX_CODES_PER_REQUEST} 之间的整数`;
  }
  return null;
}

function oneYearFromNow(): string {
  const expiresAt = new Date();
  expiresAt.setUTCFullYear(expiresAt.getUTCFullYear() + 1);
  return expiresAt.toISOString();
}

function createAutomaticBatchName(amountYuan: number): string {
  const stamp = new Date().toISOString().replace(/[-:]/g, "").slice(0, 13);
  return `${formatNumber(amountYuan)}元激活码-${stamp}`;
}

function phaseLabel(
  phase: GenerationPhase,
  quantity: number,
  amountLabel: string,
): string {
  if (phase === "creating") {
    return "正在准备…";
  }
  if (phase === "generating") {
    return "正在生成…";
  }
  if (phase === "retrieving") {
    return "正在取回激活码…";
  }
  const count = Number.isInteger(quantity) && quantity > 0 ? quantity : 1;
  return amountLabel
    ? `生成 ${count} 个 ${amountLabel} 激活码`
    : `生成 ${count} 个激活码`;
}

function phaseErrorFallback(phase: GenerationPhase): string {
  if (phase === "creating") {
    return "准备激活码失败";
  }
  if (phase === "generating") {
    return "生成激活码失败";
  }
  return "读取明文激活码失败";
}

function formatFenAsYuan(value: number): string {
  return formatNumber(value / 100);
}

function formatNumber(value: number): string {
  return new Intl.NumberFormat("zh-CN", {
    maximumFractionDigits: 2,
  }).format(value);
}
