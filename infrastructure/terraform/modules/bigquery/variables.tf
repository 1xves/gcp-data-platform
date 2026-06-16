variable "project_id" { type = string }
variable "region" { type = string }
variable "resource_prefix" { type = string }
variable "dataflow_sa_email" { type = string }
variable "pipeline_sa_email" { type = string }
variable "serving_sa_email" { type = string }
variable "raw_retention_days" { type = number }
variable "slot_capacity" { type = number }
variable "bridge_sa_email" {
  description = "OSINT bridge SA email — granted WRITER access to ml_features dataset for enriched_interventions inserts."
  type        = string
  default     = ""
}
