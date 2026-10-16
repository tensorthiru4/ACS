import { useEffect, useState } from "react";
import { api, type DraftOrder } from "../api";
import { can, type Account } from "../firebase";
import { Badge, Empty, ErrorBox, money, PageHead, Spinner, when } from "../ui";

const STATUSES = ["", "needs_review", "approved", "posting", "posted", "erp_failed", "erp_blocked", "rejected"];

export default function Orders({ account }: { account: Account }) {
  const [status, setStatus] = useState("needs_review");
  const [drafts, setDrafts] = useState<DraftOrder[] | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [error, setError] = useState<unknown>(null);

  async function load() {
    setError(null);
    setDrafts(null);
    try {
      setDrafts(await api.drafts(status || undefined));
    } catch (e) {
      setError(e);
      setDrafts([]);
    }
  }

  useEffect(() => {
    void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [status]);

  if (selected) {
    return <OrderDetail id={selected} account={account} onBack={() => { setSelected(null); void load(); }} />;
  }

  return (
    <>
      <PageHead title="Draft orders" sub="Sales orders drafted from purchase orders. Escalated ones wait here for review.">
        <div className="row">
          <select aria-label="Status filter" value={status} onChange={(e) => setStatus(e.target.value)} style={{ width: 190 }}>
            {STATUSES.map((s) => (
              <option key={s} value={s}>{s ? s.replace(/_/g, " ") : "All statuses"}</option>
            ))}
          </select>
          <button className="btn" onClick={() => void load()}>Refresh</button>
        </div>
      </PageHead>
      <ErrorBox error={error} />
      <div className="card">
        {drafts === null ? (
          <div className="row muted"><Spinner /> Loading…</div>
        ) : drafts.length === 0 ? (
          <Empty>No draft orders{status ? ` with status “${status.replace(/_/g, " ")}”` : ""}.</Empty>
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr><th>Received</th><th>PO number</th><th>Status</th><th className="num">Total</th></tr>
              </thead>
              <tbody>
                {drafts.map((d) => (
                  <tr key={d.intake_id} className="clickable" onClick={() => setSelected(d.intake_id)}>
                    <td>{when(d.created_at)}</td>
                    <td>{d.po_number ?? <span className="muted">—</span>}</td>
                    <td><Badge value={d.status} /></td>
                    <td className="num">{money(d.total_minor, d.currency)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </>
  );
}

function OrderDetail({ id, account, onBack }: { id: string; account: Account; onBack: () => void }) {
  const [draft, setDraft] = useState<DraftOrder | null>(null);
  const [overrides, setOverrides] = useState<Record<number, string>>({});
  const [customerRef, setCustomerRef] = useState("");
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [notice, setNotice] = useState("");

  async function load() {
    try {
      setDraft(await api.draft(id));
    } catch (e) {
      setError(e);
    }
  }
  useEffect(() => {
    void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id]);

  const isAdmin = can(account, "admin");
  const reviewable = draft && ["needs_review", "erp_failed"].includes(draft.status);
  const rejectable = draft && ["needs_review", "approved", "erp_failed"].includes(draft.status);

  async function release() {
    setBusy(true);
    setError(null);
    try {
      const line_overrides = Object.entries(overrides)
        .filter(([, sku]) => sku.trim())
        .map(([line_no, sku]) => ({ line_no: Number(line_no), sku: sku.trim() }));
      await api.releaseDraft(id, line_overrides, customerRef.trim() || null);
      setNotice("Released. The ERP bridge is posting it to Odoo as a quotation.");
      await load();
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  }

  async function reject() {
    if (!confirm("Reject this draft order?")) return;
    setBusy(true);
    setError(null);
    try {
      await api.rejectDraft(id, reason.trim());
      setNotice("Rejected.");
      await load();
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <PageHead title={draft?.po_number ? `PO ${draft.po_number}` : "Draft order"} sub={`Intake ${id}`}>
        <button className="btn" onClick={onBack}>← All drafts</button>
      </PageHead>
      <ErrorBox error={error} />
      {notice && <div className="alert ok" style={{ marginBottom: 12 }}>{notice}</div>}
      {!draft ? (
        <div className="row muted"><Spinner /> Loading…</div>
      ) : (
        <div className="stack">
          <div className="grid two">
            <div className="card">
              <h2>Order</h2>
              <dl className="kv">
                <dt>Status</dt><dd><Badge value={draft.status} /></dd>
                <dt>Customer</dt>
                <dd>
                  {draft.customer?.name || draft.customer?.ref || "—"}{" "}
                  {draft.customer && !draft.customer.known && <span className="badge warn">not mapped</span>}
                </dd>
                <dt>Total</dt><dd><b>{money(draft.total_minor, draft.currency)}</b></dd>
                <dt>Order date</dt><dd>{draft.order_date ?? "—"}</dd>
                <dt>Delivery</dt><dd>{draft.requested_delivery_date ?? "—"}</dd>
                <dt>Terms</dt><dd>{draft.payment_terms_days != null ? `${draft.payment_terms_days} days` : "—"}</dd>
                <dt>Source</dt><dd>{draft.source ?? "—"}</dd>
                {draft.erp?.order_id && (<><dt>Odoo order</dt><dd>#{draft.erp.order_id}</dd></>)}
              </dl>
            </div>
            <div className="card">
              <h2>Rule engine verdict</h2>
              {draft.terms ? (
                <>
                  <div className="row" style={{ marginBottom: 8 }}>
                    <Badge value={draft.terms.decision} />
                    <span className="muted small">
                      order_terms {draft.terms.rule_set_version || "(no policy deployed)"} · {draft.terms.source}
                    </span>
                  </div>
                  <ul style={{ margin: 0, paddingLeft: 18 }}>
                    {draft.terms.reasons.map((r, i) => <li key={i}>{r}</li>)}
                  </ul>
                </>
              ) : <p className="muted">—</p>}
              {draft.extraction && (
                <p className="muted small" style={{ marginTop: 10 }}>
                  Read with {Math.round(draft.extraction.confidence * 100)}% confidence
                  {draft.extraction.warnings.length > 0 && ` · ⚠ ${draft.extraction.warnings.join("; ")}`}
                </p>
              )}
              {draft.erp?.detail && draft.erp.detail.length > 0 && (
                <div className="alert warn small">{draft.erp.detail.join("; ")}</div>
              )}
            </div>
          </div>

          <div className="card">
            <h2>Lines</h2>
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>#</th><th>As written</th><th>Catalog match</th><th className="num">Qty</th>
                    <th className="num">Unit price</th><th className="num">vs list</th>
                    {reviewable && isAdmin && <th>Correct SKU</th>}
                  </tr>
                </thead>
                <tbody>
                  {(draft.lines ?? []).map((l) => (
                    <tr key={l.line_no}>
                      <td>{l.line_no}</td>
                      <td>{l.description}{l.customer_sku && <div className="muted small mono">{l.customer_sku}</div>}</td>
                      <td>
                        {l.sku ? (
                          <>
                            <span className="mono">{l.sku}</span>{" "}
                            <span className={`badge ${l.complete ? "ok" : "warn"}`}>
                              {l.match_method} {Math.round(l.match_confidence * 100)}%
                            </span>
                            <div className="muted small">{l.product_name}</div>
                          </>
                        ) : <span className="badge warn">unmatched</span>}
                      </td>
                      <td className="num">{l.quantity} {l.uom ?? ""}</td>
                      <td className="num">{money(l.unit_price_minor, draft.currency)}</td>
                      <td className="num">
                        {l.price_variance_pct ? <span className={l.price_variance_pct > 5 ? "badge bad" : ""}>{l.price_variance_pct}%</span> : "—"}
                      </td>
                      {reviewable && isAdmin && (
                        <td>
                          <input aria-label={`Correct SKU for line ${l.line_no}`} placeholder={l.complete ? "" : "e.g. WC-RED-007"}
                            value={overrides[l.line_no] ?? ""} style={{ minWidth: 130 }}
                            onChange={(e) => setOverrides({ ...overrides, [l.line_no]: e.target.value })} />
                        </td>
                      )}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>

          {isAdmin && (reviewable || rejectable) && (
            <div className="card stack">
              <h2>Review</h2>
              {reviewable && draft.customer && !draft.customer.known && (
                <label>
                  Customer reference <span className="hint">The buyer is not mapped to an Odoo customer; enter a known reference (e.g. vineanddine.example).</span>
                  <input value={customerRef} onChange={(e) => setCustomerRef(e.target.value)} />
                </label>
              )}
              <div className="row">
                {reviewable && (
                  <button className="btn primary" disabled={busy} onClick={() => void release()}>
                    {busy ? <Spinner /> : null} Approve and post to Odoo
                  </button>
                )}
                {rejectable && (
                  <>
                    <input aria-label="Rejection reason" placeholder="Reason (optional)" value={reason}
                      onChange={(e) => setReason(e.target.value)} style={{ maxWidth: 280 }} />
                    <button className="btn danger" disabled={busy} onClick={() => void reject()}>Reject</button>
                  </>
                )}
              </div>
              <p className="muted small" style={{ margin: 0 }}>
                The ERP safety gate re-checks every line, price and total before anything reaches Odoo.
              </p>
            </div>
          )}
        </div>
      )}
    </>
  );
}
