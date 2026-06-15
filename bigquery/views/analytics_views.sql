-- =============================================================================
-- BigQuery Analytical Views
-- Production-grade: partition pruning enforced, PII excluded, cost-aware.
-- =============================================================================

-- ─────────────────────────────────────────────────────────────────────────────
-- View 1: Daily Active Users (DAU)
-- Analyst-facing. Requires date range parameter via BI Engine / Looker.
-- ─────────────────────────────────────────────────────────────────────────────

CREATE OR REPLACE VIEW `{project_id}.platform_processed.v_dau` AS
SELECT
  event_date,
  country,
  plan_tier,
  COUNT(DISTINCT user_id_pseudonymized) AS daily_active_users,
  COUNT(*)                               AS total_events,
  COUNT(DISTINCT session_id)             AS unique_sessions,
  ROUND(
    COUNT(*) / NULLIF(COUNT(DISTINCT user_id_pseudonymized), 0), 2
  )                                      AS events_per_user
FROM `{project_id}.platform_processed.processed_events_analyst_view`
-- Partition filter required by the base table — callers must supply a date range
WHERE event_date BETWEEN DATE_SUB(CURRENT_DATE(), INTERVAL 90 DAY) AND CURRENT_DATE()
GROUP BY 1, 2, 3;

-- ─────────────────────────────────────────────────────────────────────────────
-- View 2: Purchase Funnel (add_to_cart → purchase conversion)
-- ─────────────────────────────────────────────────────────────────────────────

CREATE OR REPLACE VIEW `{project_id}.platform_processed.v_purchase_funnel` AS
WITH session_events AS (
  SELECT
    session_id,
    event_date,
    country,
    plan_tier,
    MAX(CASE WHEN event_type = 'page_view'    THEN 1 ELSE 0 END) AS had_page_view,
    MAX(CASE WHEN event_type = 'add_to_cart'  THEN 1 ELSE 0 END) AS had_add_to_cart,
    MAX(CASE WHEN event_type = 'purchase'     THEN 1 ELSE 0 END) AS had_purchase,
    SUM(CASE WHEN event_type = 'purchase'     THEN COALESCE(value_usd, 0) ELSE 0 END) AS revenue
  FROM `{project_id}.platform_processed.processed_events`
  WHERE event_date BETWEEN DATE_SUB(CURRENT_DATE(), INTERVAL 30 DAY) AND CURRENT_DATE()
  GROUP BY 1, 2, 3, 4
)
SELECT
  event_date,
  country,
  plan_tier,
  SUM(had_page_view)    AS sessions_with_page_view,
  SUM(had_add_to_cart)  AS sessions_with_add_to_cart,
  SUM(had_purchase)     AS sessions_with_purchase,
  ROUND(
    SAFE_DIVIDE(SUM(had_add_to_cart), SUM(had_page_view)) * 100, 2
  )                     AS add_to_cart_rate_pct,
  ROUND(
    SAFE_DIVIDE(SUM(had_purchase), SUM(had_add_to_cart)) * 100, 2
  )                     AS cart_to_purchase_rate_pct,
  ROUND(SUM(revenue), 2) AS total_revenue
FROM session_events
GROUP BY 1, 2, 3;

-- ─────────────────────────────────────────────────────────────────────────────
-- Materialized View 3: Daily User Feature Snapshot (for ML Feature Store ingestion)
-- Refreshed automatically by BigQuery (up to 5 minutes stale, configurable)
-- This view is the SOURCE for the Vertex AI Feature Store batch ingestion job.
-- ─────────────────────────────────────────────────────────────────────────────

CREATE MATERIALIZED VIEW IF NOT EXISTS `{project_id}.platform_ml_features.mv_user_features_daily`
OPTIONS (
  enable_refresh = true,
  refresh_interval_minutes = 60  -- Refresh every hour (daily job will use latest snapshot)
)
AS
WITH
  event_counts_7d AS (
    SELECT
      user_id,
      CURRENT_DATE() AS snapshot_date,
      COUNT(*)                                           AS event_count_7d,
      COUNTIF(event_type = 'purchase')                   AS purchase_count_7d,
      COUNTIF(event_type = 'search')                     AS search_count_7d,
      COUNTIF(event_type = 'page_view')                  AS page_view_7d,
      ROUND(SUM(COALESCE(value_usd, 0)), 2)              AS total_revenue_7d,
      COUNT(DISTINCT session_id)                         AS unique_sessions_7d
    FROM `{project_id}.platform_processed.processed_events`
    WHERE
      event_date BETWEEN DATE_SUB(CURRENT_DATE(), INTERVAL 7 DAY) AND CURRENT_DATE()
      AND user_id IS NOT NULL
    GROUP BY 1, 2
  ),
  event_counts_30d AS (
    SELECT
      user_id,
      COUNT(*)                                           AS event_count_30d,
      COUNTIF(event_type = 'purchase')                   AS purchase_count_30d,
      ROUND(SUM(COALESCE(value_usd, 0)), 2)              AS total_revenue_30d
    FROM `{project_id}.platform_processed.processed_events`
    WHERE
      event_date BETWEEN DATE_SUB(CURRENT_DATE(), INTERVAL 30 DAY) AND CURRENT_DATE()
      AND user_id IS NOT NULL
    GROUP BY 1
  ),
  recency AS (
    SELECT
      user_id,
      DATE_DIFF(CURRENT_DATE(), MAX(event_date), DAY) AS last_active_days_ago
    FROM `{project_id}.platform_processed.processed_events`
    WHERE
      event_date BETWEEN DATE_SUB(CURRENT_DATE(), INTERVAL 90 DAY) AND CURRENT_DATE()
      AND user_id IS NOT NULL
    GROUP BY 1
  ),
  latest_profile AS (
    SELECT DISTINCT
      user_id,
      LAST_VALUE(country)    OVER w AS country,
      LAST_VALUE(plan_tier)  OVER w AS plan_tier,
      LAST_VALUE(user_cohort) OVER w AS user_cohort
    FROM `{project_id}.platform_processed.processed_events`
    WHERE
      event_date BETWEEN DATE_SUB(CURRENT_DATE(), INTERVAL 7 DAY) AND CURRENT_DATE()
      AND user_id IS NOT NULL
    WINDOW w AS (
      PARTITION BY user_id ORDER BY event_timestamp
      ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING
    )
  ),
  session_duration_7d AS (
    SELECT
      user_id,
      ROUND(AVG(session_duration_sec), 2) AS avg_session_duration_7d
    FROM (
      SELECT
        user_id,
        session_id,
        TIMESTAMP_DIFF(MAX(event_timestamp), MIN(event_timestamp), SECOND) AS session_duration_sec
      FROM `{project_id}.platform_processed.processed_events`
      WHERE
        event_date BETWEEN DATE_SUB(CURRENT_DATE(), INTERVAL 7 DAY) AND CURRENT_DATE()
        AND user_id IS NOT NULL
      GROUP BY 1, 2
    )
    GROUP BY 1
  )
SELECT
  e7.user_id,
  e7.snapshot_date,
  e7.event_count_7d,
  e7.purchase_count_7d,
  e7.search_count_7d,
  e7.page_view_7d,
  e7.total_revenue_7d,
  e7.unique_sessions_7d,
  COALESCE(e30.event_count_30d,    0) AS event_count_30d,
  COALESCE(e30.purchase_count_30d, 0) AS purchase_count_30d,
  COALESCE(e30.total_revenue_30d,  0) AS total_revenue_30d,
  COALESCE(r.last_active_days_ago, 999) AS last_active_days_ago,
  COALESCE(sd.avg_session_duration_7d, 0.0) AS avg_session_duration_7d,
  p.country,
  p.plan_tier,
  p.user_cohort,
  CURRENT_TIMESTAMP() AS feature_timestamp
FROM event_counts_7d e7
LEFT JOIN event_counts_30d e30 USING (user_id)
LEFT JOIN recency           r   USING (user_id)
LEFT JOIN latest_profile    p   USING (user_id)
LEFT JOIN session_duration_7d sd USING (user_id);


-- ─────────────────────────────────────────────────────────────────────────────
-- View 4: Model Drift Monitoring — rolling 7-day accuracy vs. baseline
-- Scheduled query runs daily to power Cloud Monitoring custom metric
-- ─────────────────────────────────────────────────────────────────────────────

CREATE OR REPLACE VIEW `{project_id}.platform_ml_features.v_model_drift_daily` AS
WITH predictions_with_labels AS (
  -- Join prediction logs with actual outcomes (purchases within 7 days of prediction)
  SELECT
    p.user_id,
    p.model_version,
    p.prediction_date,
    p.prediction_score,
    p.predicted_label,
    -- Actual label: did the user purchase within 7 days of prediction?
    CASE WHEN e.user_id IS NOT NULL THEN 1 ELSE 0 END AS actual_label
  FROM `{project_id}.platform_ml_features.prediction_logs` p
  LEFT JOIN (
    SELECT DISTINCT user_id, DATE(event_timestamp) AS purchase_date
    FROM `{project_id}.platform_processed.processed_events`
    WHERE event_type = 'purchase'
      AND event_date BETWEEN DATE_SUB(CURRENT_DATE(), INTERVAL 14 DAY) AND CURRENT_DATE()
  ) e
    ON p.user_id = e.user_id
    AND e.purchase_date BETWEEN p.prediction_date AND DATE_ADD(p.prediction_date, INTERVAL 7 DAY)
  WHERE p.prediction_date BETWEEN DATE_SUB(CURRENT_DATE(), INTERVAL 7 DAY) AND CURRENT_DATE()
    AND NOT p.is_shadow
)
SELECT
  prediction_date,
  model_version,
  COUNT(*)                                            AS total_predictions,
  ROUND(AVG(CASE WHEN predicted_label = CAST(actual_label AS STRING)
                 THEN 1.0 ELSE 0.0 END), 4)          AS accuracy,
  ROUND(
    SAFE_DIVIDE(
      SUM(CASE WHEN predicted_label = '1' AND actual_label = 1 THEN 1 ELSE 0 END),
      SUM(CASE WHEN predicted_label = '1' THEN 1 ELSE 0 END)
    ), 4)                                             AS precision_positive,
  ROUND(
    SAFE_DIVIDE(
      SUM(CASE WHEN predicted_label = '1' AND actual_label = 1 THEN 1 ELSE 0 END),
      SUM(CASE WHEN actual_label = 1 THEN 1 ELSE 0 END)
    ), 4)                                             AS recall_positive
FROM predictions_with_labels
GROUP BY 1, 2
ORDER BY 1 DESC, 2;
