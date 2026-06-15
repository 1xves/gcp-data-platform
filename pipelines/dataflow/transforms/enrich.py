"""
transforms/enrich.py — Event enrichment with user profile data.

Enrichment strategy:
- Reference data (user → country, plan_tier, cohort) loaded from GCS as a side input
- Side input is a Dict[user_id, profile] loaded at pipeline startup and cached for TTL
- GCS side input refresh: use ValueProvider + periodic reload (not per-element API calls)
- Fallback: if user_id not in reference data, enrich with empty strings (don't DLQ)

Why GCS side input and not BigQuery direct lookup per-element?
- BigQuery streaming reads per-element would cost ~$0.01/TB × 1M events/day = ~$10/day
  just in enrichment reads, with 10-50ms latency per lookup (killing throughput)
- GCS side input loads once per worker startup (~1-5 MB), cached in memory
- Staleness is acceptable: profile data changes at most daily
- For real-time profile changes (e.g., plan upgrade), use Firestore lookup with 5min TTL cache
"""

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import apache_beam as beam

from schemas import TYPED_PROPERTY_EXTRACTORS

logger = logging.getLogger(__name__)


class LoadReferenceDataDo(beam.DoFn):
    """
    Load reference data from GCS and emit as a single dict.
    Used as a side input to EnrichEventDo.

    Expected GCS file format: newline-delimited JSON
    {"user_id": "u123", "country": "US", "plan_tier": "pro", "cohort": "2024-Q1"}
    """

    enrich_loaded = beam.metrics.Metrics.counter("enrich", "reference_data_loaded_rows")

    def process(self, gcs_path: str, *args, **kwargs):
        """Read reference data file and emit (user_id, profile) pairs."""
        from google.cloud import storage

        client = storage.Client()
        bucket_name, blob_path = gcs_path.replace("gs://", "").split("/", 1)
        blob = client.bucket(bucket_name).blob(blob_path)
        content = blob.download_as_text()

        for line in content.strip().splitlines():
            profile = json.loads(line)
            self.enrich_loaded.inc()
            yield (profile["user_id"], profile)


class EnrichEventDo(beam.DoFn):
    """
    Enrich events with user profile data from a side input dict.

    Also extracts typed properties from the raw properties map:
        properties["page_url"]  → page_url (STRING)
        properties["value_usd"] → value_usd (FLOAT64)
        etc.

    Processing time indicator:
        is_mobile: derived from producer_id (ios-app or android-app prefix)
    """

    enrich_hit  = beam.metrics.Metrics.counter("enrich", "profile_found")
    enrich_miss = beam.metrics.Metrics.counter("enrich", "profile_not_found")

    def process(self, event: Dict[str, Any], user_profiles, *args, **kwargs):
        """
        Args:
            event: parsed + deduped event dict
            user_profiles: side input dict[user_id → profile]
        """
        enriched = dict(event)  # shallow copy — do not mutate the original

        # ── Profile enrichment ────────────────────────────────────────────────
        user_id = event.get("user_id")
        if user_id and user_id in user_profiles:
            profile = user_profiles[user_id]
            enriched["country"]     = profile.get("country", "")
            enriched["plan_tier"]   = profile.get("plan_tier", "")
            enriched["user_cohort"] = profile.get("cohort", "")
            self.enrich_hit.inc()
        else:
            enriched["country"]     = None
            enriched["plan_tier"]   = None
            enriched["user_cohort"] = None
            if user_id:
                self.enrich_miss.inc()

        # ── Property extraction (map → typed columns) ─────────────────────────
        properties = event.get("properties") or {}
        for prop_key, config in TYPED_PROPERTY_EXTRACTORS.items():
            raw_val = properties.get(prop_key)
            if raw_val is not None:
                try:
                    enriched[config["bq_column"]] = config["cast"](raw_val)
                except (ValueError, TypeError):
                    enriched[config["bq_column"]] = None
            else:
                enriched[config["bq_column"]] = None

        # ── Derived fields ────────────────────────────────────────────────────
        producer_id = event.get("producer_id", "")
        enriched["is_mobile"] = producer_id.startswith(("ios-", "android-"))

        # Timestamp conversions for BigQuery partitioning
        ts_ms = event["timestamp_ms"]
        ts_sec = ts_ms / 1000.0
        dt = datetime.fromtimestamp(ts_sec, tz=timezone.utc)
        enriched["event_timestamp"] = dt.strftime("%Y-%m-%d %H:%M:%S.%f UTC")
        enriched["event_date"]      = dt.strftime("%Y-%m-%d")

        # Processing metadata
        enriched["processed_at"]    = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        enriched["pipeline_version"] = "2.0.0"  # Set by Flex Template env var in prod

        yield enriched
