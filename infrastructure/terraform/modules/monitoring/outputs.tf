###############################################################################
# Monitoring Module — Outputs
###############################################################################

output "notification_channel_id" {
  description = "Resource ID of the email notification channel (referenced by additional alert policies)."
  value       = google_monitoring_notification_channel.email.id
}

output "dashboard_name" {
  description = "Resource name of the platform overview dashboard."
  value       = google_monitoring_dashboard.platform_overview.id
}

output "dlq_spike_alert_name" {
  description = "Resource name of the DLQ spike alert policy."
  value       = google_monitoring_alert_policy.dlq_spike.name
}

output "pipeline_lag_alert_name" {
  description = "Resource name of the Dataflow pipeline lag alert policy."
  value       = google_monitoring_alert_policy.pipeline_lag.name
}

output "serving_latency_alert_name" {
  description = "Resource name of the Vertex AI serving latency alert policy (empty until model is deployed)."
  value       = length(google_monitoring_alert_policy.serving_latency) > 0 ? google_monitoring_alert_policy.serving_latency[0].name : ""
}
