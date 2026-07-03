variable "project_id" { type = string }
variable "region" { type = string }
variable "resource_prefix" { type = string }
variable "network_self_link" { type = string }
variable "subnetwork_self_link" { type = string }
variable "dataflow_sa_email" { type = string }
variable "staging_bucket" { type = string }
variable "input_subscription" { type = string }
variable "dlq_topic" { type = string }
variable "raw_events_table" { type = string }
variable "aggregates_table" { type = string }
variable "max_workers" { type = number }
variable "machine_type" { type = string }

variable "create_dataflow_job" {
  description = <<-EOT
    Set to true only after the Flex Template container image has been built,
    pushed to Artifact Registry, and the template.json registered in GCS.
    On first deploy, leave false so infrastructure can be provisioned without
    the template existing yet. Set to true on subsequent applies to start the job.
  EOT
  type        = bool
  default     = false
}

variable "on_delete" {
  description = <<-EOT
    Behaviour when Terraform destroys or replaces the Dataflow job.
    "cancel" — immediately terminates the job (fast; may drop in-flight messages).
               Use in staging where speed matters more than message safety.
    "drain"  — sends a drain signal and waits for all in-flight messages to finish
               before the job stops (can take hours on a streaming job).
               Use in production to guarantee exactly-once delivery.
  EOT
  type        = string
  default     = "drain"

  validation {
    condition     = contains(["cancel", "drain"], var.on_delete)
    error_message = "on_delete must be \"cancel\" or \"drain\"."
  }
}
