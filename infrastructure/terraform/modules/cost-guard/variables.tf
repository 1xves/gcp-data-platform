variable "project_id" {
  type        = string
  description = "GCP project id."
}

variable "region" {
  type        = string
  description = "Region for the function, scheduler, and Cloud Run target."
}

variable "resource_prefix" {
  type        = string
  description = "Prefix for named resources (e.g. \"stg\")."
}

variable "daily_limit_usd" {
  type        = number
  default     = 50
  description = "Daily net-spend threshold (USD). Teardown fires above this."
}

variable "billing_export_dataset" {
  type        = string
  default     = "billing_export"
  description = "BigQuery dataset that receives the Cloud Billing export."
}

variable "billing_account_id" {
  type        = string
  description = "Billing account id (XXXXXX-XXXXXX-XXXXXX) — used to derive the export table name."
}

variable "billing_export_table_type" {
  type        = string
  default     = "gcp_billing_export_v1" # "Standard usage cost" export
  description = "Export table prefix: gcp_billing_export_v1 (standard) or gcp_billing_export_resource_v1 (detailed)."
}

variable "predictor_service_name" {
  type        = string
  description = "Cloud Run predictor service to scale down on trip."
}

variable "predictor_runtime_sa" {
  type        = string
  description = "Runtime SA the predictor runs as — cost-guard needs actAs to patch the service."
}

variable "featurestore_id" {
  type        = string
  default     = ""
  description = "Vertex AI Feature Store id to drive online nodes to 0 (optional)."
}

variable "gke_cluster_name" {
  type        = string
  default     = ""
  description = "GKE cluster whose node pool is deleted on trip (optional). Empty = skip GKE."
}

variable "gke_node_pool_name" {
  type        = string
  default     = ""
  description = "GKE node pool to delete on trip — stops node compute; recreated via terraform."
}

variable "gke_location" {
  type        = string
  default     = ""
  description = "Location (region/zone) of the GKE cluster, for the container API resource path."
}

variable "schedule" {
  type        = string
  default     = "0 * * * *" # hourly, on the hour
  description = "Cron schedule for the spend check."
}

variable "time_zone" {
  type        = string
  default     = "America/Los_Angeles"
  description = "Time zone for the schedule and the 'today' spend window."
}

variable "dry_run" {
  type        = bool
  default     = false
  description = "If true, the guard logs teardown actions but does not execute them."
}

variable "labels" {
  type        = map(string)
  default     = {}
  description = "Common resource labels."
}
