###############################################################################
# cost-guard — daily-spend kill-switch
# Cloud Scheduler (hourly) -> Cloud Function (gen2) -> BigQuery billing export
# -> targeted, reversible teardown when today's net spend exceeds the limit.
###############################################################################

locals {
  # Standard billing export table id: dashes in the account id become underscores.
  billing_export_table = format(
    "%s.%s.%s_%s",
    var.project_id,
    var.billing_export_dataset,
    var.billing_export_table_type,
    replace(var.billing_account_id, "-", "_"),
  )
  sa_email = google_service_account.cost_guard.email
}

# ─── APIs required for gen2 functions (idempotent; never disabled on destroy) ──
resource "google_project_service" "required" {
  for_each = toset([
    "cloudfunctions.googleapis.com",
    "run.googleapis.com",
    "eventarc.googleapis.com",
  ])
  project            = var.project_id
  service            = each.value
  disable_on_destroy = false
}

# ─── BigQuery dataset that receives the Cloud Billing export ──────────────────
# NOTE: enabling the export itself (Billing -> Billing export -> BigQuery) is a
# Console-only action with no Terraform/API support and no historical backfill.
resource "google_bigquery_dataset" "billing_export" {
  project       = var.project_id
  dataset_id    = var.billing_export_dataset
  friendly_name = "Cloud Billing export"
  description   = "Daily billing export consumed by cost-guard. Enable the export in the Console pointing here."
  location      = "US"
  labels        = var.labels
}

# ─── Service account the guard runs as ───────────────────────────────────────
resource "google_service_account" "cost_guard" {
  project      = var.project_id
  account_id   = "${var.resource_prefix}-cost-guard"
  display_name = "cost-guard daily spend kill-switch"
}

# Query the billing export
resource "google_project_iam_member" "bq_job_user" {
  project = var.project_id
  role    = "roles/bigquery.jobUser"
  member  = "serviceAccount:${local.sa_email}"
}

resource "google_bigquery_dataset_iam_member" "export_reader" {
  project    = var.project_id
  dataset_id = google_bigquery_dataset.billing_export.dataset_id
  role       = "roles/bigquery.dataViewer"
  member     = "serviceAccount:${local.sa_email}"
}

# Teardown permissions
resource "google_project_iam_member" "run_admin" {
  project = var.project_id
  role    = "roles/run.admin" # scale predictor + edit its invoker IAM
  member  = "serviceAccount:${local.sa_email}"
}

resource "google_project_iam_member" "dataflow_dev" {
  project = var.project_id
  role    = "roles/dataflow.developer" # cancel running jobs
  member  = "serviceAccount:${local.sa_email}"
}

resource "google_project_iam_member" "aiplatform_admin" {
  project = var.project_id
  role    = "roles/aiplatform.admin" # set Feature Store online nodes to 0
  member  = "serviceAccount:${local.sa_email}"
}

# Patching a Cloud Run service that runs as another SA requires actAs on it.
resource "google_service_account_iam_member" "actas_runtime" {
  service_account_id = "projects/${var.project_id}/serviceAccounts/${var.predictor_runtime_sa}"
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${local.sa_email}"
}

# ─── Function source -> GCS ──────────────────────────────────────────────────
resource "google_storage_bucket" "src" {
  project                     = var.project_id
  name                        = "${var.project_id}-cost-guard-src"
  location                    = var.region
  uniform_bucket_level_access = true
  force_destroy               = true
  labels                      = var.labels
}

data "archive_file" "src" {
  type        = "zip"
  source_dir  = "${path.module}/function"
  output_path = "${path.module}/.build/cost-guard-src.zip"
}

resource "google_storage_bucket_object" "src" {
  name   = "cost-guard-src-${data.archive_file.src.output_md5}.zip"
  bucket = google_storage_bucket.src.name
  source = data.archive_file.src.output_path
}

# ─── The guard function (gen2) ───────────────────────────────────────────────
resource "google_cloudfunctions2_function" "guard" {
  project  = var.project_id
  name     = "${var.resource_prefix}-cost-guard"
  location = var.region

  build_config {
    runtime     = "python312"
    entry_point = "check_daily_spend"
    source {
      storage_source {
        bucket = google_storage_bucket.src.name
        object = google_storage_bucket_object.src.name
      }
    }
  }

  service_config {
    available_memory      = "256Mi"
    timeout_seconds       = 120
    max_instance_count    = 2
    min_instance_count    = 0
    service_account_email = local.sa_email
    ingress_settings      = "ALLOW_ALL" # auth enforced via run.invoker IAM (no allUsers)

    environment_variables = {
      PROJECT_ID           = var.project_id
      REGION               = var.region
      DAILY_LIMIT_USD      = tostring(var.daily_limit_usd)
      BILLING_EXPORT_TABLE = local.billing_export_table
      PREDICTOR_SERVICE    = var.predictor_service_name
      FEATURESTORE_ID      = var.featurestore_id
      TIME_ZONE            = var.time_zone
      DRY_RUN              = tostring(var.dry_run)
    }
  }

  depends_on = [google_project_service.required]
}

# Only the guard SA may invoke the function (Scheduler authenticates as it).
resource "google_cloud_run_service_iam_member" "invoker" {
  project  = var.project_id
  location = google_cloudfunctions2_function.guard.location
  service  = google_cloudfunctions2_function.guard.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${local.sa_email}"
}

# ─── Hourly trigger ──────────────────────────────────────────────────────────
resource "google_cloud_scheduler_job" "hourly" {
  project   = var.project_id
  name      = "${var.resource_prefix}-cost-guard-hourly"
  region    = var.region
  schedule  = var.schedule
  time_zone = var.time_zone

  http_target {
    http_method = "GET"
    uri         = google_cloudfunctions2_function.guard.service_config[0].uri
    oidc_token {
      service_account_email = local.sa_email
      audience              = google_cloudfunctions2_function.guard.service_config[0].uri
    }
  }

  depends_on = [google_cloud_run_service_iam_member.invoker]
}
