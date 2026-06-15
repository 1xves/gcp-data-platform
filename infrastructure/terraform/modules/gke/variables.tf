###############################################################################
# GKE Module — Input Variables
###############################################################################

variable "project_id" {
  description = "GCP project ID where the GKE cluster is deployed."
  type        = string

  validation {
    condition     = length(var.project_id) > 0
    error_message = "project_id must not be empty."
  }
}

variable "region" {
  description = "GCP region for the cluster. The cluster is regional (multi-zone) for HA."
  type        = string
  default     = "us-central1"

  validation {
    condition     = can(regex("^[a-z]+-[a-z]+[0-9]$", var.region))
    error_message = "region must be a valid GCP region (e.g., us-central1)."
  }
}

variable "environment" {
  description = "Deployment environment. Controls resource labels and some behaviour (e.g., deletion_protection)."
  type        = string

  validation {
    condition     = contains(["staging", "production"], var.environment)
    error_message = "environment must be one of: staging, production."
  }
}

variable "resource_prefix" {
  description = "Short prefix prepended to all resource names for environment disambiguation (e.g., 'platform-prod', 'platform-stg')."
  type        = string

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,18}[a-z0-9]$", var.resource_prefix))
    error_message = "resource_prefix must be 3–20 lowercase alphanumeric characters or hyphens, and must start/end with a letter or digit."
  }
}

variable "network_self_link" {
  description = "Self-link of the VPC network the GKE cluster is attached to. Sourced from module.networking.network_self_link."
  type        = string
}

variable "subnetwork_self_link" {
  description = "Self-link of the subnetwork for GKE node IPs. Sourced from module.networking.subnetwork_self_link."
  type        = string
}

variable "cluster_secondary_range_name" {
  description = "Name of the secondary IP range on the subnetwork reserved for GKE pod IPs (VPC-native alias IPs)."
  type        = string
}

variable "services_secondary_range_name" {
  description = "Name of the secondary IP range on the subnetwork reserved for GKE Service (ClusterIP) IPs."
  type        = string
}

variable "authorized_cidr_blocks" {
  description = "List of CIDR blocks permitted to reach the GKE master API endpoint. Each entry must have a cidr_block and a human-readable display_name. Add your Cloud Build / CI egress IPs and any bastion/VPN IPs here."
  type = list(object({
    cidr_block   = string
    display_name = string
  }))
  default = []
}

variable "machine_type" {
  description = "Compute Engine machine type for GKE worker nodes in the general node pool."
  type        = string
  default     = "e2-standard-4"
}

variable "initial_node_count" {
  description = "Number of nodes to create in each zone when the node pool is first provisioned. The cluster autoscaler takes over immediately after."
  type        = number
  default     = 1

  validation {
    condition     = var.initial_node_count >= 1
    error_message = "initial_node_count must be at least 1."
  }
}

variable "node_locations" {
  description = "Explicit list of zones within the region where nodes are created. Omit to let GKE choose all zones (default). Set to exclude zones with capacity issues (GCE_STOCKOUT)."
  type        = list(string)
  default     = []
}

variable "min_node_count" {
  description = "Minimum number of nodes per zone that the cluster autoscaler will maintain."
  type        = number
  default     = 1

  validation {
    condition     = var.min_node_count >= 1
    error_message = "min_node_count must be at least 1 to ensure the pool is never empty."
  }
}

variable "max_node_count" {
  description = "Maximum number of nodes per zone the cluster autoscaler may scale up to."
  type        = number
  default     = 5

  validation {
    condition     = var.max_node_count >= 1
    error_message = "max_node_count must be at least 1."
  }
}
