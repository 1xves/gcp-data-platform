###############################################################################
# GKE Module — Outputs
###############################################################################

output "cluster_name" {
  description = "Name of the GKE cluster. Used to construct kubectl context names and in CI kubeconfig steps."
  value       = google_container_cluster.primary.name
}

output "cluster_endpoint" {
  description = "Hostname/IP of the GKE master API endpoint. Required by the kubernetes and helm Terraform providers and by kubeconfig generation in CI."
  value       = google_container_cluster.primary.endpoint
  sensitive   = true
}

output "cluster_ca_certificate" {
  description = "Base64-encoded PEM certificate of the cluster CA. Required for authenticating API server TLS certificates when constructing kubeconfigs."
  value       = google_container_cluster.primary.master_auth[0].cluster_ca_certificate
  sensitive   = true
}

output "node_pool_name" {
  description = "Name of the general-purpose node pool. Useful for node affinity rules and for targeted operations (e.g., gcloud container node-pools upgrade)."
  value       = google_container_node_pool.general.name
}

output "predictor_service_account_email" {
  description = "Email address of the GCP Service Account used by the predictor workload via Workload Identity. Annotate the Kubernetes ServiceAccount with: iam.gke.io/gcp-service-account=<this value>."
  value       = google_service_account.predictor.email
}

output "node_service_account_email" {
  description = "Email address of the GCP Service Account attached to GKE worker nodes. Used to scope firewall rules and for audit trail filtering."
  value       = google_service_account.gke_node_sa.email
}
