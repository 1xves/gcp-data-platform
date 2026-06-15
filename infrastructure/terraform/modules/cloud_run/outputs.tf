###############################################################################
# Cloud Run Module — Outputs
###############################################################################

output "service_url" {
  description = "Public HTTPS URL of the Cloud Run predictor service. Share this in your portfolio."
  value       = google_cloud_run_v2_service.predictor.uri
}

output "service_name" {
  description = "Cloud Run service name (used by GitHub Actions CD to target the right service)"
  value       = google_cloud_run_v2_service.predictor.name
}

output "service_id" {
  description = "Full Cloud Run service resource ID"
  value       = google_cloud_run_v2_service.predictor.id
}

output "latest_ready_revision" {
  description = "Name of the latest ready revision — useful for traffic migration and rollbacks"
  value       = google_cloud_run_v2_service.predictor.latest_ready_revision
}
