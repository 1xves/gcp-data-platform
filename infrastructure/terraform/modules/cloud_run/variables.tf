###############################################################################
# Cloud Run Module — Input Variables
###############################################################################

variable "project_id" {
  description = "GCP project ID"
  type        = string
}

variable "region" {
  description = "GCP region for the Cloud Run service"
  type        = string
}

variable "resource_prefix" {
  description = "Short prefix applied to all resource names (e.g., stg, prod)"
  type        = string
}

variable "image" {
  description = <<-EOT
    Full Docker image URI including tag.
    Example: us-central1-docker.pkg.dev/my-project/stg-ml-containers/predictor:latest
    The image must exist in Artifact Registry before apply.
    On first deploy, build and push the image first, then set this variable.
  EOT
  type    = string
  default = ""
}

variable "service_account_email" {
  description = "Service account email that the Cloud Run service runs as. Must have Vertex AI, BigQuery, and GCS permissions."
  type        = string
}

variable "gcp_project" {
  description = "GCP project ID passed to the predictor as an env var (GOOGLE_CLOUD_PROJECT)"
  type        = string
}

variable "gcp_region" {
  description = "GCP region passed to the predictor as an env var (GCP_REGION)"
  type        = string
}

variable "feature_store_id" {
  description = "Vertex AI Feature Store resource ID"
  type        = string
}

variable "entity_type_id" {
  description = "Vertex AI Feature Store entity type ID"
  type        = string
  default     = "user"
}

variable "model_artifacts_gcs" {
  description = "GCS URI of the model artifacts directory (e.g., gs://my-bucket/models/staging/seed)"
  type        = string
}

variable "model_version" {
  description = "Model version string passed to the predictor"
  type        = string
  default     = "staging-seed-v1"
}

variable "prediction_logs_table" {
  description = "BigQuery table for prediction logs (project.dataset.table format)"
  type        = string
}

variable "vertex_endpoint_id" {
  description = "Vertex AI online endpoint ID. Leave empty if online endpoint is not enabled."
  type        = string
  default     = ""
}

variable "min_instance_count" {
  description = <<-EOT
    Minimum number of Cloud Run instances to keep warm.
    0 = scale to zero (cold starts ~10-15s due to model download from GCS).
    1 = always-warm, costs ~$1.50/month at idle — recommended during active demos.
  EOT
  type    = number
  default = 0

  validation {
    condition     = var.min_instance_count >= 0 && var.min_instance_count <= 5
    error_message = "min_instance_count must be between 0 and 5."
  }
}

variable "max_instance_count" {
  description = "Maximum number of Cloud Run instances. Caps burst scaling cost."
  type        = number
  default     = 3

  validation {
    condition     = var.max_instance_count >= 1 && var.max_instance_count <= 10
    error_message = "max_instance_count must be between 1 and 10."
  }
}

variable "cpu" {
  description = "vCPU allocation per instance. 1 is sufficient for the XGBoost predictor."
  type        = string
  default     = "1"
}

variable "memory" {
  description = "Memory allocation per instance. XGBoost model + Flask needs at least 512Mi."
  type        = string
  default     = "512Mi"
}

variable "request_timeout_seconds" {
  description = "Maximum seconds a request can take before Cloud Run returns a 504."
  type        = number
  default     = 30
}

variable "allow_unauthenticated" {
  description = <<-EOT
    Allow unauthenticated (public) invocations.
    true  = public URL, no auth required — convenient for demos and portfolio demos.
    false = requires a valid Google identity token — use for production or sensitive data.
  EOT
  type    = bool
  default = true
}

variable "log_level" {
  description = "Log level passed to the predictor service (DEBUG, INFO, WARNING, ERROR)"
  type        = string
  default     = "INFO"
}

variable "common_labels" {
  description = "Labels to apply to the Cloud Run service"
  type        = map(string)
  default     = {}
}

variable "churn_high_risk_topic" {
  description = <<-EOT
    Full Pub/Sub topic resource ID for high-risk churn events.
    Format: projects/{PROJECT_ID}/topics/{TOPIC_NAME}
    When set, the predictor publishes a churn.high_risk event for every prediction
    that crosses the CHURN_HIGH_RISK_THRESHOLD (default 0.7).
    Leave empty (default) to disable OSINT bridge publishing.
  EOT
  type    = string
  default = ""
}
