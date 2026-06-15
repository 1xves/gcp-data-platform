###############################################################################
# GCP Data Platform — Root Terraform Configuration
# Orchestrates all modules: networking, IAM, Pub/Sub, Dataflow, BigQuery,
# Vertex AI, and Monitoring.
###############################################################################

terraform {
  required_version = ">= 1.5.0"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 5.0"
    }
    google-beta = {
      source  = "hashicorp/google-beta"
      version = "~> 5.0"
    }
    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.4"
    }
  }

  # Remote state — bucket name passed via -backend-config at init time:
  # terraform init -backend-config="bucket=${PROJECT_ID}-tf-state"
  # See scripts/Makefile tf-init target for the exact command.
  backend "gcs" {
    prefix = "gcp-data-platform/terraform/state"
    # bucket is intentionally omitted — supplied at init time via -backend-config
    # This allows the same Terraform config to work across projects without editing files.
  }
}

provider "google" {
  project               = var.project_id
  region                = var.region
  # user_project_override + billing_project are required for billing-account-level APIs
  # (e.g. billingbudgets.googleapis.com). Without these, API calls are attributed to
  # Google's shared default project (764086051850) where the API is disabled, causing
  # a 403 SERVICE_DISABLED even after the billingbudgets API is enabled on your project.
  user_project_override = true
  billing_project       = var.project_id
}

provider "google-beta" {
  project               = var.project_id
  region                = var.region
  user_project_override = true
  billing_project       = var.project_id
}

###############################################################################
# Enable Required APIs
###############################################################################

locals {
  required_apis = [
    "pubsub.googleapis.com",
    "dataflow.googleapis.com",
    "bigquery.googleapis.com",
    "bigquerystorage.googleapis.com",
    "aiplatform.googleapis.com",
    "compute.googleapis.com",
    "storage.googleapis.com",
    "cloudkms.googleapis.com",
    "secretmanager.googleapis.com",
    "monitoring.googleapis.com",
    "logging.googleapis.com",
    "cloudtrace.googleapis.com",
    "servicenetworking.googleapis.com",
    "artifactregistry.googleapis.com",
    "container.googleapis.com",
    "cloudbuild.googleapis.com",
    "cloudscheduler.googleapis.com",
    "billingbudgets.googleapis.com",  # Required for google_billing_budget resources
  ]
}

resource "google_project_service" "apis" {
  for_each           = toset(local.required_apis)
  project            = var.project_id
  service            = each.value
  disable_on_destroy = false
}

###############################################################################
# Artifact Registry — Container image repositories
###############################################################################

resource "google_artifact_registry_repository" "ml_containers" {
  provider      = google
  project       = var.project_id
  location      = var.region
  repository_id = "${var.resource_prefix}-ml-containers"
  description   = "Docker images for Dataflow Flex Templates and ML serving containers"
  format        = "DOCKER"

  labels = var.common_labels

  depends_on = [google_project_service.apis]
}

###############################################################################
# Module: Networking (VPC + Private Google Access)
###############################################################################

module "networking" {
  source = "./modules/networking"

  project_id       = var.project_id
  region           = var.region
  network_name     = "${var.resource_prefix}-vpc"
  subnet_cidr      = var.subnet_cidr
  secondary_ranges = var.secondary_ranges

  depends_on = [google_project_service.apis]
}

###############################################################################
# Module: GKE (Private Cluster + Workload Identity)
###############################################################################

module "gke" {
  source = "./modules/gke"
  count  = var.enable_gke ? 1 : 0

  project_id                    = var.project_id
  region                        = var.region
  environment                   = var.environment
  resource_prefix               = var.resource_prefix
  network_self_link             = module.networking.network_self_link
  subnetwork_self_link          = module.networking.subnetwork_self_link
  cluster_secondary_range_name  = module.networking.pods_secondary_range_name
  services_secondary_range_name = module.networking.services_secondary_range_name
  authorized_cidr_blocks        = var.gke_authorized_cidr_blocks
  machine_type                  = var.gke_machine_type
  min_node_count                = var.gke_min_node_count
  max_node_count                = var.gke_max_node_count
  node_locations                = var.gke_node_locations

  depends_on = [google_project_service.apis, module.networking]
}

###############################################################################
# Module: IAM (Service Accounts + Bindings)
###############################################################################

module "iam" {
  source = "./modules/iam"

  project_id      = var.project_id
  resource_prefix = var.resource_prefix

  depends_on = [google_project_service.apis]
}

###############################################################################
# Module: Pub/Sub (Ingestion Layer)
###############################################################################

module "pubsub" {
  source = "./modules/pubsub"

  project_id            = var.project_id
  resource_prefix       = var.resource_prefix
  dataflow_sa_email     = module.iam.dataflow_worker_sa_email
  message_retention_sec = var.pubsub_message_retention_sec
  max_delivery_attempts = var.pubsub_max_delivery_attempts

  depends_on = [module.iam]
}

###############################################################################
# Module: GCS (Staging Buckets for Dataflow + ML Artifacts)
###############################################################################

resource "google_storage_bucket" "dataflow_staging" {
  name                        = "${var.project_id}-dataflow-staging"
  location                    = var.region
  force_destroy               = false
  uniform_bucket_level_access = true

  versioning {
    enabled = true
  }

  lifecycle_rule {
    condition { age = 30 }
    action { type = "Delete" }
  }

  labels = var.common_labels
}

resource "google_storage_bucket" "ml_artifacts" {
  name                        = "${var.project_id}-ml-artifacts"
  location                    = var.region
  force_destroy               = false
  uniform_bucket_level_access = true

  versioning {
    enabled = true
  }

  labels = var.common_labels
}

resource "google_storage_bucket" "reference_data" {
  name                        = "${var.project_id}-reference-data"
  location                    = var.region
  force_destroy               = false
  uniform_bucket_level_access = true

  labels = var.common_labels
}

###############################################################################
# Module: Dataflow (Stream Processing)
###############################################################################

module "dataflow" {
  source = "./modules/dataflow"

  project_id             = var.project_id
  region                 = var.region
  resource_prefix        = var.resource_prefix
  network_self_link      = module.networking.network_self_link
  subnetwork_self_link   = module.networking.subnetwork_self_link
  dataflow_sa_email      = module.iam.dataflow_worker_sa_email
  staging_bucket         = google_storage_bucket.dataflow_staging.name
  input_subscription     = module.pubsub.events_subscription_id
  dlq_topic              = module.pubsub.dlq_topic_id
  raw_events_table       = module.bigquery.raw_events_table_id
  aggregates_table       = module.bigquery.event_aggregates_table_id
  max_workers            = var.dataflow_max_workers
  machine_type           = var.dataflow_machine_type
  create_dataflow_job    = var.create_dataflow_job
  on_delete              = var.dataflow_on_delete

  depends_on = [module.pubsub, module.bigquery, module.networking, module.iam]
}

###############################################################################
# Module: BigQuery (Analytics Data Layer)
###############################################################################

module "bigquery" {
  source = "./modules/bigquery"

  project_id         = var.project_id
  region             = var.region
  resource_prefix    = var.resource_prefix
  dataflow_sa_email  = module.iam.dataflow_worker_sa_email
  pipeline_sa_email  = module.iam.pipeline_runner_sa_email
  serving_sa_email   = module.iam.vertex_serving_sa_email
  raw_retention_days = var.raw_events_retention_days
  slot_capacity      = var.bigquery_slot_capacity

  depends_on = [module.iam]
}

###############################################################################
# Module: Vertex AI (ML Platform)
###############################################################################

module "vertex_ai" {
  source = "./modules/vertex_ai"

  project_id          = var.project_id
  region              = var.region
  resource_prefix     = var.resource_prefix
  ml_artifacts_bucket = google_storage_bucket.ml_artifacts.name
  training_sa_email   = module.iam.vertex_training_sa_email
  serving_sa_email    = module.iam.vertex_serving_sa_email
  network_self_link   = module.networking.network_self_link
  prediction_logs_table = module.bigquery.prediction_logs_table_id
  featurestore_online_node_count = var.featurestore_online_node_count
  enable_online_endpoint         = var.enable_online_endpoint
  enable_daily_retraining        = var.enable_daily_retraining

  depends_on = [module.bigquery, module.iam]
}

###############################################################################
# Module: Cloud Run (Predictor Serving — replaces GKE for portfolio cost model)
#
# Why Cloud Run instead of GKE:
#   GKE Standard cluster = ~$73/month management fee + ~$68-205/month nodes
#   Cloud Run             = $0/month at idle, ~$0 at demo traffic (free tier)
#
# The GKE Terraform module and Helm charts remain in this repo to demonstrate
# Kubernetes expertise — they're just not deployed in staging to control cost.
# See infrastructure/terraform/modules/gke/ and helm/predictor/ for that code.
###############################################################################

module "cloud_run" {
  source = "./modules/cloud_run"

  project_id            = var.project_id
  region                = var.region
  resource_prefix       = var.resource_prefix
  service_account_email = module.iam.vertex_serving_sa_email

  # Image — updated by GitHub Actions CD on every merge to main.
  # On first apply (before image exists), this deploys a placeholder hello-world
  # container. Run the CD pipeline or push manually to replace it.
  image = var.predictor_image

  # Application config
  gcp_project           = var.project_id
  gcp_region            = var.region
  # Feature Store name is generated in the vertex_ai module as replace(resource_prefix, "-", "_") + "_feature_store"
  # Mirror that logic here so the predictor env var matches the actual resource name.
  feature_store_id      = "${replace(var.resource_prefix, "-", "_")}_feature_store"
  entity_type_id        = "user"
  model_artifacts_gcs   = "gs://${var.project_id}-ml-artifacts/models/staging/seed"
  model_version         = var.predictor_model_version
  prediction_logs_table = "${var.project_id}.${replace(var.resource_prefix, "-", "_")}_ml_features.prediction_logs"
  vertex_endpoint_id    = ""

  # Scaling — 0 = scale to zero (cold start ~15s). Set to 1 before a live demo.
  min_instance_count = var.predictor_min_instances
  max_instance_count = var.predictor_max_instances

  allow_unauthenticated = var.predictor_allow_unauthenticated
  common_labels         = var.common_labels

  depends_on = [module.iam, google_project_service.apis]
}

###############################################################################
# Module: Cost Guard (daily-spend kill-switch)
#
# Hourly Cloud Scheduler -> Cloud Function -> BigQuery billing export. If today's
# net spend exceeds the daily limit, performs a targeted, reversible teardown:
# cancel running Dataflow jobs, scale the predictor to zero + cut public access,
# and drive Feature Store online nodes to 0.
#
# Prerequisite (manual, Console-only, no backfill): enable the Cloud Billing
# BigQuery export pointing at module.cost_guard.billing_export_dataset.
###############################################################################

module "cost_guard" {
  source = "./modules/cost-guard"

  project_id      = var.project_id
  region          = var.region
  resource_prefix = var.resource_prefix

  daily_limit_usd    = var.cost_guard_daily_limit_usd
  dry_run            = var.cost_guard_dry_run
  billing_account_id = var.billing_account_id

  # Console export enabled is "Daily cost detail" -> the *resource* export table
  # (gcp_billing_export_resource_v1_*). Must match exactly or the guard 404s.
  billing_export_table_type = "gcp_billing_export_resource_v1"

  predictor_service_name = "${var.resource_prefix}-predictor"
  predictor_runtime_sa   = module.iam.vertex_serving_sa_email
  featurestore_id        = "${replace(var.resource_prefix, "-", "_")}_feature_store"

  labels = var.common_labels

  # Only depends on the runtime SA from module.iam. Deliberately NOT depends_on
  # module.cloud_run — the predictor is referenced by name (a string), and a
  # dependency edge would drag the CD-managed Cloud Run service into every
  # targeted cost_guard apply.
  depends_on = [module.iam]
}

###############################################################################
# Module: Monitoring (Dashboards + Alert Policies)
###############################################################################

module "monitoring" {
  source = "./modules/monitoring"

  project_id          = var.project_id
  resource_prefix     = var.resource_prefix
  notification_email  = var.alert_notification_email
  dlq_topic_id        = module.pubsub.dlq_topic_id
  dataflow_job_name   = module.dataflow.job_name

  depends_on = [module.dataflow, module.pubsub, module.vertex_ai]
}

###############################################################################
# Billing Budget — Hard-stop cost alerts
#
# Sends email when monthly spend crosses $25, $50, $100, and $200.
# At $200 (disable_billing_on_threshold = true) all billable APIs are
# disabled on the project, killing all running resources before costs
# compound further. This is a last-resort circuit breaker.
#
# Prerequisites: the Billing Budget API must be enabled and the service
# account running Terraform needs roles/billing.costsManager on the
# billing account.
#
# billingbudgets.googleapis.com is enabled via the required_apis list above.
###############################################################################

# Email notification channel — shared between Cloud Monitoring alerts and billing budgets.
# This ensures billing threshold emails land in the same inbox as infrastructure alerts.
# NOTE: google_monitoring_notification_channel requires the email address to be verified
# in Cloud Monitoring before alerts will actually deliver. GCP sends a verification email
# to alert_notification_email on first apply — you must click the verification link.
resource "google_monitoring_notification_channel" "billing_email" {
  display_name = "${var.resource_prefix}-billing-alert-email"
  type         = "email"
  project      = var.project_id

  labels = {
    email_address = var.alert_notification_email
  }

  depends_on = [google_project_service.apis]
}

resource "google_billing_budget" "project_budget" {
  billing_account = var.billing_account_id
  display_name    = "${var.resource_prefix}-monthly-budget"

  # Scope to this project only — does not affect other projects on the account.
  budget_filter {
    projects = ["projects/${var.project_id}"]
  }

  # Hard monthly cap in USD. Adjust per environment via staging.tfvars / production.tfvars.
  # NOTE: units must be a string — GCP proto uses int64 encoded as string for currency units.
  amount {
    specified_amount {
      currency_code = "USD"
      units         = tostring(var.budget_monthly_limit_usd)
    }
  }

  # Four alert thresholds:
  #   25% of budget  — early warning (e.g. $12.50 on a $50 limit)
  #   50% of budget  — halfway, review what's running
  #  100% of budget  — at cap, verify no runaway resources
  #  100% forecasted — GCP projects you will reach cap; useful for catching slow leaks
  threshold_rules {
    threshold_percent = 0.25
    spend_basis       = "CURRENT_SPEND"
  }
  threshold_rules {
    threshold_percent = 0.50
    spend_basis       = "CURRENT_SPEND"
  }
  threshold_rules {
    threshold_percent = 1.0
    spend_basis       = "CURRENT_SPEND"
  }
  threshold_rules {
    threshold_percent = 1.0
    spend_basis       = "FORECASTED_SPEND"
  }

  all_updates_rule {
    # Wire to the Cloud Monitoring email channel created above so alerts go to
    # alert_notification_email, not just to the billing account admin inbox.
    monitoring_notification_channels = [
      google_monitoring_notification_channel.billing_email.id,
    ]
    # Also keep default billing admin emails — belt and suspenders.
    disable_default_iam_recipients = false
  }

  depends_on = [google_project_service.apis]
}
