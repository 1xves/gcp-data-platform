###############################################################################
# Dataflow Module — Flex Template Deployment
# The Flex Template (Docker image) is built by CI/CD and referenced here.
# Terraform provisions the job with all runtime parameters.
###############################################################################

# GCS object for the Flex Template metadata JSON
resource "google_storage_bucket_object" "flex_template_metadata" {
  name   = "dataflow-templates/event-processor/metadata.json"
  bucket = var.staging_bucket
  content = jsonencode({
    name        = "Event Processor Pipeline"
    description = "Streaming pipeline: Pub/Sub → validate → deduplicate → enrich → BigQuery"
    parameters = [
      { name = "input_subscription", label = "Pub/Sub subscription ID", isOptional = false },
      { name = "raw_output_table", label = "BigQuery raw events table", isOptional = false },
      { name = "agg_output_table", label = "BigQuery aggregates table", isOptional = false },
      { name = "dlq_topic", label = "Dead-letter Pub/Sub topic", isOptional = false },
      { name = "reference_data_gcs", label = "GCS path for reference data", isOptional = false },
      { name = "temp_location", label = "Temporary GCS path", isOptional = true },
      { name = "staging_location", label = "Staging GCS path", isOptional = true },
      { name = "no_use_public_ips", label = "No Public IPs", isOptional = true },
      { name = "max_workers", label = "Max Workers", isOptional = true },
      { name = "machine_type", label = "Machine Type", isOptional = true },
      { name = "enable_streaming_engine", label = "Enable Streaming Engine", isOptional = true }
    ]
  })
}

# Dataflow Flex Template job — streaming
# count = 0 on first deploy (var.create_dataflow_job = false) so Terraform can
# provision all infrastructure before the container image exists.
# After running `make pipeline-deploy`, set create_dataflow_job = true and re-apply.
resource "google_dataflow_flex_template_job" "event_processor" {
  count                   = var.create_dataflow_job ? 1 : 0
  provider                = google-beta
  project                 = var.project_id
  name                    = "${var.resource_prefix}-event-processor-v5"
  region                  = var.region
  container_spec_gcs_path = "gs://${var.staging_bucket}/dataflow-templates/event-processor/template.json"

  # Top-level environment fields (launcher + workers)
  service_account_email = var.dataflow_sa_email
  network               = var.network_self_link
  subnetwork            = var.subnetwork_self_link

  parameters = {
    input_subscription = var.input_subscription
    raw_output_table   = var.raw_events_table
    agg_output_table   = var.aggregates_table
    dlq_topic          = var.dlq_topic
    reference_data_gcs = "gs://${var.staging_bucket}/reference-data/"
    temp_location      = "gs://${var.staging_bucket}/dataflow-temp/"
    staging_location   = "gs://${var.staging_bucket}/dataflow-staging/"

    # Networking — private, no public IPs
    no_use_public_ips = "true"

    # Scaling
    max_workers             = tostring(var.max_workers)
    machine_type            = var.machine_type
    enable_streaming_engine = "true" # Required for exactly-once Pub/Sub
  }

  # "drain" in production (preserves in-flight messages, but can take hours on a streaming job).
  # "cancel" in staging (instant; acceptable because staging has no SLA on message delivery).
  # Controlled via var.on_delete — set in staging.tfvars / production.tfvars.
  on_delete = var.on_delete

  labels = {
    layer   = "processing"
    purpose = "streaming-pipeline"
  }
}
