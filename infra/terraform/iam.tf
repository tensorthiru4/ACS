# One service account per workload, each granted only what its diagram edges need.

locals {
  service_accounts = {
    orchestrator    = "ACS Orchestrator (Cloud Run)"
    auditor         = "Agent-Readiness Auditor (Cloud Run)"
    policy_compiler = "Policy-as-Code Compiler (Cloud Run)"
    rule_engine     = "Deterministic Rule Engine (Cloud Run)"
    messy_order     = "Messy-Order-to-ERP Agent (Cloud Run)"
    erp_bridge      = "ERP Bridge + Safety Gate (Cloud Workflows)"
    gateway         = "API Gateway backend identity"
    pubsub_push     = "Pub/Sub push identity (OIDC)"
  }
  pubsub_service_agent = "serviceAccount:service-${data.google_project.this.number}@gcp-sa-pubsub.iam.gserviceaccount.com"
}

resource "google_service_account" "sa" {
  for_each     = local.service_accounts
  account_id   = "acs-${replace(each.key, "_", "-")}"
  display_name = each.value
}

# ---- Orchestrator --------------------------------------------------------------------
# Session State / ledgers in Firestore.
resource "google_project_iam_member" "orchestrator_firestore" {
  project = var.project_id
  role    = "roles/datastore.user"
  member  = google_service_account.sa["orchestrator"].member
}

# Firebase ID token revocation checks (verify_id_token check_revoked=True) read the user record.
resource "google_project_iam_member" "orchestrator_firebaseauth_viewer" {
  project = var.project_id
  role    = "roles/firebaseauth.viewer"
  member  = google_service_account.sa["orchestrator"].member
}

# "Publishes PO" — publisher on the intake topic only (not project-wide).
resource "google_pubsub_topic_iam_member" "orchestrator_publish_po" {
  topic  = google_pubsub_topic.po_intake.id
  role   = "roles/pubsub.publisher"
  member = google_service_account.sa["orchestrator"].member
}

# ---- Downstream agents ---------------------------------------------------------------
resource "google_project_iam_member" "agents_firestore" {
  # Rule engine reads rule sets; messy-order agent writes draft SOs and reads the SKU index;
  # the compiler stores drafts and deploys rule sets; the auditor persists A2A task state.
  for_each = toset(["rule_engine", "messy_order", "policy_compiler", "auditor"])
  project  = var.project_id
  role     = "roles/datastore.user"
  member   = google_service_account.sa[each.key].member
}

# "Writes Scores, Fixes" — auditor writes to the ACS dataset only.
resource "google_bigquery_dataset_iam_member" "auditor_writer" {
  dataset_id = google_bigquery_dataset.acs.dataset_id
  role       = "roles/bigquery.dataEditor"
  member     = google_service_account.sa["auditor"].member
}

resource "google_project_iam_member" "auditor_bq_jobs" {
  project = var.project_id
  role    = "roles/bigquery.jobUser"
  member  = google_service_account.sa["auditor"].member
}

# "Stores Raw PO" — messy-order agent writes objects (no delete, no ACL changes).
resource "google_storage_bucket_iam_member" "messy_order_raw_po" {
  bucket = google_storage_bucket.raw_po.name
  role   = "roles/storage.objectCreator"
  member = google_service_account.sa["messy_order"].member
}

resource "google_storage_bucket_iam_member" "messy_order_raw_po_read" {
  bucket = google_storage_bucket.raw_po.name
  role   = "roles/storage.objectViewer"
  member = google_service_account.sa["messy_order"].member
}

# "Draft Sales Order" — messy-order agent may start the ERP bridge workflow, nothing else.
resource "google_project_iam_member" "messy_order_invoke_workflow" {
  project = var.project_id
  role    = "roles/workflows.invoker"
  member  = google_service_account.sa["messy_order"].member
}

# ---- Pub/Sub push identity -----------------------------------------------------------
# Pub/Sub must be able to mint OIDC tokens for the push SA.
resource "google_service_account_iam_member" "pubsub_token_creator" {
  service_account_id = google_service_account.sa["pubsub_push"].name
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = local.pubsub_service_agent
}

# ---- Secrets: each service reads only its own secrets ----------------------------------
resource "google_secret_manager_secret_iam_member" "orchestrator_secrets" {
  for_each  = toset(["gemini-api-key", "api-key-pepper", "gmail-oauth-json"])
  secret_id = google_secret_manager_secret.secret[each.value].id
  role      = "roles/secretmanager.secretAccessor"
  member    = google_service_account.sa["orchestrator"].member
}

resource "google_secret_manager_secret_iam_member" "agent_gemini_key" {
  for_each  = toset(["auditor", "policy_compiler", "messy_order"])
  secret_id = google_secret_manager_secret.secret["gemini-api-key"].id
  role      = "roles/secretmanager.secretAccessor"
  member    = google_service_account.sa[each.key].member
}

resource "google_secret_manager_secret_iam_member" "erp_bridge_odoo" {
  secret_id = google_secret_manager_secret.secret["odoo-credentials"].id
  role      = "roles/secretmanager.secretAccessor"
  member    = google_service_account.sa["erp_bridge"].member
}

# Every workload writes logs.
resource "google_project_iam_member" "log_writer" {
  for_each = local.service_accounts
  project  = var.project_id
  role     = "roles/logging.logWriter"
  member   = google_service_account.sa[each.key].member
}
