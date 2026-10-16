variable "project_id" {
  type        = string
  description = "GCP project id."
}

variable "region" {
  type        = string
  default     = "us-central1"
  description = "Region for every regional resource. Always-free quotas for Cloud Run, GCS and Compute apply only in a few US regions."
  validation {
    condition     = contains(["us-central1", "us-east1", "us-west1"], var.region)
    error_message = "Use a free-tier region: us-central1, us-east1 or us-west1."
  }
}

variable "environment" {
  type    = string
  default = "prod"
  validation {
    condition     = contains(["dev", "prod"], var.environment)
    error_message = "environment must be dev or prod."
  }
}

variable "orchestrator_image" {
  type        = string
  description = "Container image for the orchestrator, e.g. us-central1-docker.pkg.dev/<p>/acs/orchestrator:<sha>. Pin a digest in prod."
}

variable "agent_images" {
  type = object({
    auditor         = optional(string, "")
    policy_compiler = optional(string, "")
    rule_engine     = optional(string, "")
    messy_order     = optional(string, "")
  })
  default     = {}
  description = "Images for the downstream agents. Leave empty until each agent is built; the service is skipped and the orchestrator feature stays disabled."
}

variable "public_base_url" {
  type        = string
  default     = ""
  description = "Public https origin clients use (custom domain or the API Gateway URL from the `gateway_url` output). Set after the first apply; it appears in the UCP profile and A2A agent card."
}

variable "gemini_model" {
  type    = string
  default = "gemini-3.8-flash"
}

variable "console_origins" {
  type        = list(string)
  default     = []
  description = "Browser origins allowed by CORS (Firebase Hosting domains of the Merchant Console)."
}

variable "gmail" {
  type = object({
    enabled                = bool
    mailbox                = optional(string, "")
    merchant_id            = optional(string, "")
    allowed_sender_domains = optional(list(string), [])
  })
  default     = { enabled = false }
  description = "B2B inbox (Gmail push) settings."
}

variable "erp" {
  type = object({
    # Safety gate: orders above this total (minor units) are never auto-posted.
    max_auto_post_minor = optional(number, 500000)
    max_line_quantity   = optional(number, 100000)
    # false: orders land in Odoo as quotations for a human to confirm.
    confirm_orders   = optional(bool, false)
    default_currency = optional(string, "USD")
  })
  default     = {}
  description = "ERP Bridge + Safety Gate limits and the messy-order agent's default currency."
}

variable "billing_account" {
  type        = string
  default     = ""
  description = "Billing account id for the budget alert (e.g. 0123AB-4567CD-89EF01). Empty to skip the budget."
}

variable "budget_usd" {
  type        = number
  default     = 1
  description = "Monthly budget that triggers alert e-mails, as an early warning that usage has left the free tier."
}

variable "raw_po_retention_days" {
  type    = number
  default = 90
}

variable "orchestrator_max_instances" {
  type        = number
  default     = 3
  description = "Upper bound on Cloud Run instances: caps cost and the blast radius of abuse."
}
