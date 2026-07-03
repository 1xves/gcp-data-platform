"""
test_predictor.py — unit tests for vertex_ai/serving/predictor.py

Covers:
  - Request validation: empty batch, oversized batch
  - Business rules: score capping to [0.001, 0.999], label threshold boundaries
  - Prediction logging: row shape, shadow-traffic tagging
  - OSINT bridge publishing: threshold gating, shadow suppression,
    fire-and-forget failure isolation
  - AsyncPredictionLogger retry/drop behavior (deterministic, no live threads)
  - Health/readiness probes

All GCP clients are mocked in conftest.py — no network access anywhere.
"""

import json
from unittest.mock import MagicMock

import pytest


def post_predict(client, user_ids, shadow=None):
    body = {"instances": user_ids}
    if shadow is not None:
        body["_shadow"] = shadow
    return client.post("/v1/predict", json=body)


# ─────────────────────────────────────────────────────────────────────────────
# Request validation
# ─────────────────────────────────────────────────────────────────────────────

class TestRequestValidation:
    def test_empty_instances_returns_400(self, predictor_app):
        client, _, _ = predictor_app
        resp = post_predict(client, [])

        assert resp.status_code == 400
        assert "non-empty" in resp.get_json()["error"]

    def test_missing_instances_key_returns_400(self, predictor_app):
        client, _, _ = predictor_app
        resp = client.post("/v1/predict", json={})

        assert resp.status_code == 400

    def test_batch_over_500_returns_400(self, predictor_app):
        client, _, _ = predictor_app
        resp = post_predict(client, [f"user-{i}" for i in range(501)])

        assert resp.status_code == 400
        assert "500" in resp.get_json()["error"]

    def test_batch_of_exactly_500_is_accepted(self, predictor_app):
        client, model, _ = predictor_app
        model.scores = [0.5] * 500

        resp = post_predict(client, [f"user-{i}" for i in range(500)])

        assert resp.status_code == 200
        assert len(resp.get_json()["predictions"]) == 500


# ─────────────────────────────────────────────────────────────────────────────
# Business rules: score capping and labels
# ─────────────────────────────────────────────────────────────────────────────

class TestScoreCapping:
    def test_score_zero_is_capped_to_floor(self, predictor_app):
        client, model, _ = predictor_app
        model.scores = [0.0]

        resp = post_predict(client, ["user-1"])

        assert resp.get_json()["predictions"][0]["churn_risk_score"] == 0.001

    def test_score_one_is_capped_to_ceiling(self, predictor_app):
        client, model, _ = predictor_app
        model.scores = [1.0]

        resp = post_predict(client, ["user-1"])

        assert resp.get_json()["predictions"][0]["churn_risk_score"] == 0.999

    def test_interior_score_is_not_modified(self, predictor_app):
        client, model, _ = predictor_app
        model.scores = [0.5]

        resp = post_predict(client, ["user-1"])

        assert resp.get_json()["predictions"][0]["churn_risk_score"] == 0.5


class TestLabelThresholds:
    @pytest.mark.parametrize(
        ("score", "expected_label"),
        [
            (0.999, "high_risk"),
            (0.7, "high_risk"),      # boundary: >= 0.7 is high
            (0.6999, "medium_risk"),
            (0.4, "medium_risk"),    # boundary: >= 0.4 is medium
            (0.3999, "low_risk"),
            (0.001, "low_risk"),
        ],
    )
    def test_label_boundaries(self, predictor_app, score, expected_label):
        client, model, _ = predictor_app
        model.scores = [score]

        resp = post_predict(client, ["user-1"])

        assert resp.get_json()["predictions"][0]["label"] == expected_label

    def test_batch_preserves_user_order(self, predictor_app):
        client, model, _ = predictor_app
        model.scores = [0.9, 0.5, 0.1]

        resp = post_predict(client, ["user-a", "user-b", "user-c"])
        preds = resp.get_json()["predictions"]

        assert [p["user_id"] for p in preds] == ["user-a", "user-b", "user-c"]
        assert [p["label"] for p in preds] == ["high_risk", "medium_risk", "low_risk"]


# ─────────────────────────────────────────────────────────────────────────────
# Prediction logging
# ─────────────────────────────────────────────────────────────────────────────

class TestPredictionLogging:
    def test_one_log_row_per_prediction(self, predictor_app):
        client, model, log_rows = predictor_app
        model.scores = [0.2, 0.8]

        post_predict(client, ["user-1", "user-2"])

        assert len(log_rows) == 2

    def test_log_row_shape(self, predictor_app):
        client, model, log_rows = predictor_app
        model.scores = [0.8]

        post_predict(client, ["user-1"])
        row = log_rows[0]

        assert row["user_id"] == "user-1"
        assert row["prediction_score"] == 0.8
        assert row["prediction_label"] == "high_risk"
        assert row["is_shadow"] is False
        assert row["traffic_tag"] == "production"
        assert json.loads(row["features_json"])  # valid JSON, non-empty
        assert row["request_id"].endswith("-0")  # per-instance suffix

    def test_shadow_traffic_is_tagged(self, predictor_app):
        client, model, log_rows = predictor_app
        model.scores = [0.8]

        post_predict(client, ["user-1"], shadow=True)

        assert log_rows[0]["is_shadow"] is True
        assert log_rows[0]["traffic_tag"] == "shadow"


# ─────────────────────────────────────────────────────────────────────────────
# OSINT bridge publishing
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture()
def publisher(predictor_module, predictor_app, monkeypatch):
    """Arm a fake Pub/Sub publisher with a known topic and threshold."""
    fake = MagicMock(name="PublisherClient")
    monkeypatch.setattr(predictor_module, "_pubsub_publisher", fake)
    monkeypatch.setattr(
        predictor_module, "CHURN_HIGH_RISK_TOPIC", "projects/test/topics/high-risk"
    )
    monkeypatch.setattr(predictor_module, "CHURN_HIGH_RISK_THRESHOLD", 0.7)
    return fake


class TestHighRiskPublishing:
    def test_publishes_only_scores_at_or_above_threshold(
        self, predictor_app, publisher
    ):
        client, model, _ = predictor_app
        model.scores = [0.9, 0.7, 0.69, 0.1]

        post_predict(client, ["u-hi", "u-edge", "u-mid", "u-lo"])

        published_users = [
            json.loads(call.args[1].decode("utf-8"))["user_id"]
            for call in publisher.publish.call_args_list
        ]
        assert published_users == ["u-hi", "u-edge"]

    def test_event_payload_shape(self, predictor_app, publisher):
        client, model, _ = predictor_app
        model.scores = [0.95]

        post_predict(client, ["user-1"])

        topic, data = publisher.publish.call_args.args
        assert topic == "projects/test/topics/high-risk"
        event = json.loads(data.decode("utf-8"))
        assert event["user_id"] == "user-1"
        assert event["churn_risk_score"] == 0.95
        assert event["label"] == "high_risk"
        assert "request_id" in event and "predicted_at" in event

    def test_shadow_traffic_never_publishes(self, predictor_app, publisher):
        client, model, _ = predictor_app
        model.scores = [0.99]

        post_predict(client, ["user-1"], shadow=True)

        publisher.publish.assert_not_called()

    def test_publish_failure_does_not_fail_the_prediction(
        self, predictor_app, publisher
    ):
        client, model, _ = predictor_app
        model.scores = [0.99]
        publisher.publish.side_effect = RuntimeError("pubsub unavailable")

        resp = post_predict(client, ["user-1"])

        # Fire-and-forget contract: the caller still gets their prediction.
        assert resp.status_code == 200
        assert resp.get_json()["predictions"][0]["label"] == "high_risk"

    def test_no_publisher_configured_publishes_nothing(self, predictor_app):
        client, model, _ = predictor_app
        model.scores = [0.99]

        resp = post_predict(client, ["user-1"])  # _pubsub_publisher is None

        assert resp.status_code == 200


# ─────────────────────────────────────────────────────────────────────────────
# AsyncPredictionLogger — retry/drop behavior (no live flush thread involved)
# ─────────────────────────────────────────────────────────────────────────────

class TestAsyncPredictionLogger:
    def _make_logger(self, predictor_module):
        # __new__ skips __init__: no BQ client construction, no daemon thread.
        instance = predictor_module.AsyncPredictionLogger.__new__(
            predictor_module.AsyncPredictionLogger
        )
        instance._bq_client = MagicMock()
        return instance

    def test_write_stops_after_first_success(self, predictor_module, monkeypatch):
        logger = self._make_logger(predictor_module)
        logger._bq_client.insert_rows_json.return_value = []  # success

        logger._write_to_bq([{"row": 1}])

        assert logger._bq_client.insert_rows_json.call_count == 1

    def test_write_retries_then_succeeds(self, predictor_module, monkeypatch):
        monkeypatch.setattr(predictor_module.time, "sleep", lambda _s: None)
        logger = self._make_logger(predictor_module)
        logger._bq_client.insert_rows_json.side_effect = [
            [{"errors": ["transient"]}],  # attempt 0: partial failure
            [],                            # attempt 1: success
        ]

        logger._write_to_bq([{"row": 1}])

        assert logger._bq_client.insert_rows_json.call_count == 2

    def test_write_drops_after_max_retries(self, predictor_module, monkeypatch):
        monkeypatch.setattr(predictor_module.time, "sleep", lambda _s: None)
        logger = self._make_logger(predictor_module)
        logger._bq_client.insert_rows_json.side_effect = RuntimeError("bq down")

        logger._write_to_bq([{"row": 1}])  # must not raise — best-effort

        assert (
            logger._bq_client.insert_rows_json.call_count
            == predictor_module.AsyncPredictionLogger.MAX_RETRIES
        )


# ─────────────────────────────────────────────────────────────────────────────
# Probes
# ─────────────────────────────────────────────────────────────────────────────

class TestProbes:
    def test_healthz(self, predictor_app):
        client, _, _ = predictor_app
        resp = client.get("/healthz")

        assert resp.status_code == 200
        assert resp.get_json()["status"] == "ok"

    def test_readyz_ready_when_model_loaded(self, predictor_app):
        client, _, _ = predictor_app
        resp = client.get("/readyz")

        assert resp.status_code == 200
        assert resp.get_json()["status"] == "ready"

    def test_readyz_503_when_model_missing(
        self, predictor_module, predictor_app, monkeypatch
    ):
        client, _, _ = predictor_app
        monkeypatch.setattr(predictor_module, "_model", None)

        resp = client.get("/readyz")

        assert resp.status_code == 503
