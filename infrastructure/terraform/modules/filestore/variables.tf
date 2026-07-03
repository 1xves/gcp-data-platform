variable "project_id" {
  description = "GCP project ID."
  type        = string
}

variable "zone" {
  description = "Zone for the Filestore instance (BASIC tiers are zonal). Use the same zone as GKE nodes (us-central1-a has confirmed capacity) to avoid cross-zone NFS latency."
  type        = string
  default     = "us-central1-a"
}

variable "resource_prefix" {
  description = "Prefix for resource names (e.g. stg, prod)."
  type        = string
}

variable "network_name" {
  description = "VPC network NAME to attach the instance to (module.networking.network_name — the Filestore API takes the name, not the self_link)."
  type        = string
}

variable "tier" {
  description = "Filestore service tier. BASIC_HDD is the cheapest shared-NFS tier and sufficient for read-mostly reference data. BASIC_SSD (2560 GB floor) only for latency-sensitive workloads."
  type        = string
  default     = "BASIC_HDD"

  validation {
    condition     = contains(["BASIC_HDD", "BASIC_SSD", "ZONAL", "ENTERPRISE"], var.tier)
    error_message = "tier must be one of BASIC_HDD, BASIC_SSD, ZONAL, ENTERPRISE."
  }
}

variable "capacity_gb" {
  description = "Provisioned capacity in GB. BASIC_HDD minimum (and cost floor) is 1024. You pay for provisioned capacity, not usage."
  type        = number
  default     = 1024

  validation {
    condition     = var.capacity_gb >= 1024
    error_message = "BASIC_HDD requires at least 1024 GB."
  }
}

variable "share_name" {
  description = "Name of the exported NFS file share (mounted as <ip>:/<share_name>). Alphanumeric, must start with a letter, max 16 chars."
  type        = string
  default     = "reference"
}

variable "labels" {
  description = "Labels applied to the instance."
  type        = map(string)
  default     = {}
}
