"""
transforms/parse.py — Avro parsing and schema validation transforms.

Design decisions:
- Parsing failures route to DLQ (not crash the pipeline)
- Schema version mismatch is tracked as a metric, not a hard failure
- Invalid environment (non-production) events are silently dropped with a counter
"""

import io
import json
import logging
from datetime import datetime, timezone
from typing import Optional

import apache_beam as beam
import fastavro
from apache_beam import pvalue

from schemas import PLATFORM_EVENT_AVRO_SCHEMA, VALID_EVENT_TYPES

logger = logging.getLogger(__name__)

# Output tag for events that fail validation — routed to DLQ
DLQ_TAG = "dlq"
VALID_TAG = "valid"
# Alias used by event_processor.py for symmetry with DLQ_TAG
UNIQUE_TAG = VALID_TAG


class ParseAvroDo(beam.DoFn):
    """
    Deserialize Avro-binary encoded Pub/Sub messages into Python dicts.

    Input: Pub/Sub message bytes (with attributes)
    Output[VALID_TAG]: Parsed event dict
    Output[DLQ_TAG]: Original bytes + error metadata for DLQ routing

    Metrics tracked:
        - parse_success_count
        - parse_failure_count
        - schema_version_mismatch_count
        - non_production_dropped_count
    """

    parse_success   = beam.metrics.Metrics.counter("parse", "success")
    parse_failure   = beam.metrics.Metrics.counter("parse", "failure")
    schema_mismatch = beam.metrics.Metrics.counter("parse", "schema_version_mismatch")
    env_dropped     = beam.metrics.Metrics.counter("parse", "non_production_dropped")

    def setup(self):
        """Compile the Avro parsed schema once per worker (not per element)."""
        self._avro_schema = fastavro.parse_schema(PLATFORM_EVENT_AVRO_SCHEMA)

    def process(self, element, *args, **kwargs):
        """
        Args:
            element: apache_beam.io.PubsubMessage with .data and .attributes
        """
        message_id = getattr(element, "message_id", "unknown")
        raw_bytes = element.data

        try:
            parsed = fastavro.schemaless_reader(io.BytesIO(raw_bytes), self._avro_schema)
        except Exception as exc:  # noqa: BLE001
            self.parse_failure.inc()
            logger.warning("Avro parse failure msg_id=%s: %s", message_id, exc)
            yield pvalue.TaggedOutput(DLQ_TAG, {
                "message_id": message_id,
                "raw_bytes_b64": raw_bytes.hex(),
                "error_type": "avro_parse_failure",
                "error_detail": str(exc),
                "failed_at": datetime.now(timezone.utc).isoformat(),
            })
            return

        # Drop non-production events early — before any enrichment or BQ write
        if parsed.get("environment") != "production":
            self.env_dropped.inc()
            return

        # Soft-warn on schema version changes — don't drop, allow forward compat
        if parsed.get("schema_version", 1) != 1:
            self.schema_mismatch.inc()
            logger.info("schema_version=%d event_id=%s", parsed["schema_version"], parsed["event_id"])

        self.parse_success.inc()
        yield pvalue.TaggedOutput(VALID_TAG, parsed)


class ValidateSchemaDo(beam.DoFn):
    """
    Enforce business-level validation rules on parsed events.

    Validation rules:
    1. event_id must be present and non-empty
    2. event_type must be in the known set
    3. session_id must be present
    4. timestamp_ms must be within [90 days ago, 5 minutes in future] (clock skew tolerance)
    5. producer_id must be non-empty

    Events failing validation go to DLQ with the specific rule that failed.
    """

    validation_pass = beam.metrics.Metrics.counter("validate", "pass")
    validation_fail = beam.metrics.Metrics.counter("validate", "fail")
    clock_skew_past = beam.metrics.Metrics.counter("validate", "clock_skew_past")
    clock_skew_future = beam.metrics.Metrics.counter("validate", "clock_skew_future")

    NINETY_DAYS_MS     = 90 * 24 * 60 * 60 * 1000
    FIVE_MINUTES_MS    = 5 * 60 * 1000
    MAX_EVENT_ID_LEN   = 64

    def process(self, element, *args, **kwargs):
        event = element
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        errors = []

        if not event.get("event_id") or len(event["event_id"]) > self.MAX_EVENT_ID_LEN:
            errors.append("invalid_event_id")

        if event.get("event_type") not in VALID_EVENT_TYPES:
            errors.append(f"unknown_event_type:{event.get('event_type')}")

        if not event.get("session_id"):
            errors.append("missing_session_id")

        if not event.get("producer_id"):
            errors.append("missing_producer_id")

        ts_ms = event.get("timestamp_ms", 0)
        if ts_ms < now_ms - self.NINETY_DAYS_MS:
            errors.append("timestamp_too_old")
            self.clock_skew_past.inc()
        elif ts_ms > now_ms + self.FIVE_MINUTES_MS:
            errors.append("timestamp_future")
            self.clock_skew_future.inc()

        if errors:
            self.validation_fail.inc()
            yield pvalue.TaggedOutput(DLQ_TAG, {
                "event_id": event.get("event_id", "unknown"),
                "error_type": "validation_failure",
                "error_detail": json.dumps(errors),
                "failed_at": datetime.now(timezone.utc).isoformat(),
            })
            return

        self.validation_pass.inc()
        yield pvalue.TaggedOutput(VALID_TAG, event)
