variable "project_id" { type = string }
variable "region" { type = string }
variable "resource_prefix" { type = string }
variable "dataflow_sa_email" { type = string }
variable "pipeline_sa_email" { type = string }
variable "serving_sa_email" { type = string }
variable "raw_retention_days" { type = number }
variable "slot_capacity" { type = number }
