# System Design: GCP Production Data Platform

**Status:** Accepted  
**Date:** 2026-04-21  
**Author:** Ves Mobley  
**Scope:** End-to-end event streaming, analytics, and ML serving platform on GCP

---

## 1. Requirements

### 1.1 Functional Requirements

1. **Event Ingestion** — Accept structured events (user actions, system telemetry, transactions) from heterogeneous producers at any volume.
2. **Stream Processing** — Validate, deduplicate, enrich, and window events in near real-time.
3. **Analytical Storage** — Store raw and processed events in a query-optimized, cost-efficient analytical store.
4. **Feature Engineering** — Produce and serve consistent features for both training and online inference (no training-serving skew).
5. **Model Training** — Train, evaluate, and version ML models on a reproducible, tracked pipeline.
6. **Online Inference** — Serve sub-100ms predictions to downstream services with traffic management (canary/shadow).
7. **Observability** — Full pipeline visibility: throughput, latency, error rates, model drift, data quality.

### 1.2 Non-Functional Requirements

| NFR | Target | Measurement |
|-----|--------|-------------|
| Availability | 99.9% (three nines) | Uptime of ingestion + serving endpoints |
| Throughput | 500K events/sec peak | Pub/Sub + Dataflow auto-scale |
| Processing latency | p99 < 30s | Pub/Sub publish → BigQuery write confirmed |
| Prediction latency | p99 < 100ms | Feature fetch + model inference |
| Durability | Zero data loss | Pub/Sub ack + Dataflow exactly-once + BQ inserts |
| Data freshness | < 5 minutes | For feature snapshots consumed by batch inference |
| Cost | Optimized | Slot reservations, partition pruning, Spot VMs for training |

### 1.3 Constraints

- All compute must remain within a single GCP project (single blast radius scope)
- No public IPs on data workers (VPC-only, Private Google Access)
- Secrets via Secret Manager (no plaintext credentials in code or IaC)
- Compliance: data at rest and in transit encrypted; PII fields tokenized before storage

---

## 2. High-Level Component Design

### 2.1 Data Flow

```
Producers
    │
    │ Publish (Avro + schema registry lookup)
    ▼
[Pub/Sub: events-topic]
    │
    │ Streaming pull (exactly-once)
    ▼
[Dataflow Pipeline]
    ├─ Parse + validate Avro
    ├─ Deduplicate (stateful, 24h event_id window)
    ├─ Enrich (GCS-cached reference data, max 5min TTL)
    ├─ Split: raw sink + windowed aggregation sink
    │
    ├─→ [BigQuery: raw_events] (partition: event_date, cluster: user_id, event_type)
    └─→ [BigQuery: event_aggregates] (tumbling 1-min windows)
         │
         │ Daily batch (Vertex AI Pipeline schedule)
         ▼
    [Feature Ingestion Job]
         │
         ▼
    [Vertex AI Feature Store]
    ├─ Offline store → Training data export
    └─ Online store → <10ms feature lookup
         │
         ▼
    [Vertex AI Training Pipeline]
    ├─ Data validation (TFDV)
    ├─ XGBoost training (Vertex Training Custom Job)
    ├─ Evaluation gate (AUC ≥ 0.85)
    └─ Model push → Vertex AI Model Registry
         │
         ▼
    [Vertex AI Online Endpoint]
    ├─ Production traffic (stable model)
    ├─ Canary traffic (challenger model, 10%)
    └─ Shadow traffic (log-only, no response impact)
         │
         ▼
    [BigQuery: prediction_logs] (model monitoring + retraining trigger)
```

### 2.2 Component Responsibilities

**Cloud Pub/Sub** — Managed, durable message queue. Provides at-least-once delivery with ordering keys for per-user ordering. Dead-letter topics absorb poison messages after 5 retries.

**Cloud Dataflow (Apache Beam)** — Stateful stream processing. Handles variable load via auto-scaling (1–200 workers). Guarantees exactly-once output via Dataflow's built-in checkpointing.

**BigQuery** — Columnar analytical warehouse. Three datasets: `raw` (append-only, 90-day retention), `processed` (curated, indefinite retention), `ml_features` (materialized feature snapshots). Slot reservations prevent cost surprises.

**Vertex AI Feature Store** — Eliminates training-serving skew by providing the same feature computation for training (offline) and inference (online). Features versioned with `feature_timestamp` for point-in-time correctness.

**Vertex AI Pipelines** — Orchestrates the ML lifecycle: data validation → training → evaluation → registry push → endpoint update. Each run produces a full artifact lineage graph in Vertex ML Metadata.

**Vertex AI Endpoints** — Managed online serving with autoscaling, health checks, and traffic splitting. Prediction requests logged to BigQuery for drift detection.

---

## 3. Data Models

### 3.1 Canonical Event Schema (Avro)

```json
{
  "type": "record",
  "name": "PlatformEvent",
  "namespace": "com.platform.events.v1",
  "fields": [
    { "name": "event_id",       "type": "string",  "doc": "UUID v4 — used for deduplication" },
    { "name": "event_type",     "type": "string",  "doc": "Enum: page_view | click | purchase | error" },
    { "name": "user_id",        "type": ["null","string"], "default": null },
    { "name": "session_id",     "type": "string" },
    { "name": "timestamp_ms",   "type": "long",    "doc": "Unix epoch milliseconds (UTC)" },
    { "name": "properties",     "type": { "type": "map", "values": "string" } },
    { "name": "schema_version", "type": "int",     "default": 1 }
  ]
}
```

### 3.2 BigQuery: raw_events

- **Partitioned** by `event_date` (DATE, daily) → prunes full-table scans
- **Clustered** by `event_type`, `user_id` → reduces bytes processed per query by 60–80%
- **Retention** 90 days (storage optimization, raw logs expire)

### 3.3 BigQuery: processed_events

- Deduplicated (no duplicate `event_id`)
- Enriched with user profile fields (country, cohort, plan_tier)
- Partitioned by `event_date`, clustered by `user_id`, `event_type`, `country`

### 3.4 Vertex AI Feature Store Entities

**Entity: `user`**

| Feature | Type | Freshness | Computation |
|---------|------|-----------|-------------|
| `event_count_7d` | INT64 | Daily | COUNT(events) last 7 days |
| `purchase_count_30d` | INT64 | Daily | COUNT WHERE event_type=purchase |
| `avg_session_duration_7d` | FLOAT64 | Daily | AVG session length |
| `last_active_days_ago` | INT64 | Daily | DATEDIFF(today, max(event_date)) |
| `country` | STRING | Weekly | From user profile |
| `plan_tier` | STRING | On-change | From CRM |

---

## 4. Scalability Analysis

### 4.1 Throughput Estimation

- **Peak ingest**: 500K events/sec × 1KB avg = 500 MB/s
- **Pub/Sub throughput**: auto-scales; no pre-provisioning required
- **Dataflow workers at peak**: ~40 n1-standard-4 workers (16 vCPUs, 60 GB RAM per worker)
- **BigQuery ingestion**: streaming inserts support 1M rows/sec per table; well within bounds
- **Daily volume**: ~43 billion events/day × 1KB = ~43 TB uncompressed; ~4.3 TB compressed (Snappy)

### 4.2 BigQuery Query Cost Control

1. **Partitioning** eliminates full-table scans → mandatory `WHERE event_date BETWEEN` clause
2. **Clustering** reduces bytes scanned within partitions by up to 80%
3. **Column selection** — no `SELECT *` in production queries; explicit column lists
4. **Materialized views** pre-aggregate daily cohort rollups → dashboards hit MV, not base table
5. **Slot reservations** (500 slots baseline) cap runaway queries; BI Engine for sub-second dashboard responses
6. **Table expiration** on `raw_events` (90 days) eliminates manual cleanup

### 4.3 Failure Modes and Mitigations

| Failure | Impact | Mitigation |
|---------|--------|-----------|
| Pub/Sub producer outage | No new events | Producers implement exponential backoff + local buffer |
| Dataflow worker crash | Processing pause | Dataflow auto-restarts from last checkpoint (≤ 1 min gap) |
| BigQuery API quota hit | Write backlog | Dataflow buffering; BQ write quota is 10GB/s by default |
| Feature Store unavailable | Stale features | Serving falls back to pre-computed fallback features |
| Model serving OOM | 503 responses | Autoscaling with min replicas = 2; circuit breaker in client |
| Schema evolution | Pipeline breakage | Schema Registry enforces backward compatibility; `schema_version` field |

---

## 5. Security Model

### 5.1 Identity and Access (Least Privilege)

| Service Account | Permissions |
|----------------|-------------|
| `sa-dataflow-worker` | `pubsub.subscriber`, `bigquery.dataEditor`, `storage.objectViewer` |
| `sa-pipeline-runner` | `aiplatform.user`, `bigquery.jobUser`, `storage.objectAdmin` |
| `sa-vertex-training` | `bigquery.dataViewer`, `aiplatform.customCodeServiceAgent` |
| `sa-vertex-serving` | `aiplatform.serviceAgent`, `bigquery.dataEditor` (prediction logs) |

### 5.2 Data Encryption

- **At rest**: Cloud-managed keys (CMEK optional for regulated data)
- **In transit**: TLS 1.3 enforced on all service-to-service calls
- **PII handling**: `user_id` is pseudonymized at the analytics boundary, not at ingest. `raw_events` stores the raw `user_id` (IAM-restricted dataset, 90-day partition expiry); analyst-facing access goes through an authorized view exposing `user_id_pseudonymized` (first 16 hex chars of SHA256 — see `modules/bigquery/main.tf`). Ingest-time tokenization via Cloud DLP is a production-hardening item, deliberately not implemented in staging (cost + latency on the hot path). Trade-off tracked as G-6 in `docs/requirements-traceability-matrix.md`.

### 5.3 Network Security

- All Dataflow workers run in private subnet (no public IPs)
- Private Google Access enables GCP service calls without internet egress
- VPC Service Controls perimeter isolates BigQuery and Vertex AI from external access
- Cloud Armor WAF protects any public-facing prediction API endpoints

---

## 6. Operational Runbooks

### 6.1 DLQ Spike Response

1. Alert fires when `dlq_message_count > 100` in 5-min window
2. Check Dataflow logs: `gcloud logging read "resource.type=dataflow_step AND textPayload:DLQ"`
3. Identify failing schema version via `properties.schema_version` in DLQ messages
4. If schema mismatch: coordinate with producer team, update Avro schema in registry
5. Replay DLQ after fix: `gcloud pubsub subscriptions seek events-dlq-sub --snapshot=pre-incident-snap`

### 6.2 Model Drift Response

1. Alert fires when `prediction_accuracy_7d < 0.75` (monitored via BigQuery scheduled query)
2. Inspect `prediction_logs` — check for feature distribution shift vs. training baseline
3. Trigger immediate retraining: `python vertex_ai/pipelines/run_pipeline.py --trigger=drift`
4. If new model passes evaluation gate: promote via `gcloud ai endpoints deploy-model`
5. If retraining fails: roll back to previous model version in registry

---

## 7. Cost Estimate (Monthly)

| Service | Config | Est. Cost |
|---------|--------|-----------|
| Pub/Sub | 43 TB messages/month | ~$430 |
| Dataflow | 40 workers × 730h × $0.056/vCPU-h | ~$1,644 |
| BigQuery storage | 4.3 TB/month processed + 43 TB raw (90d) | ~$215 |
| BigQuery compute | 500 slots reservation | ~$2,500 |
| Vertex AI Feature Store | 1M entities, 100K online reads/day | ~$400 |
| Vertex AI Training | 8 × A100 × 4h/day (daily retraining) | ~$960 |
| Vertex AI Serving | 2 × n1-standard-4 online endpoints | ~$350 |
| **Total** | | **~$6,499/month** |

*Optimizations: Spot VMs for training (60% discount), committed use discounts on Dataflow, BQ BI Engine for dashboards.*
