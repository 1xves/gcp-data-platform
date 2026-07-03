"""
test_parse.py — unit tests for transforms/parse.py

Covers:
  - ParseAvroDo: Avro round-trip, DLQ routing on undecodable bytes,
    non-production drop, schema_version forward-compat pass-through
  - ValidateSchemaDo: every validation rule, clock-skew boundaries,
    multi-error accumulation, DLQ record shape

Avro test payloads are real fastavro-encoded bytes (not mocks) so the tests
exercise the exact serialization path used by producers.
"""

import io
import json
from datetime import datetime, timezone

import fastavro
from apache_beam import pvalue

from schemas import PLATFORM_EVENT_AVRO_SCHEMA
from transforms.parse import DLQ_TAG, VALID_TAG, ParseAvroDo, ValidateSchemaDo

PARSED_SCHEMA = fastavro.parse_schema(PLATFORM_EVENT_AVRO_SCHEMA)


def now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def make_event(**overrides) -> dict:
    """Complete, valid PlatformEvent record. Override fields per test."""
    event = {
        "event_id": "evt-0001",
        "event_type": "page_view",
        "user_id": "user-1",
        "session_id": "sess-1",
        "timestamp_ms": now_ms(),
        "properties": {},
        "schema_version": 1,
        "producer_id": "web-frontend",
        "environment": "production",
    }
    event.update(overrides)
    return event


def encode_avro(event: dict) -> bytes:
    buf = io.BytesIO()
    fastavro.schemaless_writer(buf, PARSED_SCHEMA, event)
    return buf.getvalue()


class FakePubsubMessage:
    """Stand-in for apache_beam.io.PubsubMessage: .data + .message_id."""

    def __init__(self, data: bytes, message_id: str = "msg-1"):
        self.data = data
        self.message_id = message_id


def make_parser() -> ParseAvroDo:
    dofn = ParseAvroDo()
    dofn.setup()  # compiles the Avro schema, normally called once per worker
    return dofn


# ─────────────────────────────────────────────────────────────────────────────
# ParseAvroDo
# ─────────────────────────────────────────────────────────────────────────────

class TestParseAvro:
    def test_valid_event_round_trips_to_valid_tag(self):
        event = make_event()
        outputs = list(make_parser().process(FakePubsubMessage(encode_avro(event))))

        assert len(outputs) == 1
        assert outputs[0].tag == VALID_TAG
        parsed = outputs[0].value
        assert parsed["event_id"] == "evt-0001"
        assert parsed["event_type"] == "page_view"
        assert parsed["environment"] == "production"

    def test_undecodable_bytes_route_to_dlq(self):
        outputs = list(
            make_parser().process(FakePubsubMessage(b"\xff\xfenot-avro", "msg-bad"))
        )

        assert len(outputs) == 1
        assert outputs[0].tag == DLQ_TAG
        dlq = outputs[0].value
        assert dlq["error_type"] == "avro_parse_failure"
        assert dlq["message_id"] == "msg-bad"
        assert dlq["error_detail"]          # non-empty diagnostic
        assert dlq["failed_at"]             # ISO timestamp present

    def test_dlq_record_preserves_original_bytes(self):
        raw = b"\x00\x01\x02broken"
        outputs = list(make_parser().process(FakePubsubMessage(raw)))

        assert outputs[0].value["raw_bytes_b64"] == raw.hex()

    def test_staging_event_is_dropped_silently(self):
        event = make_event(environment="staging")
        outputs = list(make_parser().process(FakePubsubMessage(encode_avro(event))))

        assert outputs == []  # dropped: no VALID output, no DLQ

    def test_development_event_is_dropped_silently(self):
        event = make_event(environment="development")
        outputs = list(make_parser().process(FakePubsubMessage(encode_avro(event))))

        assert outputs == []

    def test_newer_schema_version_still_passes(self):
        """Forward compatibility: schema_version != 1 is metered, not dropped."""
        event = make_event(schema_version=2)
        outputs = list(make_parser().process(FakePubsubMessage(encode_avro(event))))

        assert len(outputs) == 1
        assert outputs[0].tag == VALID_TAG
        assert outputs[0].value["schema_version"] == 2

    def test_truncated_message_routes_to_dlq(self):
        """A valid message cut mid-record must fail parsing, not half-parse."""
        full = encode_avro(make_event())
        outputs = list(make_parser().process(FakePubsubMessage(full[: len(full) // 2])))

        assert outputs[0].tag == DLQ_TAG


# ─────────────────────────────────────────────────────────────────────────────
# ValidateSchemaDo
# ─────────────────────────────────────────────────────────────────────────────

def validate(event: dict):
    return list(ValidateSchemaDo().process(event))


def dlq_errors(output) -> list:
    assert output.tag == DLQ_TAG
    return json.loads(output.value["error_detail"])


class TestValidateSchema:
    def test_valid_event_passes(self):
        outputs = validate(make_event())

        assert len(outputs) == 1
        assert outputs[0].tag == VALID_TAG
        assert outputs[0].value["event_id"] == "evt-0001"

    def test_missing_event_id_fails(self):
        outputs = validate(make_event(event_id=""))
        assert "invalid_event_id" in dlq_errors(outputs[0])

    def test_event_id_at_max_length_passes(self):
        outputs = validate(make_event(event_id="x" * 64))
        assert outputs[0].tag == VALID_TAG

    def test_event_id_over_max_length_fails(self):
        outputs = validate(make_event(event_id="x" * 65))
        assert "invalid_event_id" in dlq_errors(outputs[0])

    def test_unknown_event_type_fails_with_type_in_error(self):
        outputs = validate(make_event(event_type="mystery_event"))
        assert "unknown_event_type:mystery_event" in dlq_errors(outputs[0])

    def test_missing_session_id_fails(self):
        outputs = validate(make_event(session_id=""))
        assert "missing_session_id" in dlq_errors(outputs[0])

    def test_missing_producer_id_fails(self):
        outputs = validate(make_event(producer_id=""))
        assert "missing_producer_id" in dlq_errors(outputs[0])

    def test_timestamp_older_than_90_days_fails(self):
        ninety_one_days_ms = 91 * 24 * 60 * 60 * 1000
        outputs = validate(make_event(timestamp_ms=now_ms() - ninety_one_days_ms))
        assert "timestamp_too_old" in dlq_errors(outputs[0])

    def test_timestamp_within_90_days_passes(self):
        eighty_nine_days_ms = 89 * 24 * 60 * 60 * 1000
        outputs = validate(make_event(timestamp_ms=now_ms() - eighty_nine_days_ms))
        assert outputs[0].tag == VALID_TAG

    def test_future_timestamp_beyond_skew_tolerance_fails(self):
        six_minutes_ms = 6 * 60 * 1000
        outputs = validate(make_event(timestamp_ms=now_ms() + six_minutes_ms))
        assert "timestamp_future" in dlq_errors(outputs[0])

    def test_future_timestamp_within_skew_tolerance_passes(self):
        four_minutes_ms = 4 * 60 * 1000
        outputs = validate(make_event(timestamp_ms=now_ms() + four_minutes_ms))
        assert outputs[0].tag == VALID_TAG

    def test_absent_timestamp_defaults_to_zero_and_fails_as_too_old(self):
        event = make_event()
        del event["timestamp_ms"]
        outputs = validate(event)
        assert "timestamp_too_old" in dlq_errors(outputs[0])

    def test_multiple_violations_are_all_reported(self):
        outputs = validate(
            make_event(event_id="", event_type="bogus", session_id="", producer_id="")
        )
        errors = dlq_errors(outputs[0])

        assert "invalid_event_id" in errors
        assert "unknown_event_type:bogus" in errors
        assert "missing_session_id" in errors
        assert "missing_producer_id" in errors

    def test_dlq_record_shape(self):
        outputs = validate(make_event(event_id=""))
        dlq = outputs[0].value

        assert dlq["error_type"] == "validation_failure"
        assert dlq["event_id"] == ""          # original value preserved
        assert dlq["failed_at"]
