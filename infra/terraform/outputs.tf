output "gateway_url" {
  value       = "https://${google_api_gateway_gateway.acs.default_hostname}"
  description = "Set var.public_base_url to this (or your custom domain) and apply again."
}

output "orchestrator_url" {
  value       = google_cloud_run_v2_service.orchestrator.uri
  description = "Private Cloud Run URL (IAM-protected; use the gateway)."
}

output "agent_urls" {
  value = { for k, v in google_cloud_run_v2_service.agent : k => v.uri }
}

output "service_accounts" {
  value = { for k, v in google_service_account.sa : k => v.email }
}

output "raw_po_bucket" {
  value = google_storage_bucket.raw_po.name
}

output "gmail_topic" {
  value = var.gmail.enabled ? google_pubsub_topic.gmail_inbox[0].id : null
}

output "shopper_agents_api_key" {
  value     = google_apikeys_key.shopper_agents.key_string
  sensitive = true
}

output "b2b_partners_api_key" {
  value     = google_apikeys_key.b2b_partners.key_string
  sensitive = true
}
