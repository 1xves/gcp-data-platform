"""
bridge/main.py — OSINT Integration Bridge Service

Receives Pub/Sub push notifications for high-risk churn predictions,
queries OSINT's Supabase for entity enrichment via PostgREST,
and writes enriched intervention records to BigQuery.

End-to-end flow:
  1. Predictor scores user → churn_risk_score >= threshold
  2. Predictor publishes to Pub/Sub topic: stg-churn-high-risk
  3. Pub/Sub push subscription delivers to POST /v1/enrich (this service)
  4. Bridge decodes message, looks up user in Supabase user_entity_map
  5. If entity found: fetches type/name from entity_base, computes intervention
  6. Writes enriched_interventions row to BigQuery
  7. Returns 200 → Pub/Sub acks message

Ack/Nack semantics:
  200 — ack (message processed OR expected miss, no retry needed)
  500 — nack (transient error — Supabase/BQ unavailable, retry via backoff policy)
  4xx — ack with error log (malformed message, no point retrying)

The bridge is not on the prediction hot path. It runs async after the predictor
returns its response — enrichment latency does not affect end-user latency.
"""

import base64
import json
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse

import bq_writer
import osint_client

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="OSINT Integration Bridge",
    description="Enriches high-risk churn predictions with OSINT entity context.",
    version="1.0.0",
)

# ─────────────────────────────────────────────────────────────────────────────
# Intervention routing: map OSINT entity type → recommended action
# ─────────────────────────────────────────────────────────────────────────────

ENTITY_ACTION_MAP: Dict[str, str] = {
    "Investor":        "investor_relations_call",
    "HNWI":            "executive_outreach",
    "ExecutiveHNW":    "executive_outreach",
    "Founder":         "founder_success_checkin",
    "Corporate":       "account_manager_escalation",
    "Philanthropic":   "community_engagement",
    "Political":       "government_relations_review",
    "Nonprofit":       "nonprofit_partnership_review",
    "CommunityLeader": "community_engagement",
    "Politician":      "government_relations_review",
    "IllicitActor":    "compliance_review",  # flag for compliance team
}
DEFAULT_ACTION = "account_review"


def _resolve_action(entity_type: Optional[str]) -> str:
    if not entity_type:
        return DEFAULT_ACTION
    return ENTITY_ACTION_MAP.get(entity_type, DEFAULT_ACTION)


# ─────────────────────────────────────────────────────────────────────────────
# Pub/Sub message parsing
# ─────────────────────────────────────────────────────────────────────────────

def _decode_pubsub_message(body: Dict[str, Any]) -> tuple[Dict[str, Any], str]:
    """
    Decode a Pub/Sub push delivery envelope.

    Push format:
      {
        "message": {
          "data": "<base64-encoded JSON>",
          "messageId": "...",
          "publishTime": "..."
        },
        "subscription": "projects/.../subscriptions/..."
      }

    Returns: (payload dict, message_id str)
    Raises: ValueError for malformed envelopes (caller acks these — no retry).
    """
    try:
        message = body["message"]
        raw_data = base64.b64decode(message["data"]).decode("utf-8")
        payload = json.loads(raw_data)
        message_id = message.get("messageId", "unknown")
        return payload, message_id
    except (KeyError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError(f"Malformed Pub/Sub message: {exc}") from exc


# ─────────────────────────────────────────────────────────────────────────────
# Endpoints
# ─────────────────────────────────────────────────────────────────────────────

@app.post("/v1/enrich")
async def enrich(request: Request) -> Response:
    """
    Pub/Sub push endpoint.

    Pub/Sub retries on any non-2xx response. Return 200 for:
      - Successful enrichment
      - Expected misses (user not in OSINT — most users)
      - Malformed messages (no point retrying bad data)

    Return 500 for transient infrastructure failures (Supabase/BQ down).
    """
    body = await request.json()
    now = datetime.now(timezone.utc)

    # Parse Pub/Sub envelope
    try:
        payload, message_id = _decode_pubsub_message(body)
    except ValueError as exc:
        logger.warning("Acking malformed message (no retry): %s", exc)
        return Response(status_code=200)

    user_id         = payload.get("user_id")
    churn_score     = payload.get("churn_risk_score")
    churn_label     = payload.get("label", "high_risk")
    model_version   = payload.get("model_version")
    churn_predicted = payload.get("predicted_at")
    predictor_request_id = payload.get("request_id")

    if not user_id or churn_score is None:
        logger.warning("Acking message missing user_id or churn_score: %s", payload)
        return Response(status_code=200)

    logger.info(
        "Processing enrichment: user_id=%s score=%.3f message_id=%s",
        user_id, churn_score, message_id,
    )

    # Step 1: Look up entity link in Supabase
    try:
        link = osint_client.lookup_entity_link(user_id)
    except Exception as exc:
        logger.error("Supabase lookup failed (will retry): %s", exc)
        return Response(status_code=500)

    if link is None:
        # Expected path: most users are not OSINT entities. Ack cleanly.
        return Response(status_code=200)

    # Step 2: Fetch entity metadata for intervention routing
    try:
        entity_info = osint_client.fetch_entity_info(link.osint_entity_id)
    except Exception as exc:
        logger.error("Supabase entity fetch failed (will retry): %s", exc)
        return Response(status_code=500)

    entity_type = entity_info.entity_type if entity_info else None
    entity_name = entity_info.entity_name if entity_info else None
    action = _resolve_action(entity_type)

    # Step 3: Write enriched intervention to BigQuery
    # Field names must match enriched_interventions BigQuery schema exactly.
    intervention_row = {
        "intervention_id":    str(uuid.uuid4()),      # bridge-generated UUID for deduplication
        "user_id":            user_id,
        "osint_entity_id":    link.osint_entity_id,
        "churn_risk_score":   round(float(churn_score), 4),
        "label":              churn_label,            # schema: "label" (not "churn_risk_label")
        "entity_type":        entity_type,
        "entity_name":        entity_name,
        "entity_confidence":  round(link.confidence, 4),  # schema: "entity_confidence"
        "link_method":        link.link_method,
        "model_version":      model_version,
        "request_id":         predictor_request_id,  # predictor request that triggered event
        "enrichment_source":  "osint_v1",
        "recommended_action": action,
        "pubsub_message_id":  message_id,
        "predicted_at":       churn_predicted,        # schema: "predicted_at" (not "churn_predicted_at")
        "enriched_at":        now.strftime("%Y-%m-%d %H:%M:%S UTC"),
        "intervention_date":  now.strftime("%Y-%m-%d"),
    }

    try:
        bq_writer.write_intervention(intervention_row)
    except Exception as exc:
        logger.error("BigQuery write failed (will retry): %s", exc)
        return Response(status_code=500)

    logger.info(
        "Enrichment complete: user_id=%s entity_type=%s action=%s",
        user_id, entity_type, action,
    )
    return Response(status_code=200)


@app.get("/healthz")
def health() -> JSONResponse:
    """Liveness probe — returns 200 as long as the process is running."""
    return JSONResponse({"status": "ok"})


@app.get("/readyz")
def readiness() -> JSONResponse:
    """
    Readiness probe — verifies Supabase is reachable before accepting traffic.
    Cloud Run waits for this to return 200 before routing push deliveries.
    """
    try:
        # Lightweight check: just hit the Supabase REST root (no table scan)
        import httpx
        resp = httpx.get(
            f"{osint_client.SUPABASE_URL}/rest/v1/",
            headers={"apikey": osint_client.SUPABASE_SERVICE_ROLE_KEY},
            timeout=3.0,
        )
        if resp.status_code >= 500:
            return JSONResponse(
                {"status": "not_ready", "reason": f"Supabase returned {resp.status_code}"},
                status_code=503,
            )
    except Exception as exc:
        return JSONResponse(
            {"status": "not_ready", "reason": str(exc)},
            status_code=503,
        )
    return JSONResponse({"status": "ready"})


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)
