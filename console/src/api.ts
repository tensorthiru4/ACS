/**
 * Typed client for the ACS API Gateway (orchestrator). Every call carries the signed-in
 * user's Firebase ID token; the gateway verifies it and the orchestrator re-verifies it
 * and authorizes by the merchant_id / role claims.
 */
import { auth } from "./firebase";

const BASE = import.meta.env.DEV ? "/api" : import.meta.env.VITE_API_BASE;

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
    public retryAfter?: string,
  ) {
    super(message);
  }
}

/** Orchestrator errors are {error}; FastAPI HTTPException is {detail}; gateway is {message}. */
export function errorMessage(status: number, body: unknown): string {
  if (body && typeof body === "object") {
    const b = body as Record<string, unknown>;
    for (const key of ["error", "detail", "message"]) {
      if (typeof b[key] === "string" && b[key]) return b[key] as string;
    }
  }
  if (status === 401) return "Your session expired. Sign in again.";
  if (status === 403) return "Your role does not allow this action.";
  if (status === 429) return "Too many requests. Wait a moment and try again.";
  if (status >= 500) return "The service is unavailable right now. Try again shortly.";
  return `Request failed (HTTP ${status}).`;
}

async function request<T>(
  method: string,
  path: string,
  body?: unknown | FormData,
  extraHeaders: Record<string, string> = {},
): Promise<T> {
  const user = auth.currentUser;
  if (!user) throw new ApiError(401, "Not signed in.");
  const headers: Record<string, string> = {
    ...extraHeaders,
    Authorization: `Bearer ${await user.getIdToken()}`,
  };
  let payload: BodyInit | undefined;
  if (body instanceof FormData) {
    payload = body;
  } else if (body !== undefined) {
    headers["Content-Type"] = "application/json";
    payload = JSON.stringify(body);
  }
  const response = await fetch(`${BASE}${path}`, { method, headers, body: payload });
  if (response.status === 204) return undefined as T;
  const text = await response.text();
  let data: unknown = undefined;
  try {
    data = text ? JSON.parse(text) : undefined;
  } catch {
    data = undefined;
  }
  if (!response.ok) {
    throw new ApiError(
      response.status,
      errorMessage(response.status, data),
      response.headers.get("Retry-After") ?? undefined,
    );
  }
  return data as T;
}

// ------------------------------------------------------------------ types
export type Decision = "APPROVE" | "DENY" | "ESCALATE";

export interface ChatReply {
  session_id: string;
  reply: string;
  tool_calls: string[];
}
export interface SessionInfo {
  session_id: string;
  last_update_time: number;
}
export interface TaskView {
  task_id: string | null;
  agent: string;
  state: string;
  result: { data: Record<string, any>[]; text: string[] } | null;
}
export interface Money {
  amount_minor: number;
  currency: string;
}
export interface RefundDecision {
  decision_id: string;
  order_id: string;
  decision: Decision;
  reasons: string[];
  matched_rule_ids: string[];
  rule_set_version: string;
  approved_amount: Money | null;
  decided_at: string;
  source: "rule_engine" | "failsafe";
}
export interface IntakeReceipt {
  intake_id: string;
  status: "queued" | "duplicate";
  content_type: string;
  sha256: string;
  size: number;
  message_id: string | null;
}
export interface DraftLine {
  line_no: number;
  description: string;
  customer_sku: string | null;
  quantity: number;
  uom: string | null;
  quoted_price_minor: number | null;
  unit_price_minor: number | null;
  list_price_minor: number | null;
  sku: string | null;
  product_name: string | null;
  match_method: string;
  match_confidence: number;
  price_variance_pct: number;
  complete: boolean;
}
export interface DraftOrder {
  intake_id: string;
  status: string;
  source?: string;
  po_number: string | null;
  order_date?: string | null;
  requested_delivery_date?: string | null;
  payment_terms_days?: number | null;
  currency: string;
  customer?: { ref: string; known: boolean; name: string; odoo_partner_id: number | null };
  lines?: DraftLine[];
  total_minor: number;
  extraction?: { confidence: number; warnings: string[]; buyer_name: string | null };
  terms?: { decision: Decision; reasons: string[]; rule_set_version: string; source: string };
  approved_by?: string | null;
  rejection_reason?: string | null;
  erp?: { status?: string; order_id?: number; detail?: string[]; execution?: string } | null;
  created_at?: string;
}

// ------------------------------------------------------------------ endpoints
export const api = {
  sessions: () => request<SessionInfo[]>("GET", "/v1/console/sessions"),
  createSession: () => request<{ session_id: string }>("POST", "/v1/console/sessions"),
  deleteSession: (id: string) => request<void>("DELETE", `/v1/console/sessions/${id}`),
  chat: (id: string, text: string) =>
    request<ChatReply>("POST", `/v1/console/sessions/${id}/messages`, { text }),

  startAudit: (store_url: string, journeys: string[]) =>
    request<TaskView>("POST", "/v1/audits", { store_url, journeys }),
  getAudit: (taskId: string) => request<TaskView>("GET", `/v1/audits/${taskId}`),

  compilePolicy: (policy_name: string, target_rule_set: string, policy_text: string) =>
    request<TaskView>("POST", "/v1/policies/compile", { policy_name, target_rule_set, policy_text }),
  deployPolicy: (draftId: string, expected_version: string | null) =>
    request<TaskView>("POST", `/v1/policies/${draftId}/deploy`, { expected_version }),

  /** The idempotency key makes a retried submit return the same decision. */
  decideRefund: (body: Record<string, unknown>, idempotencyKey: string) =>
    request<RefundDecision>("POST", "/v1/refunds/decide", body, {
      "Idempotency-Key": idempotencyKey,
    }),

  uploadPo: (file: File, customerRef: string) => {
    const form = new FormData();
    form.append("file", file);
    if (customerRef) form.append("customer_ref", customerRef);
    return request<IntakeReceipt>("POST", "/v1/intake/uploads", form);
  },
  intake: (id: string) => request<Record<string, unknown>>("GET", `/v1/intake/${id}`),

  drafts: (status?: string) =>
    request<DraftOrder[]>("GET", `/v1/orders/drafts${status ? `?status=${status}` : ""}`),
  draft: (id: string) => request<DraftOrder>("GET", `/v1/orders/drafts/${id}`),
  releaseDraft: (
    id: string,
    line_overrides: { line_no: number; sku: string }[],
    customer_ref: string | null,
  ) =>
    request<Record<string, unknown>>("POST", `/v1/orders/drafts/${id}/release`, {
      line_overrides,
      customer_ref,
    }),
  rejectDraft: (id: string, reason: string) =>
    request<Record<string, unknown>>("POST", `/v1/orders/drafts/${id}/reject`, { reason }),
};
