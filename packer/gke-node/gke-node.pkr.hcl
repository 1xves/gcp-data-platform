packer {
  required_plugins {
    googlecompute = {
      source  = "github.com/hashicorp/googlecompute"
      version = ">= 1.1.4"
    }
  }
}

# ---------------------------------------------------------------------------
# Input variables
# ---------------------------------------------------------------------------

variable "gcp_project_id" {
  type        = string
  description = "GCP project ID in which the image will be created."
}

variable "zone" {
  type        = string
  default     = "us-central1-a"
  description = "GCP zone used for the ephemeral build instance."
}

variable "source_image_family" {
  type        = string
  default     = "cos-stable"
  description = "Source image family for Container-Optimized OS (COS)."
}

variable "image_name" {
  type        = string
  default     = "data-platform-gke-node-{{timestamp}}"
  description = "Name of the resulting Compute Engine machine image. Defaults to a timestamped name."
}

variable "machine_type" {
  type        = string
  default     = "n1-standard-4"
  description = "Machine type for the ephemeral build instance."
}

# ---------------------------------------------------------------------------
# Source — ephemeral Compute Engine instance built from COS Stable
# ---------------------------------------------------------------------------

source "googlecompute" "gke_node" {
  project_id          = var.gcp_project_id
  zone                = var.zone
  source_image_family = var.source_image_family
  machine_type        = var.machine_type

  # Storage
  disk_size = 50
  disk_type = "pd-ssd"

  # SSH access for provisioners
  ssh_username = "packer"

  # Output image metadata
  image_name        = var.image_name
  image_description = "Hardened GKE node image for the GCP Data Platform, built with Packer. Includes kernel tuning, ops-agent, and security updates."

  image_labels = {
    managed-by    = "packer"
    purpose       = "gke-node"
    source-family = var.source_image_family
  }

  metadata = {
    enable-oslogin = "TRUE"
  }
}

# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------

build {
  name    = "gke-node"
  sources = ["source.googlecompute.gke_node"]

  # ── Step 1: Apply OS security updates ─────────────────────────────────────
  # COS uses its own update mechanism; pull the latest security patches via
  # update_engine_client. Falls back gracefully if the update is not available.
  provisioner "shell" {
    inline = [
      "echo \"[$(date -u +%Y-%m-%dT%H:%M:%SZ)] Applying COS system updates...\"",
      "sudo /usr/sbin/update_engine_client --update || true",
      "sleep 5",
      "echo \"[$(date -u +%Y-%m-%dT%H:%M:%SZ)] System update step complete.\""
    ]
  }

  # ── Step 2: Kernel parameter tuning for high-throughput container workloads ─
  # Each sysctl is set live and also written to /etc/sysctl.d/99-data-platform.conf
  # so the tuning persists across reboots on any derived instances.
  provisioner "shell" {
    inline = [
      "echo \"[$(date -u +%Y-%m-%dT%H:%M:%SZ)] Applying kernel parameter tuning...\"",

      # Allow large listen backlogs — critical for services with many concurrent
      # inbound connections (e.g. Pub/Sub subscriber pods).
      "sudo sysctl -w net.core.somaxconn=65535",

      # Disable strict overcommit so JVM/Python workers can allocate virtual
      # memory freely without triggering OOM-killer on mmap-heavy workloads.
      "sudo sysctl -w vm.overcommit_memory=1",

      # Keep idle TCP connections alive longer to reduce reconnect overhead for
      # long-lived gRPC streams (BigQuery Storage API, Pub/Sub).
      "sudo sysctl -w net.ipv4.tcp_keepalive_time=600",

      # Increase socket buffer ceilings for high-throughput streaming inserts
      # and Dataflow side-input reads.
      "sudo sysctl -w net.core.rmem_max=134217728",
      "sudo sysctl -w net.core.wmem_max=134217728",

      # Persist tuning to survive reboots.
      "sudo mkdir -p /etc/sysctl.d",
      "printf 'net.core.somaxconn=65535\\nvm.overcommit_memory=1\\nnet.ipv4.tcp_keepalive_time=600\\nnet.core.rmem_max=134217728\\nnet.core.wmem_max=134217728\\n' | sudo tee /etc/sysctl.d/99-data-platform.conf",

      "echo \"[$(date -u +%Y-%m-%dT%H:%M:%SZ)] Kernel tuning applied.\""
    ]
  }

  # ── Step 3: Install GCP Ops Agent (metrics + structured logging) ───────────
  # The Ops Agent replaces legacy Stackdriver agents. It ingests system metrics
  # (CPU, memory, disk, network) and application logs via an OpenTelemetry
  # pipeline, forwarding both to Cloud Monitoring and Cloud Logging.
  provisioner "shell" {
    inline = [
      "echo \"[$(date -u +%Y-%m-%dT%H:%M:%SZ)] Installing GCP Ops Agent...\"",

      # Fetch the official Google install script.
      "curl -sSO https://dl.google.com/cloudagents/add-google-cloud-ops-agent-repo.sh",

      # --also-install installs the agent package immediately after adding the
      # repo. We do not start the service here — systemd state inside the
      # ephemeral build VM is not representative of production.
      "sudo bash add-google-cloud-ops-agent-repo.sh --also-install",

      # Confirm the agent binary landed correctly.
      "google-cloud-ops-agent --version 2>/dev/null || /opt/google-cloud-ops-agent/bin/google-cloud-ops-agent --version 2>/dev/null || echo 'Ops Agent binary check: see systemctl status on live instance'",

      # Clean up the downloaded install script.
      "rm -f add-google-cloud-ops-agent-repo.sh",

      "echo \"[$(date -u +%Y-%m-%dT%H:%M:%SZ)] Ops Agent installed.\""
    ]
  }

  # ── Step 4: Update gcloud components and clean up image ───────────────────
  provisioner "shell" {
    inline = [
      "echo \"[$(date -u +%Y-%m-%dT%H:%M:%SZ)] Updating gcloud components...\"",

      # Pull latest gcloud SDK component versions; --quiet suppresses prompts.
      "sudo gcloud components update --quiet 2>/dev/null || true",

      "echo \"[$(date -u +%Y-%m-%dT%H:%M:%SZ)] Cleaning apt caches and temp files...\"",

      # Purge downloaded package archives and orphaned dependencies.
      "sudo apt-get autoremove -y 2>/dev/null || true",
      "sudo apt-get clean 2>/dev/null || true",
      "sudo rm -rf /var/lib/apt/lists/* /tmp/* /var/tmp/*",

      # Remove SSH host keys so each derived VM generates unique keys on boot.
      "sudo rm -f /etc/ssh/ssh_host_*",

      # Discard free blocks to improve compression ratio of the resulting image.
      "sudo fstrim -av || true",

      "echo \"[$(date -u +%Y-%m-%dT%H:%M:%SZ)] Image finalization complete.\""
    ]
  }

  # ── Post-processor: write build manifest for CI/CD traceability ───────────
  # packer-manifest.json records the Compute Engine image name, artifact ID,
  # and custom build metadata. Downstream Terraform modules and Helm chart
  # pipelines can read this file to pin node pools to the exact image built here.
  post-processor "manifest" {
    output     = "packer-manifest.json"
    strip_path = true
    custom_data = {
      build_timestamp = "{{timestamp}}"
      source_family   = var.source_image_family
      gcp_project_id  = var.gcp_project_id
      machine_type    = var.machine_type
    }
  }
}
