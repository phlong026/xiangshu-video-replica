import { useEffect, useId, useRef, useState } from "react";
import {
  customerVisibleErrorMessage,
  optimizePublishCopy,
  type PublishCopyInput,
} from "../api";
import { Button, Icon } from "./ui";

type CopyStatus =
  | { status: "idle" | "loading" }
  | { status: "suggestion" | "error"; text: string };

export function PublishCopyField({
  field,
  draft,
  disabled,
  aiDisabled,
  review,
  required,
  onChange,
}: {
  field: PublishCopyInput["field"];
  draft: Omit<PublishCopyInput, "field">;
  disabled: boolean;
  aiDisabled: boolean;
  review: boolean;
  required?: boolean;
  onChange: (text: string) => void;
}) {
  const id = useId();
  const request = useRef<AbortController | null>(null);
  const [result, setResult] = useState<CopyStatus>({ status: "idle" });
  useEffect(() => () => request.current?.abort(), []);
  const isTitle = field === "title";
  const label = isTitle ? "发布标题" : "发布描述";
  const maxLength = isTitle ? 300 : 5000;
  const value = draft[field];
  const canOptimize = Boolean(draft.title.trim() || draft.description.trim());

  function change(text: string) {
    request.current?.abort();
    request.current = null;
    setResult({ status: "idle" });
    onChange(text);
  }

  async function optimize() {
    if (request.current || disabled || aiDisabled || !canOptimize) return;
    if (review) {
      setResult({
        status: "suggestion",
        text: isTitle
          ? "农村自建房，这几项预算也要提前算"
          : "从门窗、水电到防水和庭院，带你梳理自建房主体之外需要提前规划的预算。",
      });
      return;
    }
    const controller = new AbortController();
    request.current = controller;
    setResult({ status: "loading" });
    try {
      const { title, description, platform } = draft;
      const { text } = await optimizePublishCopy(
        { title, description, platform, field },
        controller.signal,
      );
      if (controller.signal.aborted) return;
      const suggestion = text.trim();
      if (!suggestion || suggestion.length > maxLength)
        throw new Error("未生成可用文案，请重新优化。");
      setResult({ status: "suggestion", text: suggestion });
    } catch (cause) {
      if (!controller.signal.aborted)
        setResult({
          status: "error",
          text: customerVisibleErrorMessage(cause, "AI 优化失败，请稍后重试。"),
        });
    } finally {
      if (request.current === controller) request.current = null;
    }
  }

  const inputProps = {
    id,
    disabled,
    maxLength,
    value,
    "aria-label": label,
    "aria-required": required || undefined,
  };

  return (
    <div className="studio-field content-publish-copy">
      <div className="content-publish-field-heading">
        <label htmlFor={id}>
          {label}
          {required && <span aria-hidden="true"> *</span>}
        </label>
        <Button
          aria-label={`AI 优化${label}`}
          className="content-publish-optimize"
          disabled={
            disabled ||
            aiDisabled ||
            !canOptimize ||
            result.status === "loading"
          }
          onClick={() => void optimize()}
        >
          <Icon name="sparkles" size={14} />
          {result.status === "loading" ? "优化中…" : "AI 优化"}
        </Button>
      </div>
      {isTitle ? (
        <input
          {...inputProps}
          required={required}
          placeholder="填写发布标题"
          onChange={(event) => change(event.target.value)}
        />
      ) : (
        <textarea
          {...inputProps}
          rows={2}
          placeholder="填写发布描述"
          onChange={(event) => change(event.target.value)}
        />
      )}
      {result.status === "error" && <p role="alert">{result.text}</p>}
      {result.status === "suggestion" && (
        <div className="content-publish-suggestion" aria-live="polite">
          <strong>{review ? "审核示例建议" : "AI 建议"}</strong>
          <p>{result.text}</p>
          <div>
            <Button
              onClick={() => setResult({ status: "idle" })}
              variant="quiet"
            >
              忽略
            </Button>
            <Button
              disabled={disabled}
              onClick={() => change(result.text)}
              variant="outline"
            >
              采用建议
            </Button>
          </div>
        </div>
      )}
    </div>
  );
}
