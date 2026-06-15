output "events_topic_id" {
  description = "Full resource ID of the primary events Pub/Sub topic"
  value       = google_pubsub_topic.events.id
}

output "events_subscription_id" {
  description = "Full resource ID of the Dataflow pull subscription"
  value       = google_pubsub_subscription.events_dataflow_sub.id
}

output "dlq_topic_id" {
  description = "Full resource ID of the dead-letter topic"
  value       = google_pubsub_topic.events_dlq.id
}

output "dlq_subscription_id" {
  description = "Full resource ID of the dead-letter pull subscription"
  value       = google_pubsub_subscription.events_dlq_sub.id
}
