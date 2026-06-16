-- =============================================================================
-- Migration 001: user_entity_map
-- Purpose: Links GCP churn-prediction user_ids to OSINT entity_ids.
--          Consumed by the bridge service via Supabase PostgREST.
--
-- Apply via Supabase SQL Editor or psql:
--   psql $SUPABASE_DB_URL -f database/migrations/001_user_entity_map.sql
--
-- After applying, set the secret values in GCP Secret Manager:
--   echo -n "https://<project>.supabase.co" | \
--     gcloud secrets versions add stg-supabase-url --data-file=-
--   echo -n "<service_role_jwt>" | \
--     gcloud secrets versions add stg-supabase-service-role-key --data-file=-
-- =============================================================================

-- The link table sits in the public schema alongside the OSINT entity tables.
-- osint_entity_id references entity_base.id — enforced here as FK if entity_base
-- is managed in this Supabase project. Remove the FK constraint if entity_base
-- lives in a separate schema or external database.

CREATE TABLE IF NOT EXISTS public.user_entity_map (
    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),

    -- GCP side: the user_id the churn predictor scores (opaque string, no PII constraint)
    user_id         TEXT        NOT NULL,

    -- OSINT side: the entity this user has been linked to
    osint_entity_id UUID        NOT NULL,

    -- How confident we are in this match (0.0 = speculative, 1.0 = verified)
    confidence      FLOAT       NOT NULL DEFAULT 1.0
        CONSTRAINT user_entity_map_confidence_range CHECK (confidence >= 0.0 AND confidence <= 1.0),

    -- How the link was established
    link_method     TEXT        NOT NULL
        CONSTRAINT user_entity_map_link_method CHECK (
            link_method IN ('email', 'name_fuzzy', 'linkedin_url', 'external_id', 'manual')
        ),

    linked_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- Who or what created this link (e.g., 'import_v1', 'analyst:john', 'crm_sync')
    linked_by       TEXT,

    -- Optional free-text notes for audit trail
    notes           TEXT
);

-- One user_id maps to at most one OSINT entity (a user can only be one person).
-- If you need to model uncertainty across multiple candidates, use confidence
-- scores with a separate resolution step rather than multiple rows.
CREATE UNIQUE INDEX IF NOT EXISTS user_entity_map_user_id_idx
    ON public.user_entity_map (user_id);

-- Fast reverse lookup: given an entity, find all GCP user_ids linked to it.
-- Useful for deduplication (same person, multiple accounts).
CREATE INDEX IF NOT EXISTS user_entity_map_entity_id_idx
    ON public.user_entity_map (osint_entity_id);

-- Index for time-range queries (audit, backfill jobs)
CREATE INDEX IF NOT EXISTS user_entity_map_linked_at_idx
    ON public.user_entity_map (linked_at DESC);

-- =============================================================================
-- Row Level Security
-- The bridge service uses the service_role key (bypasses RLS by design).
-- Enable RLS so that anon/authenticated keys cannot read this table.
-- =============================================================================

ALTER TABLE public.user_entity_map ENABLE ROW LEVEL SECURITY;

-- No public read. service_role key bypasses RLS entirely — that's intentional.
-- Add policies here only if non-service-role access is needed in the future.

-- =============================================================================
-- PostgREST: expose the table through the Supabase API
-- The bridge queries via:
--   GET /rest/v1/user_entity_map?user_id=eq.{user_id}
--     &select=id,osint_entity_id,confidence,link_method
-- =============================================================================

-- Grant SELECT to the postgrest service role (used by the bridge with service key)
GRANT SELECT ON public.user_entity_map TO service_role;
GRANT INSERT, UPDATE ON public.user_entity_map TO service_role;

COMMENT ON TABLE public.user_entity_map IS
    'Links GCP churn-prediction user_ids to OSINT entity_ids. '
    'Queried by the OSINT integration bridge via Supabase PostgREST. '
    'Populated manually or via CRM/identity resolution pipelines.';

COMMENT ON COLUMN public.user_entity_map.user_id IS
    'Opaque user identifier from the GCP churn prediction system. '
    'Not necessarily an email — depends on how the product assigns user IDs.';

COMMENT ON COLUMN public.user_entity_map.confidence IS
    'Match confidence 0.0–1.0. '
    '1.0 = verified (email match or manual). '
    '0.7–0.99 = high-confidence fuzzy. '
    '<0.7 = speculative — bridge still processes but tags intervention accordingly.';

COMMENT ON COLUMN public.user_entity_map.link_method IS
    'email = email address matched directly. '
    'name_fuzzy = name + company fuzzy match. '
    'linkedin_url = LinkedIn profile URL match. '
    'external_id = matched via a shared external ID (CRM, etc.). '
    'manual = analyst created this link by hand.';
