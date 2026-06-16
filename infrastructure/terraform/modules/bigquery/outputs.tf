output "raw_events_table_id" {
  value = "${var.project_id}:${google_bigquery_dataset.raw.dataset_id}.${google_bigquery_table.raw_events.table_id}"
}
output "processed_events_table_id" {
  value = "${var.project_id}:${google_bigquery_dataset.processed.dataset_id}.${google_bigquery_table.processed_events.table_id}"
}
output "event_aggregates_table_id" {
  value = "${var.project_id}:${google_bigquery_dataset.processed.dataset_id}.${google_bigquery_table.event_aggregates.table_id}"
}
output "prediction_logs_table_id" {
  value = "${var.project_id}:${google_bigquery_dataset.ml_features.dataset_id}.${google_bigquery_table.prediction_logs.table_id}"
}
output "processed_dataset_id" {
  value = google_bigquery_dataset.processed.dataset_id
}
output "ml_features_dataset_id" {
  value = google_bigquery_dataset.ml_features.dataset_id
}
output "enriched_interventions_table_id" {
  value = "${var.project_id}:${google_bigquery_dataset.ml_features.dataset_id}.${google_bigquery_table.enriched_interventions.table_id}"
}
