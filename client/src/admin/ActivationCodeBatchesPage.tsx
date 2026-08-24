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

/**
 * T32 — batch creation, code generation and the one-time plaintext export.
 *
 * Every write follows the admin write contract: a non-blank reason, an
 * explicit confirmation checkbox and (via the adapter) the Idempotency-Key +
 * CSRF headers; each result surfaces the audit `request_id`. The plaintext
 * download is deliberately one-shot: it is rendered once in the page and never
 * persisted, mirroring the server-side `downloaded_at` constraint.
 */
export function ActivationCodeBatchesPage({
  readOnly = false,
  onSessionExpired,
}: {
  readOnly?: boolean;
  onSessionExpired?: () => void;
}) {
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const [name, setName] = useState("");
  const [faceValueFen, setFaceValueFen] = useState("");
  const [credits, setCredits] = useState("");
  const [quantity, setQuantity] = useState("");
  const [expiresAt, setExpiresAt] = useState("");
  const [createReason, setCreateReason] = useState("");
  const [createConfirmed, setCreateConfirmed] = useState(false);
  const [createdBatch, setCreatedBatch] =
    useState<ActivationBatchResult | null>(null);

  const [batchId, setBatchId] = useState("");
  const [generateQuantity, setGenerateQuantity] = useState("");
  const [generateReason, setGenerateReason] = useState("");
  const [generateConfirmed, setGenerateConfirmed] = useState(false);
  const [generated, setGenerated] = useState<ActivationGenerateResult | null>(
    null,
  );

  const [downloadReason, setDownloadReason] = useState("");
  const [downloadConfirmed, setDownloadConfirmed] = useState(false);
  const [downloaded, setDownloaded] = useState<ActivationDownloadResult | null>(
    null,
  );

  // One idempotency key per logical submission: ambiguous failures (timeout /
  // network) keep the key so a retry replays the T12 server snapshot instead
  // of double-creating; definitive outcomes release it.
  const [createKey, setCreateKey] = useState<string | null>(null);
  const [generateKey, setGenerateKey] = useState<string | null>(null);
  const [downloadKey, setDownloadKey] = useState<string | null>(null);

  function handleFailure(cause: unknown, fallback: string) {
    if (cause instanceof AdminActivationError && cause.status === 401) {
      setError("会话已失效，请重新登录");
      onSessionExpired?.();
      return;
    }
    setError(adminActivationErrorMessage(cause, fallback));
  }

  async function submitCreate(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError("");
    if (!name.trim()) {
      setError("请填写批次名称");
      return;
    }
    if (!isPositiveInteger(faceValueFen)) {
      setError("面值（分）必须是正整数");
      return;
    }
    if (!isPositiveInteger(credits)) {
      setError("到账条数必须是正整数");
      return;
    }
    if (!isPositiveInteger(quantity)) {
      setError("生成数量必须是正整数");
      return;
    }
    if (!expiresAt) {
      setError("请填写激活有效期");
      return;
    }
    if (!createReason.trim()) {
      setError("请填写创建原因");
      return;
    }
    if (!createConfirmed) {
      setError("请先勾选确认创建");
      return;
    }
    setBusy(true);
    const key = createKey ?? createIdempotencyKey();
    setCreateKey(key);
    try {
      const result = await createActivationCodeBatch(
        {
          name: name.trim(),
          face_value_fen: Number(faceValueFen),
          credits: Number(credits),
          quantity: Number(quantity),
          // datetime-local yields a timezone-naive local wall-clock string; the
          // server reads naive values as UTC, so normalise to an explicit
          // instant before sending.
          activation_expires_at: new Date(expiresAt).toISOString(),
          reason: createReason.trim(),
        },
        key,
      );
      setCreatedBatch(result);
      setCreateKey(null);
    } catch (cause) {
      handleFailure(cause, "创建激活码批次失败");
      if (cause instanceof AdminActivationError && cause.status !== undefined) {
        setCreateKey(null);
      }
    } finally {
      setBusy(false);
    }
  }

  async function submitGenerate(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError("");
    if (!batchId.trim()) {
      setError("请填写批次 ID");
      return;
    }
    if (!generateReason.trim()) {
      setError("请填写生成原因");
      return;
    }
    if (!generateConfirmed) {
      setError("请先勾选确认生成");
      return;
    }
    setBusy(true);
    const key = generateKey ?? createIdempotencyKey();
    setGenerateKey(key);
    try {
      const result = await generateActivationCodes(
        batchId.trim(),
        Number(generateQuantity),
        generateReason.trim(),
        key,
      );
      setGenerated(result);
      setGenerateKey(null);
      setDownloaded(null);
      setDownloadReason("");
      setDownloadConfirmed(false);
    } catch (cause) {
      handleFailure(cause, "生成激活码失败");
      if (cause instanceof AdminActivationError && cause.status !== undefined) {
        setGenerateKey(null);
      }
    } finally {
      setBusy(false);
    }
  }

  async function submitDownload(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!generated) {
      return;
    }
    setError("");
    if (!downloadReason.trim()) {
      setError("请填写下载原因");
      return;
    }
    if (!downloadConfirmed) {
      setError("请先勾选确认下载");
      return;
    }
    setBusy(true);
    const key = downloadKey ?? createIdempotencyKey();
    setDownloadKey(key);
    try {
      const result = await downloadActivationCodeExport(
        generated.export_id,
        downloadReason.trim(),
        key,
      );
      setDownloaded(result);
      setGenerated(null);
      setDownloadKey(null);
    } catch (cause) {
      handleFailure(cause, "下载明文码失败");
      if (cause instanceof AdminActivationError && cause.status !== undefined) {
        setDownloadKey(null);
      }
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="admin-panel" aria-label="激活码批次">
      {readOnly ? (
        <p className="wallet-notice" role="status">
          当前为只读模式，写操作不可用。
        </p>
      ) : null}
      {error ? (
        <p className="settings-error" role="alert">
          {error}
        </p>
      ) : null}

      <form className="admin-form" onSubmit={submitCreate}>
        <h2>创建批次</h2>
        <label>
          批次名称
          <input
            value={name}
            onChange={(event) => setName(event.target.value)}
          />
        </label>
        <label>
          面值（分）
          <input
            inputMode="numeric"
            type="number"
            value={faceValueFen}
            onChange={(event) => setFaceValueFen(event.target.value)}
          />
        </label>
        <label>
          到账条数
          <input
            inputMode="numeric"
            type="number"
            value={credits}
            onChange={(event) => setCredits(event.target.value)}
          />
        </label>
        <label>
          生成数量
          <input
            inputMode="numeric"
            type="number"
            value={quantity}
            onChange={(event) => setQuantity(event.target.value)}
          />
        </label>
        <label>
          激活有效期至
          <input
            type="datetime-local"
            value={expiresAt}
            onChange={(event) => setExpiresAt(event.target.value)}
          />
        </label>
        <label>
          创建原因
          <input
            placeholder="例如：首批渠道投放"
            value={createReason}
            onChange={(event) => setCreateReason(event.target.value)}
          />
        </label>
        <label>
          <input
            checked={createConfirmed}
            type="checkbox"
            onChange={(event) => setCreateConfirmed(event.target.checked)}
          />
          我已确认创建
        </label>
        <button disabled={readOnly || busy} type="submit">
          创建批次
        </button>
      </form>

      {createdBatch ? (
        <div className="admin-result">
          <h3>批次已创建</h3>
          <p>
            批次 ID：<code>{createdBatch.batch_id}</code>
          </p>
          <p>
            状态：<code>{createdBatch.status}</code>
          </p>
          <p>
            request id：<code>{createdBatch.request_id}</code>
          </p>
        </div>
      ) : null}

      <form className="admin-form" onSubmit={submitGenerate}>
        <h2>生成激活码</h2>
        <label>
          批次 ID
          <input
            placeholder="例如：batch-1"
            value={batchId}
            onChange={(event) => setBatchId(event.target.value)}
          />
        </label>
        <label>
          本次生成数量
          <input
            inputMode="numeric"
            type="number"
            value={generateQuantity}
            onChange={(event) => setGenerateQuantity(event.target.value)}
          />
        </label>
        <label>
          生成原因
          <input
            placeholder="例如：渠道补货"
            value={generateReason}
            onChange={(event) => setGenerateReason(event.target.value)}
          />
        </label>
        <label>
          <input
            checked={generateConfirmed}
            type="checkbox"
            onChange={(event) => setGenerateConfirmed(event.target.checked)}
          />
          我已确认生成
        </label>
        <button disabled={readOnly || busy} type="submit">
          生成激活码
        </button>
      </form>

      {generated ? (
        <div className="admin-result">
          <h3>生成结果</h3>
          <p>
            导出包 ID：<code>{generated.export_id}</code>
          </p>
          <p>
            有效期至：<code>{generated.expires_at}</code>
          </p>
          <p>
            request id：<code>{generated.request_id}</code>
          </p>
          <ul className="admin-code-list">
            {generated.codes.map((code) => (
              <li key={code.code_id}>{code.masked_code}</li>
            ))}
          </ul>
          <form className="admin-form" onSubmit={submitDownload}>
            <label>
              下载原因
              <input
                placeholder="例如：线下交付"
                value={downloadReason}
                onChange={(event) => setDownloadReason(event.target.value)}
              />
            </label>
            <label>
              <input
                checked={downloadConfirmed}
                type="checkbox"
                onChange={(event) => setDownloadConfirmed(event.target.checked)}
              />
              我已确认下载
            </label>
            <button disabled={readOnly || busy} type="submit">
              下载明文码
            </button>
          </form>
        </div>
      ) : null}

      {downloaded ? (
        <div className="admin-result">
          <h3>明文码（仅此一次）</h3>
          <p>明文码不会再显示，请立即妥善保存并安全交付。</p>
          <ul className="admin-code-list">
            {downloaded.codes.map((code) => (
              <li key={code}>
                <code>{code}</code>
              </li>
            ))}
          </ul>
          <p>
            下载时间：<code>{downloaded.downloaded_at}</code>
          </p>
          <p>
            request id：<code>{downloaded.request_id}</code>
          </p>
        </div>
      ) : null}
    </section>
  );
}

function isPositiveInteger(value: string): boolean {
  const parsed = Number(value);
  return Number.isInteger(parsed) && parsed > 0;
}
