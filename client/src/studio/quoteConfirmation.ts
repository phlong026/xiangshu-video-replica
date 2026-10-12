export type QuoteConfirmation = {
  quote_token?: string;
  quote_expires_at?: number;
  max_credits?: number;
  estimated_credits?: number;
};

export type ConfirmedQuote = QuoteConfirmation & {
  quote_token: string;
  quote_expires_at: number;
  max_credits: number;
  estimated_credits: number;
};

export function isCurrentQuote(
  quote: QuoteConfirmation | null | undefined,
  now = Date.now(),
): quote is ConfirmedQuote {
  return Boolean(
    quote &&
      typeof quote.quote_token === "string" &&
      quote.quote_token.length > 0 &&
      typeof quote.quote_expires_at === "number" &&
      Number.isFinite(quote.quote_expires_at) &&
      quote.quote_expires_at * 1000 > now &&
      Number.isSafeInteger(quote.max_credits) &&
      (quote.max_credits ?? -1) >= 0 &&
      quote.estimated_credits === quote.max_credits,
  );
}

export function isQuoteRejection(
  cause: unknown,
): cause is Error & { code: string } {
  return (
    cause instanceof Error &&
    "code" in cause &&
    typeof cause.code === "string" &&
    cause.code.startsWith("QUOTE_")
  );
}

export function watchQuoteExpiry(
  quote: QuoteConfirmation | null,
  onExpire: () => void,
): () => void {
  const expiresAt = quote?.quote_expires_at;
  if (typeof expiresAt !== "number" || !Number.isFinite(expiresAt)) {
    return () => {};
  }
  const timer = window.setTimeout(
    onExpire,
    Math.max(0, expiresAt * 1000 - Date.now()),
  );
  return () => window.clearTimeout(timer);
}
