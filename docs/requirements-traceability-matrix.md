# Requirements Traceability Matrix — GCP Data Platform

**Status:** Living document
**Source of requirements:** `architecture/system-design.md` §1 (Functional Requirements, Non-Functional Requirements, Constraints)
**Purpose:** Trace every requirement to (a) the implementation artifacts that satisfy it, (b) the verification method that proves it, and (c) its current verification status. A requirement without a verification path is treated as unmet.

Verification status legend:

| Status | Meaning |
|--------|---------|
| ✅ Verified | Automated test or deployed monitor exists and passes today |
| 🟡 Monitored | Enforced by a deployed SLO/alert; violated only if the monitor fires |
| 🔶 Planned | Verification method defined but not yet executed (e.g., load test) |
| ⚠️ Known gap | Deliberate trade-off; see Gap Register at the end |

---

## 1. Non-Functional Requirements

### NFR-1 — Availability: 99.9% (ingestion + serving endpoints)

| Aspect | Artifact | Detail |
|--------|----------|--------|
| Managed-service substrate | `infrastructure/terraform/modules/pubsub/main.tf`, `modules/dataflow/main.tf` | Pub/Sub and Dataflow are regional managed services; Dataflow auto-restarts workers from checkpoints |
| Serving health probes | `vertex_ai/serving/predictor.py` — `/healthz`, `/readyz` | Readiness gated on model load; liveness independent of dependencies |
| Bridge health probes | `infrastructure/terraform/main.tf` — `google_cloud_run_v2_service.bridge` | `startup_probe` (6×5s) + `liveness_probe` (30s period) on `/healthz` |
| Availability SLO | `monitoring/slos/slo_definitions.yaml` | Freshness SLO 99.5%, serving latency SLO 99.9%, rolling 30-day error budgets |
| Alerting | `monitoring/alerts/alert_policies.yaml` | DLQ spike, pipeline lag, error-rate policies wired to notification channel |

**Verification**

| Method | Evidence | Status |
|--------|----------|--------|
| Unit tests: probe contracts | `vertex_ai/tests/test_predictor.py::TestProbes` (healthz 200, readyz 200/503 on model state) | ✅ Verified |
| SLO monitors in staging | `monitoring/slos/slo_definitions.yaml` deployed via monitoring module | 🟡 Monitored |
| Staging scale-to-zero trade-off | `staging.tfvars` sets `predictor_min_instances = 0` → cold starts (~15s) violate 99.9% during idle-wake. Production profile requires `min_instances >= 1` | ⚠️ Known gap (staging-only, intentional cost decision) |

### NFR-2 — Throughput: 500K events/sec peak

| Aspect | Artifact | Detail |
|--------|----------|--------|
| Elastic ingestion | `modules/pubsub/main.tf` | Pub/Sub requires no pre-provisioning; scales with publishers |
| Elastic processing | `modules/dataflow/main.tf` — `max_workers`, `machine_type`, `enable_streaming_engine = "true"` | Horizontal autoscaling to `var.dataflow_max_workers`; Streaming Engine offloads shuffle |
| Hot-key avoidance | `pipelines/dataflow/transforms/dedup.py` — `build_dedup_key()` | Dedup state keyed per-user (not global) to avoid single-key bottleneck; rationale documented in the docstring |
| Sizing analysis | `architecture/system-design.md` §4.1 | 500 MB/s peak → ~40 n1-standard-4 workers; BQ streaming supports 1M rows/sec/table |

**Verification**

| Method | Evidence | Status |
|--------|----------|--------|
| Unit tests: per-key dedup scoping | `pipelines/dataflow/tests/test_dedup.py::TestStatefulDeduplication::test_dedup_scope_is_per_key` | ✅ Verified (design property) |
| Load test to 500K events/sec, autoscale response < 90s | `architecture/decisions/ADR-002-dataflow-vs-spark.md` §Verification item 6 (unchecked) | 🔶 Planned |

Throughput at the stated peak is an **analytical claim verified by design review, not yet by measurement**. The load-test plan in ADR-002 is the closing action; `scripts/test_ingest.py` provides the synthetic producer.

### NFR-3 — Processing latency: p99 < 30s (publish → BQ write)

| Aspect | Artifact | Detail |
|--------|----------|--------|
| Streaming (not micro-batch) pipeline | `pipelines/dataflow/event_processor.py` | Continuous streaming with 1-min tumbling aggregation windows, 30s allowed lateness (`transforms/aggregate.py`) |
| Freshness SLO | `monitoring/slos/slo_definitions.yaml` — `event_processing_freshness` | SLI: `dataflow system_lag < 30s`, target 99.5% over 30 days |
| Lag alerting | `monitoring/alerts/alert_policies.yaml` | Pipeline-lag policy at 300s (5 min) hard threshold |

**Verification**

| Method | Evidence | Status |
|--------|----------|--------|
| SLO monitor on `system_lag` | Deployed via monitoring module | 🟡 Monitored |
| Window/lateness semantics | `pipelines/dataflow/tests/test_aggregate.py` (CombineFn lifecycle, window row formatting) | ✅ Verified (logic level) |

### NFR-4 — Prediction latency: p99 < 100ms (feature fetch + inference)

| Aspect | Artifact | Detail |
|--------|----------|--------|
| Online feature store | `modules/vertex_ai/` + `vertex_ai/serving/predictor.py::FeatureStoreClient` | Single batched read per request; <10ms online lookup design |
| No hot-path I/O | `predictor.py` §4–5 | Prediction logging is async (buffered, background thread); Pub/Sub publish is fire-and-forget — neither delays the response |
| Latency instrumentation | `predictor.py` — `latency_ms` in every response and every prediction-log row | Enables p99 measurement from `prediction_logs` in BigQuery |
| Latency SLO | `monitoring/slos/slo_definitions.yaml` — `prediction_serving_latency` | p99 < 100ms, target 99.9% |

**Verification**

| Method | Evidence | Status |
|--------|----------|--------|
| Unit tests: hot-path isolation | `vertex_ai/tests/test_predictor.py::TestHighRiskPublishing::test_publish_failure_does_not_fail_the_prediction` | ✅ Verified |
| Unit tests: async logger never raises | `test_predictor.py::TestAsyncPredictionLogger` (retry ×3, then drop, no exception) | ✅ Verified |
| Load test endpoint to 1K rps, p99 < 100ms | `architecture/decisions/ADR-004-vertex-ai-serving-strategy.md` §Verification item 5 (unchecked) | 🔶 Planned |

### NFR-5 — Durability: zero data loss

| Aspect | Artifact | Detail |
|--------|----------|--------|
| Exactly-once ingestion | `modules/pubsub/main.tf` — `enable_exactly_once_delivery = true`; `modules/dataflow/main.tf` — Streaming Engine ("Required for exactly-once Pub/Sub") | |
| Poison-message containment | `modules/pubsub/main.tf` — `dead_letter_policy`, `max_delivery_attempts`; DLQ retention 7 days | Bad events are quarantined, never dropped |
| DLQ routing in code | `pipelines/dataflow/transforms/parse.py` — parse failures and validation failures emit tagged DLQ records with original bytes preserved | |
| Enrichment retry | `vertex_ai/bridge/main.py` — 500 (nack) on transient failure; `main.tf` push subscription `retry_policy` 10s→300s backoff, 7-day retention | |
| Documented at-least-once boundary | `transforms/dedup.py` module docstring | Events arriving >24h late may be written twice; full idempotency deferred to BQ `MERGE` |

**Verification**

| Method | Evidence | Status |
|--------|----------|--------|
| Unit tests: DLQ routing + payload preservation | `test_parse.py::TestParseAvro::test_undecodable_bytes_route_to_dlq`, `::test_dlq_record_preserves_original_bytes`, `::test_truncated_message_routes_to_dlq` | ✅ Verified |
| Unit tests: every validation rule DLQs (never silently drops) | `test_parse.py::TestValidateSchema` (13 cases) | ✅ Verified |
| Unit tests: ack/nack contract | `vertex_ai/tests/test_bridge.py::TestTransientFailures` (500 on Supabase/BQ failure → Pub/Sub retry), `::TestMalformedMessages` (200 on poison payloads → no infinite retry) | ✅ Verified |
| E2E signal-chain run | Synthetic event → bridge → Supabase → BQ row landed (2026-06-16, documented in README) | ✅ Verified (point-in-time) |
| Caveat: prediction logs are best-effort | `predictor.py::AsyncPredictionLogger` drops rows after 3 retries by design — prediction logs are observability data, not source-of-truth | ⚠️ Known gap (accepted) |

### NFR-6 — Data freshness: < 5 minutes (feature snapshots)

| Aspect | Artifact | Detail |
|--------|----------|--------|
| 1-minute aggregation windows | `transforms/aggregate.py` — `FixedWindows(60)`, `allowed_lateness=30`, ACCUMULATING mode | |
| BQ flush cadence | `transforms/aggregate.py` — `triggering_frequency=60` on `WriteToBigQuery` | Worst case ≈ 1 min window + 1 min flush ≪ 5 min |
| Reference-data staleness bound | `transforms/enrich.py` docstring — GCS side input, staleness acceptable at daily change rate | |

**Verification:** window semantics unit-tested (`test_aggregate.py`); end-to-end freshness is subsumed by the NFR-3 freshness SLO. 🟡 Monitored.

### NFR-7 — Cost: optimized

| Aspect | Artifact | Detail |
|--------|----------|--------|
| Partition pruning enforced | `modules/bigquery/main.tf` — `require_partition_filter = true`, daily partitioning, clustering (`event_type`,`user_id` / `user_id`,`event_type`,`country`) | Full-table scans are impossible, not just discouraged |
| Scale-to-zero serving | `modules/cloud_run/main.tf` — `min_instance_count` var (0 in staging) | $0 idle predictor |
| Kill-switch | `modules/cost-guard/` — hourly Cloud Function vs. BigQuery billing export, >$50/day → reversible teardown | |
| Hard budget alerts | `main.tf` — `google_billing_budget` at 25/50/100/100-forecast % | |
| Slot governance | `modules/bigquery/main.tf` + alert at 90% of 500 slots (`alert_policies.yaml`) | |

**Verification**

| Method | Evidence | Status |
|--------|----------|--------|
| Unit tests: threshold, DRY_RUN, teardown isolation, IAM-preserving scale-down | `infrastructure/terraform/modules/cost-guard/function/tests/test_cost_guard.py` (31 cases) | ✅ Verified |
| Live rehearsal | DRY_RUN trip at simulated $75: all 4 teardown actions planned, zero errors (2026-06-16) | ✅ Verified (point-in-time) |
| Real incident hardening | README "Cost Incident & Resolution" — $336 event, root causes, fixes | ✅ Verified by fire |

---

## 2. Functional Requirements

| FR | Requirement | Implementation | Verification | Status |
|----|-------------|----------------|--------------|--------|
| FR-1 | Event ingestion at any volume | `modules/pubsub/` (topic, schema-validated Avro, DLQ) | Schema registry enforces Avro at publish; `scripts/test_ingest.py` synthetic producer | ✅ / 🟡 |
| FR-2 | Validate, dedup, enrich, window in near real-time | `pipelines/dataflow/transforms/{parse,dedup,enrich,aggregate}.py` | 64 unit tests, 100% coverage on parse/dedup/enrich, 93% aggregate (`pipelines/dataflow/tests/`) | ✅ Verified |
| FR-3 | Query-optimized analytical storage | `modules/bigquery/` — bronze/silver/gold datasets, partitioning + clustering + retention | Terraform validate/plan in CI; partition filter is schema-enforced | ✅ Verified |
| FR-4 | Consistent features, no training-serving skew | Vertex AI Feature Store (`modules/vertex_ai/`); same store feeds training (offline) and predictor (online) | Single feature list `ALL_FEATURES` shared: `trainer/model.py` → imported by `predictor.py` | ✅ Verified (by construction) |
| FR-5 | Reproducible, tracked training | `vertex_ai/training/trainer/` — eval gate (AUC ≥ 0.85 in `ModelConfig`), versioned artifacts | Eval thresholds are training-blocking constants | 🟡 Monitored |
| FR-6 | Sub-100ms online inference with traffic management | `predictor.py` — batch ≤500, shadow-traffic tagging (`_shadow` → logged, never published) | `test_predictor.py::TestPredictionLogging::test_shadow_traffic_is_tagged`, `::TestHighRiskPublishing::test_shadow_traffic_never_publishes` | ✅ Verified |
| FR-7 | Full observability | `monitoring/` SLOs + alerts; Beam metrics counters in every transform; `latency_ms`/`features_json` in prediction logs | Counters asserted implicitly by transform tests; SLO deploy via Terraform | 🟡 Monitored |

---

## 3. Constraints

| Constraint | Implementation | Verification | Status |
|------------|----------------|--------------|--------|
| Single-project blast radius | All modules take one `var.project_id`; no cross-project references | `terraform validate` + code review | ✅ |
| No public IPs on data workers | `modules/networking/` — private subnet + Private Google Access; Dataflow workers VPC-only | Terraform plan review | ✅ |
| Secrets via Secret Manager only | `main.tf` — Supabase URL/key as `secret_key_ref` env mounts; values never in Terraform state or git; `staging.tfvars` carries placeholder billing ID | CI has no secret values; README security section; `.gitignore` | ✅ |
| Encryption at rest / in transit | GCP-managed CMEK-ready defaults; TLS on all service calls | Platform default; no plaintext transport configured anywhere | ✅ |
| Least-privilege IAM | `modules/iam/` — per-service SAs; topic-scoped publisher grant (`predictor_churn_publisher`); invoker SA separated from runtime SA for the bridge | `test_cost_guard.py::TestCloudRunIam` proves teardown preserves non-public IAM members | ✅ |

---

## 4. Gap Register (honest accounting)

| # | Gap | Impact | Closing action |
|---|-----|--------|----------------|
| G-1 | Load tests (ADR-002 item 6, ADR-004 item 5) not yet executed | NFR-2 and NFR-4 rest on analysis, not measurement | Run `scripts/test_ingest.py` ramp + Cloud Run load test; record results in ADR verification checklists |
| G-2 | Staging predictor scales to zero → cold-start latency violates NFR-1/NFR-4 while idle | Staging only; conscious $0-idle trade-off | Production tfvars: `predictor_min_instances >= 1`; documented in `modules/cloud_run/main.tf` header |
| G-3 | Dedup is at-least-once beyond the 24h state window | Duplicate rows possible for >24h-late events | BQ `MERGE`-based idempotent load for the silver layer (documented in `dedup.py` docstring) |
| G-4 | Prediction logs drop after 3 BQ retries | Monitoring data loss under sustained BQ outage | Accepted: logs are not source-of-truth; alternative (blocking writes) would violate NFR-4 |
| G-5 | `event_processor.py` (pipeline assembly) has no unit coverage | Wiring errors surface only at runner submission | Covered by staging pipeline launch; candidate for a DirectRunner smoke test |
| G-6 | Pseudonymization happens at the analytics boundary, not at ingest | Raw `user_id` lands in `raw_events` (IAM-restricted, 90-day expiry); analyst access goes through the SHA256 `user_id_pseudonymized` view (`modules/bigquery/main.tf`). §5.2 amended 2026-07-02 to state this accurately — the *docs-vs-reality* gap is closed; ingest-time DLP tokenization remains a production-hardening item | Add a DLP/hash DoFn before the raw BQ write when moving beyond staging |

---

*Maintenance rule: any PR that changes a transform, module, or SLO must update the corresponding row. A requirement whose verification column goes stale is treated as unverified.*
