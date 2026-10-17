# Agents on Cloud Run (scale to zero, us-central1).

locals {
  public_base_url     = var.public_base_url != "" ? var.public_base_url : "https://gateway-pending.invalid"
  gmail_push_audience = "acs-gmail-push:${var.project_id}"

  agents = {
    for name, image in {
      auditor         = var.agent_images.auditor
      policy_compiler = var.agent_images.policy_compiler
      rule_engine     = var.agent_images.rule_engine
      messy_order     = var.agent_images.messy_order
    } : name => image if image != ""
  }

  # Who may invoke each private agent (diagram edges). The auditor invokes itself through
  # Cloud Tasks (audit runs); the orchestrator relays draft-order reviews to messy_order.
  agent_invokers = {
    auditor         = ["orchestrator", "auditor"]
    policy_compiler = ["orchestrator"]
    rule_engine     = ["orchestrator", "messy_order", "policy_compiler"]
    messy_order     = ["pubsub_push", "orchestrator"]
  }

  agent_url = { for k, v in google_cloud_run_v2_service.agent : k => v.uri }
}

resource "google_cloud_run_v2_service" "orchestrator" {
  name                = "acs-orchestrator"
  location            = var.region
  ingress             = "INGRESS_TRAFFIC_ALL" # reachable by API Gateway; IAM still required
  deletion_protection = var.environment == "prod"

  template {
    service_account                  = google_service_account.sa["orchestrator"].email
    timeout                          = "300s"
    max_instance_request_concurrency = 40
    execution_environment            = "EXECUTION_ENVIRONMENT_GEN2"

    scaling {
      min_instance_count = 0
      max_instance_count = var.orchestrator_max_instances
    }

    containers {
      image = var.orchestrator_image

      resources {
        limits = {
          cpu    = "1"
          memory = "1Gi"
        }
        cpu_idle          = true # CPU only while serving requests (free-tier friendly)
        startup_cpu_boost = true
      }

      ports {
        container_port = 8080
      }

      dynamic "env" {
        for_each = {
          ACS_ENV                          = var.environment
          ACS_PROJECT_ID                   = var.project_id
          ACS_REGION                       = var.region
          ACS_PUBLIC_BASE_URL              = local.public_base_url
          ACS_GEMINI_MODEL                 = var.gemini_model
          ACS_FALLBACK_MODEL               = var.fallback_model
          ACS_FIREBASE_PROJECT_ID          = var.project_id
          ACS_PO_INTAKE_TOPIC              = google_pubsub_topic.po_intake.name
          ACS_AUDITOR_URL                  = lookup(local.agent_url, "auditor", "")
          ACS_POLICY_COMPILER_URL          = lookup(local.agent_url, "policy_compiler", "")
          ACS_RULE_ENGINE_URL              = lookup(local.agent_url, "rule_engine", "")
          ACS_MESSY_ORDER_URL              = lookup(local.agent_url, "messy_order", "")
          ACS_CORS_ALLOWED_ORIGINS         = jsonencode(var.console_origins)
          ACS_GMAIL_PUSH_AUDIENCE          = var.gmail.enabled ? local.gmail_push_audience : ""
          ACS_GMAIL_PUSH_SERVICE_ACCOUNT   = var.gmail.enabled ? google_service_account.sa["pubsub_push"].email : ""
          ACS_GMAIL_MAILBOX                = var.gmail.mailbox
          ACS_GMAIL_MERCHANT_ID            = var.gmail.merchant_id
          ACS_GMAIL_ALLOWED_SENDER_DOMAINS = jsonencode(var.gmail.allowed_sender_domains)
          GOOGLE_GENAI_USE_VERTEXAI        = "FALSE"
        }
        content {
          name  = env.key
          value = env.value
        }
      }

      # Secrets are bound at deploy time; the code never calls Secret Manager.
      dynamic "env" {
        for_each = merge(
          {
            GOOGLE_API_KEY     = "gemini-api-key"
            ACS_API_KEY_PEPPER = "api-key-pepper"
          },
          var.gmail.enabled ? { ACS_GMAIL_OAUTH_JSON = "gmail-oauth-json" } : {}
        )
        content {
          name = env.key
          value_source {
            secret_key_ref {
              secret  = google_secret_manager_secret.secret[env.value].secret_id
              version = lookup(local.secret_versions, env.value, "latest")
            }
          }
        }
      }

      startup_probe {
        http_get {
          path = "/health"
        }
        initial_delay_seconds = 0
        period_seconds        = 3
        failure_threshold     = 20
      }

      liveness_probe {
        http_get {
          path = "/health"
        }
        period_seconds = 30
      }
    }
  }

  depends_on = [
    google_project_service.enabled,
    google_secret_manager_secret_iam_member.orchestrator_secrets,
  ]

  # CI (cloudbuild/orchestrator.yaml) rolls out new image digests; Terraform owns the rest.
  lifecycle {
    ignore_changes = [
      template[0].containers[0].image,
      client,
      client_version,
    ]
  }
}

# Only API Gateway may invoke the orchestrator (no allUsers binding anywhere).
resource "google_cloud_run_v2_service_iam_member" "orchestrator_invoker" {
  name     = google_cloud_run_v2_service.orchestrator.name
  location = var.region
  role     = "roles/run.invoker"
  member   = google_service_account.sa["gateway"].member
}

# ---- Downstream agents (deployed once their images exist) ------------------------------
resource "google_cloud_run_v2_service" "agent" {
  for_each = local.agents
  name     = "acs-${replace(each.key, "_", "-")}"
  location = var.region
  # IAM (roles/run.invoker per caller) is the gate. "Internal only" ingress would also need
  # Direct VPC egress on every caller; enable both together if you add a VPC.
  ingress             = "INGRESS_TRAFFIC_ALL"
  deletion_protection = var.environment == "prod"
  # Callers that cannot know the generated URL (other agents, Cloud Tasks) use the
  # deterministic one as their ID-token audience.
  custom_audiences = [local.run_url[each.key]]

  template {
    service_account = google_service_account.sa[each.key].email
    # The auditor drives Playwright: long requests, one at a time.
    timeout                          = each.key == "auditor" ? "900s" : "300s"
    max_instance_request_concurrency = each.key == "auditor" ? 1 : 20
    execution_environment            = "EXECUTION_ENVIRONMENT_GEN2"

    scaling {
      min_instance_count = 0
      max_instance_count = 2
    }

    containers {
      image = each.value
      resources {
        limits = {
          cpu    = "1"
          memory = each.key == "auditor" ? "2Gi" : "1Gi"
        }
        cpu_idle          = true
        startup_cpu_boost = true
      }
      ports {
        container_port = 8080
      }

      dynamic "env" {
        for_each = merge(
          {
            ACS_PROJECT_ID = var.project_id
            ACS_ENV        = var.environment
          },
          local.agent_env[each.key]
        )
        content {
          name  = env.key
          value = env.value
        }
      }

      # Secrets are bound at deploy time; the code never calls Secret Manager.
      dynamic "env" {
        for_each = local.agent_secrets[each.key]
        content {
          name = env.key
          value_source {
            secret_key_ref {
              secret  = google_secret_manager_secret.secret[env.value].secret_id
              version = lookup(local.secret_versions, env.value, "latest")
            }
          }
        }
      }

      startup_probe {
        http_get {
          path = "/health"
        }
        initial_delay_seconds = 0
        period_seconds        = 3
        failure_threshold     = 20
      }
    }
  }

  depends_on = [
    google_project_service.enabled,
    google_secret_manager_secret_iam_member.agent_gemini_key,
  ]

  # CI (cloudbuild/agent.yaml) rolls out new image digests; Terraform owns the rest.
  lifecycle {
    ignore_changes = [
      template[0].containers[0].image,
      client,
      client_version,
    ]
  }
}

resource "google_cloud_run_v2_service_iam_member" "agent_invokers" {
  for_each = {
    for pair in flatten([
      for agent, _ in local.agents : [
        for caller in local.agent_invokers[agent] : { key = "${agent}:${caller}", agent = agent, caller = caller }
      ]
    ]) : pair.key => pair
  }
  name     = google_cloud_run_v2_service.agent[each.value.agent].name
  location = var.region
  role     = "roles/run.invoker"
  member   = google_service_account.sa[each.value.caller].member
}
