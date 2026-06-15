"""
task.py — Vertex AI Custom Training Job entrypoint.

Executed inside the training container. Responsibilities:
1. Parse training arguments (passed as container args by Vertex AI)
2. Load training data from BigQuery (materialized feature view)
3. Train model (ChurnRiskModel)
4. Evaluate against quality gate
5. Save artifacts to GCS (Vertex AI model upload reads from GCS)
6. Write evaluation metrics to Vertex ML Metadata (for pipeline evaluation component)
7. Exit with code 1 if quality gate fails (causes Vertex AI pipeline step to fail)
"""

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone

import pandas as pd
from google.cloud import aiplatform, bigquery, storage
from google.cloud.aiplatform import Artifact, Context, Execution

from model import ChurnRiskModel, ModelConfig

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s — %(message)s")
logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Argument Parsing
# ─────────────────────────────────────────────────────────────────────────────

def parse_args(argv):
    parser = argparse.ArgumentParser()
    parser.add_argument("--project_id",           required=True)
    parser.add_argument("--region",               default="us-central1")
    parser.add_argument("--bq_dataset",           required=True,
                        help="BigQuery dataset containing the materialized feature view")
    parser.add_argument("--feature_view",         default="mv_user_features_daily")
    parser.add_argument("--label_query_days",     type=int, default=90,
                        help="Days of history to use for training labels")
    parser.add_argument("--model_output_gcs",     required=True,
                        help="GCS directory for model artifacts (gs://bucket/path/)")
    parser.add_argument("--min_auc",              type=float, default=0.85)
    parser.add_argument("--min_precision_at_k",   type=float, default=0.80)
    parser.add_argument("--experiment_name",      default="churn-risk-training")
    parser.add_argument("--run_name",             default=None)
    return parser.parse_args(argv)


# ─────────────────────────────────────────────────────────────────────────────
# Data Loading
# ─────────────────────────────────────────────────────────────────────────────

def load_training_data(project_id: str, bq_dataset: str,
                       feature_view: str, label_days: int) -> pd.DataFrame:
    """
    Load feature snapshots joined with churn labels from BigQuery.

    Label definition: churned_within_30d = 1 if user had no activity
    in the 30 days following the snapshot_date.
    """
    client = bigquery.Client(project=project_id)

    query = f"""
    WITH features AS (
      SELECT *
      FROM `{project_id}.{bq_dataset}.{feature_view}`
      WHERE snapshot_date BETWEEN
        DATE_SUB(CURRENT_DATE(), INTERVAL {label_days + 30} DAY)
        AND DATE_SUB(CURRENT_DATE(), INTERVAL 30 DAY)
        -- We need 30 days post-snapshot to compute the label accurately
    ),
    churn_labels AS (
      SELECT
        f.user_id,
        f.snapshot_date,
        CASE
          WHEN next_activity.user_id IS NULL THEN 1
          ELSE 0
        END AS churned_within_30d
      FROM features f
      LEFT JOIN (
        SELECT DISTINCT user_id, MIN(event_date) AS first_activity_date
        FROM `{project_id}.platform_processed.processed_events`
        WHERE event_date BETWEEN DATE_SUB(CURRENT_DATE(), INTERVAL {label_days} DAY)
          AND CURRENT_DATE()
        GROUP BY 1
      ) next_activity
        ON f.user_id = next_activity.user_id
        AND next_activity.first_activity_date
            BETWEEN f.snapshot_date AND DATE_ADD(f.snapshot_date, INTERVAL 30 DAY)
    )
    SELECT f.*, cl.churned_within_30d
    FROM features f
    JOIN churn_labels cl USING (user_id, snapshot_date)
    """

    logger.info("Loading training data from BigQuery...")
    df = client.query(query).to_dataframe()
    logger.info(
        "Loaded %d rows. Churn rate: %.2f%%",
        len(df), df["churned_within_30d"].mean() * 100
    )
    return df


# ─────────────────────────────────────────────────────────────────────────────
# Main Training Loop
# ─────────────────────────────────────────────────────────────────────────────

def main(argv=None):
    args = parse_args(argv or sys.argv[1:])

    # Initialize Vertex AI SDK for experiment tracking
    aiplatform.init(
        project=args.project_id,
        location=args.region,
        experiment=args.experiment_name,
    )
    run_name = args.run_name or f"run-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}"

    with aiplatform.start_run(run_name):
        # ── Log parameters ────────────────────────────────────────────────────
        aiplatform.log_params({
            "min_auc":            args.min_auc,
            "min_precision_at_k": args.min_precision_at_k,
            "label_query_days":   args.label_query_days,
            "feature_view":       args.feature_view,
        })

        # ── Load data ─────────────────────────────────────────────────────────
        df = load_training_data(
            args.project_id, args.bq_dataset,
            args.feature_view, args.label_query_days
        )

        # Stratified train/test split (temporal — use most recent 20% as test)
        df = df.sort_values("snapshot_date")
        n_test = int(len(df) * 0.2)
        train_df = df.iloc[:-n_test]
        test_df  = df.iloc[-n_test:]
        logger.info("Train: %d rows, Test: %d rows", len(train_df), len(test_df))

        # ── Train ─────────────────────────────────────────────────────────────
        config = ModelConfig(
            min_auc=args.min_auc,
            min_precision_at_k=args.min_precision_at_k,
        )
        model = ChurnRiskModel(config=config)
        model.train(train_df)

        # ── Evaluate ──────────────────────────────────────────────────────────
        eval_result = model.evaluate(test_df)
        aiplatform.log_metrics({
            "auc":                eval_result.auc,
            "precision_positive": eval_result.precision_pos,
            "recall_positive":    eval_result.recall_pos,
            "avg_precision":      eval_result.avg_precision,
        })

        if not eval_result.passed_gate:
            logger.error("QUALITY GATE FAILED: %s", eval_result.gate_failure_reasons)
            # Write failure reason to GCS for the pipeline evaluation component to read
            reason_gcs = args.model_output_gcs.rstrip("/") + "/gate_failure.json"
            storage_client = storage.Client(project=args.project_id)
            bucket_name, blob_path = reason_gcs.replace("gs://", "").split("/", 1)
            storage_client.bucket(bucket_name).blob(blob_path).upload_from_string(
                json.dumps({"passed": False, "reasons": eval_result.gate_failure_reasons})
            )
            sys.exit(1)

        # ── Save artifacts ────────────────────────────────────────────────────
        local_output = "/tmp/model_artifacts"
        model.save(local_output)

        # Upload to GCS
        storage_client = storage.Client(project=args.project_id)
        bucket_name, prefix = args.model_output_gcs.replace("gs://", "").split("/", 1)
        bucket = storage_client.bucket(bucket_name)

        for fname in os.listdir(local_output):
            blob = bucket.blob(f"{prefix.rstrip('/')}/{fname}")
            blob.upload_from_filename(f"{local_output}/{fname}")
            logger.info("Uploaded %s → gs://%s/%s", fname, bucket_name, blob.name)

        # Write gate pass marker for the pipeline evaluation component
        success_marker = args.model_output_gcs.rstrip("/") + "/gate_success.json"
        bucket_name, blob_path = success_marker.replace("gs://", "").split("/", 1)
        storage_client.bucket(bucket_name).blob(blob_path).upload_from_string(
            json.dumps({"passed": True, "auc": eval_result.auc,
                        "precision": eval_result.precision_pos})
        )

        logger.info("Training complete. Gate PASSED. Artifacts at %s", args.model_output_gcs)


if __name__ == "__main__":
    main()
