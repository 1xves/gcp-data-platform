###############################################################################
# Monitoring Module — Input Variables
###############################################################################

variable "project_id" {
  description = "GCP project ID where monitoring resources will be created."
  type        = string
}

variable "resource_prefix" {
  description = "Prefix applied to all resource display names (e.g. 'myco-prod')."
  type        = string
}

variable "notification_email" {
  description = "Email address that receives all alert notifications."
  type        = string

  validation {
    condition     = can(regex("^[^@]+@[^@]+\\.[^@]+$", var.notification_email))
    error_message = "notification_email must be a valid email address."
  }
}

variable "dlq_topic_id" {
  description = "Full resource ID of the dead-letter Pub/Sub topic (projects/.../topics/...)."
  type        = string
}

variable "dataflow_job_name" {
  description = "Dataflow job name prefix used to scope lag alerts (regex-matched)."
  type        = string
}
