# =============================================================================
# GCP Data Platform — Makefile
# Provides consistent, documented commands for all development and deployment tasks.
# Run `make help` to see all available commands.
# =============================================================================

PROJECT_ID    ?= $(shell gcloud config get-value project)
REGION        ?= us-central1
ENV           ?= staging
PYTHON        ?= python3
TF_DIR        := infrastructure/terraform
PIPELINE_DIR  := pipelines/dataflow
VERTEX_DIR    := vertex_ai
IMAGE_TAG     ?= latest
GCP_ZONE      ?= us-central1-a

.PHONY: help init validate deploy-infra deploy-pipeline deploy-model test lint clean

# ── Help ───────────────────────────────────────────────────────────────────────

help:
	@echo ""
	@echo "GCP Data Platform — Available Commands"
	@echo "========================================"
	@echo ""
	@echo "  Setup:"
	@echo "    make init                  Bootstrap local dev environment"
	@echo "    make prereqs               Check required tools are installed"
	@echo ""
	@echo "  Infrastructure:"
	@echo "    make tf-plan               Terraform plan (preview changes)"
	@echo "    make tf-apply              Terraform apply (deploy infrastructure)"
	@echo "    make tf-destroy            Terraform destroy (WARNING: destructive)"
	@echo ""
	@echo "  Pipeline:"
	@echo "    make pipeline-test         Run pipeline tests (DirectRunner)"
	@echo "    make pipeline-template     Build Dataflow Flex Template Docker image"
	@echo "    make pipeline-deploy       Deploy Flex Template to Dataflow"
	@echo ""
	@echo "  ML:"
	@echo "    make train-local           Run training locally (sample data)"
	@echo "    make train-vertex          Submit training job to Vertex AI"
	@echo "    make run-pipeline          Run full ML pipeline (train+evaluate+deploy)"
	@echo ""
	@echo "  Quality:"
	@echo "    make test                  Run all unit tests"
	@echo "    make lint                  Run flake8 + mypy"
	@echo "    make data-quality          Run BigQuery data quality checks"
	@echo ""
	@echo "  Operations:"
	@echo "    make health-check          Verify all platform components are healthy"
	@echo "    make dlq-inspect           Show last 20 DLQ messages"
	@echo "    make model-drift           Run model drift check against prediction_logs"
	@echo ""
	@echo "Variables: PROJECT_ID=$(PROJECT_ID) REGION=$(REGION) ENV=$(ENV)"
	@echo ""

# ── Prerequisites ──────────────────────────────────────────────────────────────

prereqs:
	@echo "Checking required tools..."
	@command -v gcloud    >/dev/null 2>&1 || { echo "gcloud not found. Install: https://cloud.google.com/sdk"; exit 1; }
	@command -v terraform >/dev/null 2>&1 || { echo "terraform not found. Install: https://terraform.io"; exit 1; }
	@command -v docker    >/dev/null 2>&1 || { echo "docker not found"; exit 1; }
	@command -v python3   >/dev/null 2>&1 || { echo "python3 not found"; exit 1; }
	@echo "All prerequisites found."

# ── Local Setup ────────────────────────────────────────────────────────────────

init: prereqs
	@echo "Bootstrapping local environment..."
	pip install -r $(PIPELINE_DIR)/requirements.txt
	pip install pytest flake8 mypy black
	gcloud auth application-default login
	@echo "Init complete. Run 'make tf-plan' to preview infrastructure changes."

# ── Terraform ─────────────────────────────────────────────────────────────────

tf-init:
	cd $(TF_DIR) && terraform init -upgrade \
		-backend-config="bucket=$(PROJECT_ID)-tf-state"

tf-plan: tf-init
	cd $(TF_DIR) && terraform plan \
		-var="project_id=$(PROJECT_ID)" \
		-var="region=$(REGION)" \
		-var="resource_prefix=$(ENV)" \
		-out=tfplan.out
	@echo "Plan saved to tfplan.out. Review, then run 'make tf-apply'."

tf-apply: tf-init
	cd $(TF_DIR) && terraform apply \
		-var="project_id=$(PROJECT_ID)" \
		-var="region=$(REGION)" \
		-var="resource_prefix=$(ENV)" \
		-auto-approve

tf-destroy:
	@echo "WARNING: This will destroy ALL platform infrastructure in $(ENV)."
	@read -p "Type 'destroy-$(ENV)' to confirm: " confirm; \
	  [ "$$confirm" = "destroy-$(ENV)" ] || { echo "Aborted."; exit 1; }
	cd $(TF_DIR) && terraform destroy \
		-var="project_id=$(PROJECT_ID)" \
		-var="region=$(REGION)" \
		-var="resource_prefix=$(ENV)" \
		-auto-approve

# ── Dataflow Pipeline ──────────────────────────────────────────────────────────

pipeline-test:
	cd $(PIPELINE_DIR) && $(PYTHON) -m pytest tests/ -v --tb=short

pipeline-template:
	@echo "Building Dataflow Flex Template Docker image (linux/amd64)..."
	cd $(PIPELINE_DIR) && docker build \
		--platform linux/amd64 \
		-t $(REGION)-docker.pkg.dev/$(PROJECT_ID)/$(ENV)-ml-containers/event-processor:$(IMAGE_TAG) \
		-f Dockerfile .
	docker push $(REGION)-docker.pkg.dev/$(PROJECT_ID)/$(ENV)-ml-containers/event-processor:$(IMAGE_TAG)
	@echo "Template image pushed."

pipeline-deploy: pipeline-template
	gcloud dataflow flex-template build \
		gs://$(PROJECT_ID)-dataflow-staging/dataflow-templates/event-processor/template.json \
		--image=$(REGION)-docker.pkg.dev/$(PROJECT_ID)/$(ENV)-ml-containers/event-processor:$(IMAGE_TAG) \
		--sdk-language=PYTHON \
		--metadata-file=$(PIPELINE_DIR)/flex_template_metadata.json
	@echo "Flex Template deployed. Restart Dataflow job to pick up new template."

# ── ML Training ───────────────────────────────────────────────────────────────

train-local:
	@echo "Running local training with sample data..."
	cd $(VERTEX_DIR)/training && $(PYTHON) trainer/task.py \
		--project_id=$(PROJECT_ID) \
		--bq_dataset=platform_ml_features \
		--model_output_gcs=gs://$(PROJECT_ID)-ml-artifacts/models/churn-risk/local-test/ \
		--min_auc=0.70 \
		--min_precision_at_k=0.70

train-vertex:
	gcloud ai custom-jobs create \
		--region=$(REGION) \
		--display-name=churn-risk-training-$(shell date +%Y%m%d-%H%M%S) \
		--config=$(VERTEX_DIR)/training/vertex_training_config.yaml \
		--args="--project_id=$(PROJECT_ID),--bq_dataset=platform_ml_features,--model_output_gcs=gs://$(PROJECT_ID)-ml-artifacts/models/churn-risk/$(shell date +%Y%m%d)/"

run-pipeline:
	$(PYTHON) $(VERTEX_DIR)/pipelines/run_pipeline.py \
		--project_id=$(PROJECT_ID) \
		--region=$(REGION)

# ── Code Quality ──────────────────────────────────────────────────────────────

test:
	$(PYTHON) -m pytest pipelines/dataflow/tests/ vertex_ai/tests/ -v --cov=. --cov-report=term-missing

lint:
	flake8 pipelines/ vertex_ai/ --max-line-length=100 --exclude=__pycache__
	mypy pipelines/dataflow/ --ignore-missing-imports

# ── Operations ────────────────────────────────────────────────────────────────

health-check:
	@echo "=== Platform Health Check ==="
	@echo "1. Checking Dataflow job status..."
	gcloud dataflow jobs list --region=$(REGION) --filter="name:$(ENV)-event-processor" --format="table(id,name,currentState)"
	@echo "2. Checking Pub/Sub subscription backlog..."
	gcloud pubsub subscriptions describe $(ENV)-events-dataflow-sub --format="value(name,messageRetentionDuration)"
	@echo "3. Checking Vertex AI endpoint status..."
	gcloud ai endpoints list --region=$(REGION) --filter="displayName:$(ENV)-churn-risk"
	@echo "=== Health check complete ==="

dlq-inspect:
	@echo "Pulling last 20 DLQ messages (not acking — inspect only)..."
	gcloud pubsub subscriptions pull $(ENV)-events-dlq-sub \
		--limit=20 \
		--format="json" \
		| python3 -c "import sys,json; [print(json.dumps(json.loads(m['message']['data'] if isinstance(m['message']['data'],str) else '{}'), indent=2)) for m in json.load(sys.stdin)]"

model-drift:
	@echo "Running model drift check against last 7 days of prediction logs..."
	bq query --nouse_legacy_sql --project_id=$(PROJECT_ID) \
		"SELECT prediction_date, model_version, accuracy, precision_positive \
		 FROM \`$(PROJECT_ID).platform_ml_features.v_model_drift_daily\` \
		 WHERE prediction_date >= DATE_SUB(CURRENT_DATE(), INTERVAL 7 DAY) \
		 ORDER BY 1 DESC"

clean:
	find . -type f -name "*.pyc" -delete
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name ".pytest_cache" -exec rm -rf {} + 2>/dev/null || true
	rm -f $(TF_DIR)/tfplan.out

## ─── Packer ───────────────────────────────────────────────────────────────────
.PHONY: packer-init packer-build-dataflow packer-build-gke packer-build-all

packer-init: ## Initialize Packer plugins (run once)
	cd packer/dataflow-worker && packer init .
	cd packer/gke-node && packer init .

packer-build-dataflow: ## Build and push the Dataflow worker container image
	@echo "==> Building Dataflow worker image (tag: $(IMAGE_TAG))"
	cd packer/dataflow-worker && packer build \
		-var "image_tag=$(IMAGE_TAG)" \
		-var "gcp_project_id=$(PROJECT_ID)" \
		-var-file=variables.pkrvars.hcl \
		dataflow-worker.pkr.hcl

packer-build-gke: ## Build and register the hardened GKE node image
	@echo "==> Building GKE node image"
	cd packer/gke-node && packer build \
		-var "gcp_project_id=$(PROJECT_ID)" \
		-var "zone=$(GCP_ZONE)" \
		gke-node.pkr.hcl

packer-build-all: packer-build-dataflow packer-build-gke ## Build all Packer images

## ─── Helm ─────────────────────────────────────────────────────────────────────
.PHONY: helm-lint helm-template helm-deploy-staging helm-deploy-production helm-diff

helm-lint: ## Lint the predictor Helm chart
	helm lint helm/predictor --values helm/predictor/values.yaml --values helm/predictor/values-staging.yaml
	helm lint helm/predictor --values helm/predictor/values.yaml --values helm/predictor/values-production.yaml

helm-template: ## Render Helm templates to stdout (staging)
	helm template predictor helm/predictor \
		--values helm/predictor/values.yaml \
		--values helm/predictor/values-staging.yaml \
		--set image.tag=$(IMAGE_TAG)

helm-deploy-staging: ## Deploy predictor to staging via Helm
	@echo "==> Deploying predictor to staging (tag: $(IMAGE_TAG))"
	helm upgrade --install predictor helm/predictor \
		--namespace predictor-staging \
		--create-namespace \
		--values helm/predictor/values.yaml \
		--values helm/predictor/values-staging.yaml \
		--set image.tag=$(IMAGE_TAG) \
		--wait --timeout 5m

helm-deploy-production: ## Deploy predictor to production via Helm
	@echo "==> Deploying predictor to production (tag: $(IMAGE_TAG))"
	helm upgrade --install predictor helm/predictor \
		--namespace predictor-production \
		--create-namespace \
		--values helm/predictor/values.yaml \
		--values helm/predictor/values-production.yaml \
		--set image.tag=$(IMAGE_TAG) \
		--wait --timeout 10m --atomic

helm-diff: ## Show diff between current and pending Helm release (requires helm-diff plugin)
	helm diff upgrade predictor helm/predictor \
		--values helm/predictor/values.yaml \
		--values helm/predictor/values-$(ENV).yaml \
		--set image.tag=$(IMAGE_TAG)

## ─── ArgoCD ───────────────────────────────────────────────────────────────────
.PHONY: argocd-bootstrap argocd-install argocd-sync-staging argocd-sync-production argocd-status

argocd-bootstrap: ## Bootstrap ArgoCD onto the cluster (run once per cluster)
	@echo "==> Installing ArgoCD v2.10.0"
	kubectl create namespace argocd --dry-run=client -o yaml | kubectl apply -f -
	kubectl apply -n argocd -f https://raw.githubusercontent.com/argoproj/argo-cd/v2.10.0/manifests/install.yaml
	@echo "==> Waiting for ArgoCD to be ready"
	kubectl wait --for=condition=available --timeout=300s deployment/argocd-server -n argocd
	kubectl apply -f gitops/argocd/bootstrap/argocd-install.yaml

argocd-install: argocd-bootstrap ## Alias for argocd-bootstrap

argocd-sync-staging: ## Force-sync the staging application in ArgoCD
	argocd app sync predictor-staging --timeout 300

argocd-sync-production: ## Force-sync the production application in ArgoCD (requires manual confirmation)
	@echo "WARNING: This will sync production. Press Ctrl+C to abort."
	@sleep 5
	argocd app sync predictor-production --timeout 600

argocd-status: ## Show ArgoCD sync status for all data-platform apps
	argocd app list --project data-platform
