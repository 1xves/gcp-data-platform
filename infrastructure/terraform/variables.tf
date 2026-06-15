###############################################################################
# Input Variables
###############################################################################

variable "project_id" {
  description = "GCP project ID where all resources are deployed"
  type        = string

  validation {
    condition     = length(var.project_id) > 0
    error_message = "project_id must not be empty"
  }
}

variable "region" {
  description = "Primary GCP region for resource deployment"
  type        = string
  default     = "us-central1"

  validation {
    condition     = can(regex("^[a-z]+-[a-z]+[0-9]$", var.region))
    error_message = "region must be a valid GCP region (e.g., us-central1)"
  }
}

variable "environment" {
  description = "Deployment environment. Applied as a resource label and consumed by the GKE module to set behaviour (e.g., deletion_protection)."
  type        = string
  default     = "production"

  validation {
    condition     = contains(["staging", "production"], var.environment)
    error_message = "environment must be one of: staging, production."
  }
}

variable "resource_prefix" {
  description = "Short prefix applied to all resource names for environment disambiguation (e.g., prod, staging)"
  type        = string
  default     = "platform"
}

variable "common_labels" {
  description = "Labels applied to all GCP resources for cost allocation and governance"
  type        = map(string)
  default = {
    team        = "data-engineering"
    managed_by  = "terraform"
    environment = "production"
  }
}

# ── Networking ──────────────────────────────────────────────────────────────

variable "subnet_cidr" {
  description = "CIDR block for the primary subnet (Dataflow workers use this)"
  type        = string
  default     = "10.0.0.0/24"
}

variable "secondary_ranges" {
  description = "Secondary IP ranges for the subnet (used if GKE is added later)"
  type = list(object({
    range_name    = string
    ip_cidr_range = string
  }))
  default = []
}

# ── Pub/Sub ──────────────────────────────────────────────────────────────────

variable "pubsub_message_retention_sec" {
  description = "How long Pub/Sub retains unacked messages (seconds). Max 604800 = 7 days"
  type        = number
  default     = 604800 # 7 days

  validation {
    condition     = var.pubsub_message_retention_sec <= 604800
    error_message = "Pub/Sub maximum retention is 7 days (604800 seconds)"
  }
}

variable "pubsub_max_delivery_attempts" {
  description = "Number of delivery attempts before routing to dead-letter topic"
  type        = number
  default     = 5

  validation {
    condition     = var.pubsub_max_delivery_attempts >= 5 && var.pubsub_max_delivery_attempts <= 100
    error_message = "max_delivery_attempts must be between 5 and 100"
  }
}

# ── Dataflow ──────────────────────────────────────────────────────────────────

variable "dataflow_max_workers" {
  description = "Maximum number of Dataflow workers during auto-scaling"
  type        = number
  default     = 100
}

variable "dataflow_machine_type" {
  description = "Compute Engine machine type for Dataflow workers"
  type        = string
  default     = "n1-standard-4"
}

# ── BigQuery ──────────────────────────────────────────────────────────────────

variable "raw_events_retention_days" {
  description = "Days before raw_events table partitions are automatically deleted"
  type        = number
  default     = 90
}

variable "bigquery_slot_capacity" {
  description = "BigQuery slot reservation capacity (baseline slots purchased)"
  type        = number
  default     = 500
}

# ── Vertex AI ──────────────────────────────────────────────────────────────────

variable "vertex_endpoint_min_replicas" {
  description = "Minimum number of online endpoint replicas (for HA)"
  type        = number
  default     = 2
}

variable "vertex_endpoint_max_replicas" {
  description = "Maximum number of online endpoint replicas (for burst traffic)"
  type        = number
  default     = 10
}

variable "featurestore_online_node_count" {
  description = "Online serving nodes for the Feature Store. 0 = offline-only (no node-hour billing)."
  type        = number
  default     = 0
  validation {
    condition     = var.featurestore_online_node_count >= 0 && var.featurestore_online_node_count <= 10
    error_message = "featurestore_online_node_count must be between 0 and 10."
  }
}

variable "enable_online_endpoint" {
  description = "Create the Vertex AI online prediction endpoint. Disable in staging to avoid an idle endpoint resource."
  type        = bool
  default     = false
}

variable "enable_daily_retraining" {
  description = "Create the Cloud Scheduler job that triggers the daily retraining pipeline. Disable in staging to avoid nightly pipeline runs."
  type        = bool
  default     = false
}

# ── Dataflow job gate ────────────────────────────────────────────────────────────

variable "create_dataflow_job" {
  description = <<-EOT
    Controls whether Terraform provisions the live Dataflow Flex Template job.
    Set to false on first deploy (before the container image exists).
    After running `make pipeline-deploy`, set to true and re-apply.
  EOT
  type    = bool
  default = false
}

variable "dataflow_on_delete" {
  description = <<-EOT
    Behaviour when Terraform destroys or replaces the Dataflow streaming job.
    "cancel" — immediately stops the job. Use in staging (fast teardown, no message SLA).
    "drain"  — waits for in-flight messages to finish. Use in production (can take hours).
  EOT
  type    = string
  default = "drain"

  validation {
    condition     = contains(["cancel", "drain"], var.dataflow_on_delete)
    error_message = "dataflow_on_delete must be \"cancel\" or \"drain\"."
  }
}

# ── Monitoring ──────────────────────────────────────────────────────────────────

variable "alert_notification_email" {
  description = "Email address for Cloud Monitoring alert notifications"
  type        = string
  sensitive   = true
}

# ── Cloud Run (Predictor Serving) ────────────────────────────────────────────────

variable "predictor_image" {
  description = <<-EOT
    Full Docker image URI for the predictor service.
    Example: us-central1-docker.pkg.dev/my-project/stg-ml-containers/predictor:abc1234
    Updated automatically by GitHub Actions CD on every merge to main.
    Leave empty on first apply — a placeholder hello-world image will be used.
  EOT
  type    = string
  default = ""
}

variable "predictor_model_version" {
  description = "Model version string passed to the predictor as MODEL_VERSION env var."
  type        = string
  default     = "staging-seed-v1"
}

variable "predictor_min_instances" {
  description = <<-EOT
    Minimum warm Cloud Run instances.
    0 = scale to zero, $0/month at idle (cold start ~15s due to GCS model download).
    1 = always warm, ~$1.50/month — set this before a live demo to avoid cold start.
  EOT
  type    = number
  default = 0
}

variable "predictor_max_instances" {
  description = "Maximum Cloud Run instances. Prevents runaway scaling cost."
  type        = number
  default     = 3
}

variable "predictor_allow_unauthenticated" {
  description = "Allow public (unauthenticated) access to the predictor URL. true for portfolio demos."
  type        = bool
  default     = true
}

# ── Cost Guard (daily-spend kill-switch) ─────────────────────────────────────────
variable "cost_guard_daily_limit_usd" {
  description = "Daily net-spend threshold (USD). cost-guard tears down workloads above this."
  type        = number
  default     = 50
}

variable "cost_guard_dry_run" {
  description = "If true, cost-guard logs teardown actions but does not execute them. Set true to validate before arming."
  type        = bool
  default     = false
}

# ── Billing Budgets ──────────────────────────────────────────────────────────────

variable "billing_account_id" {
  description = <<-EOT
    GCP Billing Account ID that this project is attached to.
    Format: XXXXXX-XXXXXX-XXXXXX (find in GCP Console → Billing → Account overview).
    Required to create google_billing_budget resources. The Terraform service account
    must have roles/billing.costsManager on the billing account.
  EOT
  type      = string
  sensitive = true

  validation {
    condition     = can(regex("^[0-9A-F]{6}-[0-9A-F]{6}-[0-9A-F]{6}$", var.billing_account_id))
    error_message = "billing_account_id must be in the format XXXXXX-XXXXXX-XXXXXX (hex segments)."
  }
}

variable "budget_monthly_limit_usd" {
  description = <<-EOT
    Monthly spend cap in USD for this project. Alerts fire at 25%, 50%, 100% of this amount
    and at 100% of the forecasted spend.
    Recommended: 50 for staging, 500 for production.
    The budget is informational only — no automatic disablement occurs unless
    you wire a Cloud Function to the Pub/Sub notification channel.
  EOT
  type    = number
  default = 50

  validation {
    condition     = var.budget_monthly_limit_usd > 0
    error_message = "budget_monthly_limit_usd must be a positive number."
  }
}

# ── GKE ──────────────────────────────────────────────────────────────────────

variable "gke_authorized_cidr_blocks" {
  description = "CIDR blocks authorized to reach the GKE master API endpoint. Add Cloud Build egress IPs, bastion IPs, and VPN exit IPs here."
  type = list(object({
    cidr_block   = string
    display_name = string
  }))
  default = []
}

variable "gke_machine_type" {
  description = "GCE machine type for GKE worker nodes in the general node pool."
  type        = string
  default     = "e2-standard-4"
}

variable "gke_min_node_count" {
  description = "Minimum number of nodes per zone in the general node pool. The cluster autoscaler will not scale below this value."
  type        = number
  default     = 1
}

variable "gke_max_node_count" {
  description = "Maximum number of nodes per zone in the general node pool. The cluster autoscaler will not scale above this value."
  type        = number
  default     = 5
}

variable "gke_node_locations" {
  description = "Explicit list of zones for GKE nodes. Use to avoid zones with GCE_STOCKOUT capacity issues. Empty list lets GKE use all zones in the region."
  type        = list(string)
  default     = []
}

variable "enable_gke" {
  description = <<-EOT
    Provision the GKE cluster, node pool, and Workload Identity resources.
    Set to false (default) when only validating the data pipeline (Pub/Sub → Dataflow → BigQuery).
    Set to true when ready to test the predictor serving layer (Helm chart, ArgoCD).
    Costs ~$170-200/month when enabled; $0 when disabled.
  EOT
  type    = bool
  default = false
}
