"""
test_cost_guard.py — unit tests for the cost-guard kill-switch function.

Covers:
  * spend threshold decision (under / at / over the daily limit, strict >)
  * ?test_spend= hook (bypasses billing query, marks result simulated)
  * billing-export-not-ready no-op path (skipped, 200)
  * DRY_RUN mode (full action list logged, zero mutating API calls)
  * per-step teardown isolation (one step failing doesn't stop the others)
  * per-service teardown behavior (Dataflow, Cloud Run IAM, Feature Store, GKE)

The module under test comes from the `cost_guard` fixture (see conftest).
All GCP surfaces (googleapiclient discovery, bigquery.Client) are mocked;
no test touches the network.
"""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import httplib2
import pytest
from googleapiclient.errors import HttpError

BILLING_TABLE = "test-project.billing_export.gcp_billing_export_v1"


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def call_endpoint(cost_guard, request):
    """Invoke the HTTP entrypoint and parse its (body, status, headers) tuple."""
    body, status, headers = cost_guard.check_daily_spend(request)
    assert headers["Content-Type"] == "application/json"
    return json.loads(body), status


def make_http_error(status):
    """Build a googleapiclient HttpError with the given HTTP status."""
    resp = httplib2.Response({"status": status})
    return HttpError(resp=resp, content=b"error")


@pytest.fixture()
def gcp_apis(cost_guard, monkeypatch):
    """Replace googleapiclient.discovery.build with per-service MagicMocks.

    Returns a dict keyed by service name ("dataflow", "run", "aiplatform",
    "container") with sensible default responses that tests can override.
    """
    apis = {
        "dataflow": MagicMock(name="dataflow"),
        "run": MagicMock(name="run"),
        "aiplatform": MagicMock(name="aiplatform"),
        "container": MagicMock(name="container"),
    }

    # Dataflow: one active job by default.
    jobs = apis["dataflow"].projects.return_value.locations.return_value.jobs.return_value
    jobs.list.return_value.execute.return_value = {
        "jobs": [{"id": "job-1", "name": "event-processor"}]
    }

    # Cloud Run: public service with one extra invoker member.
    services = apis["run"].projects.return_value.locations.return_value.services.return_value
    services.get.return_value.execute.return_value = {
        "template": {"scaling": {"minInstanceCount": 1}}
    }
    services.getIamPolicy.return_value.execute.return_value = {
        "bindings": [
            {
                "role": "roles/run.invoker",
                "members": ["allUsers", "serviceAccount:ci@test-project.iam"],
            },
            {"role": "roles/run.viewer", "members": ["user:ves@example.com"]},
        ]
    }

    def fake_build(service, version, **kwargs):
        return apis[service]

    monkeypatch.setattr(cost_guard.googleapiclient.discovery, "build", fake_build)
    return apis


@pytest.fixture()
def bq_client(cost_guard, monkeypatch):
    """Point the module at a fake billing table and mock bigquery.Client."""
    monkeypatch.setattr(cost_guard, "BILLING_EXPORT_TABLE", BILLING_TABLE)
    client = MagicMock(name="bigquery.Client")

    def set_net_cost(value):
        client.query.return_value.result.return_value = [
            SimpleNamespace(net_cost=value)
        ]

    set_net_cost(0.0)
    monkeypatch.setattr(cost_guard.bigquery, "Client", MagicMock(return_value=client))
    client.set_net_cost = set_net_cost
    return client


# ─────────────────────────────────────────────────────────────────────────────
# Threshold decision (via the ?test_spend= hook)
# ─────────────────────────────────────────────────────────────────────────────

class TestThresholdDecision:
    def test_under_limit_takes_no_action(self, cost_guard, make_request):
        result, status = call_endpoint(cost_guard, make_request({"test_spend": "25"}))
        assert status == 200
        assert result["status"] == "evaluated"
        assert result["over_limit"] is False
        assert result["actions"] == []

    def test_exactly_at_limit_does_not_trip(self, cost_guard, make_request):
        """The comparison is strict: spend must EXCEED the limit."""
        result, _ = call_endpoint(cost_guard, make_request({"test_spend": "50"}))
        assert result["over_limit"] is False
        assert result["status"] == "evaluated"

    def test_over_limit_trips_teardown(self, cost_guard, make_request, gcp_apis):
        result, status = call_endpoint(cost_guard, make_request({"test_spend": "75"}))
        assert status == 200
        assert result["status"] == "tripped"
        assert result["over_limit"] is True
        assert result["actions"], "tripped response must list teardown actions"

    def test_response_echoes_spend_and_limit(self, cost_guard, make_request):
        result, _ = call_endpoint(cost_guard, make_request({"test_spend": "75.5"}))
        assert result["today_spend_usd"] == 75.5
        assert result["daily_limit_usd"] == 50.0

    def test_one_cent_over_limit_trips(self, cost_guard, make_request, gcp_apis):
        result, _ = call_endpoint(cost_guard, make_request({"test_spend": "50.01"}))
        assert result["over_limit"] is True


# ─────────────────────────────────────────────────────────────────────────────
# test_spend hook semantics
# ─────────────────────────────────────────────────────────────────────────────

class TestTestSpendHook:
    def test_hook_bypasses_billing_query(self, cost_guard, make_request, monkeypatch):
        def explode():
            raise AssertionError("billing query must not run when test_spend is set")

        monkeypatch.setattr(cost_guard, "_today_spend_usd", explode)
        result, _ = call_endpoint(cost_guard, make_request({"test_spend": "10"}))
        assert result["status"] == "evaluated"

    def test_hook_marks_result_simulated(self, cost_guard, make_request):
        result, _ = call_endpoint(cost_guard, make_request({"test_spend": "10"}))
        assert result["simulated"] is True

    def test_real_path_is_not_simulated(self, cost_guard, make_request, monkeypatch):
        monkeypatch.setattr(cost_guard, "_today_spend_usd", lambda: 10.0)
        result, _ = call_endpoint(cost_guard, make_request())
        assert result["simulated"] is False
        assert result["today_spend_usd"] == 10.0

    def test_none_request_uses_billing_query(self, cost_guard, monkeypatch):
        """Scheduler invocations may pass no request object at all."""
        monkeypatch.setattr(cost_guard, "_today_spend_usd", lambda: 10.0)
        result, _ = call_endpoint(cost_guard, None)
        assert result["status"] == "evaluated"


# ─────────────────────────────────────────────────────────────────────────────
# Billing export not ready — graceful no-op
# ─────────────────────────────────────────────────────────────────────────────

class TestBillingExportNotReady:
    def test_none_spend_returns_skipped_200(self, cost_guard, monkeypatch):
        monkeypatch.setattr(cost_guard, "_today_spend_usd", lambda: None)
        result, status = call_endpoint(cost_guard, None)
        assert status == 200
        assert result["status"] == "skipped"

    def test_unconfigured_table_returns_none(self, cost_guard):
        # BILLING_EXPORT_TABLE is unset in conftest — default module state.
        assert cost_guard.BILLING_EXPORT_TABLE == ""
        assert cost_guard._today_spend_usd() is None

    def test_query_exception_returns_none(self, cost_guard, monkeypatch):
        monkeypatch.setattr(cost_guard, "BILLING_EXPORT_TABLE", BILLING_TABLE)
        client = MagicMock()
        client.query.side_effect = Exception("Not found: Table")
        monkeypatch.setattr(
            cost_guard.bigquery, "Client", MagicMock(return_value=client)
        )
        assert cost_guard._today_spend_usd() is None

    def test_query_returns_net_cost_as_float(self, cost_guard, bq_client):
        bq_client.set_net_cost(12.34)
        assert cost_guard._today_spend_usd() == 12.34

    def test_null_net_cost_coerces_to_zero(self, cost_guard, bq_client):
        bq_client.set_net_cost(None)
        assert cost_guard._today_spend_usd() == 0.0

    def test_query_targets_configured_table(self, cost_guard, bq_client):
        cost_guard._today_spend_usd()
        sql = bq_client.query.call_args[0][0]
        assert BILLING_TABLE in sql


# ─────────────────────────────────────────────────────────────────────────────
# DRY_RUN mode
# ─────────────────────────────────────────────────────────────────────────────

class TestDryRun:
    def test_dry_run_logs_actions_without_mutations(
        self, cost_guard, make_request, gcp_apis, monkeypatch
    ):
        monkeypatch.setattr(cost_guard, "DRY_RUN", True)
        result, _ = call_endpoint(cost_guard, make_request({"test_spend": "75"}))

        assert result["dry_run"] is True
        assert result["status"] == "tripped"
        # Full action list is still reported...
        joined = " ".join(result["actions"])
        assert "dataflow:event-processor(job-1):cancelled" in joined
        assert "cloudrun:stg-predictor:min_instances=0" in joined
        assert "cloudrun:stg-predictor:public_invoker_removed" in joined
        assert "featurestore:test_feature_store:online_nodes=0" in joined

        # ...but no mutating API call was made.
        jobs = gcp_apis["dataflow"].projects.return_value.locations.return_value.jobs.return_value
        services = gcp_apis["run"].projects.return_value.locations.return_value.services.return_value
        featurestores = (
            gcp_apis["aiplatform"].projects.return_value.locations.return_value
            .featurestores.return_value
        )
        jobs.update.assert_not_called()
        services.patch.assert_not_called()
        services.setIamPolicy.assert_not_called()
        featurestores.patch.assert_not_called()

    def test_live_run_executes_mutations(
        self, cost_guard, make_request, gcp_apis, monkeypatch
    ):
        monkeypatch.setattr(cost_guard, "DRY_RUN", False)
        result, _ = call_endpoint(cost_guard, make_request({"test_spend": "75"}))

        assert result["dry_run"] is False
        jobs = gcp_apis["dataflow"].projects.return_value.locations.return_value.jobs.return_value
        services = gcp_apis["run"].projects.return_value.locations.return_value.services.return_value
        featurestores = (
            gcp_apis["aiplatform"].projects.return_value.locations.return_value
            .featurestores.return_value
        )
        jobs.update.assert_called_once()
        services.patch.assert_called_once()
        services.setIamPolicy.assert_called_once()
        featurestores.patch.assert_called_once()

    def test_cancel_requests_cancelled_state(self, cost_guard, gcp_apis, monkeypatch):
        monkeypatch.setattr(cost_guard, "DRY_RUN", False)
        cost_guard._cancel_dataflow_jobs()
        jobs = gcp_apis["dataflow"].projects.return_value.locations.return_value.jobs.return_value
        body = jobs.update.call_args.kwargs["body"]
        assert body == {"requestedState": "JOB_STATE_CANCELLED"}


# ─────────────────────────────────────────────────────────────────────────────
# Teardown step isolation
# ─────────────────────────────────────────────────────────────────────────────

class TestTeardownIsolation:
    def test_failing_first_step_does_not_stop_later_steps(
        self, cost_guard, gcp_apis, monkeypatch
    ):
        def broken():
            raise RuntimeError("dataflow API unreachable")

        monkeypatch.setattr(cost_guard, "_cancel_dataflow_jobs", broken)
        actions = cost_guard._teardown()

        errors = [a for a in actions if ":ERROR:" in a]
        assert len(errors) == 1
        assert "dataflow API unreachable" in errors[0]
        # Later steps still ran.
        joined = " ".join(actions)
        assert "cloudrun:stg-predictor:min_instances=0" in joined
        assert "featurestore:test_feature_store:online_nodes=0" in joined

    def test_every_step_failing_still_returns_all_errors(self, cost_guard, monkeypatch):
        for name in ("_cancel_dataflow_jobs", "_scale_down_cloud_run",
                     "_scale_down_featurestore", "_scale_down_gke"):
            monkeypatch.setattr(
                cost_guard, name,
                lambda _n=name: (_ for _ in ()).throw(RuntimeError(f"{_n} down")),
            )
        actions = cost_guard._teardown()
        assert len(actions) == 4
        assert all(":ERROR:" in a for a in actions)


# ─────────────────────────────────────────────────────────────────────────────
# Per-service teardown behavior
# ─────────────────────────────────────────────────────────────────────────────

class TestDataflow:
    def test_no_active_jobs_reports_none_active(self, cost_guard, gcp_apis):
        jobs = gcp_apis["dataflow"].projects.return_value.locations.return_value.jobs.return_value
        jobs.list.return_value.execute.return_value = {}
        assert cost_guard._cancel_dataflow_jobs() == ["dataflow:none-active"]

    def test_cancels_every_active_job(self, cost_guard, gcp_apis, monkeypatch):
        monkeypatch.setattr(cost_guard, "DRY_RUN", False)
        jobs = gcp_apis["dataflow"].projects.return_value.locations.return_value.jobs.return_value
        jobs.list.return_value.execute.return_value = {
            "jobs": [{"id": "j1", "name": "a"}, {"id": "j2", "name": "b"}]
        }
        actions = cost_guard._cancel_dataflow_jobs()
        assert len(actions) == 2
        assert jobs.update.call_count == 2


class TestCloudRunIam:
    def test_removes_only_all_users_keeps_other_members(
        self, cost_guard, gcp_apis, monkeypatch
    ):
        monkeypatch.setattr(cost_guard, "DRY_RUN", False)
        cost_guard._scale_down_cloud_run()
        services = gcp_apis["run"].projects.return_value.locations.return_value.services.return_value
        policy = services.setIamPolicy.call_args.kwargs["body"]["policy"]
        invoker = [b for b in policy["bindings"] if b["role"] == "roles/run.invoker"]
        assert invoker == [
            {"role": "roles/run.invoker",
             "members": ["serviceAccount:ci@test-project.iam"]}
        ]
        # Unrelated binding untouched.
        assert {"role": "roles/run.viewer", "members": ["user:ves@example.com"]} \
            in policy["bindings"]

    def test_already_private_service_skips_iam_write(
        self, cost_guard, gcp_apis, monkeypatch
    ):
        monkeypatch.setattr(cost_guard, "DRY_RUN", False)
        services = gcp_apis["run"].projects.return_value.locations.return_value.services.return_value
        services.getIamPolicy.return_value.execute.return_value = {
            "bindings": [{"role": "roles/run.invoker",
                          "members": ["serviceAccount:ci@test-project.iam"]}]
        }
        actions = cost_guard._scale_down_cloud_run()
        assert "cloudrun:stg-predictor:already_private" in actions
        services.setIamPolicy.assert_not_called()

    def test_binding_with_only_all_users_is_dropped_entirely(
        self, cost_guard, gcp_apis, monkeypatch
    ):
        monkeypatch.setattr(cost_guard, "DRY_RUN", False)
        services = gcp_apis["run"].projects.return_value.locations.return_value.services.return_value
        services.getIamPolicy.return_value.execute.return_value = {
            "bindings": [{"role": "roles/run.invoker", "members": ["allUsers"]}]
        }
        cost_guard._scale_down_cloud_run()
        policy = services.setIamPolicy.call_args.kwargs["body"]["policy"]
        assert policy["bindings"] == []


class TestFeatureStore:
    def test_not_configured_no_ops(self, cost_guard, monkeypatch):
        monkeypatch.setattr(cost_guard, "FEATURESTORE_ID", "")
        assert cost_guard._scale_down_featurestore() == ["featurestore:not_configured"]

    def test_scales_online_nodes_to_zero(self, cost_guard, gcp_apis, monkeypatch):
        monkeypatch.setattr(cost_guard, "DRY_RUN", False)
        cost_guard._scale_down_featurestore()
        featurestores = (
            gcp_apis["aiplatform"].projects.return_value.locations.return_value
            .featurestores.return_value
        )
        kwargs = featurestores.patch.call_args.kwargs
        assert kwargs["body"] == {"onlineServingConfig": {"fixedNodeCount": 0}}
        assert kwargs["updateMask"] == "online_serving_config.fixed_node_count"


class TestGke:
    def test_not_configured_no_ops(self, cost_guard):
        # GKE env vars are unset in conftest — default module state.
        assert cost_guard._scale_down_gke() == ["gke:not_configured"]

    @staticmethod
    def _configure_gke(cost_guard, monkeypatch):
        monkeypatch.setattr(cost_guard, "GKE_CLUSTER", "stg-gke")
        monkeypatch.setattr(cost_guard, "GKE_NODE_POOL", "default-pool")
        monkeypatch.setattr(cost_guard, "GKE_LOCATION", "us-central1")

    def test_deletes_node_pool(self, cost_guard, gcp_apis, monkeypatch):
        self._configure_gke(cost_guard, monkeypatch)
        monkeypatch.setattr(cost_guard, "DRY_RUN", False)
        actions = cost_guard._scale_down_gke()
        assert actions == ["gke:stg-gke/default-pool:node_pool_deleted"]

    def test_missing_node_pool_is_not_an_error(self, cost_guard, gcp_apis, monkeypatch):
        self._configure_gke(cost_guard, monkeypatch)
        monkeypatch.setattr(cost_guard, "DRY_RUN", False)
        node_pools = (
            gcp_apis["container"].projects.return_value.locations.return_value
            .clusters.return_value.nodePools.return_value
        )
        node_pools.delete.return_value.execute.side_effect = make_http_error(404)
        actions = cost_guard._scale_down_gke()
        assert actions == ["gke:stg-gke/default-pool:already_absent"]

    def test_non_404_error_propagates(self, cost_guard, gcp_apis, monkeypatch):
        self._configure_gke(cost_guard, monkeypatch)
        monkeypatch.setattr(cost_guard, "DRY_RUN", False)
        node_pools = (
            gcp_apis["container"].projects.return_value.locations.return_value
            .clusters.return_value.nodePools.return_value
        )
        node_pools.delete.return_value.execute.side_effect = make_http_error(500)
        with pytest.raises(HttpError):
            cost_guard._scale_down_gke()
