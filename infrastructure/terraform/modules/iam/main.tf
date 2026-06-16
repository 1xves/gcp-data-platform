###############################################################################
# IAM Module — Least-Privilege Service Accounts
#
# Principle: each component gets exactly the permissions it needs.
# No cross-component permission inflation. No editor/owner roles.
###############################################################################

###############################################################################
# Dataflow Worker Service Account
# Reads from Pub/Sub, writes to BigQuery + GCS
###############################################################################

resource "google_service_account" "dataflow_worker" {
  account_id   = "${var.resource_prefix}-dataflow-worker"
  display_name = "Dataflow Worker SA — Event Processing Pipeline"
  project      = var.project_id
}

resource "google_project_iam_member" "dataflow_worker_roles" {
  for_each = toset([
    "roles/dataflow.worker",            # Run as Dataflow worker
    "roles/bigquery.dataEditor",        # Write rows to BigQuery tables
    "roles/bigquery.jobUser",           # Submit BigQuery jobs
    "roles/storage.objectViewer",       # Read reference data from GCS
    "roles/storage.objectAdmin",        # Write Dataflow temp/staging files
    "roles/pubsub.subscriber",          # Pull from subscriptions
    "roles/pubsub.publisher",           # Publish to DLQ topic
    "roles/secretmanager.secretAccessor", # Read pipeline secrets
    "roles/monitoring.metricWriter",    # Write custom Dataflow metrics
    "roles/cloudtrace.agent",           # Write trace data
    "roles/artifactregistry.reader",    # Pull Docker images for Flex Templates
  ])
  project = var.project_id
  role    = each.value
  member  = "serviceAccount:${google_service_account.dataflow_worker.email}"
}

###############################################################################
# Vertex AI Pipeline Runner Service Account
# Orchestrates training runs, reads BQ, writes to GCS + model registry
###############################################################################

resource "google_service_account" "pipeline_runner" {
  account_id   = "${var.resource_prefix}-pipeline-runner"
  display_name = "Vertex AI Pipeline Runner SA"
  project      = var.project_id
}

resource "google_project_iam_member" "pipeline_runner_roles" {
  for_each = toset([
    "roles/aiplatform.user",            # Submit Vertex AI jobs + pipelines
    "roles/bigquery.dataViewer",        # Read training data from BigQuery
    "roles/bigquery.jobUser",           # Submit BigQuery export jobs
    "roles/storage.objectAdmin",        # Read/write ML artifacts to GCS
    "roles/artifactregistry.writer",    # Push Docker images for custom training
    "roles/iam.serviceAccountUser",     # Impersonate training SA for custom jobs
  ])
  project = var.project_id
  role    = each.value
  member  = "serviceAccount:${google_service_account.pipeline_runner.email}"
}

###############################################################################
# Vertex AI Training Service Account
# Runs inside custom training containers — reads BQ/GCS, writes model artifacts
###############################################################################

resource "google_service_account" "vertex_training" {
  account_id   = "${var.resource_prefix}-vertex-training"
  display_name = "Vertex AI Custom Training Job SA"
  project      = var.project_id
}

resource "google_project_iam_member" "vertex_training_roles" {
  for_each = toset([
    "roles/aiplatform.customCodeServiceAgent", # Required for custom training containers
    "roles/bigquery.dataViewer",               # Read training data
    "roles/bigquery.jobUser",                  # Submit BQ read jobs
    "roles/storage.objectAdmin",               # Read features, write model checkpoints
    "roles/monitoring.metricWriter",           # Write training metrics
  ])
  project = var.project_id
  role    = each.value
  member  = "serviceAccount:${google_service_account.vertex_training.email}"
}

###############################################################################
# Vertex AI Serving Service Account
# Runs online endpoint — reads Feature Store, writes prediction logs
###############################################################################

resource "google_service_account" "vertex_serving" {
  account_id   = "${var.resource_prefix}-vertex-serving"
  display_name = "Vertex AI Online Serving SA"
  project      = var.project_id
}

resource "google_project_iam_member" "vertex_serving_roles" {
  for_each = toset([
    "roles/aiplatform.serviceAgent",     # Read from Feature Store online serving API
    "roles/bigquery.dataEditor",         # Write prediction logs
    "roles/bigquery.jobUser",            # Submit streaming inserts
    "roles/monitoring.metricWriter",     # Write serving latency metrics
    "roles/secretmanager.secretAccessor", # Read API keys for downstream enrichment
  ])
  project = var.project_id
  role    = each.value
  member  = "serviceAccount:${google_service_account.vertex_serving.email}"
}

###############################################################################
# OSINT Integration Bridge Service Account
# Runs the bridge Cloud Run service — writes enriched interventions to BQ,
# reads Supabase credentials from Secret Manager
###############################################################################

resource "google_service_account" "bridge" {
  account_id   = "${var.resource_prefix}-bridge"
  display_name = "OSINT Integration Bridge SA"
  project      = var.project_id
}

resource "google_project_iam_member" "bridge_roles" {
  for_each = toset([
    "roles/bigquery.dataEditor",          # Write enriched_interventions rows
    "roles/bigquery.jobUser",             # Submit streaming inserts
    "roles/secretmanager.secretAccessor", # Read Supabase URL + service role key
    "roles/monitoring.metricWriter",      # Write bridge latency metrics
  ])
  project = var.project_id
  role    = each.value
  member  = "serviceAccount:${google_service_account.bridge.email}"
}

###############################################################################
# Workload Identity: Allow GKE workloads to impersonate SAs (if GKE added later)
# Commented out — uncomment when GKE is provisioned
###############################################################################

# resource "google_service_account_iam_member" "dataflow_worker_workload_identity" {
#   service_account_id = google_service_account.dataflow_worker.name
#   role               = "roles/iam.workloadIdentityUser"
#   member             = "serviceAccount:${var.project_id}.svc.id.goog[dataflow/dataflow-worker]"
# }
