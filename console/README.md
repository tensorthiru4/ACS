# ACS Merchant Console

A React + Vite single-page app for merchant staff. It's hosted on Firebase Hosting, signs in with
Firebase Auth, and calls the API Gateway (orchestrator).

| Page | Role | API |
|---|---|---|
| Assistant | viewer+ | `/v1/console/sessions…`: chat with the `acs_orchestrator` ADK agent |
| Draft orders | viewer (review: admin) | `/v1/orders/drafts…`: escalated POs; correct SKUs, release to Odoo, reject |
| PO intake | viewer (upload: analyst) | `/v1/intake/uploads`, `/v1/intake/{id}` |
| Policies | admin | `/v1/policies/compile`, `/v1/policies/{draft}/deploy` |
| Store audits | viewer (start: analyst) | `/v1/audits`, `/v1/audits/{task}` (polled) |
| Refunds | analyst+ | `/v1/refunds/decide` (with `Idempotency-Key`) |

Pages a role can't use are hidden. The orchestrator enforces the same rules server-side regardless.

## Develop

```bash
cp .env.example .env.local   # VITE_FIREBASE_* from Firebase project settings; VITE_API_BASE = gateway URL
npm install
npm run dev                  # http://localhost:5173; /api is proxied to the gateway (no CORS needed)
npm test                     # vitest
npm run build                # typecheck + production bundle in dist/
```

## Deploy (Firebase Hosting)

```bash
npm run build
cd ../firebase
firebase deploy --only hosting --project <project-id>
```

`firebase/firebase.json` serves `console/dist` with a strict CSP that allows only
`*.gateway.dev`, Firebase Auth and same-origin assets. The hosted origins must be listed in
Terraform `console_origins` so the orchestrator's CORS accepts them.

## Notes

* Chat transcripts are cached per session in the browser. The API keeps the conversation state but
  does not return past turns.
* Users need `merchant_id` and `role` custom claims:
  `acs-admin set-claims --uid <uid> --merchant m1 --role admin`.
