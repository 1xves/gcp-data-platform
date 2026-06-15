#!/usr/bin/env bash
# install-pipeline-deps.sh
# Installs pinned Python dependencies required by all GCP Data Platform
# Dataflow pipeline jobs. Designed to run inside the Packer build container.
set -euo pipefail

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

log() {
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*"
}

# ---------------------------------------------------------------------------
# 1. Upgrade pip to latest stable
# ---------------------------------------------------------------------------

log "Upgrading pip..."
python3 -m pip install --upgrade pip
log "pip upgraded to: $(python3 -m pip --version)"

# ---------------------------------------------------------------------------
# 2. Install pinned pipeline dependencies
#
#    Flags:
#      --no-cache-dir  — avoids stale cache artifacts inside the image layer
#      --compile       — byte-compiles .py files to .pyc at install time,
#                        which eliminates first-import compilation overhead
#                        (important for cold-start latency on Dataflow workers)
# ---------------------------------------------------------------------------

log "Installing pipeline dependencies..."

python3 -m pip install \
  --no-cache-dir \
  --compile \
  "apache-beam[gcp]==2.55.0" \
  "google-cloud-bigquery==3.13.0" \
  "google-cloud-pubsub==2.18.4" \
  "fastavro==1.9.3" \
  "google-cloud-aiplatform==1.44.0"

log "All pipeline dependencies installed successfully."

# ---------------------------------------------------------------------------
# 3. Show installed package versions for build log traceability
# ---------------------------------------------------------------------------

log "Installed package versions:"
python3 -m pip show \
  apache-beam \
  google-cloud-bigquery \
  google-cloud-pubsub \
  fastavro \
  google-cloud-aiplatform \
  | grep -E "^(Name|Version):"

# ---------------------------------------------------------------------------
# 4. Purge pip cache to keep the image layer lean
# ---------------------------------------------------------------------------

log "Cleaning pip cache..."
python3 -m pip cache purge || true
rm -rf /root/.cache/pip /tmp/pip-* 2>/dev/null || true
log "Cache cleaned."

log "install-pipeline-deps.sh completed successfully."
