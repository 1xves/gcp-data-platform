###############################################################################
# Root Terraform Outputs
# These expose the most operationally useful values: endpoints, SA emails,
# and identifiers needed by external systems (CI, Helm, ArgoCD).
###############################################################################

# ── Networking ────────────────────────────────────────────────────────────────

output "network_name" {
  description = "Name of the VPC network."
  value       = module.networking.network_name
}

output "network_self_link" {
  description = "Self-link of the VPC network."
  value       = module.networking.network_self_link
}

# ── GKE ───────────────────────────────────────────────────────────────────────

output "gke_cluster_name" {
  description = "Name of the GKE cluster. Use with: gcloud container clusters get-credentials <name> --region <region> --project <project>"
  value       = var.enable_gke ? module.gke[0].cluster_name : null
}

output "gke_cluster_endpoint" {
  description = "GKE cluster API server endpoint. Marked sensitive — do not log."
  value       = var.enable_gke ? module.gke[0].cluster_endpoint : null
  sensitive   = true
}

output "predictor_service_account_email" {
  description = "GCP Service Account email for the predictor Workload Identity. Annotate the Kubernetes ServiceAccount with: iam.gke.io/gcp-service-account=<this value>."
  value       = var.enable_gke ? module.gke[0].predictor_service_account_email : null
}

output "gke_node_service_account_email" {
  description = "GCP Service Account email attached to GKE worker nodes."
  value       = var.enable_gke ? module.gke[0].node_service_account_email : null
}

# ── Cloud Run ─────────────────────────────────────────────────────────────────

output "predictor_url" {
  description = "Public HTTPS URL of the Cloud Run predictor service. Use this in your portfolio and for smoke tests."
  value       = module.cloud_run.service_url
}

output "predictor_service_name" {
  description = "Cloud Run service name — used by GitHub Actions CD: gcloud run deploy <this value>"
  value       = module.cloud_run.service_name
}

# ── IAM / Service Accounts ────────────────────────────────────────────────────

output "dataflow_worker_sa_email" {
  description = "Service Account email for Dataflow workers."
  value       = module.iam.dataflow_worker_sa_email
}

output "vertex_serving_sa_email" {
  description = "Service Account email for Vertex AI online serving."
  value       = module.iam.vertex_serving_sa_email
}
