output "network_self_link" {
  description = "Self-link of the VPC network. Pass to GKE and other modules that require the network reference."
  value       = google_compute_network.vpc.self_link
}

output "subnetwork_self_link" {
  description = "Self-link of the primary data subnet. Pass to GKE (node IPs) and Dataflow (worker IPs)."
  value       = google_compute_subnetwork.data_subnet.self_link
}

output "network_name" {
  description = "Name of the VPC network."
  value       = google_compute_network.vpc.name
}

output "pods_secondary_range_name" {
  description = "Name of the secondary IP range designated for GKE pod IPs (VPC-native alias IPs). Pass to module.gke.cluster_secondary_range_name."
  value = (
    length(var.secondary_ranges) > 0
    ? var.secondary_ranges[0].range_name
    : ""
  )
}

output "services_secondary_range_name" {
  description = "Name of the secondary IP range designated for GKE Service (ClusterIP) IPs. Pass to module.gke.services_secondary_range_name."
  value = (
    length(var.secondary_ranges) > 1
    ? var.secondary_ranges[1].range_name
    : ""
  )
}
