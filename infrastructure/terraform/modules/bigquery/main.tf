###############################################################################
# BigQuery Module — Analytics Data Layer
#
# Three-layer architecture: raw (bronze) → processed (silver) → ml_features (gold)
# Each layer has strict partitioning, clustering, and IAM controls.
###############################################################################

###############################################################################
# Slot Reservation (cost cap — prevents runaway queries)
###############################################################################

resource "google_bigquery_reservation" "platform_reservation" {
  provider = google-beta
  name     = "${var.resource_prefix}-reservation"
  project  = var.project_id
  location = var.region

  # STANDARD edition uses autoscale — baseline slot_capacity must be 0.
  # Autoscale scales between 0 and max_slots on demand; you pay per slot-hour used.
  # STANDARD edition: baseline must be 0, idle slot sharing must be disabled.
  slot_capacity     = 0
  edition           = "STANDARD"
  ignore_idle_slots = true

  autoscale {
    max_slots = var.slot_capacity # e.g. 100 for staging — hard cap on concurrent slots
  }
}

resource "google_bigquery_reservation_assignment" "platform_assignment" {
  provider    = google-beta
  project     = var.project_id
  location    = var.region
  reservation = google_bigquery_reservation.platform_reservation.id
  assignee    = "projects/${var.project_id}"
  job_type    = "QUERY"
}

###############################################################################
# Datasets
###############################################################################

resource "google_bigquery_dataset" "raw" {
  dataset_id                 = "${replace(var.resource_prefix, "-", "_")}_raw"
  location                   = var.region
  project                    = var.project_id
  description                = "Bronze layer: append-only raw events directly from Dataflow. Immutable."
  delete_contents_on_destroy = false

  # Default 90-day table expiration (overridden per-table where needed)
  default_table_expiration_ms = var.raw_retention_days * 24 * 60 * 60 * 1000

  access {
    role          = "WRITER"
    user_by_email = var.dataflow_sa_email
  }
  access {
    role          = "READER"
    user_by_email = var.pipeline_sa_email
  }
  # Owners — data engineering team
  access {
    role          = "OWNER"
    special_group = "projectOwners"
  }

  labels = {
    layer = "raw"
    tier  = "bronze"
  }
}

resource "google_bigquery_dataset" "processed" {
  dataset_id                 = "${replace(var.resource_prefix, "-", "_")}_processed"
  location                   = var.region
  project                    = var.project_id
  description                = "Silver layer: deduplicated, enriched, analytics-ready events."
  delete_contents_on_destroy = false

  access {
    role          = "WRITER"
    user_by_email = var.dataflow_sa_email
  }
  access {
    role          = "READER"
    user_by_email = var.pipeline_sa_email
  }
  access {
    role          = "READER"
    user_by_email = var.serving_sa_email
  }
  access {
    role          = "OWNER"
    special_group = "projectOwners"
  }

  labels = {
    layer = "processed"
    tier  = "silver"
  }
}

resource "google_bigquery_dataset" "ml_features" {
  dataset_id                 = "${replace(var.resource_prefix, "-", "_")}_ml_features"
  location                   = var.region
  project                    = var.project_id
  description                = "Gold layer: feature snapshots for ML training and monitoring."
  delete_contents_on_destroy = false

  access {
    role          = "WRITER"
    user_by_email = var.pipeline_sa_email
  }
  access {
    role          = "READER"
    user_by_email = var.serving_sa_email
  }
  access {
    role          = "OWNER"
    special_group = "projectOwners"
  }

  labels = {
    layer = "ml"
    tier  = "gold"
  }
}

###############################################################################
# Table: raw_events (bronze)
###############################################################################

resource "google_bigquery_table" "raw_events" {
  dataset_id          = google_bigquery_dataset.raw.dataset_id
  table_id            = "raw_events"
  project             = var.project_id
  description         = "Append-only raw events from Pub/Sub via Dataflow. Partitioned by day, clustered by event_type + user_id."
  deletion_protection = true

  # Partition by event_date (derived from timestamp_ms in Dataflow)
  require_partition_filter = true # HARD REQUIREMENT — no full-table scans allowed

  time_partitioning {
    type          = "DAY"
    field         = "event_date"
    expiration_ms = var.raw_retention_days * 24 * 60 * 60 * 1000
  }

  # Cluster within partitions: event_type first (enum = low cardinality), then user_id
  clustering = ["event_type", "user_id"]

  schema = file("${path.module}/schemas/raw_events.json")

  labels = {
    table_type  = "raw"
    partitioned = "true"
    clustered   = "true"
  }
}

###############################################################################
# Table: processed_events (silver)
###############################################################################

resource "google_bigquery_table" "processed_events" {
  dataset_id          = google_bigquery_dataset.processed.dataset_id
  table_id            = "processed_events"
  project             = var.project_id
  description         = "Deduplicated, enriched events. Primary analytics table."
  deletion_protection = true

  require_partition_filter = true

  time_partitioning {
    type  = "DAY"
    field = "event_date"
  }

  clustering = ["user_id", "event_type", "country"]

  schema = file("${path.module}/schemas/processed_events.json")

  labels = {
    table_type  = "processed"
    partitioned = "true"
    clustered   = "true"
  }
}

###############################################################################
# Table: event_aggregates (silver — windowed aggregations from Dataflow)
###############################################################################

resource "google_bigquery_table" "event_aggregates" {
  dataset_id = google_bigquery_dataset.processed.dataset_id
  table_id   = "event_aggregates"
  project    = var.project_id
  description = "1-minute tumbling window aggregations per user per event_type."

  require_partition_filter = true

  time_partitioning {
    type  = "DAY"
    field = "window_date"
  }

  clustering = ["user_id", "event_type"]

  schema = file("${path.module}/schemas/event_aggregates.json")
}

###############################################################################
# Table: prediction_logs (gold — model serving output)
###############################################################################

resource "google_bigquery_table" "prediction_logs" {
  dataset_id  = google_bigquery_dataset.ml_features.dataset_id
  table_id    = "prediction_logs"
  project     = var.project_id
  description = "Online model prediction logs for drift detection and retraining triggers."

  require_partition_filter = false # Monitoring queries need flexibility

  time_partitioning {
    type  = "DAY"
    field = "prediction_date"
  }

  clustering = ["model_version", "user_id"]

  schema = file("${path.module}/schemas/prediction_logs.json")
}

###############################################################################
# Authorized Views — Row-Level Security for Analysts
###############################################################################

# Analysts get a view that excludes raw PII and enforces a 90-day lookback
resource "google_bigquery_table" "processed_events_analyst_view" {
  dataset_id = google_bigquery_dataset.processed.dataset_id
  table_id   = "processed_events_analyst_view"
  project    = var.project_id
  description = "Authorized view for analysts: PII excluded, last 90 days only."
  deletion_protection = false

  view {
    query = <<-SQL
      SELECT
        event_id,
        event_type,
        session_id,
        event_timestamp,
        event_date,
        -- Pseudonymized user_id: first 8 chars of SHA256 (not reversible)
        SUBSTR(TO_HEX(SHA256(user_id)), 1, 16) AS user_id_pseudonymized,
        country,
        plan_tier,
        page_url,
        referrer,
        value_usd,
        producer_id,
        schema_version
      FROM `${var.project_id}.${google_bigquery_dataset.processed.dataset_id}.processed_events`
      WHERE event_date >= DATE_SUB(CURRENT_DATE(), INTERVAL 90 DAY)
    SQL
    use_legacy_sql = false
  }
}
