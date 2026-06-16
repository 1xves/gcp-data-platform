output "dataflow_worker_sa_email" {
  value = google_service_account.dataflow_worker.email
}
output "pipeline_runner_sa_email" {
  value = google_service_account.pipeline_runner.email
}
output "vertex_training_sa_email" {
  value = google_service_account.vertex_training.email
}
output "vertex_serving_sa_email" {
  value = google_service_account.vertex_serving.email
}
output "bridge_sa_email" {
  value = google_service_account.bridge.email
}
