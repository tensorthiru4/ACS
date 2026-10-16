import { useState, type FormEvent } from "react";
import { api, type RefundDecision } from "../api";
import { Badge, ErrorBox, money, PageHead, Spinner, when } from "../ui";

const REASONS = ["damaged", "not_as_described", "not_received", "late_delivery", "wrong_item", "changed_mind", "other"];
const CONDITIONS = ["", "unopened", "opened", "used", "damaged", "missing"];

export default function Refunds() {
  const [form, setForm] = useState({
    order_id: "", amount: "", currency: "INR", reason: "damaged", days: "", condition: "", note: "",
  });
  const [key, setKey] = useState(() => crypto.randomUUID());
  const [decision, setDecision] = useState<RefundDecision | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);

  function set(field: keyof typeof form, value: string) {
    setForm({ ...form, [field]: value });
    setKey(crypto.randomUUID()); // a changed request is a new decision
  }

  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const body: Record<string, unknown> = {
        order_id: form.order_id.trim(),
        amount: { amount_minor: Math.round(Number(form.amount) * 100), currency: form.currency },
        reason: form.reason,
      };
      if (form.days !== "") body.days_since_delivery = Number(form.days);
      if (form.condition) body.item_condition = form.condition;
      if (form.note.trim()) body.customer_note = form.note.trim();
      setDecision(await api.decideRefund(body, key));
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <PageHead title="Refunds" sub="Get a deterministic refund decision from your deployed refund rules. No AI decides the outcome." />
      <ErrorBox error={error} />
      <div className="grid two" style={{ marginTop: 12, alignItems: "start" }}>
        <form className="card stack" onSubmit={submit}>
          <h2>Refund request</h2>
          <label>Order id<input required pattern="[A-Za-z0-9._:\-]+" value={form.order_id} onChange={(e) => set("order_id", e.target.value)} /></label>
          <div className="row" style={{ flexWrap: "nowrap" }}>
            <label style={{ flex: 2 }}>Amount<input required type="number" min="0" step="0.01" value={form.amount} onChange={(e) => set("amount", e.target.value)} /></label>
            <label style={{ flex: 1 }}>Currency<input required pattern="[A-Z]{3}" value={form.currency} onChange={(e) => set("currency", e.target.value.toUpperCase())} /></label>
          </div>
          <label>Reason
            <select value={form.reason} onChange={(e) => set("reason", e.target.value)}>
              {REASONS.map((r) => <option key={r} value={r}>{r.replace(/_/g, " ")}</option>)}
            </select>
          </label>
          <div className="row" style={{ flexWrap: "nowrap" }}>
            <label style={{ flex: 1 }}>Days since delivery<input type="number" min="0" max="3650" value={form.days} onChange={(e) => set("days", e.target.value)} /></label>
            <label style={{ flex: 1 }}>Item condition
              <select value={form.condition} onChange={(e) => set("condition", e.target.value)}>
                {CONDITIONS.map((c) => <option key={c} value={c}>{c || "unknown"}</option>)}
              </select>
            </label>
          </div>
          <label>Customer note <span className="hint">optional</span><textarea rows={3} maxLength={1000} value={form.note} onChange={(e) => set("note", e.target.value)} /></label>
          <button className="btn primary" disabled={busy}>{busy ? <Spinner /> : null} Get decision</button>
        </form>

        <div className="card stack">
          {!decision ? (
            <p className="muted">The decision appears here, with the rules that produced it.</p>
          ) : (
            <>
              <div className="row" style={{ justifyContent: "space-between" }}>
                <h2 style={{ margin: 0 }}>Order {decision.order_id}</h2>
                <Badge value={decision.decision} />
              </div>
              {decision.approved_amount && (
                <p>Approved amount: <b>{money(decision.approved_amount.amount_minor, decision.approved_amount.currency)}</b></p>
              )}
              <ul style={{ margin: 0, paddingLeft: 18 }}>{decision.reasons.map((r, i) => <li key={i}>{r}</li>)}</ul>
              <dl className="kv small">
                <dt>Rules</dt><dd className="mono">{decision.matched_rule_ids.join(", ") || "—"}</dd>
                <dt>Rule set</dt><dd>{decision.rule_set_version || "none deployed"}</dd>
                <dt>Source</dt><dd>{decision.source === "failsafe" ? "fail-safe (rule engine unavailable)" : "rule engine"}</dd>
                <dt>Decided</dt><dd>{when(decision.decided_at)}</dd>
                <dt>Decision id</dt><dd className="mono">{decision.decision_id}</dd>
              </dl>
            </>
          )}
        </div>
      </div>
    </>
  );
}
