###############################################################################
# Cloud Run Module — Predictor Serving Layer
#
# Replaces the GKE-hosted predictor with a serverless Cloud Run service.
#
# Cost model:
#   Idle (min_instance_count = 0):  $0.00/month
#   Always-warm (min_instances = 1): ~$1.50/month
#   Per-request cost at demo traffic: effectively $0 (within free tier)
#
# Free tier (per month):
#   2,000,000 requests
#   360,000 GB-seconds memory
#   180,000 vCPU-seconds
#
# Compared to GKE (~$140-280/month), this is the correct choice for a
# portfolio project that needs to be demonstrable on demand at near-zero cost.
###############################################################################

resource "google_cloud_run_v2_service" "predictor" {
  name     = "${var.resource_prefix}-predictor"
  location = var.region
  project  = var.project_id

  # Allow unauthenticated for demo/portfolio purposes.
  # Set allow_unauthenticated = false in production.tfvars.
  ingress = "INGRESS_TRAFFIC_ALL"

  template {
    service_account = var.service_account_email

    scaling {
      min_instance_count = var.min_instance_count
      max_instance_count = var.max_instance_count
    }

    # Cloud Run sends SIGTERM and waits up to 10s before SIGKILL.
    # The Flask/Gunicorn server handles this gracefully.
    max_instance_request_concurrency = 80

    timeout = "${var.request_timeout_seconds}s"

    containers {
      image = var.image == "" ? "us-docker.pkg.dev/cloudrun/container/hello" : var.image

      resources {
        limits = {
          cpu    = var.cpu
          memory = var.memory
        }
        # CPU is only allocated during request processing when min_instances = 0.
        # Set cpu_idle = true to keep CPU allocated between requests (required if
        # running background threads or if cold start latency is critical).
        cpu_idle          = var.min_instance_count > 0
        startup_cpu_boost = true # Extra CPU during cold start for faster model load
      }

      # ── Environment Variables ──────────────────────────────────────────────
      # All config passed as env vars — no ConfigMap or Helm values needed.
      # Secrets (if any) should use secret_env_vars referencing Secret Manager.

      env {
        name  = "GOOGLE_CLOUD_PROJECT"
        value = var.gcp_project
      }
      env {
        name  = "GCP_PROJECT"
        value = var.gcp_project
      }
      env {
        name  = "CLOUD_ML_REGION"
        value = var.gcp_region
      }
      env {
        name  = "GCP_REGION"
        value = var.gcp_region
      }
      env {
        name  = "FEATURE_STORE_ID"
        value = var.feature_store_id
      }
      env {
        name  = "ENTITY_TYPE_ID"
        value = var.entity_type_id
      }
      env {
        name  = "AIP_STORAGE_URI"
        value = var.model_artifacts_gcs
      }
      env {
        name  = "MODEL_VERSION"
        value = var.model_version
      }
      env {
        name  = "VERTEX_ENDPOINT_ID"
        value = var.vertex_endpoint_id
      }
      env {
        name  = "ENDPOINT_ID"
        value = var.vertex_endpoint_id
      }
      env {
        name  = "PREDICTION_LOGS_TABLE"
        value = var.prediction_logs_table
      }
      env {
        name  = "LOG_LEVEL"
        value = var.log_level
      }
      # Cloud Run injects PORT automatically (default 8080).
      # The Dockerfile CMD uses $PORT — no need to set it here.

      # ── Health Checks ──────────────────────────────────────────────────────
      # Cloud Run uses startup probe to determine when container is ready.
      # The predictor downloads model from GCS on startup (~5-15s depending on size).
      # startup_probe gives the container enough time to complete model download.

      startup_probe {
        http_get {
          path = "/healthz"
          port = 8080
        }
        initial_delay_seconds = 5
        period_seconds        = 5
        failure_threshold     = 12 # 12 × 5s = 60s max startup time for model download
        timeout_seconds       = 5
      }

      liveness_probe {
        http_get {
          path = "/healthz"
          port = 8080
        }
        period_seconds    = 30
        failure_threshold = 3
        timeout_seconds   = 5
      }
    }
  }

  labels = var.common_labels

  lifecycle {
    # Ignore image changes from Terraform — image updates are handled by
    # GitHub Actions CD pipeline (docker build → push → gcloud run deploy).
    # If Terraform manages the image, every deploy requires a terraform apply.
    ignore_changes = [
      template[0].containers[0].image,
      template[0].labels,
      labels,
    ]
  }
}

###############################################################################
# IAM — Public invoker (for portfolio demo)
# Remove this block and set allow_unauthenticated = false for production.
###############################################################################

resource "google_cloud_run_v2_service_iam_member" "public_invoker" {
  count = var.allow_unauthenticated ? 1 : 0

  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_service.predictor.name
  role     = "roles/run.invoker"
  member   = "allUsers"
}
