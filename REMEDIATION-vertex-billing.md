# Remediation Runbook — Vertex AI Feature Store Billing ($245.06)

**Project:** `project-6db0f664-1423-47cb-86d` (display name: "My First Project")
**Region:** `us-central1`
**Date:** 2026-06-13
**Root cause:** `infrastructure/terraform/modules/vertex_ai/main.tf` provisions a legacy Vertex AI
Feature Store with `online_serving_config { fixed_node_count = 3 }`. Three online-serving nodes
have run continuously since ~June 1, billing ~$0.26/node-hr (3 × 24h × ~13d ≈ 936 node-hrs ≈ $245).

> **ADDENDUM 2026-07-03 — timeline and rate corrected against the billing report.**
> The root-cause identification above (3 hardcoded online-serving nodes) is correct, but the
> estimate is wrong in two ways that roughly cancel out in dollars:
> - **Start date:** the billing report shows Vertex AI charges near-zero June 1–8, spiking June 9.
>   The nodes were created during the June 9–13 deployment push, not on June 1.
> - **Rate and duration (settled by the usage column, 2026-07-07):** the SKU "Feature Store
>   online serving node" billed **$271.14 for 288.45 node-hours** — i.e. **$0.94/node-hr**
>   (matches the published us-central1 rate), not $0.26. 288.45 ÷ 3 nodes = **~96 hours ≈ 4
>   days per node**: the nodes ran June 9 → June 13, not June 1 → 13.
> Month totals (verified in Billing → Reports, June 1–30): **$393.01 account-wide (+1,026% vs
> May)**; Vertex AI $271.63 of which $271.14 was the single online-serving-node SKU; GKE ~$42
> combined (mgmt fee $7.58 + node VMs under Compute Engine). Remediation below executed
> 2026-06-13; daily spend collapsed to <$1 on June 14. Lesson recorded in the README: diagnose
> from SKU-level billing data, not from estimates.

> ⚠️ The state file on disk is `errored.tfstate` — a local dump from a failed apply. Do **not**
> trust it as ground truth. Every step below verifies against live GCP first.

---

## Set shell variables

```bash
export PROJECT_ID="project-6db0f664-1423-47cb-86d"
export REGION="us-central1"
gcloud config set project "$PROJECT_ID"
gcloud auth list   # confirm you're the right identity
```

---

## STEP 1 — Verify what is actually live (read-only)

```bash
# Feature Store (the $245 driver) — legacy Featurestore API
gcloud ai featurestores list --region="$REGION" --project="$PROJECT_ID" \
  || gcloud beta ai featurestores list --region="$REGION" --project="$PROJECT_ID"

# Scheduler job (daily pipeline trigger — sleeper cost)
gcloud scheduler jobs list --location="$REGION" --project="$PROJECT_ID"

# GKE (expected: control plane only, no node pools)
gcloud container clusters list --project="$PROJECT_ID"
gcloud container node-pools list --cluster=stg-gke --region="$REGION" --project="$PROJECT_ID"

# BigQuery reservation (expected: 0 baseline slots)
gcloud bq reservations list --project="$PROJECT_ID" --location="$REGION" 2>/dev/null \
  || bq ls --reservation --project_id="$PROJECT_ID" --location="$REGION"

# Cloud NAT
gcloud compute routers nats list --router=stg-vpc-nat-router --region="$REGION" --project="$PROJECT_ID"

# Confirm the endpoint was NOT created (expected: empty)
gcloud ai endpoints list --region="$REGION" --project="$PROJECT_ID"
```

Record the Feature Store ID from the first command — you need it in Step 2.

---

## STEP 2 — Stop the bleeding immediately (surgical)

Do this first, regardless of whether you later keep or tear down staging. Order matters:
pause the trigger before deleting its dependencies.

### 2a. Pause the daily retraining trigger
```bash
gcloud scheduler jobs pause stg-daily-retraining --location="$REGION" --project="$PROJECT_ID"
```

### 2b. Kill Feature Store online-serving billing
The legacy Featurestore bills per online node-hour. You cannot set this resource to a free state
while keeping the resource around for serving — so either drop online nodes to 0 (offline-only) or
delete it outright. For staging, delete is cleanest.

```bash
# Capture the ID
FS_ID=$(gcloud ai featurestores list --region="$REGION" --project="$PROJECT_ID" \
        --format="value(name)" | head -n1)
echo "Feature Store: $FS_ID"

# Delete (removes entity types + features too)
gcloud ai featurestores delete "$FS_ID" --region="$REGION" --project="$PROJECT_ID" --force
#   ^ if 'gcloud ai' errors, prefix with 'gcloud beta ai'
```

Verify the node charge has stopped:
```bash
gcloud ai featurestores list --region="$REGION" --project="$PROJECT_ID"   # expect empty
```

> This is the single action that ends the $245/period accrual. Everything below is cleanup and
> prevention.

---

## STEP 3 — Decide: keep staging, or full teardown

### Option A — You are NOT actively using staging → tear it all down
The local state is untrustworthy, so reconcile before destroying.

```bash
cd infrastructure/terraform

# Point Terraform at the GCS backend and pull the real state
terraform init -reconfigure
terraform state list          # see what Terraform actually tracks
terraform plan                # review drift between code and live infra
```

- If `terraform plan` reads cleanly, run a targeted destroy (Feature Store already gone in Step 2,
  so destroy the rest):
  ```bash
  terraform destroy
  ```
- If state is broken / out of sync (likely, given `errored.tfstate`), delete resources directly and
  skip Terraform for now:
  ```bash
  gcloud scheduler jobs delete stg-daily-retraining --location="$REGION" --project="$PROJECT_ID" --quiet
  gcloud container clusters delete stg-gke --region="$REGION" --project="$PROJECT_ID" --quiet
  gcloud compute routers nats delete stg-vpc-nat --router=stg-vpc-nat-router --region="$REGION" --project="$PROJECT_ID" --quiet
  # buckets (empty them first if needed)
  for B in dataflow-staging ml-artifacts reference-data; do
    gsutil rm -r "gs://${PROJECT_ID}-${B}" 2>/dev/null || true
  done
  ```
  Then clean up state so the next apply starts fresh:
  ```bash
  rm -f errored.tfstate
  # if a GCS backend object exists, archive it:
  gsutil mv "gs://${PROJECT_ID}-tf-state/<state-path>" "gs://${PROJECT_ID}-tf-state/_archived/" 2>/dev/null || true
  ```

### Option B — You WANT to keep iterating on staging → make it cheap
Edit `infrastructure/terraform/modules/vertex_ai/main.tf` so staging never runs 3 HA nodes again.
See Step 4 — apply that change, then re-create the Feature Store only when you actively need online
serving for a test.

---

## STEP 4 — Fix the root cause so it cannot recur

The hardcoded `fixed_node_count = 3` is the real bug: it makes *every* environment pay for
production-grade HA. Parameterize it and default staging to 0.

In `modules/vertex_ai/variables.tf`:
```hcl
variable "featurestore_online_node_count" {
  description = "Online serving nodes for the Feature Store. 0 = offline-only (no node-hour billing)."
  type        = number
  default     = 0
  validation {
    condition     = var.featurestore_online_node_count >= 0 && var.featurestore_online_node_count <= 10
    error_message = "featurestore_online_node_count must be between 0 and 10."
  }
}
```

In `modules/vertex_ai/main.tf`:
```hcl
resource "google_vertex_ai_featurestore" "platform" {
  provider = google-beta
  name     = "${replace(var.resource_prefix, "-", "_")}_feature_store"
  project  = var.project_id
  region   = var.region

  online_serving_config {
    fixed_node_count = var.featurestore_online_node_count
  }
  # ... labels unchanged
}
```

In `staging.tfvars`:
```hcl
featurestore_online_node_count = 0     # offline-only in staging
```

In production tfvars (only where you truly need low-latency online serving):
```hcl
featurestore_online_node_count = 3
```

Also fix the unconditional scheduler: gate `google_cloud_scheduler_job.daily_retraining` behind a
`var.enable_daily_retraining` flag defaulting to `false` for staging, so a half-finished staging
apply never launches nightly pipeline jobs.

---

## STEP 5 — Add a guardrail (so the next surprise is caught in hours, not weeks)

```bash
# Budget with email alerts at 50/90/100% — requires the billing account ID
gcloud billing accounts list
gcloud billing budgets create \
  --billing-account=<BILLING_ACCOUNT_ID> \
  --display-name="data-platform-staging-guard" \
  --budget-amount=50USD \
  --threshold-rule=percent=0.5 \
  --threshold-rule=percent=0.9 \
  --threshold-rule=percent=1.0 \
  --filter-projects="projects/$PROJECT_ID"
```

Consider also a Cloud Monitoring alert on the `aiplatform.googleapis.com` Feature Store node metric.

---

## STEP 6 — Verify it's actually fixed

```bash
# No online-serving nodes anywhere
gcloud ai featurestores list --region="$REGION" --project="$PROJECT_ID"

# Scheduler paused or deleted
gcloud scheduler jobs list --location="$REGION" --project="$PROJECT_ID"
```

Then, 24–48h later, open **Billing → Reports**, filter to this project + Vertex AI, and confirm the
daily run-rate for "Feature Store online serving node" has dropped to $0. Do not consider this closed
until the billing graph flattens — the resource-list check alone doesn't prove the meter stopped.

---

## Summary of cost-relevant resources (from state serial 13)

| Resource | Name | Billing | Action |
|---|---|---|---|
| Vertex AI Feature Store | `stg_feature_store` (3 online nodes) | **Continuous — the $245** | Delete now (Step 2b) |
| Cloud Scheduler job | `stg-daily-retraining` | Triggers nightly pipeline runs | Pause now (Step 2a) |
| GKE cluster | `stg-gke` (no node pools) | Control plane only (minor/free) | Delete in teardown |
| BigQuery reservation | `stg-reservation` (0 baseline slots) | ~$0 idle, usage-based | Optional |
| Cloud NAT | `stg-vpc-nat` | ~$1/day gateway | Delete in teardown |
| Storage buckets ×3 | `*-dataflow-staging` etc. | Minor | Delete in teardown |
| Vertex AI endpoint | `churn_risk` | **Not created** | None |

---

## RESOLUTION LOG (2026-06-13) — what was actually done

**Outcome:** Billing stopped, root cause fixed in code, environment parked safely. Verified against the live GCP console + billing report.

**Live actions taken (by hand, via gcloud):**
- Deleted the Feature Store, Cloud NAT, and GCS buckets.
- Deleted the `stg-daily-retraining` scheduler job.
- Created the $50 billing budget (active).
- Manually deleted the GKE cluster (background op).
- Billing report confirmed the Vertex AI "Feature Store online serving node" daily run-rate dropped to $0 after Jun 13. Final charge: ~$245.48 (Jun 9–13).

**Code changes applied (verified `terraform validate` = success on Terraform 1.5.7):**
- `featurestore_online_node_count` parameterized; **staging = 0** (offline-only, no node billing). `fixed_node_count = 0` is a valid GCP value (disables online serving).
- `enable_online_endpoint` and `enable_daily_retraining` flags added (`count` gating, both **default false**, staging false). Scheduler `precondition` requires an endpoint. Module outputs guarded for absent endpoint.

**State reconciliation — Option A (restore) chosen and completed:**
- The previously-archived state was **moved back** to the GCS backend path. Terraform now tracks reality again. (Lesson: archiving state does not delete resources — it strands them as orphans and makes the next apply collide. Don't leave state archived.)
- Reconciled plan = **38 to add, 1 to update** (GKE `node_locations` `a,b → a,f`, in-place). Confirmed via `terraform show -json`: scheduler is NOT recreated; Feature Store create is at `fixed_node_count = 0`.

**Cleanup:** all stale `tfplan-*.out` files deleted (including the dangerous `tfplan-final-sync.out`, a 104-create collision plan).

### Known drift / caveat
GKE was deleted by hand but is still managed in state, so a future plan will want to recreate it. **Do not reuse any saved plan file to resume** — saved plans are point-in-time, go stale, and assume GKE still exists.

### Correct RESUME procedure (regenerate every time)
```bash
cd infrastructure/terraform
terraform init -reconfigure -backend-config="bucket=project-6db0f664-1423-47cb-86d-tf-state"
terraform plan -var-file=staging.tfvars -out=tfplan-new.out   # re-reads reality
# review, then:
terraform apply tfplan-new.out
```
Set `featurestore_online_node_count` / `enable_online_endpoint` / `enable_daily_retraining` deliberately before resuming online serving — those three drive Vertex AI cost.
