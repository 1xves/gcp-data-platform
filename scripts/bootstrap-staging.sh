#!/usr/bin/env bash
###############################################################################
# GCP Data Platform — Staging Bootstrap Script
#
# Phased deployment:
#   Phase 1 — Create TF state bucket + apply all infra (no Dataflow job yet)
#   Phase 2 — Build & push Flex Template container image
#   Phase 3 — Enable Dataflow job + apply Monitoring
#   Phase 4 — GKE credentials + bootstrap ArgoCD + sync predictor
#
# Prerequisites:
#   gcloud auth login && gcloud auth application-default login
#   gcloud config set project project-6db0f664-1423-47cb-86d
#   terraform >= 1.5, docker, helm, kubectl installed
#
# Usage:
#   cd Products\ For\ Resume/gcp-data-platform
#   chmod +x scripts/bootstrap-staging.sh
#   ./scripts/bootstrap-staging.sh
###############################################################################

set -euo pipefail

PROJECT_ID="project-6db0f664-1423-47cb-86d"
REGION="us-central1"
ENV="staging"
TF_DIR="infrastructure/terraform"
STATE_BUCKET="${PROJECT_ID}-tf-state"

# Colours
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info()    { echo -e "${GREEN}[INFO]${NC}  $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
err()     { echo -e "${RED}[ERROR]${NC} $*" >&2; exit 1; }
section() { echo -e "\n${YELLOW}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"; echo -e "${YELLOW}  $*${NC}"; echo -e "${YELLOW}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}\n"; }

###############################################################################
# Preflight checks
###############################################################################

section "Preflight checks"

command -v gcloud    >/dev/null 2>&1 || err "gcloud not found. Install from https://cloud.google.com/sdk"
command -v terraform >/dev/null 2>&1 || err "terraform not found. Install from https://terraform.io"
command -v docker    >/dev/null 2>&1 || err "docker not found"
command -v kubectl   >/dev/null 2>&1 || err "kubectl not found"
command -v helm      >/dev/null 2>&1 || err "helm not found"

ACTIVE_PROJECT=$(gcloud config get-value project 2>/dev/null)
if [[ "${ACTIVE_PROJECT}" != "${PROJECT_ID}" ]]; then
  warn "Active gcloud project is '${ACTIVE_PROJECT}', expected '${PROJECT_ID}'."
  warn "Setting project to ${PROJECT_ID}..."
  gcloud config set project "${PROJECT_ID}"
fi

info "Active project: ${PROJECT_ID}"
info "Region: ${REGION}"
info "Environment: ${ENV}"

###############################################################################
# Phase 1 — Terraform: Infrastructure (no Dataflow job)
###############################################################################

section "Phase 1 — Terraform: Infrastructure"

# 1a. Create state bucket if it doesn't exist
if gsutil ls -b "gs://${STATE_BUCKET}" >/dev/null 2>&1; then
  info "State bucket gs://${STATE_BUCKET} already exists."
else
  info "Creating Terraform state bucket gs://${STATE_BUCKET}..."
  gsutil mb -p "${PROJECT_ID}" -l "${REGION}" -b on "gs://${STATE_BUCKET}"
  gsutil versioning set on "gs://${STATE_BUCKET}"
  info "State bucket created with versioning enabled."
fi

# 1b. terraform init
info "Running terraform init..."
cd "${TF_DIR}"
terraform init \
  -upgrade \
  -backend-config="bucket=${STATE_BUCKET}"

# 1c. terraform plan — infra only (create_dataflow_job = false by default in staging.tfvars)
info "Running terraform plan (Dataflow job gated off)..."
terraform plan \
  -var-file="staging.tfvars" \
  -out=tfplan-phase1.out

echo ""
warn "Review the plan above. Press Enter to apply, or Ctrl+C to abort."
read -r

# 1d. terraform apply
info "Applying infrastructure..."
terraform apply tfplan-phase1.out
info "Phase 1 complete — VPC, GKE, IAM, Pub/Sub, GCS, BigQuery, Vertex AI provisioned."

cd - >/dev/null

###############################################################################
# Phase 2 — Build & push Flex Template container image
###############################################################################

section "Phase 2 — Build & push Dataflow Flex Template image"

IMAGE_TAG=$(git rev-parse --short HEAD 2>/dev/null || echo "latest")
AR_HOST="${REGION}-docker.pkg.dev"
AR_REPO="${AR_HOST}/${PROJECT_ID}/stg-ml-containers"
IMAGE_URI="${AR_REPO}/event-processor:${IMAGE_TAG}"

info "Authenticating Docker to Artifact Registry..."
gcloud auth configure-docker "${AR_HOST}" --quiet

info "Building event-processor image (tag: ${IMAGE_TAG})..."
docker build \
  -t "${IMAGE_URI}" \
  -f pipelines/dataflow/Dockerfile \
  pipelines/dataflow/

info "Pushing image to Artifact Registry..."
docker push "${IMAGE_URI}"

info "Registering Flex Template in GCS..."
TEMPLATE_GCS="gs://${PROJECT_ID}-dataflow-staging/dataflow-templates/event-processor/template.json"
gcloud dataflow flex-template build "${TEMPLATE_GCS}" \
  --image="${IMAGE_URI}" \
  --sdk-language=PYTHON \
  --metadata-file="pipelines/dataflow/flex_template_metadata.json"

info "Phase 2 complete — image pushed and template registered at ${TEMPLATE_GCS}"

###############################################################################
# Phase 3 — Terraform: Enable Dataflow job + Monitoring
###############################################################################

section "Phase 3 — Terraform: Enable Dataflow job"

info "Enabling Dataflow job (create_dataflow_job = true)..."
cd "${TF_DIR}"

terraform plan \
  -var-file="staging.tfvars" \
  -var="create_dataflow_job=true" \
  -out=tfplan-phase3.out

echo ""
warn "This will start the live Dataflow streaming job. Press Enter to apply, or Ctrl+C to abort."
read -r

terraform apply tfplan-phase3.out
info "Phase 3 complete — Dataflow job started, Monitoring alerts active."

cd - >/dev/null

###############################################################################
# Phase 4 — GKE: Credentials + ArgoCD bootstrap
# Skipped when enable_gke = false in staging.tfvars (default).
# To enable: set enable_gke = true and re-run this script.
###############################################################################

ENABLE_GKE=$(grep -E "^enable_gke\s*=" "${TF_DIR}/staging.tfvars" | grep -c "true" || true)

if [[ "${ENABLE_GKE}" -eq 0 ]]; then
  section "Phase 4 — GKE: SKIPPED (enable_gke = false)"
  warn "GKE is disabled. Set enable_gke = true in staging.tfvars and re-run to bootstrap ArgoCD."
else
  section "Phase 4 — GKE: ArgoCD bootstrap"

  info "Fetching GKE credentials for stg-gke cluster..."
  gcloud container clusters get-credentials stg-gke \
    --region "${REGION}" \
    --project "${PROJECT_ID}"

  info "Verifying cluster access..."
  kubectl get nodes

  # Bootstrap ArgoCD
  info "Installing ArgoCD v2.10.0..."
  kubectl create namespace argocd --dry-run=client -o yaml | kubectl apply -f -
  kubectl apply -n argocd \
    -f https://raw.githubusercontent.com/argoproj/argo-cd/v2.10.0/manifests/install.yaml

  info "Waiting for ArgoCD server to be ready (up to 5 min)..."
  kubectl wait --for=condition=available \
    --timeout=300s \
    deployment/argocd-server \
    -n argocd

  info "Applying App of Apps bootstrap..."
  kubectl apply -f gitops/argocd/bootstrap/argocd-install.yaml

  info "Waiting for predictor-staging to sync (up to 5 min)..."
  kubectl wait --for=condition=Available \
    --timeout=300s \
    deployment/predictor \
    -n predictor-staging 2>/dev/null || warn "predictor-staging not ready yet — ArgoCD may still be syncing. Run: kubectl get pods -n predictor-staging"

  # Get ArgoCD admin password
  ARGOCD_PASS=$(kubectl -n argocd get secret argocd-initial-admin-secret \
    -o jsonpath="{.data.password}" | base64 -d)
  info "ArgoCD admin password: ${ARGOCD_PASS}"
  info "Port-forward ArgoCD UI: kubectl port-forward svc/argocd-server -n argocd 8080:443"
fi

###############################################################################
# Done
###############################################################################

section "Bootstrap complete"

echo ""
info "Infrastructure:"
echo "  VPC/subnet:     stg-vpc (us-central1)"
echo "  GKE cluster:    stg-gke"
echo "  BigQuery:       stg_raw, stg_processed, stg_ml_features"
echo "  Pub/Sub topics: stg-events, stg-events-dlq"
echo "  Dataflow job:   stg-event-processor (streaming)"
echo "  Vertex AI:      Feature Store + online endpoint"
echo "  ArgoCD:         predictor-staging deployed"
echo ""
info "Next steps:"
echo "  1. Send a test event:  gcloud pubsub topics publish stg-events --message='{\"user_id\":\"test\",\"event_type\":\"page_view\"}'"
echo "  2. Check Dataflow:     gcloud dataflow jobs list --region=${REGION}"
echo "  3. Query BigQuery:     bq query --nouse_legacy_sql 'SELECT COUNT(*) FROM \`${PROJECT_ID}.stg_raw.raw_events\` WHERE event_date = CURRENT_DATE()'"
echo "  4. Health check:       make health-check PROJECT_ID=${PROJECT_ID} ENV=staging"
echo ""
