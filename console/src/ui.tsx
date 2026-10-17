import type { ReactNode } from "react";
import { ApiError } from "./api";

export function money(minor: number | null | undefined, currency: string): string {
  if (minor === null || minor === undefined) return "—";
  const digits = ["JPY", "KRW", "VND", "CLP", "ISK"].includes(currency) ? 0 : 2;
  const locale = currency === "INR" ? "en-IN" : undefined;
  try {
    return new Intl.NumberFormat(locale, { style: "currency", currency }).format(
      minor / 10 ** digits,
    );
  } catch {
    return `${(minor / 10 ** digits).toFixed(digits)} ${currency}`;
  }
}

export function when(value: string | number | undefined | null): string {
  if (value === undefined || value === null || value === "") return "—";
  const date = typeof value === "number" ? new Date(value * 1000) : new Date(value);
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString();
}

export function describeError(error: unknown): string {
  if (error instanceof ApiError) {
    return error.retryAfter ? `${error.message} (retry in ~${error.retryAfter}s)` : error.message;
  }
  return error instanceof Error ? error.message : "Something went wrong.";
}

const TONE: Record<string, string> = {
  APPROVE: "ok", approved: "ok", posted: "ok", COMPLETED: "ok", drafted: "ok", queued: "info",
  DENY: "bad", rejected: "bad", erp_failed: "bad", erp_blocked: "bad", FAILED: "bad", failed: "bad",
  REJECTED: "bad",
  ESCALATE: "warn", needs_review: "warn", posting: "info", WORKING: "info", SUBMITTED: "info",
  processing: "info", duplicate: "info",
};

export function Badge({ value }: { value: string | null | undefined }) {
  const label = (value ?? "—").replace(/^TASK_STATE_/, "");
  return <span className={`badge ${TONE[label] ?? ""}`}>{label.replace(/_/g, " ")}</span>;
}

export function Spinner() {
  return <span className="spinner" aria-hidden="true" />;
}

export function ErrorBox({ error }: { error: unknown }) {
  if (!error) return null;
  return (
    <div className="alert bad" role="alert">
      {describeError(error)}
    </div>
  );
}

export function PageHead({ title, sub, children }: { title: string; sub: string; children?: ReactNode }) {
  return (
    <div className="page-head">
      <div>
        <h1>{title}</h1>
        <p>{sub}</p>
      </div>
      {children}
    </div>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="empty">{children}</div>;
}

/** Inline 18px icons (no icon font: the hosting CSP only allows same-origin assets). */
export function Icon({ name }: { name: string }) {
  const paths: Record<string, string> = {
    chat: "M4 5h16v10H8l-4 4z",
    orders: "M6 3h12l2 4v14H4V7zM4 7h16M9 11h6",
    intake: "M12 3v12m0 0-4-4m4 4 4-4M4 17v3h16v-3",
    policy: "M6 3h9l3 3v15H6zM9 10h6M9 14h6M9 18h4",
    audit: "M11 4a7 7 0 1 0 0 14 7 7 0 0 0 0-14zm9 16-4.5-4.5",
    refund: "M4 12a8 8 0 1 0 2.3-5.7M4 4v4h4M12 8v4l3 2",
    logout: "M15 4h4v16h-4M10 8l-4 4 4 4M6 12h10",
  };
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor"
      strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d={paths[name] ?? ""} />
    </svg>
  );
}
