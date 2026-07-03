"""
conftest.py — pytest configuration for cost-guard function tests.

main.py reads its configuration into module-level constants at import time
(PROJECT_ID is a hard KeyError without it), so env vars must be set before
the module is imported. Tests that need different config (DRY_RUN, limits,
billing table) monkeypatch the module attributes directly rather than
reloading the module.

The function module is loaded once, under the unique name "cost_guard_main"
(via importlib, not `import main`), so a combined pytest run can never
collide with vertex_ai/bridge/main.py — which also ships as a top-level
main.py. Tests receive it through the `cost_guard` fixture.

No test in this package talks to a real GCP API: googleapiclient.discovery.build
and bigquery.Client are replaced per-test with MagicMocks. No credentials,
no network.
"""

import importlib.util
import os
import sys
from pathlib import Path

import pytest

# ── Environment (before main.py import) ──────────────────────────────────────

_REQUIRED_ENV = {
    "PROJECT_ID": "test-project",
    "REGION": "us-central1",
    "DAILY_LIMIT_USD": "50",
    "PREDICTOR_SERVICE": "stg-predictor",
    "FEATURESTORE_ID": "test_feature_store",
    "TIME_ZONE": "America/Los_Angeles",
    "DRY_RUN": "false",
}
for key, value in _REQUIRED_ENV.items():
    os.environ.setdefault(key, value)

# Intentionally unset: tests assert the module's unconfigured defaults and
# opt in per test via monkeypatch on the module attributes.
for key in ("BILLING_EXPORT_TABLE", "GKE_CLUSTER", "GKE_NODE_POOL", "GKE_LOCATION"):
    os.environ.pop(key, None)

_FUNCTION_DIR = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session")
def cost_guard():
    """Load function/main.py once, under a collision-proof module name."""
    if "cost_guard_main" not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            "cost_guard_main", _FUNCTION_DIR / "main.py"
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules["cost_guard_main"] = module
        spec.loader.exec_module(module)
    return sys.modules["cost_guard_main"]


class FakeRequest:
    """Minimal stand-in for the functions-framework Flask request."""

    def __init__(self, args=None):
        self.args = args or {}


@pytest.fixture()
def make_request():
    return FakeRequest
