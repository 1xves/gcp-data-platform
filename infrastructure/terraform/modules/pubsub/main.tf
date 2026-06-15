###############################################################################
# Pub/Sub Module — Event Ingestion Layer
#
# Creates:
#   - Avro schema for event validation
#   - Primary events topic (with schema enforcement)
#   - Dead-letter topic + subscription
#   - Dataflow pull subscription with ordering + retry policy
#   - IAM bindings for Dataflow worker SA
###############################################################################

resource "google_pubsub_schema" "events_schema" {
  name       = "${var.resource_prefix}-events-schema"
  project    = var.project_id
  type       = "AVRO"
  definition = file("${path.module}/schemas/platform_event.avsc")
}

###############################################################################
# Dead-Letter Topic (created first so events-topic can reference it)
###############################################################################

resource "google_pubsub_topic" "events_dlq" {
  name    = "${var.resource_prefix}-events-dlq"
  project = var.project_id

  message_retention_duration = "${var.message_retention_sec}s"

  labels = {
    layer   = "ingestion"
    purpose = "dead-letter"
  }
}

resource "google_pubsub_subscription" "events_dlq_sub" {
  name    = "${var.resource_prefix}-events-dlq-sub"
  topic   = google_pubsub_topic.events_dlq.name
  project = var.project_id

  # Long retention — DLQ messages need investigation before replay
  message_retention_duration = "604800s" # 7 days

  # Manual ack — do NOT auto-delete DLQ messages
  ack_deadline_seconds = 600

  # Retain acked messages for potential snapshot/seek replay
  retain_acked_messages = true

  labels = {
    layer   = "ingestion"
    purpose = "dlq-consumer"
  }
}

###############################################################################
# Primary Events Topic
###############################################################################

resource "google_pubsub_topic" "events" {
  name    = "${var.resource_prefix}-events"
  project = var.project_id

  # Schema enforcement — reject messages that don't match Avro schema
  schema_settings {
    schema   = google_pubsub_schema.events_schema.id
    encoding = "BINARY" # Avro binary encoding (compact, fast)
  }

  message_retention_duration = "${var.message_retention_sec}s"

  labels = {
    layer   = "ingestion"
    purpose = "primary-events"
  }
}

###############################################################################
# Dataflow Pull Subscription
###############################################################################

resource "google_pubsub_subscription" "events_dataflow_sub" {
  name    = "${var.resource_prefix}-events-dataflow-sub"
  topic   = google_pubsub_topic.events.name
  project = var.project_id

  # Dataflow recommends 600s ack deadline for streaming jobs
  ack_deadline_seconds = 600

  # Retain for 7 days — enables snapshot-based replay during pipeline redeployment
  message_retention_duration = "${var.message_retention_sec}s"
  retain_acked_messages      = true

  # Exponential backoff retry before routing to DLQ
  retry_policy {
    minimum_backoff = "10s"
    maximum_backoff = "600s"
  }

  # Dead-letter routing after max_delivery_attempts
  dead_letter_policy {
    dead_letter_topic     = google_pubsub_topic.events_dlq.id
    max_delivery_attempts = var.max_delivery_attempts
  }

  # Ordering keys enable per-user ordered delivery
  enable_message_ordering = true

  # Exactly-once delivery (requires ordering to be enabled)
  enable_exactly_once_delivery = true

  labels = {
    layer    = "ingestion"
    consumer = "dataflow"
  }
}

###############################################################################
# IAM: Dataflow Worker SA — Subscribe + Acknowledge
###############################################################################

resource "google_pubsub_subscription_iam_member" "dataflow_subscriber" {
  project      = var.project_id
  subscription = google_pubsub_subscription.events_dataflow_sub.name
  role         = "roles/pubsub.subscriber"
  member       = "serviceAccount:${var.dataflow_sa_email}"
}

# Dataflow needs to publish to DLQ for routing
resource "google_pubsub_topic_iam_member" "dataflow_dlq_publisher" {
  project = var.project_id
  topic   = google_pubsub_topic.events_dlq.name
  role    = "roles/pubsub.publisher"
  member  = "serviceAccount:${var.dataflow_sa_email}"
}

# Pub/Sub service account needs DLQ topic publish rights for automatic DLQ routing
data "google_project" "current" {
  project_id = var.project_id
}

resource "google_pubsub_topic_iam_member" "pubsub_sa_dlq_publisher" {
  project = var.project_id
  topic   = google_pubsub_topic.events_dlq.name
  role    = "roles/pubsub.publisher"
  member  = "serviceAccount:service-${data.google_project.current.number}@gcp-sa-pubsub.iam.gserviceaccount.com"
}
