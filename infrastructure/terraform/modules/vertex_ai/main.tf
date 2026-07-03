###############################################################################
# Vertex AI Module — Feature Store, Model Registry, Online Endpoint
###############################################################################

###############################################################################
# Vertex AI Feature Store (Online + Offline)
# NOTE: resource type is google_vertex_ai_featurestore (no underscores in "featurestore")
###############################################################################

resource "google_vertex_ai_featurestore" "platform" {
  provider = google-beta
  name     = "${replace(var.resource_prefix, "-", "_")}_feature_store_v2"
  project  = var.project_id
  region   = var.region

  dynamic "online_serving_config" {
    for_each = var.featurestore_online_node_count > 0 ? [1] : []
    content {
      fixed_node_count = var.featurestore_online_node_count
    }
  }

  labels = {
    layer   = "ml"
    purpose = "feature-store"
  }
}

resource "google_vertex_ai_featurestore_entitytype" "user" {
  provider     = google-beta
  name         = "user"
  featurestore = google_vertex_ai_featurestore.platform.id
  description  = "User-level entity — one row per user_id"

  monitoring_config {
    snapshot_analysis {
      disabled                 = false
      monitoring_interval_days = 1 # Daily feature drift snapshots
    }
    numerical_threshold_config {
      value = 0.3 # Alert if feature distribution shifts > 30% (Jensen-Shannon)
    }
    categorical_threshold_config {
      value = 0.3
    }
  }
}

resource "google_vertex_ai_featurestore_entitytype_feature" "user_features" {
  provider = google-beta
  for_each = {
    event_count_7d          = { type = "INT64", description = "Total event count in last 7 days" }
    purchase_count_30d      = { type = "INT64", description = "Purchase events in last 30 days" }
    avg_session_duration_7d = { type = "DOUBLE", description = "Average session duration (seconds) in last 7 days" }
    last_active_days_ago    = { type = "INT64", description = "Days since last activity" }
    country                 = { type = "STRING", description = "User country from profile" }
    plan_tier               = { type = "STRING", description = "Subscription plan tier" }
    page_view_7d            = { type = "INT64", description = "Page view count last 7 days" }
    search_count_7d         = { type = "INT64", description = "Search event count last 7 days" }
    total_revenue_30d       = { type = "DOUBLE", description = "Total revenue attributed to user in 30 days" }
    churn_risk_prev         = { type = "DOUBLE", description = "Previous model churn risk score (feature for next model)" }
  }

  name        = each.key
  entitytype  = google_vertex_ai_featurestore_entitytype.user.id
  value_type  = each.value.type
  description = each.value.description
}

###############################################################################
# Vertex AI Online Endpoint
###############################################################################

resource "google_vertex_ai_endpoint" "churn_risk" {
  count        = var.enable_online_endpoint ? 1 : 0
  provider     = google-beta
  name         = "${var.resource_prefix}-churn-risk-endpoint"
  display_name = "Churn Risk Scoring Endpoint"
  location     = var.region
  project      = var.project_id
  description  = "Online serving endpoint for churn risk predictions. Supports traffic splitting and shadow mode."

  # NOTE: Private endpoint (network field) requires VPC peering with Google's
  # managed network and a project NUMBER (not ID) in the path. Omitting for
  # staging — endpoint is public but IAM-restricted via serving SA.
  # Re-enable for production with proper VPC peering setup.

  labels = {
    layer   = "ml"
    purpose = "online-serving"
    model   = "churn-risk"
  }
}

###############################################################################
# Daily Retraining Schedule — Cloud Scheduler → Vertex AI Pipelines REST API
#
# google_vertex_ai_schedule is not available in provider ~5.0.
# Cloud Scheduler is the standard alternative: it POST-creates a PipelineJob
# via the Vertex AI REST API on a cron schedule.
# The training SA must have roles/aiplatform.user to create pipeline jobs.
###############################################################################

resource "google_cloud_scheduler_job" "daily_retraining" {
  count     = var.enable_daily_retraining ? 1 : 0
  provider  = google
  name      = "${var.resource_prefix}-daily-retraining"
  project   = var.project_id
  region    = var.region
  schedule  = "0 2 * * *" # 2:00 AM UTC daily
  time_zone = "UTC"

  lifecycle {
    precondition {
      condition     = var.enable_online_endpoint
      error_message = "enable_daily_retraining requires enable_online_endpoint = true — the retraining pipeline deploys the new model to the endpoint."
    }
  }

  http_target {
    http_method = "POST"
    uri         = "https://${var.region}-aiplatform.googleapis.com/v1/projects/${var.project_id}/locations/${var.region}/pipelineJobs"

    oauth_token {
      service_account_email = var.training_sa_email
    }

    body = base64encode(jsonencode({
      displayName = "${var.resource_prefix}-daily-churn-retraining"
      templateUri = "https://us-central1-kfp.pkg.dev/${var.project_id}/ml-pipelines/churn-risk-pipeline:latest"
      runtimeConfig = {
        gcsOutputDirectory = "gs://${var.ml_artifacts_bucket}/pipeline-outputs"
        parameterValues = {
          project_id             = var.project_id
          region                 = var.region
          bq_dataset             = "${replace(var.resource_prefix, "-", "_")}_processed"
          feature_store_id       = google_vertex_ai_featurestore.platform.name
          endpoint_id            = google_vertex_ai_endpoint.churn_risk[0].name
          min_auc_threshold      = "0.85"
          min_precision_at_k     = "0.80"
          training_days_lookback = "90"
        }
      }
      serviceAccount = var.training_sa_email
    }))
  }

  depends_on = [google_vertex_ai_endpoint.churn_risk]
}
