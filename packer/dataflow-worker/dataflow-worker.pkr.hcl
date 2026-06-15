packer {
  required_plugins {
    docker = {
      source  = "github.com/hashicorp/docker"
      version = ">= 1.0.9"
    }
  }
}

# ---------------------------------------------------------------------------
# Input variables
# ---------------------------------------------------------------------------

variable "image_tag" {
  type        = string
  default     = "latest"
  description = "Tag applied to the built container image."
}

variable "artifact_registry_region" {
  type        = string
  default     = "us-central1"
  description = "GCP region where the Artifact Registry repository lives."
}

variable "artifact_registry_repo" {
  type        = string
  description = "Artifact Registry repository name (e.g. data-platform-images)."
}

variable "gcp_project_id" {
  type        = string
  description = "GCP project ID that owns the Artifact Registry repository."
}

# ---------------------------------------------------------------------------
# Locals
# ---------------------------------------------------------------------------

locals {
  registry_host = "${var.artifact_registry_region}-docker.pkg.dev"
  image_uri     = "${local.registry_host}/${var.gcp_project_id}/${var.artifact_registry_repo}/dataflow-worker:${var.image_tag}"
}

# ---------------------------------------------------------------------------
# Source — start from the official Dataflow Flex Template base image
# ---------------------------------------------------------------------------

source "docker" "dataflow_worker" {
  # Official Google-maintained base for Python 3.10 Flex Template workers.
  # Includes the template launcher binary and a Python 3.10 interpreter.
  image  = "gcr.io/dataflow-templates-base/python310-template-launcher-base:flex_latest"
  commit = true
}

# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------

build {
  name    = "dataflow-worker"
  sources = ["source.docker.dataflow_worker"]

  # ── Step 1: OS-level system dependencies ──────────────────────────────────
  provisioner "shell" {
    inline = [
      "echo '[$(date -u +%Y-%m-%dT%H:%M:%SZ)] Updating apt package index...'",
      "apt-get update -qq",
      "echo '[$(date -u +%Y-%m-%dT%H:%M:%SZ)] Installing system dependencies...'",
      "DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends libssl-dev build-essential curl ca-certificates git pkg-config",
      "echo '[$(date -u +%Y-%m-%dT%H:%M:%SZ)] Cleaning apt caches...'",
      "apt-get clean",
      "rm -rf /var/lib/apt/lists/*"
    ]
    # Prevent apt interactive prompts from blocking the build.
    environment_vars = ["DEBIAN_FRONTEND=noninteractive"]
  }

  # ── Step 2: Upload provisioning scripts ────────────────────────────────────
  provisioner "file" {
    source      = "scripts/install-pipeline-deps.sh"
    destination = "/tmp/install-pipeline-deps.sh"
  }

  provisioner "file" {
    source      = "scripts/validate.sh"
    destination = "/tmp/validate.sh"
  }

  # ── Step 3: Install pinned Python pipeline dependencies ────────────────────
  provisioner "shell" {
    inline = [
      "chmod +x /tmp/install-pipeline-deps.sh",
      "bash /tmp/install-pipeline-deps.sh"
    ]
  }

  # ── Step 4: Validate all packages import correctly ─────────────────────────
  provisioner "shell" {
    inline = [
      "chmod +x /tmp/validate.sh",
      "bash /tmp/validate.sh"
    ]
  }

  # ── Step 5: Clean up temp scripts ─────────────────────────────────────────
  provisioner "shell" {
    inline = [
      "rm -f /tmp/install-pipeline-deps.sh /tmp/validate.sh"
    ]
  }

  # ── Post-processor: tag the committed image ────────────────────────────────
  post-processor "docker-tag" {
    repository = "${local.registry_host}/${var.gcp_project_id}/${var.artifact_registry_repo}/dataflow-worker"
    tags       = [var.image_tag]
  }

  # ── Post-processor: push to Artifact Registry ─────────────────────────────
  post-processor "docker-push" {
    # docker-push uses the tag applied above; no additional config needed
    # when `gcloud auth configure-docker` has already been run.
    login          = false  # rely on credential helper configured by gcloud
    login_server   = "${local.registry_host}"
  }
}
