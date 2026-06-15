###############################################################################
# Monitoring Module — Cloud Monitoring Dashboard, Alert Policies, SLOs
###############################################################################

###############################################################################
# Notification Channel — Email (base channel for all alerts)
###############################################################################

resource "google_monitoring_notification_channel" "email" {
  display_name = "${var.resource_prefix}-platform-alerts-email"
  type         = "email"
  project      = var.project_id

  labels = {
    email_address = var.notification_email
  }
}

###############################################################################
# Alert Policy 1: Pub/Sub DLQ Spike
###############################################################################

resource "google_monitoring_alert_policy" "dlq_spike" {
  display_name = "[${var.resource_prefix}] Pub/Sub DLQ Message Spike"
  project      = var.project_id
  combiner     = "OR"

  conditions {
    display_name = "DLQ undelivered message count > 100"
    condition_threshold {
      filter          = <<-EOT
        resource.type="pubsub_subscription"
        AND metric.type="pubsub.googleapis.com/subscription/num_undelivered_messages"
        AND resource.labels.subscription_id="${replace(var.dlq_topic_id, "projects/${var.project_id}/topics/", "")}-sub"
      EOT
      comparison      = "COMPARISON_GT"
      threshold_value = 100
      duration        = "300s"

      aggregations {
        alignment_period   = "300s"
        per_series_aligner = "ALIGN_MAX"
      }
    }
  }

  documentation {
    content = <<-EOT
      ## DLQ Spike Alert

      More than 100 messages routed to dead-letter topic in 5 minutes.

      **Immediate Actions:**
      1. `gcloud logging read "resource.type=dataflow_step AND textPayload:DLQ"`
      2. `gcloud pubsub subscriptions pull ${replace(var.dlq_topic_id, "projects/${var.project_id}/topics/", "")}-sub --limit=10`
      3. Identify failing schema_version and coordinate with producer team
    EOT
    mime_type = "text/markdown"
  }

  # notification_rate_limit is only valid for log-based alerts — omitted here.
  alert_strategy {
    auto_close = "1800s" # Auto-close if condition clears for 30 minutes
  }

  notification_channels = [google_monitoring_notification_channel.email.id]
}

###############################################################################
# Alert Policy 2: Dataflow Pipeline Lag
###############################################################################

resource "google_monitoring_alert_policy" "pipeline_lag" {
  display_name = "[${var.resource_prefix}] Dataflow Pipeline Lag Critical"
  project      = var.project_id
  combiner     = "OR"

  conditions {
    display_name = "Dataflow system lag > 300 seconds"
    condition_threshold {
      filter = <<-EOT
        resource.type="dataflow_job"
        AND metric.type="dataflow.googleapis.com/job/system_lag"
        AND resource.labels.job_name=monitoring.regex.full_match("${var.dataflow_job_name}.*")
      EOT
      comparison      = "COMPARISON_GT"
      threshold_value = 300  # 5 minutes
      duration        = "120s"

      aggregations {
        alignment_period   = "60s"
        per_series_aligner = "ALIGN_MAX"
      }
    }
  }

  documentation {
    content = <<-EOT
      ## Dataflow Lag Alert

      Pipeline watermark > 5 minutes behind real-time. Data freshness SLO at risk.

      **Actions:**
      1. Check Dataflow UI → Worker count (should be auto-scaling)
      2. Check BigQuery write errors in job logs
      3. If stuck: `make pipeline-deploy` to restart with current template
    EOT
    mime_type = "text/markdown"
  }

  notification_channels = [google_monitoring_notification_channel.email.id]
}

###############################################################################
# Alert Policy 3: Vertex AI Endpoint P99 Latency
###############################################################################

resource "google_monitoring_alert_policy" "serving_latency" {
  # count = 0 until the Vertex AI endpoint has a deployed model and live traffic.
  # GCP validates the metric descriptor at create time even when enabled=false.
  # The metric only appears after predictions are served.
  # To enable: remove count = 0 and re-apply after model deployment.
  count        = 0

  display_name = "[${var.resource_prefix}] Vertex AI Endpoint P99 Latency Exceeded"
  project      = var.project_id
  combiner     = "OR"

  conditions {
    display_name = "Prediction P99 latency > 100ms"
    condition_threshold {
      filter = <<-EOT
        resource.type="aiplatform.googleapis.com/Endpoint"
        AND metric.type="aiplatform.googleapis.com/prediction/online/response_latencies"
      EOT
      comparison      = "COMPARISON_GT"
      threshold_value = 100  # milliseconds
      duration        = "120s"

      aggregations {
        alignment_period     = "60s"
        per_series_aligner   = "ALIGN_PERCENTILE_99"
        cross_series_reducer = "REDUCE_MAX"
      }
    }
  }

  documentation {
    content = <<-EOT
      ## Vertex AI Serving Latency Alert

      Online prediction P99 latency > 100ms SLO.

      **Actions:**
      1. Check Feature Store online read latency (target: < 10ms)
      2. Verify endpoint replica count — may need manual scale-out
      3. Check model memory pressure in endpoint logs
    EOT
    mime_type = "text/markdown"
  }

  notification_channels = [google_monitoring_notification_channel.email.id]
}

###############################################################################
# Dashboard — Platform Overview
###############################################################################

resource "google_monitoring_dashboard" "platform_overview" {
  project        = var.project_id
  dashboard_json = jsonencode({
    displayName = "${var.resource_prefix} — GCP Data Platform Overview"
    gridLayout = {
      columns = "2"
      widgets = [
        {
          title = "Pub/Sub DLQ Message Count"
          xyChart = {
            dataSets = [{
              timeSeriesQuery = {
                timeSeriesFilter = {
                  filter = "metric.type=\"pubsub.googleapis.com/subscription/num_undelivered_messages\" resource.type=\"pubsub_subscription\""
                  aggregation = { perSeriesAligner = "ALIGN_MAX", alignmentPeriod = "60s" }
                }
              }
              plotType = "LINE"
            }]
            timeshiftDuration = "0s"
          }
        },
        {
          title = "Dataflow System Lag (seconds)"
          xyChart = {
            dataSets = [{
              timeSeriesQuery = {
                timeSeriesFilter = {
                  filter = "metric.type=\"dataflow.googleapis.com/job/system_lag\" resource.type=\"dataflow_job\""
                  aggregation = { perSeriesAligner = "ALIGN_MAX", alignmentPeriod = "60s" }
                }
              }
              plotType = "LINE"
            }]
          }
        },
        {
          title = "Vertex AI Endpoint P99 Latency (ms)"
          xyChart = {
            dataSets = [{
              timeSeriesQuery = {
                timeSeriesFilter = {
                  filter = "metric.type=\"aiplatform.googleapis.com/prediction/online/response_latencies\" resource.type=\"aiplatform.googleapis.com/Endpoint\""
                  aggregation = { perSeriesAligner = "ALIGN_PERCENTILE_99", alignmentPeriod = "60s" }
                }
              }
              plotType = "LINE"
            }]
          }
        },
        {
          title = "Dataflow Worker Count"
          xyChart = {
            dataSets = [{
              timeSeriesQuery = {
                timeSeriesFilter = {
                  filter = "metric.type=\"dataflow.googleapis.com/job/current_num_vcpus\" resource.type=\"dataflow_job\""
                  aggregation = { perSeriesAligner = "ALIGN_MAX", alignmentPeriod = "60s" }
                }
              }
              plotType = "LINE"
            }]
          }
        }
      ]
    }
  })
}
