"""
test_bridge.py — unit tests for vertex_ai/bridge (main.py, osint_client.py)

Covers:
  - Pub/Sub ack/nack contract:
      200 ack   — success, expected entity miss, malformed message
      500 nack  — transient Supabase/BigQuery failures (Pub/Sub retries)
  - ENTITY_ACTION_MAP intervention routing, including unknown/None fallback
  - Enriched intervention row shape (BigQuery schema field names)
  - osint_client PostgREST parsing via httpx.MockTransport (no network)

Supabase and BigQuery are always mocked — no external calls.
"""

import base64
import json
from unittest.mock import MagicMock

import httpx
import pytest
from fastapi.testclient import TestClient


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def make_envelope(payload: dict, message_id: str = "msg-1") -> dict:
    """Build a Pub/Sub push delivery envelope around a JSON payload."""
    return {
        "message": {
            "data": base64.b64encode(json.dumps(payload).encode("utf-8")).decode(),
            "messageId": message_id,
            "publishTime": "2026-01-01T00:00:00Z",
        },
        "subscription": "projects/test/subscriptions/stg-churn-high-risk-push",
    }


def make_payload(**overrides) -> dict:
    payload = {
        "user_id": "user-1",
        "churn_risk_score": 0.87,
        "label": "high_risk",
        "model_version": "v3",
        "request_id": "req-123",
        "predicted_at": "2026-01-01T00:00:00+00:00",
    }
    payload.update(overrides)
    return payload


@pytest.fixture()
def bridge(bridge_module, monkeypatch):
    """TestClient plus mocked OSINT/BQ collaborators, fresh per test."""
    osint = MagicMock(name="osint_client")
    osint.lookup_entity_link.return_value = None
    osint.fetch_entity_info.return_value = None

    bq = MagicMock(name="bq_writer")

    monkeypatch.setattr(bridge_module, "osint_client", osint)
    monkeypatch.setattr(bridge_module, "bq_writer", bq)

    client = TestClient(bridge_module.app)
    return client, osint, bq


def entity_link(bridge_module, entity_id="ent-42", confidence=0.91, method="email_hash"):
    import osint_client as real_osint_client

    return real_osint_client.EntityLink(
        osint_entity_id=entity_id, confidence=confidence, link_method=method
    )


def entity_info(entity_type="Investor", name="Acme Capital"):
    import osint_client as real_osint_client

    return real_osint_client.EntityInfo(entity_type=entity_type, entity_name=name)


# ─────────────────────────────────────────────────────────────────────────────
# Ack semantics: malformed input is acked (200) so Pub/Sub never retries it
# ─────────────────────────────────────────────────────────────────────────────

class TestMalformedMessages:
    def test_missing_message_key_is_acked(self, bridge):
        client, osint, _ = bridge
        resp = client.post("/v1/enrich", json={"not_a_pubsub": "envelope"})

        assert resp.status_code == 200
        osint.lookup_entity_link.assert_not_called()

    def test_invalid_base64_is_acked(self, bridge):
        client, _, _ = bridge
        resp = client.post(
            "/v1/enrich", json={"message": {"data": "!!!not-base64!!!"}}
        )

        assert resp.status_code == 200

    def test_non_json_payload_is_acked(self, bridge):
        client, _, _ = bridge
        data = base64.b64encode(b"plain text, not json").decode()
        resp = client.post("/v1/enrich", json={"message": {"data": data}})

        assert resp.status_code == 200

    def test_payload_missing_user_id_is_acked(self, bridge):
        client, osint, _ = bridge
        payload = make_payload()
        del payload["user_id"]

        resp = client.post("/v1/enrich", json=make_envelope(payload))

        assert resp.status_code == 200
        osint.lookup_entity_link.assert_not_called()

    def test_payload_missing_score_is_acked(self, bridge):
        client, osint, _ = bridge
        payload = make_payload()
        del payload["churn_risk_score"]

        resp = client.post("/v1/enrich", json=make_envelope(payload))

        assert resp.status_code == 200
        osint.lookup_entity_link.assert_not_called()


# ─────────────────────────────────────────────────────────────────────────────
# Expected miss: most users have no OSINT entity — ack cleanly, write nothing
# ─────────────────────────────────────────────────────────────────────────────

class TestEntityMiss:
    def test_no_entity_link_acks_without_bq_write(self, bridge):
        client, osint, bq = bridge
        osint.lookup_entity_link.return_value = None

        resp = client.post("/v1/enrich", json=make_envelope(make_payload()))

        assert resp.status_code == 200
        osint.lookup_entity_link.assert_called_once_with("user-1")
        bq.write_intervention.assert_not_called()


# ─────────────────────────────────────────────────────────────────────────────
# Happy path: entity found → enriched row written → ack
# ─────────────────────────────────────────────────────────────────────────────

class TestEnrichmentHappyPath:
    def test_full_enrichment_writes_row_and_acks(self, bridge, bridge_module):
        client, osint, bq = bridge
        osint.lookup_entity_link.return_value = entity_link(bridge_module)
        osint.fetch_entity_info.return_value = entity_info("Investor", "Acme Capital")

        resp = client.post(
            "/v1/enrich", json=make_envelope(make_payload(), message_id="msg-77")
        )

        assert resp.status_code == 200
        bq.write_intervention.assert_called_once()
        row = bq.write_intervention.call_args.args[0]

        # Field names must match the enriched_interventions BigQuery schema.
        assert row["user_id"] == "user-1"
        assert row["osint_entity_id"] == "ent-42"
        assert row["churn_risk_score"] == 0.87
        assert row["label"] == "high_risk"
        assert row["entity_type"] == "Investor"
        assert row["entity_name"] == "Acme Capital"
        assert row["entity_confidence"] == 0.91
        assert row["link_method"] == "email_hash"
        assert row["recommended_action"] == "investor_relations_call"
        assert row["pubsub_message_id"] == "msg-77"
        assert row["request_id"] == "req-123"
        assert row["enrichment_source"] == "osint_v1"
        assert row["intervention_id"]  # non-empty UUID
        assert row["enriched_at"] and row["intervention_date"]

    def test_entity_info_missing_falls_back_to_default_action(
        self, bridge, bridge_module
    ):
        client, osint, bq = bridge
        osint.lookup_entity_link.return_value = entity_link(bridge_module)
        osint.fetch_entity_info.return_value = None  # link exists, base record gone

        resp = client.post("/v1/enrich", json=make_envelope(make_payload()))

        assert resp.status_code == 200
        row = bq.write_intervention.call_args.args[0]
        assert row["recommended_action"] == bridge_module.DEFAULT_ACTION
        assert row["entity_type"] is None


# ─────────────────────────────────────────────────────────────────────────────
# Nack semantics: transient infra failure → 500 → Pub/Sub retries
# ─────────────────────────────────────────────────────────────────────────────

class TestTransientFailures:
    def test_supabase_lookup_failure_nacks(self, bridge):
        client, osint, bq = bridge
        osint.lookup_entity_link.side_effect = httpx.ConnectError("supabase down")

        resp = client.post("/v1/enrich", json=make_envelope(make_payload()))

        assert resp.status_code == 500
        bq.write_intervention.assert_not_called()

    def test_entity_fetch_failure_nacks(self, bridge, bridge_module):
        client, osint, bq = bridge
        osint.lookup_entity_link.return_value = entity_link(bridge_module)
        osint.fetch_entity_info.side_effect = httpx.ReadTimeout("slow supabase")

        resp = client.post("/v1/enrich", json=make_envelope(make_payload()))

        assert resp.status_code == 500
        bq.write_intervention.assert_not_called()

    def test_bigquery_write_failure_nacks(self, bridge, bridge_module):
        client, osint, bq = bridge
        osint.lookup_entity_link.return_value = entity_link(bridge_module)
        osint.fetch_entity_info.return_value = entity_info()
        bq.write_intervention.side_effect = RuntimeError("streaming insert failed")

        resp = client.post("/v1/enrich", json=make_envelope(make_payload()))

        assert resp.status_code == 500


# ─────────────────────────────────────────────────────────────────────────────
# Intervention routing table
# ─────────────────────────────────────────────────────────────────────────────

class TestResolveAction:
    @pytest.mark.parametrize(
        ("entity_type", "expected"),
        [
            ("Investor", "investor_relations_call"),
            ("HNWI", "executive_outreach"),
            ("ExecutiveHNW", "executive_outreach"),
            ("Founder", "founder_success_checkin"),
            ("Corporate", "account_manager_escalation"),
            ("Philanthropic", "community_engagement"),
            ("Political", "government_relations_review"),
            ("Politician", "government_relations_review"),
            ("Nonprofit", "nonprofit_partnership_review"),
            ("CommunityLeader", "community_engagement"),
            ("IllicitActor", "compliance_review"),
        ],
    )
    def test_known_entity_types_route_correctly(
        self, bridge_module, entity_type, expected
    ):
        assert bridge_module._resolve_action(entity_type) == expected

    def test_unknown_entity_type_falls_back_to_default(self, bridge_module):
        assert bridge_module._resolve_action("AlienOverlord") == bridge_module.DEFAULT_ACTION

    def test_none_entity_type_falls_back_to_default(self, bridge_module):
        assert bridge_module._resolve_action(None) == bridge_module.DEFAULT_ACTION

    def test_action_map_covers_no_duplicate_defaults(self, bridge_module):
        # Guard: the default action must never appear as a mapped value, so a
        # "default" row in BQ always means "unmapped entity type".
        assert bridge_module.DEFAULT_ACTION not in bridge_module.ENTITY_ACTION_MAP.values()


# ─────────────────────────────────────────────────────────────────────────────
# osint_client — PostgREST response parsing via httpx.MockTransport
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture()
def osint_with_transport(bridge_module, monkeypatch):
    """Swap osint_client's shared HTTP client for one with a MockTransport."""
    import osint_client

    def with_handler(handler):
        transport = httpx.MockTransport(handler)
        mock_client = httpx.Client(
            base_url="https://test-instance.supabase.co/rest/v1",
            transport=transport,
        )
        monkeypatch.setattr(osint_client, "_http_client", mock_client)
        return osint_client

    return with_handler


class TestOsintClient:
    def test_lookup_parses_entity_link(self, osint_with_transport):
        def handler(request):
            assert "user_entity_map" in str(request.url)
            assert "user_id=eq.user-1" in str(request.url)
            return httpx.Response(
                200,
                json=[{
                    "osint_entity_id": "ent-9",
                    "confidence": "0.85",  # PostgREST may return numerics as strings
                    "link_method": "email_hash",
                }],
            )

        client = osint_with_transport(handler)
        link = client.lookup_entity_link("user-1")

        assert link.osint_entity_id == "ent-9"
        assert link.confidence == pytest.approx(0.85)
        assert link.link_method == "email_hash"

    def test_lookup_returns_none_on_empty_result(self, osint_with_transport):
        client = osint_with_transport(lambda _req: httpx.Response(200, json=[]))

        assert client.lookup_entity_link("user-unknown") is None

    def test_lookup_raises_on_server_error(self, osint_with_transport):
        client = osint_with_transport(
            lambda _req: httpx.Response(503, json={"message": "unavailable"})
        )

        with pytest.raises(httpx.HTTPStatusError):
            client.lookup_entity_link("user-1")

    def test_fetch_entity_info_parses_record(self, osint_with_transport):
        def handler(request):
            assert "entity_base" in str(request.url)
            return httpx.Response(
                200, json=[{"entity_type": "Founder", "name": "Jane Doe"}]
            )

        client = osint_with_transport(handler)
        info = client.fetch_entity_info("ent-9")

        assert info.entity_type == "Founder"
        assert info.entity_name == "Jane Doe"

    def test_fetch_entity_info_returns_none_when_missing(self, osint_with_transport):
        client = osint_with_transport(lambda _req: httpx.Response(200, json=[]))

        assert client.fetch_entity_info("ent-ghost") is None
