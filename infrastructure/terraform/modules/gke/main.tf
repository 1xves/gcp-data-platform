###############################################################################
# GKE Module — Private GKE Cluster with Workload Identity
#
# Design decisions:
#   - VPC-native (alias IP) networking — required for GKE Dataplane V2 and
#     any future NetworkPolicy enforcement.
#   - Private nodes (no public IPs on workers). Master endpoint remains
#     publicly accessible so Cloud Build / CI can reach it; restrict via
#     master_authorized_networks_config.
#   - Workload Identity enabled cluster-wide. Each workload SA is bound 1:1
#     to a GCP SA, eliminating node-level key files entirely.
#   - Binary Authorization in ENFORCE mode — only images that pass the
#     project attestation policy are admitted at runtime.
#   - Release channel REGULAR — gets GKE patches ~2 weeks after Rapid,
#     before Stable. Good balance for a production-ish data platform.
###############################################################################

###############################################################################
# GKE Cluster
###############################################################################

resource "google_container_cluster" "primary" {
  name     = "${var.resource_prefix}-gke"
  location = var.region
  project  = var.project_id

  # Restrict node zones to avoid GCE_STOCKOUT in specific zones.
  # Empty list = GKE picks all zones in the region (default).
  node_locations = length(var.node_locations) > 0 ? var.node_locations : null

  # We manage node pools explicitly — delete the default one immediately.
  remove_default_node_pool = true
  initial_node_count       = 1

  # ── Networking ─────────────────────────────────────────────────────────────
  networking_mode = "VPC_NATIVE"
  network         = var.network_self_link
  subnetwork      = var.subnetwork_self_link

  ip_allocation_policy {
    cluster_secondary_range_name  = var.cluster_secondary_range_name
    services_secondary_range_name = var.services_secondary_range_name
  }

  # Private cluster: nodes have internal IPs only. The master control plane
  # is reachable over the public endpoint (enable_private_endpoint = false)
  # but restricted to the CIDRs in master_authorized_networks_config.
  private_cluster_config {
    enable_private_nodes    = true
    enable_private_endpoint = false
    master_ipv4_cidr_block  = "172.16.0.0/28"
  }

  master_authorized_networks_config {
    dynamic "cidr_blocks" {
      for_each = var.authorized_cidr_blocks
      content {
        cidr_block   = cidr_blocks.value.cidr_block
        display_name = cidr_blocks.value.display_name
      }
    }
  }

  # ── Identity & Security ────────────────────────────────────────────────────
  workload_identity_config {
    workload_pool = "${var.project_id}.svc.id.goog"
  }

  # Binary Authorization: full attestation enforcement in production only.
  # Staging uses DISABLED — no attestation policy is configured yet, and
  # PROJECT_SINGLETON_POLICY_ENFORCE defaults to "deny all," which would
  # block every pod from starting (including system pods and ArgoCD).
  # Wire up a Cloud Build attestor + policy before enabling in production.
  binary_authorization {
    evaluation_mode = var.environment == "production" ? "PROJECT_SINGLETON_POLICY_ENFORCE" : "DISABLED"
  }

  # ── Release Channel ────────────────────────────────────────────────────────
  release_channel {
    channel = "REGULAR"
  }

  # ── Add-ons ────────────────────────────────────────────────────────────────
  addons_config {
    horizontal_pod_autoscaling {
      disabled = false
    }
    http_load_balancing {
      disabled = false
    }
    gce_persistent_disk_csi_driver_config {
      enabled = true
    }
  }

  # ── Observability ──────────────────────────────────────────────────────────
  logging_service    = "logging.googleapis.com/kubernetes"
  monitoring_service = "monitoring.googleapis.com/kubernetes"

  # ── Maintenance Policy ─────────────────────────────────────────────────────
  # Allow GKE to perform control-plane upgrades on weekends only,
  # during a 6-hour window to minimise business-hours blast radius.
  maintenance_policy {
    recurring_window {
      start_time = "2024-01-06T00:00:00Z" # Saturday 00:00 UTC
      end_time   = "2024-01-06T06:00:00Z" # Saturday 06:00 UTC
      recurrence = "FREQ=WEEKLY;BYDAY=SA,SU"
    }
  }

  # ── Labels ─────────────────────────────────────────────────────────────────
  resource_labels = {
    managed-by = "terraform"
    env        = var.environment
    team       = "data-engineering"
  }

  # Cluster deletion protection — set to false here so Terraform can tear
  # down the cluster in non-production. For production, override via
  # var.environment conditional or set deletion_protection = true explicitly.
  deletion_protection = false

  depends_on = [
    google_service_account.gke_node_sa,
    google_project_iam_member.node_sa_log_writer,
    google_project_iam_member.node_sa_metric_writer,
    google_project_iam_member.node_sa_monitoring_viewer,
    google_project_iam_member.node_sa_storage_viewer,
    google_project_iam_member.node_sa_ar_reader,
  ]
}

###############################################################################
# General-Purpose Node Pool
###############################################################################

resource "google_container_node_pool" "general" {
  name     = "${var.resource_prefix}-general"
  cluster  = google_container_cluster.primary.id
  location = var.region
  project  = var.project_id

  # Start with var.initial_node_count nodes per zone; autoscaler takes over.
  node_count = var.initial_node_count

  autoscaling {
    min_node_count = var.min_node_count
    max_node_count = var.max_node_count
  }

  management {
    auto_repair  = true
    auto_upgrade = true
  }

  # Rolling upgrade strategy: allow 1 surge node, never leave a zone with
  # zero available nodes (max_unavailable = 0).
  upgrade_settings {
    max_surge       = 1
    max_unavailable = 0
  }

  node_config {
    machine_type = var.machine_type
    disk_type    = "pd-ssd"
    disk_size_gb = 100

    # Cloud Platform scope gives the node identity access to all GCP APIs;
    # fine-grained permissions are applied at the workload SA level via
    # Workload Identity, not at the node level.
    oauth_scopes = ["https://www.googleapis.com/auth/cloud-platform"]

    service_account = google_service_account.gke_node_sa.email

    # Workload Identity: pods annotated with the KSA use GKE Metadata Server
    # to exchange their KSA token for a GCP access token. This replaces the
    # old metadata-concealment approach.
    workload_metadata_config {
      mode = "GKE_METADATA"
    }

    # Shielded nodes: Secure Boot + integrity monitoring defend against
    # rootkit / boot-level tampering.
    shielded_instance_config {
      enable_secure_boot          = true
      enable_integrity_monitoring = true
    }

    labels = {
      pool = "general"
      env  = var.environment
    }

    # Network tags used in firewall rules to allow GKE node → master traffic.
    tags = ["gke-node", "${var.resource_prefix}-gke"]

    # Disable legacy Compute metadata server endpoint on nodes.
    metadata = {
      "disable-legacy-endpoints" = "true"
    }
  }
}

###############################################################################
# GKE Node Service Account
###############################################################################

resource "google_service_account" "gke_node_sa" {
  account_id   = "${var.resource_prefix}-gke-node"
  display_name = "GKE Node Service Account"
  description  = "Minimal SA attached to GKE worker nodes. Workload-level permissions are granted via Workload Identity, not via this SA."
  project      = var.project_id
}

# Nodes need to ship logs to Cloud Logging.
resource "google_project_iam_member" "node_sa_log_writer" {
  project = var.project_id
  role    = "roles/logging.logWriter"
  member  = "serviceAccount:${google_service_account.gke_node_sa.email}"
}

# Nodes need to push metrics to Cloud Monitoring.
resource "google_project_iam_member" "node_sa_metric_writer" {
  project = var.project_id
  role    = "roles/monitoring.metricWriter"
  member  = "serviceAccount:${google_service_account.gke_node_sa.email}"
}

# GKE system components (kube-state-metrics, etc.) read monitoring data.
resource "google_project_iam_member" "node_sa_monitoring_viewer" {
  project = var.project_id
  role    = "roles/monitoring.viewer"
  member  = "serviceAccount:${google_service_account.gke_node_sa.email}"
}

# Nodes pull container images from GCS-backed registries.
resource "google_project_iam_member" "node_sa_storage_viewer" {
  project = var.project_id
  role    = "roles/storage.objectViewer"
  member  = "serviceAccount:${google_service_account.gke_node_sa.email}"
}

# Nodes pull container images from Artifact Registry.
resource "google_project_iam_member" "node_sa_ar_reader" {
  project = var.project_id
  role    = "roles/artifactregistry.reader"
  member  = "serviceAccount:${google_service_account.gke_node_sa.email}"
}

###############################################################################
# Predictor Workload Identity — GCP Service Account
#
# The predictor service (running in namespace "predictor") uses this GCP SA
# via Workload Identity to call Vertex AI endpoints and read BigQuery tables.
# The KSA "predictor" in namespace "predictor" is annotated with:
#   iam.gke.io/gcp-service-account: <predictor_sa_email>
###############################################################################

resource "google_service_account" "predictor" {
  account_id   = "${var.resource_prefix}-predictor"
  display_name = "Predictor Workload Identity SA"
  description  = "GCP SA for the predictor Kubernetes workload. Bound to KSA predictor/predictor via Workload Identity."
  project      = var.project_id
}

# Predictor needs to invoke Vertex AI online prediction endpoints.
resource "google_project_iam_member" "predictor_vertex_user" {
  project = var.project_id
  role    = "roles/aiplatform.user"
  member  = "serviceAccount:${google_service_account.predictor.email}"
}

# Predictor reads feature data and logs from BigQuery.
resource "google_project_iam_member" "predictor_bq_viewer" {
  project = var.project_id
  role    = "roles/bigquery.dataViewer"
  member  = "serviceAccount:${google_service_account.predictor.email}"
}

# Workload Identity binding: allow the Kubernetes ServiceAccount "predictor"
# in both the production namespace ("predictor") and the staging namespace
# ("predictor-staging") to impersonate this GCP SA.
# ArgoCD deploys to predictor-staging in staging; the production namespace
# is used in production. Both must be listed so the same SA covers both envs.
resource "google_service_account_iam_binding" "predictor_workload_identity" {
  service_account_id = google_service_account.predictor.name
  role               = "roles/iam.workloadIdentityUser"

  members = [
    "serviceAccount:${var.project_id}.svc.id.goog[predictor/predictor]",
    "serviceAccount:${var.project_id}.svc.id.goog[predictor-staging/predictor]",
  ]
}
