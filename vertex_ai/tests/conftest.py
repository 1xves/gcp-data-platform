"""
conftest.py — pytest configuration for predictor and bridge tests.

Both services read config from env vars and construct GCP clients at import
time, so this conftest must (in order):
  1. Set the env vars the modules require (setdefault — CI-provided values win)
  2. Put serving/, training/, and bridge/ on sys.path (mirrors the Docker
     image layout: /app/predictor.py + /app/trainer/)
  3. Neutralize import-time GCP side effects BEFORE importing the modules:
       - trainer.model.ChurnRiskModel.load  → fake model (no artifacts on disk)
       - google.cloud.bigquery.Client       → MagicMock (no credentials in CI)
       - google.cloud.aiplatform_v1         → MagicMock module (GAPIC client
         construction requires credentials + a gRPC transport)

No test in this package ever talks to a real GCP API or network endpoint.
"""

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ── 1. Environment (before any module import) ────────────────────────────────

_REQUIRED_ENV = {
    # predictor
    "GOOGLE_CLOUD_PROJECT": "test-project",
    "FEATURE_STORE_ID": "test_feature_store",
    "AIP_STORAGE_URI": "/tmp/test-model",  # local path → no GCS download branch
    "PREDICTION_LOGS_TABLE": "test-project.test_dataset.prediction_logs",
    # bridge
    "SUPABASE_URL": "https://test-instance.supabase.co",
    "SUPABASE_SERVICE_ROLE_KEY": "test-service-role-key",
    "ENRICHED_INTERVENTIONS_TABLE": "test-project.test_dataset.enriched_interventions",
}
for key, value in _REQUIRED_ENV.items():
    os.environ.setdefault(key, value)

# Publishing must be OFF at import so tests opt in explicitly per test.
os.environ.pop("CHURN_HIGH_RISK_TOPIC", None)

# ── 2. sys.path (mirrors container layout) ───────────────────────────────────

VERTEX_AI_ROOT = Path(__file__).resolve().parents[1]
for subdir in ("serving", "training", "bridge"):
    p = str(VERTEX_AI_ROOT / subdir)
    if p not in sys.path:
        sys.path.insert(0, p)


# ── 3. Module fixtures ────────────────────────────────────────────────────────

class FakeChurnModel:
    """Deterministic stand-in for ChurnRiskModel. Set .scores per test."""

    def __init__(self):
        self.scores = [0.5]
        self.received_frames = []

    def predict_proba(self, features_df):
        import numpy as np

        self.received_frames.append(features_df)
        assert len(self.scores) == len(features_df), (
            "test bug: FakeChurnModel.scores length must match the batch size"
        )
        return np.asarray(self.scores, dtype=float)


class FakeFeatureClient:
    """Returns a features DataFrame without touching Feature Store."""

    def fetch_features(self, user_ids):
        import pandas as pd

        from trainer.model import ALL_FEATURES

        return pd.DataFrame(
            [{"user_id": uid, **{f: 0 for f in ALL_FEATURES}} for uid in user_ids]
        )


@pytest.fixture(scope="session")
def predictor_module():
    """Import predictor.py exactly once with all GCP side effects neutralized.

    Patches stay active for the whole session: predictor methods re-import
    google.cloud symbols inside function bodies, so the stubs must remain in
    sys.modules after import, not just during it.
    """
    fake_model = FakeChurnModel()

    aiplatform_v1 = MagicMock(name="google.cloud.aiplatform_v1")
    aiplatform_v1_types = MagicMock(name="google.cloud.aiplatform_v1.types")

    patchers = [
        patch("trainer.model.ChurnRiskModel.load", return_value=fake_model),
        patch("google.cloud.bigquery.Client", MagicMock(name="bigquery.Client")),
        patch.dict(
            sys.modules,
            {
                "google.cloud.aiplatform_v1": aiplatform_v1,
                "google.cloud.aiplatform_v1.types": aiplatform_v1_types,
            },
        ),
    ]
    for p in patchers:
        p.start()

    import predictor  # noqa: PLC0415 — import must happen under the patches

    yield predictor

    for p in reversed(patchers):
        p.stop()


@pytest.fixture()
def predictor_app(predictor_module, monkeypatch):
    """Flask test client with fresh fakes swapped in for every test."""
    fake_model = FakeChurnModel()
    log_rows = []

    fake_logger = MagicMock(name="AsyncPredictionLogger")
    fake_logger.log.side_effect = log_rows.append

    monkeypatch.setattr(predictor_module, "_model", fake_model)
    monkeypatch.setattr(predictor_module, "_feature_client", FakeFeatureClient())
    monkeypatch.setattr(predictor_module, "_prediction_logger", fake_logger)
    monkeypatch.setattr(predictor_module, "_pubsub_publisher", None)

    client = predictor_module.app.test_client()
    return client, fake_model, log_rows


@pytest.fixture(scope="session")
def bridge_module():
    """Import the bridge FastAPI app (env vars already set above)."""
    import main as bridge_main  # vertex_ai/bridge/main.py via sys.path

    return bridge_main
