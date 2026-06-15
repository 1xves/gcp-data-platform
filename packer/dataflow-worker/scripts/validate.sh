#!/usr/bin/env bash
# validate.sh
# Verifies that every pipeline dependency installed by install-pipeline-deps.sh
# can be imported by Python 3 and that apache-beam reports the expected version.
# Exits 1 if any check fails so the Packer build is aborted on bad installs.
set -euo pipefail

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

EXPECTED_BEAM_VERSION="2.55.0"

PASS="PASS"
FAIL="FAIL"

# Tracks whether any check has failed.
overall_status=0

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

log() {
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*"
}

check_import() {
  local module="$1"
  local label="${2:-$1}"

  if python3 -c "import ${module}" 2>/dev/null; then
    echo "  [${PASS}] import ${label}"
  else
    echo "  [${FAIL}] import ${label} — module could not be imported"
    overall_status=1
  fi
}

# ---------------------------------------------------------------------------
# 1. Import checks
# ---------------------------------------------------------------------------

log "Running import validation checks..."

check_import "apache_beam"            "apache_beam"
check_import "google.cloud.bigquery"  "google.cloud.bigquery"
check_import "google.cloud.pubsub_v1" "google.cloud.pubsub_v1"
check_import "fastavro"               "fastavro"
check_import "google.cloud.aiplatform" "google.cloud.aiplatform"

# ---------------------------------------------------------------------------
# 2. apache-beam version pin check
# ---------------------------------------------------------------------------

log "Checking apache-beam version..."

actual_beam_version=$(python3 -c "import apache_beam; print(apache_beam.__version__)" 2>/dev/null || echo "UNKNOWN")

if [[ "${actual_beam_version}" == "${EXPECTED_BEAM_VERSION}" ]]; then
  echo "  [${PASS}] apache_beam version == ${EXPECTED_BEAM_VERSION}"
else
  echo "  [${FAIL}] apache_beam version mismatch: expected=${EXPECTED_BEAM_VERSION}, actual=${actual_beam_version}"
  overall_status=1
fi

# ---------------------------------------------------------------------------
# 3. Spot-check that google-cloud-bigquery exposes Client
# ---------------------------------------------------------------------------

log "Checking google.cloud.bigquery.Client symbol..."

if python3 -c "from google.cloud import bigquery; bigquery.Client" 2>/dev/null; then
  echo "  [${PASS}] google.cloud.bigquery.Client symbol accessible"
else
  echo "  [${FAIL}] google.cloud.bigquery.Client symbol not found"
  overall_status=1
fi

# ---------------------------------------------------------------------------
# 4. Spot-check fastavro schema parsing
# ---------------------------------------------------------------------------

log "Checking fastavro schema parse capability..."

if python3 -c "
import fastavro
schema = fastavro.parse_schema({'type': 'record', 'name': 'Test', 'fields': [{'name': 'id', 'type': 'int'}]})
assert schema is not None
" 2>/dev/null; then
  echo "  [${PASS}] fastavro.parse_schema functional"
else
  echo "  [${FAIL}] fastavro.parse_schema returned unexpected result"
  overall_status=1
fi

# ---------------------------------------------------------------------------
# 5. Final result
# ---------------------------------------------------------------------------

echo ""
if [[ "${overall_status}" -eq 0 ]]; then
  log "All validation checks PASSED. Image is ready."
else
  log "One or more validation checks FAILED. Aborting build."
  exit 1
fi
