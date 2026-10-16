# Deploying ACS on the GCP free tier

The steps below assume a project with billing enabled (Cloud Run requires it) and the `gcloud`,
`terraform` (1.6 or later), `firebase` and `uv` CLIs.

## 0. Free-tier budget at a glance

| Service | Always-free allowance (per month) | ACS usage |
|---|---|---|
| Cloud Run | 2M requests, 360k GiB-s, 180k vCPU-s (us-central1) | All agents scale to zero |
| Firestore | 1 GiB, 50k reads / 20k writes / 20k deletes per day | Sessions (TTL 7d), ledgers, rule sets, SKU vectors |
| Pub/Sub | 10 GiB | PO intake events (8 MiB max each) |
| Cloud Storage | 5 GB regional (US) | Raw POs, deleted after 90 days |
| BigQuery | 10 GB storage, 1 TB query | Readiness scores, fixes, audit log |
| Secret Manager | 6 active versions, 10k accesses | 4 secrets, read once per instance start |
| API Gateway | 2M calls | Front door |
| Artifact Registry | 0.5 GB | Cleanup keeps the 3 newest images |
| Cloud Build | 2,500 build-min (e2-standard-2) | CI |
| Gemini Flash | AI Studio free-tier rate limits | All LLM agents |
| Cloud Tasks | 1M operations | One task per storefront audit |
| Cloud Workflows | 5,000 internal steps, 2,000 external HTTP calls | ERP bridge: about 15 steps + 3 to 4 Odoo calls per order |

## 1. Bootstrap

```bash
export PROJECT_ID=my-acs-project REGION=us-central1
gcloud config set project $PROJECT_ID
gcloud services enable cloudresourcemanager.googleapis.com serviceusage.googleapis.com
gsutil mb -l $REGION -b on gs://$PROJECT_ID-tfstate && gsutil versioning set on gs://$PROJECT_ID-tfstate
firebase projects:addfirebase $PROJECT_ID     # enables Firebase Auth + Hosting
```

In the Firebase console, enable **Authentication → Email/Password**. Optionally add Google as a
provider. Do not enable anonymous sign-in.

## 2. Create the APIs and image registry, then build the first image

```bash
cd infra/terraform
cp terraform.tfvars.example terraform.tfvars   # set project_id (image comes next)
terraform init -backend-config="bucket=$PROJECT_ID-tfstate"
terraform apply -target=google_project_service.enabled -target=google_artifact_registry_repository.acs
cd ../..
gcloud builds submit --config cloudbuild/orchestrator.yaml \
  --substitutions=SHORT_SHA=$(git rev-parse --short HEAD) .
```

On the first build the service does not exist yet, so the build skips the deploy and prints the
image digest to put in `orchestrator_image`.

## 3. Apply the infrastructure

```bash
cd infra/terraform
terraform apply
```

Then add secret values. Secret Manager keeps them; Terraform never holds them.

```bash
printf '%s' "$GEMINI_KEY" | gcloud secrets versions add gemini-api-key --data-file=-
openssl rand -base64 48 | tr -d '\n' | gcloud secrets versions add api-key-pepper --data-file=-
# optional: Gmail B2B inbox  {"client_id":..,"client_secret":..,"refresh_token":..}
gcloud secrets versions add gmail-oauth-json --data-file=gmail-oauth.json
```

Set `public_base_url` to the `gateway_url` output (or your custom domain) and run
`terraform apply` again. This value feeds the UCP profile and the A2A agent card.

## 4. Provision tenants and credentials

```bash
cd services/orchestrator
export ACS_PROJECT_ID=$PROJECT_ID
uv run --group admin acs-admin register-merchant --merchant m1 --domain shop.example.com
uv run --group admin acs-admin set-claims --uid <firebase-uid> --merchant m1 --role admin
uv run --group admin acs-admin create-client --merchant m1 --kind shopper_agent \
    --ucp-profile https://agent.example/.well-known/ucp
uv run --group admin acs-admin create-client --merchant m1 --kind b2b_partner
```

Give each machine client two things: its `acsk_…` credential, sent as `Authorization: Bearer`,
and the matching gateway API key, sent as `x-api-key`. Read the key with
`terraform output -raw shopper_agents_api_key`.

## 5. Gmail B2B inbox (optional)

Set `gmail.enabled = true` in tfvars, apply, then start the watch:

```bash
uv run --group admin acs-admin gmail-watch --topic "$(terraform -chdir=../../infra/terraform output -raw gmail_topic)"
```

Watches expire after 7 days, so re-run the command daily. A Cloud Scheduler job can do this, and
three jobs are free.

## 6. Smoke test

```bash
GW=$(terraform -chdir=infra/terraform output -raw gateway_url)
curl -s $GW/.well-known/ucp | jq .ucp.version
curl -s $GW/a2a/.well-known/agent-card.json | jq .name
curl -s -X POST $GW/v1/console/sessions -H "Authorization: Bearer $FIREBASE_ID_TOKEN"
```

## 7. CI/CD

Create a Workload Identity Federation pool and provider for GitHub. Grant the deploy SA
`roles/cloudbuild.builds.editor`, `roles/run.developer`, `roles/artifactregistry.writer` and
`roles/iam.serviceAccountUser` on the orchestrator SA. Then set these repo variables:
`GCP_WIF_PROVIDER`, `GCP_DEPLOY_SA` and `GCP_PROJECT_ID`.

## 8. Downstream agents and the ERP bridge

Build each agent from the repository root. On the first build the service does not exist yet,
so the build prints the digest to put in `agent_images`:

```bash
for agent in rule_engine policy_compiler auditor messy_order; do
  gcloud builds submit --config cloudbuild/agent.yaml \
    --substitutions=_AGENT=$agent,SHORT_SHA=$(git rev-parse --short HEAD) .
done
```

Set `agent_images = { rule_engine = "…@sha256:…", … }` (and optionally `erp = {…}`) in
`terraform.tfvars`, then run `terraform apply`. This creates the agents, the `acs-audits` Cloud
Tasks queue, the `acs-erp-bridge` workflow and the Pub/Sub push subscription, and wires every agent
URL into the orchestrator. The rule engine can be deployed alone first: refunds then use its
baseline rules, so everything escalates until a policy is deployed.

**Odoo.** Run Odoo Community yourself (HTTPS required) and create a dedicated user with sales
rights and an API key. Then store the credentials:

```bash
printf '%s' '{"url":"https://erp.example.com","db":"prod","login":"acs-bridge@example.com","api_key":"…"}' \
  | gcloud secrets versions add odoo-credentials --data-file=-
```

**Catalog and customers** (needed before orders can auto-post):

```bash
cd services/messy_order
uv run acs-messy-admin load-skus --merchant m1 --csv catalog.csv
uv run acs-messy-admin set-customer --merchant m1 --ref buyer-one.example --partner-id 42 --terms 30
```

**First policies.** Compile in the console, or with `POST /v1/policies/compile` and
`target_rule_set` set to `refunds` or `order_terms`. Review the test table, then deploy with
`POST /v1/policies/{draft_id}/deploy`. Orders auto-post only when an `order_terms` rule
approves them. Otherwise they wait in `GET /v1/orders/drafts?status=needs_review`.
