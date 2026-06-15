"""
event_processor.py — Main Apache Beam / Dataflow streaming pipeline.

Pipeline DAG:
    Pub/Sub
        → ParseAvro            (Avro decode; DLQ on failure)
        → ValidateSchema       (business rules; DLQ on violation)
        → PrepareDeduplicationKey
        → StatefulDeduplication (24h BagState; silently drop dupes)
        → EnrichEvent          (GCS side input; typed column extraction)
        ├─→ [raw_events BQ]    (append-only bronze sink)
        └─→ build_aggregation_pipeline → [event_aggregates BQ]

    DLQ events from parse/validate → Pub/Sub DLQ topic

Deploy:
    python event_processor.py \
        --project=my-project \
        --region=us-central1 \
        --runner=DataflowRunner \
        --streaming \
        --input_subscription=projects/my-project/subscriptions/platform-events-dataflow-sub \
        --raw_output_table=my-project:platform_raw.raw_events \
        --agg_output_table=my-project:platform_processed.event_aggregates \
        --dlq_topic=projects/my-project/topics/platform-events-dlq \
        --reference_data_gcs=gs://my-project-reference-data/user_profiles.jsonl \
        --max_num_workers=100 \
        --machine_type=n1-standard-4 \
        --enable_streaming_engine \
        --experiments=enable_exactly_once_pubsub_subscribe
"""

import argparse
import logging
import os
from typing import List

import apache_beam as beam
from apache_beam.io import BigQueryDisposition, ReadFromPubSub, WriteToBigQuery
from apache_beam.io.gcp.pubsub import PubsubMessage
from apache_beam.options.pipeline_options import (
    GoogleCloudOptions,
    PipelineOptions,
    SetupOptions,
    StandardOptions,
    WorkerOptions,
)
from apache_beam.transforms import window

from schemas import PROCESSED_EVENTS_BQ_SCHEMA, RAW_EVENTS_BQ_SCHEMA
from transforms.aggregate import build_aggregation_pipeline
from transforms.dedup import PrepareDeduplicationKeyDo, StatefulDeduplicationDo
from transforms.enrich import EnrichEventDo
from transforms.parse import DLQ_TAG, UNIQUE_TAG, ParseAvroDo, ValidateSchemaDo

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Pipeline Arguments
# ─────────────────────────────────────────────────────────────────────────────

def parse_pipeline_args(argv: List[str]):
    parser = argparse.ArgumentParser(description="GCP Platform Event Processor")

    parser.add_argument("--input_subscription",  required=True,
                        help="Pub/Sub subscription resource path")
    parser.add_argument("--raw_output_table",    required=True,
                        help="BigQuery table for raw events (project:dataset.table)")
    parser.add_argument("--agg_output_table",    required=True,
                        help="BigQuery table for event aggregates")
    parser.add_argument("--dlq_topic",           required=True,
                        help="Pub/Sub topic for dead-letter events")
    parser.add_argument("--reference_data_gcs",  required=True,
                        help="GCS path to user profile reference data (jsonl)")
    parser.add_argument("--pipeline_version",    default="2.0.0",
                        help="Pipeline version tag written to processed_events")

    known_args, pipeline_args = parser.parse_known_args(argv)
    return known_args, pipeline_args


# ─────────────────────────────────────────────────────────────────────────────
# DLQ Publisher — Routes failed events to Pub/Sub DLQ topic
# ─────────────────────────────────────────────────────────────────────────────

class PublishToDlqDo(beam.DoFn):
    """Serialize failed event metadata and publish to DLQ topic."""

    dlq_published = beam.metrics.Metrics.counter("dlq", "published")

    def __init__(self, dlq_topic: str):
        self._dlq_topic = dlq_topic

    def setup(self):
        from google.cloud import pubsub_v1
        self._publisher = pubsub_v1.PublisherClient()

    def process(self, element, *args, **kwargs):
        import json
        payload = json.dumps(element).encode("utf-8")
        future = self._publisher.publish(
            self._dlq_topic,
            data=payload,
            error_type=element.get("error_type", "unknown"),
        )
        future.result()  # Block to ensure delivery before ack
        self.dlq_published.inc()


# ─────────────────────────────────────────────────────────────────────────────
# Format Raw Row — adds ingestion metadata before BQ write
# ─────────────────────────────────────────────────────────────────────────────

class FormatRawRowDo(beam.DoFn):
    """Prepare parsed event dict for raw_events BigQuery sink."""

    def __init__(self, job_id: str):
        self._job_id = job_id

    def process(self, event, *args, **kwargs):
        import json
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        ts_ms = event["timestamp_ms"]
        dt = datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc)

        yield {
            "event_id":        event["event_id"],
            "event_type":      event["event_type"],
            "user_id":         event.get("user_id"),
            "session_id":      event["session_id"],
            "event_timestamp": dt.strftime("%Y-%m-%d %H:%M:%S.%f UTC"),
            "event_date":      dt.strftime("%Y-%m-%d"),
            "properties":      json.dumps(event.get("properties") or {}),
            "schema_version":  event.get("schema_version", 1),
            "producer_id":     event["producer_id"],
            "environment":     event.get("environment", "production"),
            "ingested_at":     now.strftime("%Y-%m-%d %H:%M:%S UTC"),
            "dataflow_job_id": self._job_id,
        }


# ─────────────────────────────────────────────────────────────────────────────
# Main Pipeline
# ─────────────────────────────────────────────────────────────────────────────

def run(argv=None):
    known_args, pipeline_argv = parse_pipeline_args(argv)

    options = PipelineOptions(pipeline_argv)
    options.view_as(StandardOptions).streaming = True

    gcp_opts = options.view_as(GoogleCloudOptions)
    job_id = gcp_opts.job_name or "local-dev"

    with beam.Pipeline(options=options) as pipeline:

        # ── 1. Load reference data as a side input ───────────────────────────
        user_profiles_side_input = (
            pipeline
            | "CreateRefDataPath" >> beam.Create([known_args.reference_data_gcs])
            | "LoadRefData" >> beam.io.ReadAllFromText()
            | "ParseRefData" >> beam.Map(lambda line: __import__("json").loads(line))
            | "KeyRefData"   >> beam.Map(lambda p: (p["user_id"], p))
            | "CombineRefData" >> beam.combiners.ToDict()
        )

        # ── 2. Read from Pub/Sub ─────────────────────────────────────────────
        raw_messages = (
            pipeline
            | "ReadPubSub" >> ReadFromPubSub(
                subscription=known_args.input_subscription,
                with_attributes=True,  # Get message_id + attributes for DLQ
                timestamp_attribute=None,  # Use Pub/Sub publish time as processing time
            )
        )

        # ── 3. Parse Avro ────────────────────────────────────────────────────
        parsed = raw_messages | "ParseAvro" >> beam.ParDo(ParseAvroDo()).with_outputs(
            DLQ_TAG, main=UNIQUE_TAG
        )

        # ── 4. Validate schema ───────────────────────────────────────────────
        validated = parsed[UNIQUE_TAG] | "ValidateSchema" >> beam.ParDo(
            ValidateSchemaDo()
        ).with_outputs(DLQ_TAG, main=UNIQUE_TAG)

        # ── 5. Collect and publish DLQ events ────────────────────────────────
        (
            (parsed[DLQ_TAG], validated[DLQ_TAG])
            | "FlattenDLQ"     >> beam.Flatten()
            | "PublishToDLQ"   >> beam.ParDo(PublishToDlqDo(known_args.dlq_topic))
        )

        # ── 6. Stateful deduplication ─────────────────────────────────────────
        keyed_events = (
            validated[UNIQUE_TAG]
            | "PrepareDeduplicationKey" >> beam.ParDo(PrepareDeduplicationKeyDo())
        )
        deduped = keyed_events | "StatefulDedup" >> beam.ParDo(
            StatefulDeduplicationDo()
        ).with_outputs("duplicate", main="unique")

        unique_events = deduped["unique"] | "UnpackDeduped" >> beam.Values()

        # ── 7. Enrich events ──────────────────────────────────────────────────
        enriched_events = (
            unique_events
            | "EnrichEvents" >> beam.ParDo(
                EnrichEventDo(),
                user_profiles=beam.pvalue.AsSingleton(user_profiles_side_input),
            )
        )

        # ── 8. Write raw events to BigQuery (bronze sink) ─────────────────────
        (
            unique_events
            | "FormatRawRow"  >> beam.ParDo(FormatRawRowDo(job_id=job_id))
            | "WriteRawBQ"    >> WriteToBigQuery(
                table=known_args.raw_output_table,
                schema=RAW_EVENTS_BQ_SCHEMA,
                write_disposition=BigQueryDisposition.WRITE_APPEND,
                create_disposition=BigQueryDisposition.CREATE_IF_NEEDED,
                method=WriteToBigQuery.Method.STREAMING_INSERTS,
                additional_bq_parameters={
                    "timePartitioning": {"type": "DAY", "field": "event_date"},
                    "clustering": {"fields": ["event_type", "user_id"]},
                },
            )
        )

        # ── 9. Write processed events to BigQuery (silver sink) ───────────────
        (
            enriched_events
            | "WriteProcessedBQ" >> WriteToBigQuery(
                table=known_args.raw_output_table.replace("raw_events", "processed_events")
                      .replace("_raw.", "_processed."),
                schema=PROCESSED_EVENTS_BQ_SCHEMA,
                write_disposition=BigQueryDisposition.WRITE_APPEND,
                create_disposition=BigQueryDisposition.CREATE_IF_NEEDED,
                method=WriteToBigQuery.Method.STREAMING_INSERTS,
                additional_bq_parameters={
                    "timePartitioning": {"type": "DAY", "field": "event_date"},
                    "clustering": {"fields": ["user_id", "event_type", "country"]},
                },
            )
        )

        # ── 10. Windowed aggregation pipeline ─────────────────────────────────
        build_aggregation_pipeline(enriched_events, known_args.agg_output_table)

    logger.info("Pipeline completed (or streaming pipeline started).")


if __name__ == "__main__":
    import sys
    run(sys.argv[1:])
