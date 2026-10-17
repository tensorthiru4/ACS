# ---------------------------------------------------------------- Firestore (free tier: 1 GiB)
resource "google_firestore_database" "default" {
  name                              = "(default)"
  location_id                       = var.region
  type                              = "FIRESTORE_NATIVE"
  concurrency_mode                  = "OPTIMISTIC"
  point_in_time_recovery_enablement = "POINT_IN_TIME_RECOVERY_DISABLED"
  delete_protection_state           = var.environment == "prod" ? "DELETE_PROTECTION_ENABLED" : "DELETE_PROTECTION_DISABLED"
  depends_on                        = [google_project_service.enabled]
}

# TTL policies purge abandoned ADK sessions/events and console progress records (keeps
# storage inside the free tier).
resource "google_firestore_field" "ttl" {
  for_each   = toset(["sessions", "events", "console_progress"])
  database   = google_firestore_database.default.name
  collection = each.value
  field      = "expire_at"
  ttl_config {}
  # Exempt the TTL field from indexing: saves index storage and write cost.
  index_config {}
}

# "Vector SKU Match": KNN index used by the Messy-Order-to-ERP agent.
resource "google_firestore_index" "sku_vector" {
  database   = google_firestore_database.default.name
  collection = "skus"
  fields {
    field_path = "merchant_id"
    order      = "ASCENDING"
  }
  # Firestore appends this implicitly; declaring it avoids a perpetual replace.
  fields {
    field_path = "__name__"
    order      = "ASCENDING"
  }
  fields {
    field_path = "embedding"
    vector_config {
      dimension = 768
      flat {}
    }
  }
}

# Clients never talk to Firestore directly: deny-all rules.
resource "google_firebaserules_ruleset" "firestore" {
  source {
    files {
      name = "firestore.rules"
      # Normalize line endings so a Windows checkout (autocrlf) does not force a new ruleset.
      content = replace(file("${path.module}/../../firebase/firestore.rules"), "\r\n", "\n")
    }
  }
  depends_on = [google_firestore_database.default]
}

resource "google_firebaserules_release" "firestore" {
  name         = "cloud.firestore"
  ruleset_name = google_firebaserules_ruleset.firestore.name
  lifecycle {
    replace_triggered_by = [google_firebaserules_ruleset.firestore]
  }
}

# ---------------------------------------------------------------- Cloud Storage (free: 5 GB regional)
resource "google_storage_bucket" "raw_po" {
  name                        = "${var.project_id}-acs-raw-po"
  location                    = upper(var.region)
  storage_class               = "STANDARD"
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = var.environment != "prod"

  # Soft delete retains (and bills) deleted bytes; raw POs have their own retention below.
  soft_delete_policy {
    retention_duration_seconds = 0
  }

  lifecycle_rule {
    condition {
      age = var.raw_po_retention_days
    }
    action {
      type = "Delete"
    }
  }
}

# ---------------------------------------------------------------- Pub/Sub (free: 10 GiB/month)
resource "google_pubsub_topic" "po_intake" {
  name = "po-intake-events"
  message_storage_policy {
    allowed_persistence_regions = [var.region]
  }
  depends_on = [google_project_service.enabled]
}

resource "google_pubsub_topic" "po_intake_dlq" {
  name = "po-intake-events-dlq"
  message_storage_policy {
    allowed_persistence_regions = [var.region]
  }
}

# Pub/Sub service agent forwards undeliverable messages to the DLQ.
resource "google_pubsub_topic_iam_member" "dlq_publisher" {
  topic  = google_pubsub_topic.po_intake_dlq.id
  role   = "roles/pubsub.publisher"
  member = local.pubsub_service_agent
}

resource "google_pubsub_subscription" "po_intake_dlq_hold" {
  name                       = "po-intake-events-dlq-hold"
  topic                      = google_pubsub_topic.po_intake_dlq.id
  message_retention_duration = "604800s"
  expiration_policy {
    ttl = ""
  }
}

# "Push Consume": authenticated push into the Messy-Order-to-ERP agent.
resource "google_pubsub_subscription" "po_intake_push" {
  count                = var.agent_images.messy_order != "" ? 1 : 0
  name                 = "po-intake-events-messy-order"
  topic                = google_pubsub_topic.po_intake.id
  ack_deadline_seconds = 600

  push_config {
    push_endpoint = "${google_cloud_run_v2_service.agent["messy_order"].uri}/pubsub/po-intake"
    oidc_token {
      service_account_email = google_service_account.sa["pubsub_push"].email
      audience              = google_cloud_run_v2_service.agent["messy_order"].uri
    }
  }

  retry_policy {
    minimum_backoff = "10s"
    maximum_backoff = "600s"
  }

  dead_letter_policy {
    dead_letter_topic     = google_pubsub_topic.po_intake_dlq.id
    max_delivery_attempts = 5
  }

  expiration_policy {
    ttl = ""
  }
}

resource "google_pubsub_subscription_iam_member" "dlq_subscriber" {
  count        = length(google_pubsub_subscription.po_intake_push)
  subscription = google_pubsub_subscription.po_intake_push[0].id
  role         = "roles/pubsub.subscriber"
  member       = local.pubsub_service_agent
}

# Gmail `users.watch` publishes mailbox change notifications here.
resource "google_pubsub_topic" "gmail_inbox" {
  count = var.gmail.enabled ? 1 : 0
  name  = "gmail-b2b-inbox"
  message_storage_policy {
    allowed_persistence_regions = [var.region]
  }
}

resource "google_pubsub_topic_iam_member" "gmail_publisher" {
  count  = var.gmail.enabled ? 1 : 0
  topic  = google_pubsub_topic.gmail_inbox[0].id
  role   = "roles/pubsub.publisher"
  member = "serviceAccount:gmail-api-push@system.gserviceaccount.com"
}

resource "google_pubsub_subscription" "gmail_push" {
  count                = var.gmail.enabled ? 1 : 0
  name                 = "gmail-b2b-inbox-orchestrator"
  topic                = google_pubsub_topic.gmail_inbox[0].id
  ack_deadline_seconds = 120

  push_config {
    # Through API Gateway (key in query for edge quota); orchestrator verifies the OIDC token.
    push_endpoint = "https://${google_api_gateway_gateway.acs.default_hostname}/v1/intake/gmail-push?key=${google_apikeys_key.pubsub_push.key_string}"
    oidc_token {
      service_account_email = google_service_account.sa["pubsub_push"].email
      audience              = local.gmail_push_audience
    }
  }

  retry_policy {
    minimum_backoff = "10s"
    maximum_backoff = "300s"
  }

  expiration_policy {
    ttl = ""
  }
}

# ---------------------------------------------------------------- BigQuery (free: 10 GB, 1 TB query)
resource "google_bigquery_dataset" "acs" {
  dataset_id                 = "acs"
  location                   = "US"
  description                = "Readiness scores, audit fixes and security audit logs."
  delete_contents_on_destroy = var.environment != "prod"
  depends_on                 = [google_project_service.enabled]
}

resource "google_bigquery_table" "readiness_scores" {
  dataset_id          = google_bigquery_dataset.acs.dataset_id
  table_id            = "readiness_scores"
  deletion_protection = var.environment == "prod"
  time_partitioning {
    type  = "DAY"
    field = "audited_at"
  }
  clustering = ["merchant_id"]
  schema = jsonencode([
    { name = "audit_id", type = "STRING", mode = "REQUIRED" },
    { name = "merchant_id", type = "STRING", mode = "REQUIRED" },
    { name = "store_url", type = "STRING", mode = "REQUIRED" },
    { name = "journey", type = "STRING", mode = "REQUIRED" },
    { name = "score", type = "FLOAT64", mode = "REQUIRED" },
    { name = "details", type = "JSON", mode = "NULLABLE" },
    { name = "audited_at", type = "TIMESTAMP", mode = "REQUIRED" },
  ])
}

resource "google_bigquery_table" "audit_fixes" {
  dataset_id          = google_bigquery_dataset.acs.dataset_id
  table_id            = "audit_fixes"
  deletion_protection = var.environment == "prod"
  time_partitioning {
    type  = "DAY"
    field = "created_at"
  }
  clustering = ["merchant_id"]
  schema = jsonencode([
    { name = "audit_id", type = "STRING", mode = "REQUIRED" },
    { name = "merchant_id", type = "STRING", mode = "REQUIRED" },
    { name = "severity", type = "STRING", mode = "REQUIRED" },
    { name = "finding", type = "STRING", mode = "REQUIRED" },
    { name = "suggested_fix", type = "STRING", mode = "NULLABLE" },
    { name = "created_at", type = "TIMESTAMP", mode = "REQUIRED" },
  ])
}

# Security audit trail: structured `audit=true` log lines from every ACS service.
resource "google_logging_project_sink" "audit_to_bq" {
  name                   = "acs-audit-to-bigquery"
  destination            = "bigquery.googleapis.com/projects/${var.project_id}/datasets/${google_bigquery_dataset.acs.dataset_id}"
  filter                 = "resource.type=\"cloud_run_revision\" AND jsonPayload.audit=true"
  unique_writer_identity = true
  bigquery_options {
    use_partitioned_tables = true
  }
}

resource "google_bigquery_dataset_iam_member" "sink_writer" {
  dataset_id = google_bigquery_dataset.acs.dataset_id
  role       = "roles/bigquery.dataEditor"
  member     = google_logging_project_sink.audit_to_bq.writer_identity
}

# ---------------------------------------------------------------- Secret Manager (free: 6 active versions)
locals {
  secrets = {
    "gemini-api-key"   = "Google AI Studio API key for Gemini Flash"
    "groq-api-key"     = "Groq API key for the fallback model used when Gemini is out of quota"
    "api-key-pepper"   = "HMAC pepper for ACS client credential hashes (>= 32 random chars)"
    "gmail-oauth-json" = "Gmail OAuth client + refresh token JSON (gmail.readonly)"
    "odoo-credentials" = "Odoo XML-RPC user + API key for the ERP bridge"
  }
}

resource "google_secret_manager_secret" "secret" {
  for_each  = local.secrets
  secret_id = each.key
  labels    = { app = "acs" }
  annotations = {
    purpose = each.value
  }
  replication {
    # groq-api-key was created in the console with automatic replication; replication
    # cannot change in place, so matching it avoids replacing the secret (and its key).
    dynamic "auto" {
      for_each = each.key == "groq-api-key" ? [1] : []
      content {}
    }
    dynamic "user_managed" {
      for_each = each.key == "groq-api-key" ? [] : [1]
      content {
        replicas {
          location = var.region
        }
      }
    }
  }
  depends_on = [google_project_service.enabled]
}

# ---------------------------------------------------------------- Artifact Registry (free: 0.5 GB)
resource "google_artifact_registry_repository" "acs" {
  location      = var.region
  repository_id = "acs"
  format        = "DOCKER"

  docker_config {
    immutable_tags = true
  }

  cleanup_policy_dry_run = false
  cleanup_policies {
    id     = "keep-recent"
    action = "KEEP"
    most_recent_versions {
      keep_count = 3
    }
  }
  cleanup_policies {
    id     = "delete-old"
    action = "DELETE"
    condition {
      older_than = "604800s"
    }
  }
  depends_on = [google_project_service.enabled]
}
