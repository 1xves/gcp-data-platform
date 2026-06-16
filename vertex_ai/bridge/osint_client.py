"""
osint_client.py — Supabase PostgREST client for the OSINT integration bridge.

Queries the OSINT system's Supabase instance via its auto-generated REST API.
No PostgreSQL driver needed — all calls are HTTPS with a service role key.

Tables queried:
  user_entity_map   — links GCP user_ids to OSINT entity_ids
  entity_base       — base entity record (type, name, status)

The service role key bypasses Supabase Row Level Security and has full read
access to all tables. It must be stored in GCP Secret Manager and injected
as an env var — never hardcoded or logged.
"""

import logging
import os
from dataclasses import dataclass
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

SUPABASE_URL            = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_SERVICE_ROLE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]

# Shared HTTP client with connection pooling and reasonable timeouts.
# Bridge handles ~1 request/min on average — a single client is fine.
_http_client = httpx.Client(
    base_url=f"{SUPABASE_URL}/rest/v1",
    headers={
        "apikey": SUPABASE_SERVICE_ROLE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    },
    timeout=httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=2.0),
)


@dataclass
class EntityLink:
    """Result of a user_entity_map lookup."""
    osint_entity_id: str
    confidence: float
    link_method: str


@dataclass
class EntityInfo:
    """Minimal entity metadata from entity_base, enough for intervention routing."""
    entity_type: str          # Investor, HNWI, Founder, Corporate, etc.
    entity_name: Optional[str]


def lookup_entity_link(user_id: str) -> Optional[EntityLink]:
    """
    Query user_entity_map for the OSINT entity linked to this GCP user_id.

    Returns None if the user has no corresponding OSINT entity — the expected
    outcome for the majority of users (most product users are not in OSINT).

    Raises httpx.HTTPError if Supabase is unreachable (bridge will nack and retry).
    """
    resp = _http_client.get(
        "/user_entity_map",
        params={
            "user_id": f"eq.{user_id}",
            "select": "osint_entity_id,confidence,link_method",
            "limit": "1",
        },
    )
    resp.raise_for_status()
    rows = resp.json()

    if not rows:
        logger.info("user_entity_map miss: user_id=%s has no OSINT entity link", user_id)
        return None

    row = rows[0]
    return EntityLink(
        osint_entity_id=row["osint_entity_id"],
        confidence=float(row["confidence"]),
        link_method=row["link_method"],
    )


def fetch_entity_info(entity_id: str) -> Optional[EntityInfo]:
    """
    Fetch entity_type and name from entity_base for intervention routing.

    Returns None if entity not found (should not normally happen if link is valid).
    Raises httpx.HTTPError if Supabase is unreachable.
    """
    resp = _http_client.get(
        "/entity_base",
        params={
            "id": f"eq.{entity_id}",
            "select": "entity_type,name",
            "limit": "1",
        },
    )
    resp.raise_for_status()
    rows = resp.json()

    if not rows:
        logger.warning("entity_base miss: osint_entity_id=%s not found", entity_id)
        return None

    row = rows[0]
    return EntityInfo(
        entity_type=row.get("entity_type", "unknown"),
        entity_name=row.get("name"),
    )
