import { useState, type FormEvent } from "react";
import { api, type TaskView } from "../api";
import { Badge, ErrorBox, PageHead, Spinner } from "../ui";

const RULE_SETS = [
  ["order_terms", "Order terms (which purchase orders auto-post to the ERP)"],
  ["refunds", "Refunds"],
  ["pricing", "Pricing"],
];

interface Rule {
  id: string;
  description?: string;
  when: string;
  decision: string;
  reason: string;
  max_refund_minor?: number;
}
interface TestResult {
  name: string;
  expected: string;
  actual: string | null;
  passed: boolean;
  error?: string;
}

export default function Policies() {
  const [name, setName] = useState("");
  const [ruleSet, setRuleSet] = useState("order_terms");
  const [text, setText] = useState("");
  const [compiled, setCompiled] = useState<TaskView | null>(null);
  const [deployed, setDeployed] = useState<TaskView | null>(null);
  const [busy, setBusy] = useState<"" | "compile" | "deploy">("");
  const [error, setError] = useState<unknown>(null);

  const draft = compiled?.result?.data?.[0] as
    | {
        draft_id: string; version: string; proposed_version: string; default_decision: string;
        rules: Rule[]; baseline_rules: Rule[]; test_results: TestResult[]; tests_passed: number;
        tests_total: number; assumptions: string[];
      }
    | undefined;

  async function compile(event: FormEvent) {
    event.preventDefault();
    setBusy("compile");
    setError(null);
    setCompiled(null);
    setDeployed(null);
    try {
      setCompiled(await api.compilePolicy(name.trim(), ruleSet, text.trim()));
    } catch (e) {
      setError(e);
    } finally {
      setBusy("");
    }
  }

  async function deploy() {
    if (!draft) return;
    if (!confirm(`Deploy ${draft.rules.length} rules as ${ruleSet} ${draft.proposed_version}? This takes effect within 30 seconds.`)) return;
    setBusy("deploy");
    setError(null);
    try {
      setDeployed(await api.deployPolicy(draft.draft_id, draft.version));
    } catch (e) {
      setError(e);
    } finally {
      setBusy("");
    }
  }

  return (
    <>
      <PageHead title="Policies" sub="Write a business policy in plain language. Gemini compiles it into deterministic rules; you review the tests and deploy." />
      <ErrorBox error={error} />
      <div className="grid two" style={{ marginTop: 12, alignItems: "start" }}>
        <form className="card stack" onSubmit={compile}>
          <h2>1 · Write the policy</h2>
          <label>
            Name <span className="hint">lowercase letters, digits, - and _</span>
            <input value={name} required pattern="[a-z0-9_\-]+" maxLength={80} placeholder="wine-order-terms"
              onChange={(e) => setName(e.target.value)} />
          </label>
          <label>
            Applies to
            <select value={ruleSet} onChange={(e) => setRuleSet(e.target.value)}>
              {RULE_SETS.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
            </select>
          </label>
          <label>
            Policy <span className="hint">Amounts in your currency; the compiler converts to minor units.</span>
            <textarea value={text} required minLength={20} maxLength={20000} rows={9}
              placeholder="Auto-approve orders up to ₹2,00,000 from known trade customers when every line matched our catalog and prices are within 5% of list…"
              onChange={(e) => setText(e.target.value)} />
          </label>
          <button className="btn primary" disabled={busy !== ""}>
            {busy === "compile" ? <><Spinner /> Compiling…</> : "Compile draft"}
          </button>
          <p className="muted small" style={{ margin: 0 }}>Compiling never changes live rules.</p>
        </form>

        <div className="stack">
          {compiled && !draft && (
            <div className="card">
              <h2>Compile result</h2>
              <Badge value={compiled.state} />
              {compiled.result?.text?.map((t, i) => <p key={i} className="alert warn" style={{ marginTop: 10 }}>{t}</p>)}
            </div>
          )}
          {draft && (
            <div className="card stack">
              <div className="row" style={{ justifyContent: "space-between" }}>
                <h2 style={{ margin: 0 }}>2 · Review the draft</h2>
                <span className={`badge ${draft.tests_passed === draft.tests_total ? "ok" : "warn"}`}>
                  tests {draft.tests_passed}/{draft.tests_total}
                </span>
              </div>
              <p className="muted small" style={{ margin: 0 }}>
                {draft.version} → {draft.proposed_version} · when no rule matches: <b>{draft.default_decision}</b>
              </p>
              {draft.rules.map((r) => (
                <div key={r.id} className="rule">
                  <div className="row"><Badge value={r.decision} /> <b>{r.reason}</b></div>
                  <code>{r.when}</code>
                  {r.max_refund_minor != null && <div className="small muted">cap: {r.max_refund_minor} minor units</div>}
                </div>
              ))}
              <details>
                <summary className="small">Always-on safety guards ({draft.baseline_rules.length})</summary>
                {draft.baseline_rules.map((r) => (
                  <div key={r.id} className="rule" style={{ marginTop: 6 }}>
                    <div className="row"><Badge value={r.decision} /> {r.reason}</div>
                    <code>{r.when}</code>
                  </div>
                ))}
              </details>
              <h3>Test cases</h3>
              <div className="table-wrap">
                <table>
                  <thead><tr><th>Case</th><th>Expected</th><th>Rules say</th></tr></thead>
                  <tbody>
                    {draft.test_results.map((t, i) => (
                      <tr key={i}>
                        <td>{t.passed ? "✅" : "⚠️"} {t.name}</td>
                        <td><Badge value={t.expected} /></td>
                        <td>{t.actual ? <Badge value={t.actual} /> : <span className="small">{t.error}</span>}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              {draft.assumptions.length > 0 && (
                <>
                  <h3>Assumptions the compiler made</h3>
                  <ul className="small" style={{ margin: 0, paddingLeft: 18 }}>
                    {draft.assumptions.map((a, i) => <li key={i}>{a}</li>)}
                  </ul>
                </>
              )}
              <div className="divider" />
              <h2>3 · Deploy</h2>
              {deployed ? (
                <div className={`alert ${deployed.state.endsWith("COMPLETED") ? "ok" : "bad"}`}>
                  {deployed.state.endsWith("COMPLETED")
                    ? `Deployed ${ruleSet} ${String(deployed.result?.data?.[0]?.version ?? "")}. Live within 30 seconds.`
                    : deployed.result?.text?.join(" ") || deployed.state}
                </div>
              ) : (
                <button className="btn primary" disabled={busy !== ""} onClick={() => void deploy()}>
                  {busy === "deploy" ? <><Spinner /> Deploying…</> : `Approve and deploy ${draft.proposed_version}`}
                </button>
              )}
            </div>
          )}
          {!compiled && (
            <div className="card muted small">
              Rules decide refunds and which purchase orders post to Odoo automatically. Anything the rules don't
              approve goes to a human, and built-in guards always escalate unverified or risky cases.
            </div>
          )}
        </div>
      </div>
    </>
  );
}
