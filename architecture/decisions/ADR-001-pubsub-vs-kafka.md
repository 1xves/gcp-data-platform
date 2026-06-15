# ADR-001: Event Streaming — Cloud Pub/Sub vs. Apache Kafka

**Status:** Accepted  
**Date:** 2026-04-21  
**Deciders:** Platform Engineering Team

---

## Context

The platform needs a durable, high-throughput message bus capable of ingesting 500K events/sec peak
from heterogeneous producers (mobile, web, backend microservices). The choice of event streaming
technology is foundational — it affects operational burden, scalability, ordering guarantees,
schema enforcement, and total cost of ownership for the lifetime of the platform.

Three candidates were evaluated: Cloud Pub/Sub (managed GCP native), Confluent Kafka (managed
Kafka), and self-hosted Kafka on GKE.

---

## Decision

**Use Cloud Pub/Sub** as the primary event streaming layer.

---

## Options Considered

### Option A: Cloud Pub/Sub (Selected)

| Dimension | Assessment |
|-----------|------------|
| Complexity | Low — fully managed, no broker management |
| Cost | Medium — ~$0.01/GB; predictable, no idle-cluster cost |
| Scalability | Very High — auto-scales to millions of msgs/sec globally |
| Ordering | Per-key ordering via `ordering_key` (sufficient for user-scoped streams) |
| Retention | 7 days max (configurable); sufficient for replay window |
| Schema enforcement | Avro/JSON schemas via Pub/Sub Schema Registry |
| Team familiarity | High — native GCP, familiar API |

**Pros:**
- Zero operational overhead (no brokers, ZooKeeper, or partition rebalancing)
- Seamless integration with Dataflow via `ReadFromPubSub` with exactly-once support
- Global availability with multi-region topics
- Dead-letter topics with automatic routing after configurable retry count
- Schema Registry built-in (Avro/JSON Schema validation on publish)

**Cons:**
- Maximum 7-day message retention (Kafka supports indefinite)
- No consumer groups (each subscriber must track its own ack/seek position)
- Ordering guaranteed only within `ordering_key` scope — cross-partition ordering not possible
- Cannot replay arbitrary historical offsets beyond 7 days (requires BigQuery for historical replay)

---

### Option B: Confluent Managed Kafka

| Dimension | Assessment |
|-----------|------------|
| Complexity | Medium — managed Kafka but requires Confluent account, Schema Registry config |
| Cost | High — dedicated cluster pricing; idle cost even at low load |
| Scalability | High — horizontal partition scaling; manual rebalancing |
| Ordering | Total ordering within partition; topic-level offset replay |
| Retention | Unlimited (storage-backed) |
| Schema enforcement | Confluent Schema Registry (mature, AVRO/Protobuf/JSON) |
| Team familiarity | Medium |

**Pros:**
- Unlimited retention for full historical replay
- Consumer groups enable multiple independent consumers without subscription management
- Mature ecosystem (Kafka Streams, ksqlDB, Kafka Connect)
- Fine-grained partition control for advanced ordering scenarios

**Cons:**
- Cluster cost is always-on: ~$1,500-3,000/month for a production Confluent cluster
- Cross-cloud egress charges when Dataflow (GCP) reads from Confluent (multi-cloud)
- Requires Confluent account and separate billing outside GCP
- Partition rebalancing during scale events causes brief processing pauses

---

### Option C: Self-Hosted Kafka on GKE

| Dimension | Assessment |
|-----------|------------|
| Complexity | Very High — manage brokers, ZooKeeper, monitoring, upgrades, storage |
| Cost | Medium hardware cost, Very High operational cost (dedicated SRE time) |
| Scalability | High — but requires manual capacity planning |
| Ordering | Same as Kafka |
| Retention | Configurable |
| Team familiarity | Low for operational overhead |

**Rejected immediately** — operational burden is disproportionate for a data platform team. Managing
Kafka brokers, ZooKeeper quorum, disk I/O tuning, and partition leadership elections is a full-time
job. This cost was not acceptable given that the team's core competency is data engineering, not
infrastructure administration.

---

## Trade-off Analysis

The core tension is **retention depth vs. operational simplicity**.

Confluent Kafka wins on retention (unlimited) and consumer group semantics, but loses on cost
(always-on cluster), operational coupling (cross-cloud latency + billing), and team operational
overhead during incidents.

Cloud Pub/Sub's 7-day retention limit is mitigated by the fact that **BigQuery is the system of
record for historical replay** — any event older than 7 days is reconstructable from the `raw_events`
table. Dataflow supports seek-to-timestamp for Pub/Sub to replay within the retention window.

The ordering limitation (per-key only) is acceptable because our primary ordering requirement is
user-scoped (process a user's events in order), which maps directly to `user_id` as the `ordering_key`.

---

## Consequences

**What becomes easier:**
- Dataflow integration is native (ReadFromPubSub is a first-class transform with exactly-once support)
- No cluster management; team focuses on business logic
- GCP-native IAM controls eliminate separate Kafka ACL management
- Dead-letter routing is built-in — no custom DLQ consumer needed

**What becomes harder:**
- Historical replay beyond 7 days requires BigQuery → Pub/Sub re-publish script
- Consumer fan-out requires separate subscriptions per consumer (managed in Terraform)
- Cannot use Kafka Streams or ksqlDB for lightweight stream transformations

**What we'll need to revisit:**
- If we need > 7-day replay for audit/compliance, evaluate Cloud Storage export + re-publish pattern
- If ordering requirements expand to cross-user global ordering, re-evaluate Kafka partitioning

---

## Action Items

1. [x] Provision `events-topic` with Avro schema in Pub/Sub Schema Registry
2. [x] Create `events-dlq` topic with 7-day retention
3. [x] Configure subscription `events-dataflow-sub` with `ordering_key=user_id`
4. [x] Set retry policy: 5 retries with exponential backoff (10s → 600s), then route to DLQ
5. [x] Write Terraform module for all Pub/Sub resources (see `infrastructure/terraform/modules/pubsub/`)
6. [ ] Implement snapshot-before-deploy runbook for zero-downtime redeployments
