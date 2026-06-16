"""
bq_writer.py — BigQuery writer for enriched intervention records.

Writes rows to the enriched_interventions table via streaming insert.
Streaming insert is appropriate here: low volume (~1 row/enrichment event),
no batch accumulation needed, immediate availability for queries.
"""

import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict

from google.cloud import bigquery

logger = logging.getLogger(__name__)

ENRICHED_INTERVENTIONS_TABLE = os.environ["ENRICHED_INTERVENTIONS_TABLE"]

_bq_client: bigquery.Client | None = None


def _get_client() -> bigquery.Client:
    """Lazy singleton BQ client — avoids init at import time during cold start."""
    global _bq_client
    if _bq_client is None:
        _bq_client = bigquery.Client()
    return _bq_client


def write_intervention(row: Dict[str, Any]) -> None:
    """
    Streaming-insert a single intervention row to BigQuery.

    Raises google.cloud.exceptions.GoogleAPIError on failure (bridge will
    return 500 and Pub/Sub will retry).
    """
    client = _get_client()
    errors = client.insert_rows_json(ENRICHED_INTERVENTIONS_TABLE, [row])
    if errors:
        # insert_rows_json returns a list of error dicts on partial failure
        raise RuntimeError(
            f"BigQuery streaming insert failed for intervention "
            f"intervention_id={row.get('intervention_id')}: {errors}"
        )
    logger.info(
        "Wrote intervention: user_id=%s entity_id=%s score=%.3f action=%s",
        row.get("user_id"),
        row.get("osint_entity_id"),
        row.get("churn_risk_score", 0),
        row.get("recommended_action"),
    )
