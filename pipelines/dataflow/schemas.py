"""
schemas.py — Avro and BigQuery schema definitions for the event processing pipeline.

All schema definitions live in a single place to ensure consistency between
the Pub/Sub schema registry, the Dataflow parser, and the BigQuery writer.
"""

import json
from typing import Any, Dict

# ─────────────────────────────────────────────────────────────────────────────
# Avro Schema (must match infrastructure/terraform/modules/pubsub/schemas/platform_event.avsc)
# ─────────────────────────────────────────────────────────────────────────────

PLATFORM_EVENT_AVRO_SCHEMA = {
    "type": "record",
    "name": "PlatformEvent",
    "namespace": "com.platform.events.v1",
    "fields": [
        {"name": "event_id",       "type": "string"},
        {"name": "event_type",     "type": {"type": "enum", "name": "EventType",
                                            "symbols": ["page_view", "click", "purchase",
                                                        "add_to_cart", "search", "error",
                                                        "session_start", "session_end"]}},
        {"name": "user_id",        "type": ["null", "string"], "default": None},
        {"name": "session_id",     "type": "string"},
        {"name": "timestamp_ms",   "type": "long"},
        {"name": "properties",     "type": {"type": "map", "values": "string"}, "default": {}},
        {"name": "schema_version", "type": "int",    "default": 1},
        {"name": "producer_id",    "type": "string"},
        {"name": "environment",    "type": {"type": "enum", "name": "Environment",
                                            "symbols": ["production", "staging", "development"]},
                                   "default": "production"},
    ]
}

AVRO_SCHEMA_JSON = json.dumps(PLATFORM_EVENT_AVRO_SCHEMA)

# ─────────────────────────────────────────────────────────────────────────────
# BigQuery Table Schemas
# ─────────────────────────────────────────────────────────────────────────────

RAW_EVENTS_BQ_SCHEMA = {
    "fields": [
        {"name": "event_id",        "type": "STRING",    "mode": "REQUIRED"},
        {"name": "event_type",      "type": "STRING",    "mode": "REQUIRED"},
        {"name": "user_id",         "type": "STRING",    "mode": "NULLABLE"},
        {"name": "session_id",      "type": "STRING",    "mode": "REQUIRED"},
        {"name": "event_timestamp", "type": "TIMESTAMP", "mode": "REQUIRED"},
        {"name": "event_date",      "type": "DATE",      "mode": "REQUIRED"},
        {"name": "properties",      "type": "JSON",      "mode": "NULLABLE"},
        {"name": "schema_version",  "type": "INTEGER",   "mode": "REQUIRED"},
        {"name": "producer_id",     "type": "STRING",    "mode": "REQUIRED"},
        {"name": "environment",     "type": "STRING",    "mode": "REQUIRED"},
        {"name": "ingested_at",     "type": "TIMESTAMP", "mode": "REQUIRED"},
        {"name": "dataflow_job_id", "type": "STRING",    "mode": "NULLABLE"},
    ]
}

PROCESSED_EVENTS_BQ_SCHEMA = {
    "fields": [
        {"name": "event_id",             "type": "STRING",    "mode": "REQUIRED"},
        {"name": "event_type",           "type": "STRING",    "mode": "REQUIRED"},
        {"name": "user_id",              "type": "STRING",    "mode": "NULLABLE"},
        {"name": "session_id",           "type": "STRING",    "mode": "REQUIRED"},
        {"name": "event_timestamp",      "type": "TIMESTAMP", "mode": "REQUIRED"},
        {"name": "event_date",           "type": "DATE",      "mode": "REQUIRED"},
        {"name": "schema_version",       "type": "INTEGER",   "mode": "REQUIRED"},
        {"name": "producer_id",          "type": "STRING",    "mode": "REQUIRED"},
        {"name": "page_url",             "type": "STRING",    "mode": "NULLABLE"},
        {"name": "referrer",             "type": "STRING",    "mode": "NULLABLE"},
        {"name": "value_usd",            "type": "FLOAT64",   "mode": "NULLABLE"},
        {"name": "search_query",         "type": "STRING",    "mode": "NULLABLE"},
        {"name": "country",              "type": "STRING",    "mode": "NULLABLE"},
        {"name": "plan_tier",            "type": "STRING",    "mode": "NULLABLE"},
        {"name": "user_cohort",          "type": "STRING",    "mode": "NULLABLE"},
        {"name": "is_mobile",            "type": "BOOLEAN",   "mode": "NULLABLE"},
        {"name": "session_sequence_num", "type": "INTEGER",   "mode": "NULLABLE"},
        {"name": "processed_at",         "type": "TIMESTAMP", "mode": "REQUIRED"},
        {"name": "pipeline_version",     "type": "STRING",    "mode": "NULLABLE"},
    ]
}

EVENT_AGGREGATES_BQ_SCHEMA = {
    "fields": [
        {"name": "user_id",         "type": "STRING",    "mode": "REQUIRED"},
        {"name": "event_type",      "type": "STRING",    "mode": "REQUIRED"},
        {"name": "window_start",    "type": "TIMESTAMP", "mode": "REQUIRED"},
        {"name": "window_end",      "type": "TIMESTAMP", "mode": "REQUIRED"},
        {"name": "window_date",     "type": "DATE",      "mode": "REQUIRED"},
        {"name": "event_count",     "type": "INTEGER",   "mode": "REQUIRED"},
        {"name": "total_value_usd", "type": "FLOAT64",   "mode": "NULLABLE"},
        {"name": "unique_sessions", "type": "INTEGER",   "mode": "REQUIRED"},
        {"name": "computed_at",     "type": "TIMESTAMP", "mode": "REQUIRED"},
    ]
}

# ─────────────────────────────────────────────────────────────────────────────
# Known property keys extracted from the properties map to typed BQ columns
# ─────────────────────────────────────────────────────────────────────────────

TYPED_PROPERTY_EXTRACTORS: Dict[str, Any] = {
    "page_url":    {"bq_column": "page_url",    "cast": str},
    "referrer":    {"bq_column": "referrer",    "cast": str},
    "value_usd":   {"bq_column": "value_usd",   "cast": float},
    "search_query":{"bq_column": "search_query","cast": str},
}

VALID_EVENT_TYPES = frozenset([
    "page_view", "click", "purchase", "add_to_cart",
    "search", "error", "session_start", "session_end"
])
