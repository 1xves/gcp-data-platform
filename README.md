# GCP ML Data Platform

A production-grade machine learning data platform built on Google Cloud Platform. The system ingests real-time user behavior events, processes them through a streaming pipeline, generates churn risk predictions using an XGBoost model, and serves predictions via a low-latency REST API — all managed as infrastructure-as-code with automated CI/CD.

Built as a portfolio project to demonstrate end-to-end cloud engineering and DevOps skills on GCP.

---

## Architecture

```
                         ┌─────────────────────────────────────────────────────┐
                         │                    GCP Project                      │
                         │                                                     │
  Event Producers        │  Ingestion          Processing         Storage      │
  ─────────────          │  ─────────          ──────────         ───────      │
                         │                                                     │
  Web / Mobile App  ────►│  Pub/Sub   ────►   Dataflow      ────► BigQuery    │
  (user events)          │  Topic              Flex Template       bronze /    │
                         │                     (Apache Beam)       silver /    │
                         │  Dead-Letter         │                  gold        │
                         │  Topic  ◄────────────┘                             │
                         │  (failed msgs)                                      │
                         │                                                     │
                         │  ML Layer                                           │
                         │  ────────                                           │
                         │                                                     │
                         │  Vertex AI           Vertex AI    Cloud             │
                         │  Feature Store ◄──── Training  ◄─ Scheduler        │
                         │  (online serving)    Pipeline      (nightly)        │
                         │       │                                             │
                         │       ▼                                             │
                         │  Cloud Run  ◄────── Artifact Registry              │
                         │  Predictor          (Docker images)                │
                         │  (/v1/predict)                                      │
                         │       │                                             │
                         │       └──────────────────────────► BigQuery         │
                         │                                    prediction_logs  │
                         │                                                     │
                         │  Infrastructure                                     │
                         │  ──────────────                                     │
                         │  VPC (private subnets)    Cloud Monitoring          │
                         │  Cloud NAT                Billing Budgets           │
                         │  IAM + Workload Identity  Secret Manager            │
                         └─────────────────────────────────────────────────────┘
                                           │
                         GitHub Actions CI/CD
                         PR → validate + plan
                         Merge → build + push + deploy
```

---

## Tech Stack

| Layer | Technology | Notes |
|-------|-----------|-------|
| Infrastructure as Code | Terraform 1.5+ | Modular, multi-environment, remote GCS state |
| Event Ingestion | Cloud Pub/Sub | Dead-letter topic, configurable retention |
| Stream Processing | Dataflow Flex Template (Apache Beam) | Validate → deduplicate → enrich → BigQuery |
| Data Warehouse | BigQuery | Bronze/silver/gold layers, partitioned tables, slot reservations |
| ML Feature Store | Vertex AI Feature Store | Online + offline serving, training-serving consistency |
| Model Training | Vertex AI Custom Training | XGBoost, Cloud Scheduler nightly retraining |
| Model Serving | Cloud Run | Scales to zero, $0 idle cost, live HTTPS endpoint |
| Container Registry | Artifact Registry | Docker images for Dataflow template and predictor |
| CI/CD | GitHub Actions | Workload Identity Federation (no stored keys), plan-on-PR, deploy-on-merge |
| Networking | VPC + Cloud NAT + Private Google Access | All workers on private IPs |
| IAM | Service accounts + Workload Identity | Least-privilege, no user-managed keys |
| Monitoring | Cloud Monitoring + Billing Budgets | Alerting policies, dashboard, spend alerts |
| Kubernetes (demo) | GKE Standard + Helm | Terraform module and Helm chart in repo — see `infrastructure/terraform/modules/gke/` and `helm/predictor/` |

---

## Repository Structure

```
gcp-data-platform/
├── infrastructure/
│   └── terraform/
│       ├── main.tf                 # Root — wires all modules together
│       ├── variables.tf            # All input variables with validation
│       ├── outputs.tf              # Useful outputs: URLs, SA emails
│       ├── staging.tfvars          # Staging environment values
│       └── modules/
│           ├── networking/         # VPC, subnets, Cloud NAT, Private Google Access
│           ├── iam/                # Service accounts, IAM bindings
│           ├── pubsub/             # Topics, subscriptions, dead-letter
│           ├── dataflow/           # Flex Template job, GCS template staging
│           ├── bigquery/           # Datasets, tables, slot reservations
│           ├── vertex_ai/          # Feature Store, training pipeline, online endpoint
│           ├── gke/                # Private GKE cluster, node pool, Workload Identity
│           ├── cloud_run/          # Predictor serving (replaces GKE in staging)
│           └── monitoring/         # Alert policies, notification channels
├── vertex_ai/
│   ├── serving/
│   │   ├── predictor.py            # Flask API — feature fetch, inference, BQ logging
│   │   ├── Dockerfile              # Cloud Run / GKE compatible container
│   │   └── requirements.txt
│   └── training/
│       └── trainer/
│           └── model.py            # ChurnRiskModel — XGBoost + preprocessor
├── helm/
│   └── predictor/                  # Helm chart for GKE deployment (Kubernetes demo)
├── scripts/
│   ├── bootstrap-staging.sh        # Phased deploy script (4 phases)
│   ├── phase4-test.sh              # GKE deploy + smoke test + teardown
│   └── seed_model.py               # Generates minimal XGBoost model for staging
└── .github/
    └── workflows/
        ├── ci.yml                  # PR: fmt check, validate, plan, docker build
        └── cd.yml                  # Merge: build, push, deploy to Cloud Run
```

---

## Key Design Decisions

**Modular Terraform over monolithic config.** Every infrastructure concern (networking, IAM, data, ML, serving) is a separate module with its own `variables.tf` and `outputs.tf`. Modules are composed in the root `main.tf`. This makes each layer independently testable and reusable across environments.

**Training-serving consistency via Feature Store.** The same features used for model training (fetched offline from Feature Store) are fetched online at prediction time. There is no separate feature computation path — the most common source of training-serving skew in production ML systems.

**Cloud Run over GKE for portfolio serving cost.** GKE Standard costs ~$140-280/month in staging (cluster management fee + nodes). Cloud Run costs $0 at idle and roughly $0 per demo request within the free tier. The GKE Terraform module and Helm chart remain in the repo to demonstrate Kubernetes expertise — they just aren't deployed continuously.

**Workload Identity Federation for CI/CD auth.** GitHub Actions authenticates to GCP via OIDC tokens, not long-lived service account JSON keys. No secrets to rotate, no credentials that can leak from the repo.

**`on_delete = "cancel"` for staging Dataflow.** Streaming Dataflow jobs with `on_delete = "drain"` wait indefinitely for in-flight messages before Terraform can finish. In staging, this caused a 2h24m hang. Staging uses `"cancel"` for instant teardown; production uses `"drain"` to avoid data loss. The value is a Terraform variable set per environment in `staging.tfvars` / `production.tfvars`.

---

## Cost Model

| State | Monthly Cost | What's Running |
|-------|-------------|----------------|
| Fully parked | ~$35-45 | Cloud NAT, GCS, Artifact Registry, BQ schema |
| Development (on-demand) | ~$50-80 | Above + Dataflow during active sessions |
| Demo-ready | ~$40-50 | Above + 1 warm Cloud Run instance (~$1.50/mo) |
| Full staging always-on | ~$380-520 | Above + Dataflow streaming + GKE always-on |

The environment parks at near-zero cost between sessions via feature flags in `staging.tfvars`:

```hcl
create_dataflow_job      = false  # Stops streaming job — $0 compute
enable_gke               = false  # Destroys cluster — $0 nodes + management fee
predictor_min_instances  = 0      # Cloud Run scales to zero — $0 idle
```

Billing budget alerts fire at 25%, 50%, and 100% of a configurable monthly cap, with a second alert at 100% of forecasted spend — configured via Terraform in `main.tf`.

---

## Cost Incident & Resolution

*This section was corrected on 2026-07-03 after auditing the actual billing-report data. An earlier version attributed the spike primarily to GKE; the billing SKU breakdown shows that was wrong.*

**What happened.** During the June 2026 staging build-out (June 9–13), account spend jumped from near-zero to $55–75/day, closing the month at **$393.01 — a 1,026% increase over May**. The dominant cost was a single SKU: **Vertex AI "Feature Store online serving node" — $271.14** of the month's total. The Terraform Vertex AI module hardcoded `online_serving_config { fixed_node_count = 3 }`, so every apply during the deployment push provisioned three always-on serving nodes. The billing report's usage column shows **288.45 node-hours** billed — 3 nodes × ~96 hours (almost exactly 4 days, June 9 → June 13) at $0.94/node-hour: ~$68/day, a ~$2,030/month run-rate — production-grade HA capacity, reserved around the clock, serving zero requests in a staging environment. GKE contributed a further ~$42 (management fee $7.58 plus node VMs billed under Compute Engine), and a Dataflow streaming job added ~$3/day. The spike was not caught in real time because no billing budget alerts had been configured, and manual teardowns didn't stick: applying the configuration for any other reason re-asserted the full blueprint, recreating the expensive resources.

**Root causes identified:**

*Provisioned-capacity pricing was misunderstood.* Feature Store online nodes bill per node-hour **reserved**, not per request served. "Usage cost" on the invoice was 100% idle standby. Every resource in a blueprint should be classified as billing-by-existence or billing-by-activity; the by-existence ones are the dangerous class.

*A production default hardcoded in a module.* `fixed_node_count = 3` made every environment — including staging — pay for HA online serving. Expensive settings must be variables with cheap defaults, not constants.

*No billing governance.* No `google_billing_budget` alerts, and no billing export feeding any automated response. The first signal was the invoice.

*Manual teardowns are drift, not fixes.* Resources switched off by hand outside Terraform were "corrected" back on by the next apply. The safe state must live in the committed configuration.

*`on_delete = "drain"` on a streaming Dataflow job.* A teardown attempt hung for 2h24m waiting for a running streaming job to drain, extending the billing window.

**What was fixed (remediation 2026-06-13 — daily spend collapsed from ~$55 to under $1 the following day):**

- Parameterized Feature Store nodes: `featurestore_online_node_count` with a committed default of **0** (offline store retained; online capacity scaled up only for demos)
- Added `google_billing_budget` with alerts at 25%, 50%, 100%, and 100%-forecasted of a $50/month cap, wired to email notifications
- Built the cost-guard kill-switch: an hourly Cloud Function that tears down billable workloads (reversibly) if daily spend exceeds $50 — rehearsed in DRY_RUN at simulated $75 before arming
- Changed Dataflow `on_delete` to a per-environment variable — staging uses `"cancel"`, production `"drain"`
- Replaced GKE with Cloud Run as the staging serving layer ($0 idle); `enable_gke = false` is the committed default, with on-demand `gke-up`/`gke-down` workflows
- Added `trap teardown EXIT` to the Phase 4 test script so teardown runs on success, failure, and Ctrl+C
- Audited all GCP projects on the billing account for unexpected running resources

**The takeaway.** Billing governance is infrastructure, not an afterthought — and the off switch has to live in the code. Every expensive resource now defaults to zero/disabled in committed config, so a `terraform apply` re-asserts that things are *off* instead of resurrecting them. The corrected diagnosis is itself part of the lesson: the original write-up blamed the most visible resource (GKE) rather than the most expensive one, because the SKU-level billing data hadn't been audited. Conclusions about incidents should come from the billing export, not from memory of what was being worked on at the time.

---

## CI/CD Pipeline

**`ci.yml` — runs on every PR:**
- `terraform fmt -check` — enforces consistent HCL formatting
- `terraform validate` — catches syntax errors and missing variables
- `terraform plan` — posts the full diff as a PR comment
- Docker build — verifies the predictor image compiles clean
- Predictor unit tests — validates prediction logic without live GCP access

**`cd.yml` — runs on merge to main:**
- Builds predictor Docker image tagged with git SHA (immutable, traceable)
- Pushes to Artifact Registry
- Deploys to Cloud Run via `gcloud run deploy` (zero-downtime rolling update)
- Smoke tests the live endpoint: `/healthz`, `/readyz`, `/v1/predict`
- Posts deployment summary to the commit

Authentication uses Workload Identity Federation — no service account JSON keys stored as GitHub secrets.

---

## How to Deploy

### Prerequisites

- GCP project with billing enabled
- `gcloud` CLI authenticated
- Terraform >= 1.5
- Docker with `linux/amd64` build support

### 1. Initialize Terraform

```bash
cd infrastructure/terraform
terraform init -backend-config="bucket=${PROJECT_ID}-tf-state"
```

### 2. Deploy base infrastructure

```bash
terraform apply -var-file=staging.tfvars
```

### 3. Build and push the predictor image

```bash
cd ../..
docker build --platform linux/amd64 \
  -t us-central1-docker.pkg.dev/${PROJECT_ID}/stg-ml-containers/predictor:latest \
  -f vertex_ai/serving/Dockerfile .
docker push us-central1-docker.pkg.dev/${PROJECT_ID}/stg-ml-containers/predictor:latest
```

### 4. Update staging.tfvars and redeploy

```hcl
predictor_image      = "us-central1-docker.pkg.dev/<PROJECT>/stg-ml-containers/predictor:latest"
create_dataflow_job  = true
```

```bash
terraform apply -var-file=staging.tfvars
terraform output predictor_url   # Live HTTPS endpoint
```

### 5. Test the live endpoint

```bash
SERVICE_URL=$(terraform output -raw predictor_url)

curl ${SERVICE_URL}/healthz
# {"status": "ok", "model_version": "staging-seed-v1"}

curl -X POST ${SERVICE_URL}/v1/predict \
  -H "Content-Type: application/json" \
  -d '{"instances": ["user_001", "user_002"]}'
# {"predictions": [{"user_id": "user_001", "churn_risk_score": 0.72, "label": "high_risk"}, ...]}
```

### Park the environment

```bash
# staging.tfvars: create_dataflow_job = false
# predictor_min_instances = 0 (Cloud Run already scales to zero automatically)
terraform apply -var-file=staging.tfvars
```

---

## GitHub Actions Setup

Add these secrets under **Settings → Secrets and variables → Actions**:

| Secret | Value |
|--------|-------|
| `GCP_PROJECT_ID` | Your GCP project ID |
| `GCP_REGION` | `us-central1` |
| `GCP_WORKLOAD_IDENTITY_PROVIDER` | From WIF setup (see below) |
| `GCP_SERVICE_ACCOUNT` | `github-actions@<PROJECT>.iam.gserviceaccount.com` |
| `TF_STATE_BUCKET` | `<PROJECT>-tf-state` |
| `BILLING_ACCOUNT_ID` | Your GCP billing account ID |
| `ARTIFACT_REGISTRY_REPO` | `stg-ml-containers` |

**Workload Identity Federation setup (one-time):**

```bash
# Create WIF pool
gcloud iam workload-identity-pools create "github-actions" \
  --project="${PROJECT_ID}" --location="global"

# Create OIDC provider
gcloud iam workload-identity-pools providers create-oidc "github" \
  --project="${PROJECT_ID}" \
  --location="global" \
  --workload-identity-pool="github-actions" \
  --attribute-mapping="google.subject=assertion.sub,attribute.repository=assertion.repository" \
  --issuer-uri="https://token.actions.githubusercontent.com"

# Allow this repo to impersonate the CI/CD service account
gcloud iam service-accounts add-iam-policy-binding \
  "github-actions@${PROJECT_ID}.iam.gserviceaccount.com" \
  --role="roles/iam.workloadIdentityUser" \
  --member="principalSet://iam.googleapis.com/projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/github-actions/attribute.repository/YOUR_GITHUB_USERNAME/gcp-data-platform"
```

---

*Terraform · Apache Beam · Dataflow · BigQuery · Vertex AI · Cloud Run · GitHub Actions · Workload Identity Federation*
