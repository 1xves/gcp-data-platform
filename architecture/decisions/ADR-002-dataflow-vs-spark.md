# ADR-002: Stream Processing — Dataflow vs. Spark Streaming vs. Flink

**Status:** Accepted  
**Date:** 2026-04-21  
**Deciders:** Platform Engineering Team

---

## Context

The platform requires a stateful, exactly-once stream processing engine that can:
- Parse and validate Avro-encoded events from Pub/Sub
- Perform stateful deduplication across a 24-hour sliding window
- Enrich events with reference data
- Write to BigQuery with exactly-once semantics
- Auto-scale from 2 workers (low traffic) to 200+ workers (peak)

The choice of processing engine determines pipeline correctness guarantees, operational burden,
debugging ergonomics, and long-term extensibility to batch workloads.

---

## Decision

**Use Apache Beam on Google Cloud Dataflow** as the stream processing engine.

---

## Options Considered

### Option A: Apache Beam / Cloud Dataflow (Selected)

| Dimension | Assessment |
|-----------|------------|
| Complexity | Medium — Beam SDK is expressive; Dataflow handles execution |
| Cost | Usage-based ($0.056/vCPU-h, $0.003/GB-h); no idle cost |
| Scalability | Very High — auto-scales to 1,000+ workers; horizontal sharding |
| Exactly-once | Native — Dataflow's shuffle service + checkpointing |
| State management | Built-in (BagState, ValueState, MapState, SetState) |
| Windowing | Rich: fixed, sliding, session, custom |
| Unified batch+stream | Yes — same code runs on DirectRunner (local) and DataflowRunner |
| Pub/Sub integration | First-class: `ReadFromPubSub` with exactly-once |
| BigQuery integration | First-class: `WriteToBigQuery` with FILE_LOADS + streaming inserts |

**Pros:**
- **Unified model**: same Beam pipeline code runs as batch or streaming; simplifies backfill
- **Exactly-once out of the box**: Dataflow's Streaming Engine handles deduplication at the execution level
- **Auto-scaling**: worker count adjusts to backlog size every 60 seconds; no manual tuning
- **Flex Templates**: package as Docker image; deploy new versions without pipeline downtime
- **Native GCP integrations**: Pub/Sub, BigQuery, GCS, Datastore — all first-class I/O transforms
- **Python SDK**: accessible to ML engineers who contribute pipeline transforms
- **Stateful processing**: `DoFn` with state and timers enables deduplication, session tracking, late data handling

**Cons:**
- Dataflow-managed Shuffle has a per-job overhead (~5-10 min startup for new jobs)
- Python Beam SDK is slower than Java SDK for CPU-intensive transforms (~2-3x throughput difference)
- Limited ecosystem outside GCP (Java Flink has wider connector library)
- Cost scales linearly with workers — predictability requires careful backlog monitoring

---

### Option B: Apache Spark Structured Streaming on Dataproc

| Dimension | Assessment |
|-----------|------------|
| Complexity | High — Dataproc cluster management, Spark tuning, YARN configuration |
| Cost | Medium — cluster must be pre-provisioned; idle cost during low traffic |
| Exactly-once | Achievable but requires Kafka (Pub/Sub offset management is weaker in Spark) |
| State management | Arbitrary stateful operators with `flatMapGroupsWithState` |
| Windowing | Supported but less expressive than Beam |
| Unified batch+stream | Yes — Spark DataFrame API works for both |
| Pub/Sub integration | Community connector; not first-class |

**Rejected because:**
- Dataproc cluster is always-on cost (minimum 3 nodes × $0.048/vCPU-h even at idle)
- Spark's Pub/Sub integration is community-maintained (reliability concern for production)
- Cluster scaling (adding Dataproc workers) takes 3-5 minutes; Dataflow worker scaling is 30-60 seconds
- Operational burden: YARN tuning, executor memory, shuffle spill management is non-trivial SRE work
- Exactly-once with Pub/Sub requires extra care (Pub/Sub doesn't expose Kafka-style offsets)

---

### Option C: Apache Flink on GKE

| Dimension | Assessment |
|-----------|------------|
| Complexity | Very High — self-managed Flink on Kubernetes; JobManager HA, state backend |
| Cost | Medium hardware + Very High operational cost |
| Exactly-once | Native — Flink's distributed snapshots (Chandy-Lamport) |
| State management | Best-in-class: RocksDB state backend, incremental checkpoints |
| Windowing | Most expressive of the three options |
| Pub/Sub integration | Community connector |

**Rejected because:**
- Managing Flink on GKE requires a dedicated SRE who understands Flink internals
- State backend management (RocksDB sizing, checkpoint storage, recovery time) is complex
- GKE cluster resource planning for Flink is manual
- Beam on Dataflow provides 90% of Flink's capabilities with 1/10th the operational overhead

---

## Trade-off Analysis

**Exactly-once vs. operational simplicity trade-off:** Flink has the most sophisticated state management and exactly-once guarantees, but the operational burden is unacceptable without a dedicated Flink team. Dataflow achieves exactly-once via its Shuffle service and checkpoint mechanism with zero operator intervention.

**Cost model trade-off:** Dataflow's usage-based pricing is ideal for workloads with variable load patterns. A Dataproc or GKE-based solution requires pre-provisioning for peak, meaning you pay for peak capacity during off-peak hours.

**Language trade-off:** The Python Beam SDK enables data scientists to contribute pipeline transforms without learning Java. The throughput penalty (2-3x vs. Java) is acceptable at our scale because Dataflow horizontal scaling compensates for per-worker throughput.

---

## Consequences

**What becomes easier:**
- Backfill historical data using the same pipeline with `--runner=DirectRunner` and batch data source
- Deploy new pipeline versions via Flex Templates without pipeline restart
- Stateful deduplication across 24h windows without managing external cache (Redis/Memcached)
- Auto-scaling during traffic spikes eliminates manual capacity management

**What becomes harder:**
- Debugging stateful transforms requires understanding Beam's state and timer API
- Exactly-once semantics require Dataflow Streaming Engine (not available in all regions)
- Pipeline startup latency (5-10 min) means Pub/Sub backlog can grow during cold start

**What we'll need to revisit:**
- If throughput exceeds 1M events/sec, evaluate Java SDK rewrite for per-worker efficiency
- If cross-cloud data sources become a requirement, Flink's connector ecosystem is broader

---

## Phase-by-Phase Implementation

### Phase 2A — Core Pipeline (Week 3)
```python
# Pipeline skeleton — see pipelines/dataflow/event_processor.py
pipeline
  | 'Read Pub/Sub' >> beam.io.ReadFromPubSub(subscription=SUB, with_attributes=True)
  | 'Parse Avro'   >> beam.ParDo(ParseAvroDo())
  | 'Validate'     >> beam.ParDo(ValidateSchemaDo())
  | 'Dedup'        >> beam.ParDo(StatefulDeduplicationDo()).with_outputs('valid', 'duplicate')
```

### Phase 2B — Enrichment + Windowing (Week 4)
```python
  | 'Enrich'       >> beam.ParDo(EnrichWithReferenceDo(side_input=ref_data))
  | 'Window'       >> beam.WindowInto(beam.window.SlidingWindows(60, 10))
  | 'Aggregate'    >> beam.CombinePerKey(EventAggregationFn())
```

### Phase 2C — Dual Sink (Week 5)
```python
  | 'Write Raw BQ' >> beam.io.WriteToBigQuery(RAW_TABLE, schema=RAW_SCHEMA,
                        write_disposition=WRITE_APPEND,
                        create_disposition=CREATE_IF_NEEDED)
  | 'Write Agg BQ' >> beam.io.WriteToBigQuery(AGG_TABLE, ...)
```

## Action Items

1. [x] Write `event_processor.py` with full pipeline DAG
2. [x] Implement `StatefulDeduplicationDo` with 24h BagState + GC timer
3. [x] Implement `EnrichWithReferenceDo` with side inputs (GCS-backed)
4. [x] Write Flex Template Dockerfile and `metadata.json`
5. [x] Terraform `modules/dataflow/` with template GCS path, SA, network
6. [ ] Load test to 500K events/sec; verify auto-scaling response time < 90s
