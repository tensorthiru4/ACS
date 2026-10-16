# Downstream agents: configuration, Cloud Tasks (auditor runs), the ERP Bridge + Safety
# Gate workflow, and the Firestore TTLs / indexes the agents rely on.

locals {
  # Deterministic Cloud Run URLs (https://<service>-<project number>.<region>.run.app).
  # Agents reference each other through these to avoid a dependency cycle inside the
  # for_each; each service also lists its URL in custom_audiences.
  run_url = {
    for k in ["auditor", "policy_compiler", "rule_engine", "messy_order"] :
    k => "https://acs-${replace(k, "_", "-")}-${data.google_project.this.number}.${var.region}.run.app"
  }
  sa_email = { for k, v in google_service_account.sa : k => v.email }

  agent_env = {
    rule_engine = {
      ACS_ALLOWED_CALLERS = jsonencode([for c in local.agent_invokers.rule_engine : local.sa_email[c]])
    }
    policy_compiler = {
      ACS_SELF_URL              = local.run_url.policy_compiler
      ACS_GEMINI_MODEL          = var.gemini_model
      ACS_ALLOWED_CALLERS       = jsonencode([local.sa_email.orchestrator])
      GOOGLE_GENAI_USE_VERTEXAI = "FALSE"
    }
    auditor = {
      ACS_SELF_URL              = local.run_url.auditor
      ACS_REGION                = var.region
      ACS_GEMINI_MODEL          = var.gemini_model
      ACS_ALLOWED_CALLERS       = jsonencode([local.sa_email.orchestrator])
      ACS_RUNNER_CALLERS        = jsonencode([local.sa_email.auditor])
      ACS_TASKS_QUEUE           = google_cloud_tasks_queue.audits.name
      ACS_TASKS_SERVICE_ACCOUNT = local.sa_email.auditor
      ACS_BIGQUERY_DATASET      = google_bigquery_dataset.acs.dataset_id
      GOOGLE_GENAI_USE_VERTEXAI = "FALSE"
      PLAYWRIGHT_BROWSERS_PATH  = "/ms-playwright"
    }
    messy_order = {
      ACS_REGION                = var.region
      ACS_RAW_PO_BUCKET         = google_storage_bucket.raw_po.name
      ACS_RULE_ENGINE_URL       = local.run_url.rule_engine
      ACS_ERP_WORKFLOW          = google_workflows_workflow.erp_bridge.name
      ACS_GEMINI_MODEL          = var.gemini_model
      ACS_PUSH_CALLERS          = jsonencode([local.sa_email.pubsub_push])
      ACS_ORCHESTRATOR_CALLERS  = jsonencode([local.sa_email.orchestrator])
      ACS_DEFAULT_CURRENCY      = var.erp.default_currency
      ACS_SKU_MAX_DISTANCE      = tostring(var.erp.sku_max_distance)
      ACS_SKU_MIN_MARGIN        = tostring(var.erp.sku_min_margin)
      GOOGLE_GENAI_USE_VERTEXAI = "FALSE"
    }
  }

  agent_secrets = {
    rule_engine     = {}
    policy_compiler = { GOOGLE_API_KEY = "gemini-api-key" }
    auditor         = { GOOGLE_API_KEY = "gemini-api-key" }
    messy_order     = { GOOGLE_API_KEY = "gemini-api-key" }
  }
}

# ---------------------------------------------------------------- Cloud Tasks (free: 1M ops/month)
# Cloud Run only gives an instance CPU while a request is in flight, so each audit runs
# as its own authenticated request rather than as background work after the A2A reply.
resource "google_cloud_tasks_queue" "audits" {
  name     = "acs-audits"
  location = var.region

  rate_limits {
    max_dispatches_per_second = 1
    max_concurrent_dispatches = 2
  }

  retry_config {
    max_attempts  = 3
    min_backoff   = "10s"
    max_backoff   = "300s"
    max_doublings = 3
  }

  depends_on = [google_project_service.enabled]
}

resource "google_cloud_tasks_queue_iam_member" "auditor_enqueue" {
  name     = google_cloud_tasks_queue.audits.name
  location = var.region
  role     = "roles/cloudtasks.enqueuer"
  member   = google_service_account.sa["auditor"].member
}

# Cloud Tasks mints the run request's OIDC token as the auditor SA (actAs on itself only).
resource "google_service_account_iam_member" "auditor_act_as_self" {
  service_account_id = google_service_account.sa["auditor"].name
  role               = "roles/iam.serviceAccountUser"
  member             = google_service_account.sa["auditor"].member
}

# ---------------------------------------------------------------- ERP Bridge + Safety Gate
# Cloud Workflows (free: 5,000 internal steps + 2,000 external calls per month).
resource "google_workflows_workflow" "erp_bridge" {
  name            = "acs-erp-bridge"
  region          = var.region
  description     = "ERP Bridge + Safety Gate: re-checks approved draft sales orders and posts them to Odoo over XML-RPC."
  service_account = google_service_account.sa["erp_bridge"].id
  # XML-RPC request bodies carry the Odoo API key, so step call logging stays off.
  call_log_level = "LOG_NONE"
  user_env_vars = {
    FIRESTORE_DATABASE  = google_firestore_database.default.name
    ODOO_SECRET         = google_secret_manager_secret.secret["odoo-credentials"].secret_id
    MAX_AUTO_POST_MINOR = tostring(var.erp.max_auto_post_minor)
    MAX_LINES           = "200"
    MAX_LINE_QUANTITY   = tostring(var.erp.max_line_quantity)
    CONFIRM_ORDERS      = var.erp.confirm_orders ? "true" : "false"
  }
  # Normalize line endings so a Windows checkout (autocrlf) does not force a new revision.
  source_contents = replace(file("${path.module}/../../workflows/erp_bridge.yaml"), "\r\n", "\n")

  depends_on = [google_project_service.enabled]
}

# The bridge reads and updates draft_sales_orders (and nothing else needs it).
resource "google_project_iam_member" "erp_bridge_firestore" {
  project = var.project_id
  role    = "roles/datastore.user"
  member  = google_service_account.sa["erp_bridge"].member
}

# ---------------------------------------------------------------- Firestore TTLs and indexes
# A2A task state (auditor, compiler) and uncompiled policy drafts expire on their own.
resource "google_firestore_field" "agent_ttl" {
  for_each   = toset(["a2a_task_state", "policy_drafts"])
  database   = google_firestore_database.default.name
  collection = each.value
  field      = "expire_at"
  ttl_config {}
  index_config {}
}

# Merchant Console: newest draft sales orders first (orchestrator list_draft_orders).
resource "google_firestore_index" "draft_orders_by_merchant" {
  database   = google_firestore_database.default.name
  collection = "draft_sales_orders"
  fields {
    field_path = "merchant_id"
    order      = "ASCENDING"
  }
  fields {
    field_path = "created_at"
    order      = "DESCENDING"
  }
}
