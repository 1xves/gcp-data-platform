#!/usr/bin/env bash
###############################################################################
# Phase 4 — Predictor Serving Layer: Deploy, Test, Teardown
#
# What this does:
#   1.  Applies Terraform with enable_gke=true (provisions cluster)
#   2.  Uploads seed model to GCS (if not already there)
#   3.  Builds + pushes predictor Docker image
#   4.  Helm installs predictor to predictor-staging namespace
#   5.  Runs smoke tests (health check + prediction endpoint)
#   6.  Prints pass/fail results
#   7.  ALWAYS tears down GKE on exit (trap ensures this even on failure)
#
# Flags:
#   --keep-alive   Skip GKE teardown (useful if you want to inspect the cluster)
#   --skip-build   Skip Docker build (reuse existing image in Artifact Registry)
#   --skip-seed    Skip seed model upload (reuse existing GCS model)
#
# Usage:
#   cd "Products For Resume/gcp-data-platform"
#   chmod +x scripts/phase4-test.sh
#   ./scripts/phase4-test.sh
###############################################################################

set -euo pipefail

PROJECT_ID="project-6db0f664-1423-47cb-86d"
REGION="us-central1"
CLUSTER="stg-gke"
NAMESPACE="predictor-staging"
TF_DIR="infrastructure/terraform"
AR_REPO="us-central1-docker.pkg.dev/${PROJECT_ID}/stg-ml-containers/predictor"
IMAGE_TAG="latest"
SEED_GCS="gs://${PROJECT_ID}-ml-artifacts/models/staging/seed"

KEEP_ALIVE=false
SKIP_BUILD=false
SKIP_SEED=false

for arg in "$@"; do
  case $arg in
    --keep-alive) KEEP_ALIVE=true ;;
    --skip-build) SKIP_BUILD=true ;;
    --skip-seed)  SKIP_SEED=true  ;;
  esac
done

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
info()    { echo -e "${GREEN}[INFO]${NC}  $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
err()     { echo -e "${RED}[ERROR]${NC} $*" >&2; }
section() { echo -e "\n${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"; echo -e "${CYAN}  $*${NC}"; echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}\n"; }

PORTFWD_PID=""
PASS_COUNT=0
FAIL_COUNT=0

###############################################################################
# Teardown — runs on ANY exit (success, failure, or Ctrl+C)
###############################################################################

teardown() {
  local exit_code=$?

  # Kill port-forward if running
  if [[ -n "${PORTFWD_PID}" ]]; then
    kill "${PORTFWD_PID}" 2>/dev/null || true
  fi

  if [[ "${KEEP_ALIVE}" == "true" ]]; then
    warn "--keep-alive set. GKE cluster left running — remember to tear down manually:"
    warn "  cd ${TF_DIR} && terraform apply -var-file=staging.tfvars -var='enable_gke=false' -auto-approve"
    exit "${exit_code}"
  fi

  section "Teardown — disabling GKE"
  info "Destroying GKE cluster and node pool (enable_gke → false)..."
  cd "${TF_DIR}"
  terraform apply \
    -var-file="staging.tfvars" \
    -var="enable_gke=false" \
    -auto-approve \
    2>&1 | tail -20
  cd - >/dev/null
  info "GKE destroyed. Billing stopped."
  exit "${exit_code}"
}

trap teardown EXIT

###############################################################################
# Step 1 — Terraform: provision GKE
###############################################################################

section "Step 1 — Terraform: enable GKE"

cd "${TF_DIR}"
info "Applying with enable_gke=true..."
terraform apply \
  -var-file="staging.tfvars" \
  -var="enable_gke=true" \
  -auto-approve
cd - >/dev/null
info "GKE cluster provisioned."

###############################################################################
# Step 2 — Seed model (skip if already in GCS)
###############################################################################

section "Step 2 — Seed model"

if [[ "${SKIP_SEED}" == "true" ]]; then
  info "Skipping seed model upload (--skip-seed)."
elif gsutil -q stat "${SEED_GCS}/model.xgb" 2>/dev/null; then
  info "Seed model already in GCS at ${SEED_GCS}. Skipping upload."
else
  info "Uploading seed model to GCS..."
  python3 -m pip install --quiet xgboost scikit-learn numpy pandas google-cloud-storage
  python3 scripts/seed_model.py
fi

###############################################################################
# Step 3 — Build and push predictor image
###############################################################################

section "Step 3 — Build + push predictor image"

if [[ "${SKIP_BUILD}" == "true" ]]; then
  info "Skipping Docker build (--skip-build). Using existing image in Artifact Registry."
else
  info "Configuring Docker auth for Artifact Registry..."
  gcloud auth configure-docker us-central1-docker.pkg.dev --quiet

  info "Building image (platform linux/amd64)..."
  docker build \
    --platform linux/amd64 \
    -t "${AR_REPO}:${IMAGE_TAG}" \
    -f vertex_ai/serving/Dockerfile \
    .

  info "Pushing to Artifact Registry..."
  docker push "${AR_REPO}:${IMAGE_TAG}"
  info "Image pushed: ${AR_REPO}:${IMAGE_TAG}"
fi

###############################################################################
# Step 4 — Helm install
###############################################################################

section "Step 4 — Helm install predictor"

info "Fetching GKE credentials..."
gcloud container clusters get-credentials "${CLUSTER}" \
  --region "${REGION}" \
  --project "${PROJECT_ID}"

info "Deploying predictor via Helm..."
helm upgrade --install predictor ./helm/predictor \
  -f helm/predictor/values.yaml \
  -f helm/predictor/values-staging.yaml \
  --namespace "${NAMESPACE}" \
  --create-namespace \
  --wait \
  --timeout 5m

info "Deployment complete. Pod status:"
kubectl get pods -n "${NAMESPACE}"

###############################################################################
# Step 5 — Smoke tests
###############################################################################

section "Step 5 — Smoke tests"

info "Starting port-forward (svc/predictor → localhost:8080)..."
kubectl port-forward svc/predictor 8080:80 -n "${NAMESPACE}" &
PORTFWD_PID=$!
sleep 3  # Let the tunnel establish

run_test() {
  local name="$1"
  local result="$2"
  local expected="$3"

  if echo "${result}" | grep -q "${expected}"; then
    echo -e "  ${GREEN}✓ PASS${NC} — ${name}"
    PASS_COUNT=$((PASS_COUNT + 1))
  else
    echo -e "  ${RED}✗ FAIL${NC} — ${name}"
    echo -e "       Expected to find: ${expected}"
    echo -e "       Got: ${result}"
    FAIL_COUNT=$((FAIL_COUNT + 1))
  fi
}

# Test 1: liveness
HEALTH=$(curl -sf http://localhost:8080/healthz 2>/dev/null || echo "CURL_FAILED")
run_test "GET /healthz → {status: ok}" "${HEALTH}" '"status": "ok"'

# Test 2: readiness
READY=$(curl -sf http://localhost:8080/readyz 2>/dev/null || echo "CURL_FAILED")
run_test "GET /readyz → {status: ready}" "${READY}" '"status": "ready"'

# Test 3: prediction endpoint — valid batch
PREDICT=$(curl -sf -X POST http://localhost:8080/v1/predict \
  -H 'Content-Type: application/json' \
  -d '{"instances": ["test-user-001", "test-user-002"]}' \
  2>/dev/null || echo "CURL_FAILED")
run_test "POST /v1/predict → predictions array" "${PREDICT}" '"predictions"'

# Test 4: prediction response contains churn_risk_score
run_test "POST /v1/predict → churn_risk_score in response" "${PREDICT}" '"churn_risk_score"'

# Test 5: validation — empty instances rejected
EMPTY=$(curl -sf -X POST http://localhost:8080/v1/predict \
  -H 'Content-Type: application/json' \
  -d '{"instances": []}' \
  -o /dev/null -w "%{http_code}" \
  2>/dev/null || echo "000")
run_test "POST /v1/predict (empty instances) → 400" "${EMPTY}" "400"

# Test 6: validation — oversized batch rejected
TOOLARGE=$(curl -sf -X POST http://localhost:8080/v1/predict \
  -H 'Content-Type: application/json' \
  -d "{\"instances\": [$(python3 -c "print(','.join(['\"u\"']*501))")]}" \
  -o /dev/null -w "%{http_code}" \
  2>/dev/null || echo "000")
run_test "POST /v1/predict (501 instances) → 400" "${TOOLARGE}" "400"

kill "${PORTFWD_PID}" 2>/dev/null || true
PORTFWD_PID=""

###############################################################################
# Results
###############################################################################

section "Results"

TOTAL=$((PASS_COUNT + FAIL_COUNT))
echo -e "  Tests passed:  ${GREEN}${PASS_COUNT}/${TOTAL}${NC}"
echo -e "  Tests failed:  ${RED}${FAIL_COUNT}/${TOTAL}${NC}"
echo ""

if [[ "${FAIL_COUNT}" -gt 0 ]]; then
  err "Some smoke tests failed. Review pod logs:"
  err "  kubectl logs -n ${NAMESPACE} -l app.kubernetes.io/name=predictor --tail=50"
  exit 1
else
  info "All smoke tests passed."
fi

# teardown() runs automatically from this point via trap
