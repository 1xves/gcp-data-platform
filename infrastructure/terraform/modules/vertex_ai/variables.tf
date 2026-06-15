variable "project_id" { type = string }
variable "region" { type = string }
variable "resource_prefix" { type = string }
variable "ml_artifacts_bucket" { type = string }
variable "training_sa_email" { type = string }
variable "serving_sa_email" { type = string }
variable "network_self_link" { type = string }
variable "prediction_logs_table" { type = string }

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
  description = "Create the Cloud Scheduler job that triggers the daily retraining pipeline. Disable in staging to avoid nightly pipeline runs (and the pipeline compute they incur)."
  type        = bool
  default     = false
}
