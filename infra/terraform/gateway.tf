# Firebase Hosting + Firebase Auth + API Gateway (the "Routes Requests" box).

resource "google_api_gateway_api" "acs" {
  provider     = google-beta
  api_id       = "acs-api"
  display_name = "ACS API"
  depends_on   = [google_project_service.enabled]
}

resource "google_api_gateway_api_config" "acs" {
  provider             = google-beta
  api                  = google_api_gateway_api.acs.api_id
  api_config_id_prefix = "acs-"

  openapi_documents {
    document {
      path = "gateway.yaml"
      # Normalize line endings so a Windows checkout (autocrlf) does not force a new config.
      contents = base64encode(replace(templatefile("${path.module}/openapi/gateway.yaml.tftpl", {
        backend         = google_cloud_run_v2_service.orchestrator.uri
        project_id      = var.project_id
        managed_service = google_api_gateway_api.acs.managed_service
      }), "\r\n", "\n"))
    }
  }

  gateway_config {
    backend_config {
      google_service_account = google_service_account.sa["gateway"].email
    }
  }

  lifecycle {
    create_before_destroy = true
  }
}

resource "google_api_gateway_gateway" "acs" {
  provider   = google-beta
  gateway_id = "acs-gateway"
  region     = var.region
  api_config = google_api_gateway_api_config.acs.id
}

# The gateway's managed service must be enabled before API keys can target it.
resource "google_project_service" "gateway_managed_service" {
  service            = google_api_gateway_api.acs.managed_service
  disable_on_destroy = false
  depends_on         = [google_api_gateway_gateway.acs]
}

# Edge API keys: one per machine channel, each restricted to this gateway only.
# They provide edge quota/attribution; identity comes from ACS client credentials.
resource "google_apikeys_key" "shopper_agents" {
  name         = "acs-shopper-agents"
  display_name = "ACS - AI shopper agents (UCP / A2A)"
  restrictions {
    api_targets {
      service = google_api_gateway_api.acs.managed_service
    }
  }
  depends_on = [google_project_service.gateway_managed_service]
}

resource "google_apikeys_key" "b2b_partners" {
  name         = "acs-b2b-partners"
  display_name = "ACS - B2B partner PO intake"
  restrictions {
    api_targets {
      service = google_api_gateway_api.acs.managed_service
    }
  }
  depends_on = [google_project_service.gateway_managed_service]
}

resource "google_apikeys_key" "pubsub_push" {
  name         = "acs-pubsub-push"
  display_name = "ACS - Gmail Pub/Sub push"
  restrictions {
    api_targets {
      service = google_api_gateway_api.acs.managed_service
    }
  }
  depends_on = [google_project_service.gateway_managed_service]
}
