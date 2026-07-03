"""
test_enrich.py — unit tests for transforms/enrich.py

Covers:
  - EnrichEventDo: profile hit/miss/anonymous paths, typed property extraction
    (including cast failures), is_mobile derivation, timestamp conversion,
    input immutability
  - LoadReferenceDataDo: NDJSON parsing from a mocked GCS client

The GCS client is stubbed via sys.modules so these tests run identically
with apache-beam core (local) and apache-beam[gcp] (CI).
"""

import json
import sys
from types import ModuleType
from unittest.mock import MagicMock

from transforms.enrich import EnrichEventDo, LoadReferenceDataDo


def make_event(**overrides):
    event = {
        "event_id": "evt-1",
        "event_type": "page_view",
        "user_id": "user-1",
        "session_id": "sess-1",
        "timestamp_ms": 1_750_000_000_000,  # 2025-06-15 15:06:40 UTC
        "properties": {},
        "schema_version": 1,
        "producer_id": "web-frontend",
        "environment": "production",
    }
    event.update(overrides)
    return event


PROFILES = {
    "user-1": {"user_id": "user-1", "country": "US", "plan_tier": "pro", "cohort": "2024-Q1"},
}


def enrich(event, profiles=None):
    outputs = list(EnrichEventDo().process(event, PROFILES if profiles is None else profiles))
    assert len(outputs) == 1
    return outputs[0]


# ─────────────────────────────────────────────────────────────────────────────
# Profile enrichment
# ─────────────────────────────────────────────────────────────────────────────

class TestProfileEnrichment:
    def test_known_user_gets_profile_fields(self):
        row = enrich(make_event())

        assert row["country"] == "US"
        assert row["plan_tier"] == "pro"
        assert row["user_cohort"] == "2024-Q1"

    def test_unknown_user_gets_null_profile_fields(self):
        row = enrich(make_event(user_id="user-unknown"))

        assert row["country"] is None
        assert row["plan_tier"] is None
        assert row["user_cohort"] is None

    def test_anonymous_event_gets_null_profile_fields(self):
        row = enrich(make_event(user_id=None))

        assert row["country"] is None
        assert row["plan_tier"] is None
        assert row["user_cohort"] is None

    def test_partial_profile_defaults_missing_keys_to_empty(self):
        profiles = {"user-1": {"user_id": "user-1", "country": "DE"}}
        row = enrich(make_event(), profiles)

        assert row["country"] == "DE"
        assert row["plan_tier"] == ""      # profile present, key absent → ""
        assert row["user_cohort"] == ""

    def test_input_event_is_not_mutated(self):
        event = make_event()
        snapshot = dict(event)

        enrich(event)

        assert event == snapshot


# ─────────────────────────────────────────────────────────────────────────────
# Typed property extraction
# ─────────────────────────────────────────────────────────────────────────────

class TestPropertyExtraction:
    def test_extracts_typed_properties(self):
        row = enrich(make_event(properties={
            "page_url": "/pricing",
            "referrer": "https://google.com",
            "value_usd": "49.99",
            "search_query": "enterprise plan",
        }))

        assert row["page_url"] == "/pricing"
        assert row["referrer"] == "https://google.com"
        assert row["value_usd"] == 49.99          # cast str → float
        assert row["search_query"] == "enterprise plan"

    def test_uncastable_value_becomes_null_not_dlq(self):
        row = enrich(make_event(properties={"value_usd": "not-a-number"}))

        assert row["value_usd"] is None           # bad cast degrades gracefully

    def test_absent_properties_become_null_columns(self):
        row = enrich(make_event(properties={}))

        assert row["page_url"] is None
        assert row["value_usd"] is None

    def test_none_properties_map_is_tolerated(self):
        row = enrich(make_event(properties=None))

        assert row["page_url"] is None


# ─────────────────────────────────────────────────────────────────────────────
# Derived fields
# ─────────────────────────────────────────────────────────────────────────────

class TestDerivedFields:
    def test_ios_producer_is_mobile(self):
        assert enrich(make_event(producer_id="ios-app-v2"))["is_mobile"] is True

    def test_android_producer_is_mobile(self):
        assert enrich(make_event(producer_id="android-app"))["is_mobile"] is True

    def test_web_producer_is_not_mobile(self):
        assert enrich(make_event(producer_id="web-frontend"))["is_mobile"] is False

    def test_missing_producer_is_not_mobile(self):
        event = make_event()
        del event["producer_id"]
        assert enrich(event)["is_mobile"] is False

    def test_timestamp_conversion_to_bq_formats(self):
        # 1750000000000 ms = 2025-06-15 15:06:40 UTC
        row = enrich(make_event(timestamp_ms=1_750_000_000_000))

        assert row["event_timestamp"] == "2025-06-15 15:06:40.000000 UTC"
        assert row["event_date"] == "2025-06-15"

    def test_processing_metadata_present(self):
        row = enrich(make_event())

        assert row["processed_at"].endswith("UTC")
        assert row["pipeline_version"]


# ─────────────────────────────────────────────────────────────────────────────
# LoadReferenceDataDo — GCS NDJSON parsing
# ─────────────────────────────────────────────────────────────────────────────

class TestLoadReferenceData:
    def _install_fake_gcs(self, monkeypatch, ndjson: str):
        """Stub `from google.cloud import storage` with a canned download."""
        fake_storage = ModuleType("google.cloud.storage")
        client = MagicMock(name="storage.Client()")
        blob = client.bucket.return_value.blob.return_value
        blob.download_as_text.return_value = ndjson
        fake_storage.Client = MagicMock(return_value=client)

        fake_cloud = ModuleType("google.cloud")
        fake_cloud.storage = fake_storage

        monkeypatch.setitem(sys.modules, "google.cloud", fake_cloud)
        monkeypatch.setitem(sys.modules, "google.cloud.storage", fake_storage)
        return client

    def test_parses_ndjson_into_kv_pairs(self, monkeypatch):
        ndjson = "\n".join([
            json.dumps({"user_id": "u1", "country": "US", "plan_tier": "pro"}),
            json.dumps({"user_id": "u2", "country": "DE", "plan_tier": "free"}),
        ])
        client = self._install_fake_gcs(monkeypatch, ndjson)

        pairs = list(LoadReferenceDataDo().process("gs://ref-bucket/profiles.ndjson"))

        assert pairs[0][0] == "u1"
        assert pairs[0][1]["country"] == "US"
        assert pairs[1][0] == "u2"
        assert len(pairs) == 2
        client.bucket.assert_called_once_with("ref-bucket")
        client.bucket.return_value.blob.assert_called_once_with("profiles.ndjson")

    def test_trailing_whitespace_is_tolerated(self, monkeypatch):
        ndjson = json.dumps({"user_id": "u1", "country": "US"}) + "\n\n"
        self._install_fake_gcs(monkeypatch, ndjson)

        pairs = list(LoadReferenceDataDo().process("gs://ref-bucket/profiles.ndjson"))

        assert len(pairs) == 1
