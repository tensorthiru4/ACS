import { useEffect, useState, type FormEvent } from "react";
import { api, type TaskView } from "../api";
import { can, type Account } from "../firebase";
import { Badge, ErrorBox, PageHead, Spinner } from "../ui";

const JOURNEYS = ["discovery", "product_detail", "cart", "checkout", "returns"];
const RECENT_KEY = "acs.audits.recent";

interface AuditResult {
  audit_id: string;
  store_url: string;
  score: number;
  journeys: { journey: string; score: number; checks: { id: string; title: string; passed: boolean }[] }[];
  fixes: { severity: string; finding: string; suggested_fix: string }[];
  errors: string[];
}

function recent(): { id: string; url: string }[] {
  try {
    return JSON.parse(localStorage.getItem(RECENT_KEY) ?? "[]");
  } catch {
    return [];
  }
}

export default function Audits({ account }: { account: Account }) {
  const [url, setUrl] = useState("https://");
  const [journeys, setJourneys] = useState(JOURNEYS.slice(0, 4));
  const [taskId, setTaskId] = useState<string | null>(recent()[0]?.id ?? null);
  const [task, setTask] = useState<TaskView | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);

  useEffect(() => {
    if (!taskId) return;
    let stop = false;
    async function poll() {
      try {
        const t = await api.getAudit(taskId!);
        if (stop) return;
        setTask(t);
        if (/SUBMITTED|WORKING/.test(t.state)) setTimeout(poll, 5000);
      } catch (e) {
        if (!stop) setError(e);
      }
    }
    setTask(null);
    void poll();
    return () => {
      stop = true;
    };
  }, [taskId]);

  async function start(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const t = await api.startAudit(url.trim(), journeys);
      if (t.task_id) {
        try {
          localStorage.setItem(RECENT_KEY, JSON.stringify([{ id: t.task_id, url: url.trim() }, ...recent()].slice(0, 10)));
        } catch {
          /* ignore */
        }
        setTaskId(t.task_id);
      }
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  }

  const result = task?.result?.data?.[0] as AuditResult | undefined;
  const running = task && /SUBMITTED|WORKING/.test(task.state);

  return (
    <>
      <PageHead title="Store audits" sub="Simulate AI-shopper journeys on your storefront and get an agent-readiness score with fixes." />
      <ErrorBox error={error} />
      <div className="grid two" style={{ marginTop: 12, alignItems: "start" }}>
        <div className="stack">
          {can(account, "analyst") && (
            <form className="card stack" onSubmit={start}>
              <h2>New audit</h2>
              <label>
                Storefront URL <span className="hint">Must be one of your registered storefront domains.</span>
                <input type="url" required value={url} onChange={(e) => setUrl(e.target.value)} />
              </label>
              <div className="chips">
                {JOURNEYS.map((j) => (
                  <label key={j} className={`chip${journeys.includes(j) ? " on" : ""}`}>
                    <input type="checkbox" checked={journeys.includes(j)}
                      onChange={() => setJourneys(journeys.includes(j) ? journeys.filter((x) => x !== j) : [...journeys, j])} />
                    {j.replace("_", " ")}
                  </label>
                ))}
              </div>
              <button className="btn primary" disabled={busy || journeys.length === 0}>
                {busy ? <Spinner /> : null} Start audit
              </button>
            </form>
          )}
          <div className="card stack">
            <h2>Recent audits</h2>
            {recent().length === 0 && <p className="muted small">None yet.</p>}
            {recent().map((r) => (
              <button key={r.id} className={`btn small${r.id === taskId ? " primary" : ""}`} onClick={() => setTaskId(r.id)}>
                {r.url}
              </button>
            ))}
          </div>
        </div>

        <div className="card stack">
          {!taskId && <p className="muted">Start an audit to see results here.</p>}
          {taskId && !task && <div className="row muted"><Spinner /> Loading…</div>}
          {task && (
            <>
              <div className="row" style={{ justifyContent: "space-between" }}>
                <h2 style={{ margin: 0 }}>Result</h2>
                <Badge value={task.state} />
              </div>
              {running && <div className="alert info row"><Spinner /> Browsing the storefront — this takes a few minutes.</div>}
              {task.result?.text?.map((t, i) => <div key={i} className="alert bad">{t}</div>)}
              {result && (
                <>
                  <div className="row">
                    <span className="score">{result.score}</span>
                    <span className="muted">/ 100 agent readiness<br /><span className="small">{result.store_url}</span></span>
                  </div>
                  {result.journeys.map((j) => (
                    <details key={j.journey}>
                      <summary>
                        <span className="row" style={{ display: "inline-flex", width: "calc(100% - 20px)" }}>
                          <b style={{ minWidth: 120 }}>{j.journey.replace("_", " ")}</b>
                          <span className="bar" style={{ flex: 1 }}><span style={{ width: `${j.score}%` }} /></span>
                          <span className="small">{j.score}</span>
                        </span>
                      </summary>
                      <ul className="small" style={{ margin: "6px 0", paddingLeft: 18 }}>
                        {j.checks.map((c) => <li key={c.id}>{c.passed ? "✅" : "❌"} {c.title}</li>)}
                      </ul>
                    </details>
                  ))}
                  {result.fixes.length > 0 && <h3>Suggested fixes</h3>}
                  {result.fixes.map((f, i) => (
                    <div key={i} className="rule">
                      <div className="row"><span className={`badge ${f.severity === "high" ? "bad" : f.severity === "medium" ? "warn" : ""}`}>{f.severity}</span> <b>{f.finding}</b></div>
                      <p className="small" style={{ margin: "4px 0 0" }}>{f.suggested_fix}</p>
                    </div>
                  ))}
                </>
              )}
            </>
          )}
        </div>
      </div>
    </>
  );
}
