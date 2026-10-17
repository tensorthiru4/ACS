# ACS Messy-Order-to-ERP Agent (Gemini multimodal for PDF and audio)

Turns purchase orders that arrive as PDFs, voice notes or e-mails into draft sales orders, and
hands approved ones to the ERP Bridge + Safety Gate.

```
Pub/Sub po-intake-events ─push─► POST /pubsub/po-intake
   lease po_intake/{id} ─► raw PO ─► GCS ─► Gemini extraction (schema-constrained, untrusted input)
   ─► SKU match: exact code/alias, then Firestore KNN (cosine, margin to runner-up)
   ─► customer from the authenticated channel (customers/{merchant}:{ref})
   ─► Rule Engine order_terms ─► draft_sales_orders/{id}
        APPROVE  ─► Cloud Workflows acs-erp-bridge ─► Odoo
        ESCALATE ─► needs_review ─► console: POST /v1/orders/drafts/{id}/release|reject (orchestrator)
        DENY     ─► rejected
```

Contract: [`docs/contracts/README.md#4`](../../docs/contracts/README.md#4-po-intake-event--pubsub-po-intake-events)
and [`#5`](../../docs/contracts/README.md#5-draft-sales-orders-and-the-erp-bridge).

## Reliability

* **Idempotent on `intake_id`.** A delivery must win a 10-minute lease on `po_intake/{id}`.
  Duplicates during the lease get 503 and are retried later. Finished intakes are acknowledged.
  Drafts are created once and never overwritten.
* **Errors.** Permanent errors (malformed event, sha256 mismatch, not a PO, invalid model output)
  mark the intake `failed` and acknowledge the message. Transient errors (model busy, storage)
  return 503 for a Pub/Sub retry. The 5th attempt marks the intake `failed`, and the message goes
  to the DLQ.
* **Fail-safe.** If the rule engine is unavailable, the draft is escalated to human review.

## Security

* The document is untrusted. The model is told it is data, the output is schema-constrained and
  re-validated, and nothing it says can approve or post an order.
* The buyer's identity comes from the authenticated channel (Gmail sender domain, partner client,
  uploader's choice), never from the document text.
* Unit prices are compared to catalog prices (`max_price_variance_pct`), so a document that
  "discounts" itself is escalated.

## Catalog and customers

```bash
export ACS_PROJECT_ID=... GOOGLE_API_KEY=...
uv run acs-messy-admin load-skus --merchant m1 --csv catalog.csv
uv run acs-messy-admin set-customer --merchant m1 --ref buyer-one.example --partner-id 42 --terms 30
```

`catalog.csv` columns: `sku,name,description,uom,list_price,currency,odoo_product_id,aliases`.
`aliases` are `;`-separated buyer codes, and embeddings are 768-dim `gemini-embedding-001`, matching
the Firestore vector index.

## Run locally

```bash
uv sync && uv run pytest
uv run python -m acs_messy_order.main
```
