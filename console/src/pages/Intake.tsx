import { useState, type FormEvent } from "react";
import { api, type IntakeReceipt } from "../api";
import { can, type Account } from "../firebase";
import { Badge, ErrorBox, PageHead, Spinner, when } from "../ui";

const RECENT_KEY = "acs.intake.customers";

function recentCustomers(): string[] {
  try {
    return JSON.parse(localStorage.getItem(RECENT_KEY) ?? "[]");
  } catch {
    return [];
  }
}

function rememberCustomer(ref: string) {
  try {
    const next = [ref, ...recentCustomers().filter((r) => r !== ref)].slice(0, 10);
    localStorage.setItem(RECENT_KEY, JSON.stringify(next));
  } catch {
    /* storage unavailable */
  }
}

export default function Intake({ account }: { account: Account }) {
  const [file, setFile] = useState<File | null>(null);
  const [customer, setCustomer] = useState("");
  const [receipt, setReceipt] = useState<IntakeReceipt | null>(null);
  const [lookup, setLookup] = useState("");
  const [status, setStatus] = useState<Record<string, unknown> | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);

  async function upload(event: FormEvent) {
    event.preventDefault();
    if (!file) return;
    setBusy(true);
    setError(null);
    try {
      const r = await api.uploadPo(file, customer.trim());
      if (customer.trim()) rememberCustomer(customer.trim());
      setReceipt(r);
      setLookup(r.intake_id);
      setStatus(null);
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  }

  async function check(event?: FormEvent) {
    event?.preventDefault();
    if (!lookup.trim()) return;
    setError(null);
    try {
      setStatus(await api.intake(lookup.trim()));
    } catch (e) {
      setError(e);
      setStatus(null);
    }
  }

  return (
    <>
      <PageHead title="PO intake" sub="Upload a purchase order (PDF or voice note). The Messy-Order agent reads it and drafts a sales order." />
      <ErrorBox error={error} />
      <div className="grid two" style={{ marginTop: 12 }}>
        {can(account, "analyst") ? (
          <form className="card stack" onSubmit={upload}>
            <h2>Upload a purchase order</h2>
            <label>
              File <span className="hint">PDF, WAV, MP3, OGG, FLAC, WEBM or M4A, up to 8 MB</span>
              <input type="file" accept=".pdf,audio/*" onChange={(e) => setFile(e.target.files?.[0] ?? null)} required />
            </label>
            <label>
              Customer <span className="hint">The buyer reference mapped to an Odoo customer. Identity comes from here, never from the document text; leave empty to send the order to review.</span>
              <input list="recent-customers" value={customer} placeholder="e.g. vineanddine.example"
                onChange={(e) => setCustomer(e.target.value)} />
              <datalist id="recent-customers">
                {recentCustomers().map((ref) => <option key={ref} value={ref} />)}
              </datalist>
            </label>
            <button className="btn primary" disabled={busy || !file}>
              {busy ? <Spinner /> : null} Upload
            </button>
            {receipt && (
              <div className="alert ok">
                {receipt.status === "duplicate" ? "Already received" : "Queued"} — intake{" "}
                <span className="mono">{receipt.intake_id.slice(0, 12)}…</span>. It usually takes under a minute;
                check below or in Draft orders.
              </div>
            )}
          </form>
        ) : (
          <div className="card"><h2>Upload</h2><p className="muted">Uploading needs the analyst role.</p></div>
        )}

        <form className="card stack" onSubmit={check}>
          <h2>Intake status</h2>
          <label>
            Intake id
            <input className="mono" value={lookup} onChange={(e) => setLookup(e.target.value)} placeholder="40-character id" />
          </label>
          <button className="btn">Check status</button>
          {status && (
            <dl className="kv">
              <dt>Status</dt><dd><Badge value={String(status.status)} /></dd>
              <dt>File</dt><dd>{String(status.filename ?? "—")}</dd>
              <dt>Type</dt><dd>{String(status.content_type ?? "—")}</dd>
              <dt>Source</dt><dd>{String(status.source ?? "—")}</dd>
              <dt>Received</dt><dd>{when(status.created_at as string)}</dd>
            </dl>
          )}
          {status?.status === "drafted" && (
            <a className="btn" href="#/orders">Open in Draft orders →</a>
          )}
        </form>
      </div>
    </>
  );
}
