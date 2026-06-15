###############################################################################
# Staging Environment — Terraform Variable Values
# Project: project-6db0f664-1423-47cb-86d
# Deploy:  cd infrastructure/terraform && terraform apply -var-file=staging.tfvars
###############################################################################

project_id      = "project-6db0f664-1423-47cb-86d"
region          = "us-central1"
environment     = "staging"
resource_prefix = "stg"

common_labels = {
  team        = "data-engineering"
  managed_by  = "terraform"
  environment = "staging"
}

# ── Networking ────────────────────────────────────────────────────────────────
# Primary subnet: Dataflow workers + GKE nodes
# Secondary ranges must be index 0 = pods, index 1 = services (networking module convention)

subnet_cidr = "10.0.0.0/24"

secondary_ranges = [
  {
    range_name    = "stg-pods"
    ip_cidr_range = "10.1.0.0/16"   # /16 = 65k pod IPs
  },
  {
    range_name    = "stg-services"
    ip_cidr_range = "10.2.0.0/20"   # /20 = 4k service ClusterIPs
  }
]

# ── GKE ───────────────────────────────────────────────────────────────────────
# Phase 4 complete. GKE torn down. Set true only when running phase4-test.sh.
# Idle cost when enabled: ~$170-200/month. Cost when false: $0.
enable_gke = false

# Staging: smaller nodes, 1-3 per zone (us-central1 = 3 zones → 3-9 nodes max)
# 0.0.0.0/0 allows any IP to reach the master API endpoint — acceptable for staging.
# Lock this down to Cloud Build egress CIDRs for production.

gke_authorized_cidr_blocks = [
  {
    cidr_block   = "0.0.0.0/0"
    display_name = "all-staging"
  }
]

gke_machine_type   = "n1-standard-2"   # 2 vCPU / 7.5 GB — n1 family; highest availability in most regions
gke_min_node_count = 1
gke_max_node_count = 3

# Zone history (n1-standard-2 capacity):
#   us-central1-c — GCE_STOCKOUT on initial deploy (e2 and n2 also stocked out)
#   us-central1-b — ZONE_RESOURCE_POOL_EXHAUSTED on 2026-06-10 (MIG retried
#                   3:08–3:43 PM PT, every attempt failed, GKE gave up →
#                   node pool created in ERROR state). Zone a provisioned fine.
#   us-central1-f — untested; trying as replacement for b.
# Fallback if f is also exhausted: single-zone ["us-central1-a"] for staging.
# Use only us-central1-a for now — confirmed to have capacity.
# Other zones in us-central1 (-b, -c, -f) are currently hitting GCE_STOCKOUT.
gke_node_locations = ["us-central1-a"]

# ── Pub/Sub ───────────────────────────────────────────────────────────────────
pubsub_message_retention_sec = 86400  # 1 day (vs 7 days in production)
pubsub_max_delivery_attempts = 5

# ── Dataflow ──────────────────────────────────────────────────────────────────
dataflow_max_workers  = 5
dataflow_machine_type = "n1-standard-2"

# Set to true only when actively testing the pipeline. Recreating costs ~$100-200/month while running.
create_dataflow_job   = false
# "cancel" in staging = instant teardown. "drain" in production = wait for in-flight messages.
# Using "drain" in staging caused a 2h+ Terraform hang (2026-06-14 incident).
dataflow_on_delete    = "cancel"

# ── BigQuery ──────────────────────────────────────────────────────────────────
raw_events_retention_days = 30    # 30 days for staging (vs 90 in production)
bigquery_slot_capacity    = 100   # STANDARD edition minimum — ~$20/month

# ── Vertex AI ─────────────────────────────────────────────────────────────────
featurestore_online_node_count = 0     # Offline-only in staging — 0 online nodes = no node-hour billing
enable_online_endpoint         = false # No idle online endpoint in staging
enable_daily_retraining        = false # No nightly retraining trigger in staging

# ── Cloud Run (Predictor Serving) ─────────────────────────────────────────────
# Image is updated automatically by GitHub Actions CD on every merge to main.
# First apply uses placeholder hello-world image — run the CD pipeline to deploy real image.
predictor_image                 = "us-central1-docker.pkg.dev/project-6db0f664-1423-47cb-86d/stg-ml-containers/predictor:latest"
predictor_model_version         = "staging-seed-v1"
predictor_min_instances         = 0      # Scale to zero = $0/month idle. Set to 1 before a live demo.
predictor_max_instances         = 3
predictor_allow_unauthenticated = true   # Public URL for portfolio demos. Set false in production.

# ── Monitoring ────────────────────────────────────────────────────────────────
alert_notification_email = "sylmobleyiii@gmail.com"

# ── Billing Budget ─────────────────────────────────────────────────────────────
# Find your billing account ID: GCP Console → Billing → Account overview → "Account ID"
# Format: XXXXXX-XXXXXX-XXXXXX
# The Terraform SA needs roles/billing.costsManager on this billing account.
# Grant it: gcloud beta billing accounts add-iam-policy-binding <BILLING_ACCOUNT_ID> \
#              --member="serviceAccount:<TERRAFORM_SA>@<PROJECT>.iam.gserviceaccount.com" \
#              --role="roles/billing.costsManager"
billing_account_id       = "XXXXXX-XXXXXX-XXXXXX"  # set per-deploy — do not commit real value
budget_monthly_limit_usd = 50   # Alert at $12.50 / $25 / $50 / $50 forecasted
