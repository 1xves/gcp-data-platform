output "function_uri" {
  description = "HTTPS URL of the cost-guard function (auth required)."
  value       = google_cloudfunctions2_function.guard.service_config[0].uri
}

output "function_name" {
  value = google_cloudfunctions2_function.guard.name
}

output "service_account_email" {
  description = "Identity the guard runs as and that Scheduler authenticates with."
  value       = google_service_account.cost_guard.email
}

output "billing_export_dataset" {
  description = "Point the Console billing export at this dataset."
  value       = google_bigquery_dataset.billing_export.dataset_id
}

output "billing_export_table" {
  description = "Table the guard queries once the export is enabled and populated."
  value       = local.billing_export_table
}

output "scheduler_job" {
  value = google_cloud_scheduler_job.hourly.name
}
