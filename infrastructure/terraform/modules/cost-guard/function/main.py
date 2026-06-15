"""
cost-guard — daily spend kill-switch.

Triggered hourly by Cloud Scheduler. Computes today's net cost from the
BigQuery billing export and, if it exceeds DAILY_LIMIT_USD, performs a
TARGETED, REVERSIBLE teardown of the project's billable workloads:

  * cancel all running Dataflow jobs        (dominant daily-cost driver)
  * Cloud Run predictor: min instances -> 0 and remove public invoker
  * Vertex AI Feature Store online nodes -> 0

Nothing is deleted; every action is reversible by re-applying Terraform.

Env vars (set by Terraform):
  PROJECT_ID            GCP project id
  REGION                e.g. us-central1
  DAILY_LIMIT_USD       threshold, default "50"
  BILLING_EXPORT_TABLE  fully-qualified `project.dataset.table` of the export
  PREDICTOR_SERVICE     Cloud Run service name (default "stg-predictor")
  FEATURESTORE_ID       Vertex Feature Store id (optional; enforced to 0 nodes)
  TIME_ZONE             IANA tz for "today", default "America/Los_Angeles"
  DRY_RUN               "true" => log actions but don't execute them

Test hook (no real billing data required):
  GET ...?test_spend=75   -> evaluate the decision path as if today's spend
                             were $75. Combine with DRY_RUN=true to exercise
                             teardown logging without touching resources.
"""
import json
import logging
import os

import functions_framework
import googleapiclient.discovery
from google.cloud import bigquery

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("cost-guard")

PROJECT_ID = os.environ["PROJECT_ID"]
REGION = os.environ.get("REGION", "us-central1")
DAILY_LIMIT_USD = float(os.environ.get("DAILY_LIMIT_USD", "50"))
BILLING_EXPORT_TABLE = os.environ.get("BILLING_EXPORT_TABLE", "")
PREDICTOR_SERVICE = os.environ.get("PREDICTOR_SERVICE", "stg-predictor")
FEATURESTORE_ID = os.environ.get("FEATURESTORE_ID", "")
TIME_ZONE = os.environ.get("TIME_ZONE", "America/Los_Angeles")
DRY_RUN = os.environ.get("DRY_RUN", "false").lower() == "true"


def _today_spend_usd():
    """Net cost (cost + credits) for the current day in TIME_ZONE.

    Returns None if the export table does not exist yet (export not enabled
    or no data has landed), so the caller can no-op gracefully.
    """
    if not BILLING_EXPORT_TABLE:
        return None
    client = bigquery.Client(project=PROJECT_ID)
    sql = f"""
      SELECT
        IFNULL(SUM(cost), 0)
        + IFNULL(SUM((SELECT SUM(c.amount) FROM UNNEST(credits) c)), 0) AS net_cost
      FROM `{BILLING_EXPORT_TABLE}`
      WHERE DATE(usage_start_time, @tz) = CURRENT_DATE(@tz)
    """
    cfg = bigquery.QueryJobConfig(
        query_parameters=[bigquery.ScalarQueryParameter("tz", "STRING", TIME_ZONE)]
    )
    try:
        rows = list(client.query(sql, job_config=cfg).result())
    except Exception as exc:  # table missing / export not ready
        log.warning("Billing export not queryable yet (%s): %s",
                    BILLING_EXPORT_TABLE, exc)
        return None
    return float(rows[0].net_cost or 0.0)


def _cancel_dataflow_jobs():
    df = googleapiclient.discovery.build("dataflow", "v1b3", cache_discovery=False)
    out = []
    resp = (
        df.projects().locations().jobs()
        .list(projectId=PROJECT_ID, location=REGION, filter="ACTIVE")
        .execute()
    )
    for j in resp.get("jobs", []):
        jid, jname = j["id"], j.get("name", "?")
        if not DRY_RUN:
            df.projects().locations().jobs().update(
                projectId=PROJECT_ID, location=REGION, jobId=jid,
                body={"requestedState": "JOB_STATE_CANCELLED"},
            ).execute()
        out.append(f"dataflow:{jname}({jid}):cancelled")
    return out or ["dataflow:none-active"]


def _scale_down_cloud_run():
    run = googleapiclient.discovery.build("run", "v2", cache_discovery=False)
    name = (f"projects/{PROJECT_ID}/locations/{REGION}"
            f"/services/{PREDICTOR_SERVICE}")
    out = []
    svc = run.projects().locations().services().get(name=name).execute()
    scaling = svc.setdefault("template", {}).setdefault("scaling", {})
    scaling["minInstanceCount"] = 0
    if not DRY_RUN:
        run.projects().locations().services().patch(
            name=name, updateMask="template.scaling", body=svc
        ).execute()
    out.append(f"cloudrun:{PREDICTOR_SERVICE}:min_instances=0")

    # Cut public traffic so request-driven compute stops (reversible).
    policy = (run.projects().locations().services()
              .getIamPolicy(resource=name).execute())
    kept = []
    removed_public = False
    for b in policy.get("bindings", []):
        if b.get("role") == "roles/run.invoker" and "allUsers" in b.get("members", []):
            members = [m for m in b["members"] if m != "allUsers"]
            removed_public = True
            if members:
                kept.append({**b, "members": members})
        else:
            kept.append(b)
    if removed_public:
        if not DRY_RUN:
            policy["bindings"] = kept
            run.projects().locations().services().setIamPolicy(
                resource=name, body={"policy": policy}
            ).execute()
        out.append(f"cloudrun:{PREDICTOR_SERVICE}:public_invoker_removed")
    else:
        out.append(f"cloudrun:{PREDICTOR_SERVICE}:already_private")
    return out


def _scale_down_featurestore():
    if not FEATURESTORE_ID:
        return ["featurestore:not_configured"]
    aip = googleapiclient.discovery.build(
        "aiplatform", "v1", cache_discovery=False,
        client_options={"api_endpoint": f"https://{REGION}-aiplatform.googleapis.com"},
    )
    name = (f"projects/{PROJECT_ID}/locations/{REGION}"
            f"/featurestores/{FEATURESTORE_ID}")
    if not DRY_RUN:
        aip.projects().locations().featurestores().patch(
            name=name, updateMask="online_serving_config.fixed_node_count",
            body={"onlineServingConfig": {"fixedNodeCount": 0}},
        ).execute()
    return [f"featurestore:{FEATURESTORE_ID}:online_nodes=0"]


def _teardown():
    actions = []
    for fn in (_cancel_dataflow_jobs, _scale_down_cloud_run, _scale_down_featurestore):
        try:
            actions.extend(fn())
        except Exception as exc:
            log.exception("teardown step %s failed", fn.__name__)
            actions.append(f"{fn.__name__}:ERROR:{exc}")
    return actions


@functions_framework.http
def check_daily_spend(request):
    test_spend = request.args.get("test_spend") if request else None
    spend = float(test_spend) if test_spend is not None else _today_spend_usd()

    if spend is None:
        msg = "billing export not ready — no data for today; no action"
        log.info(msg)
        return (json.dumps({"status": "skipped", "reason": msg}), 200,
                {"Content-Type": "application/json"})

    over = spend > DAILY_LIMIT_USD
    result = {
        "status": "evaluated",
        "today_spend_usd": round(spend, 4),
        "daily_limit_usd": DAILY_LIMIT_USD,
        "over_limit": over,
        "dry_run": DRY_RUN,
        "simulated": test_spend is not None,
        "actions": [],
    }
    if over:
        log.warning("DAILY LIMIT EXCEEDED: $%.2f > $%.2f — tearing down%s",
                    spend, DAILY_LIMIT_USD, " (DRY_RUN)" if DRY_RUN else "")
        result["actions"] = _teardown()
        result["status"] = "tripped"
    else:
        log.info("OK: $%.2f <= $%.2f", spend, DAILY_LIMIT_USD)

    return (json.dumps(result), 200, {"Content-Type": "application/json"})
