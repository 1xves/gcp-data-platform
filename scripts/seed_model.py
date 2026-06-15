#!/usr/bin/env python3
"""
scripts/seed_model.py — Generate and upload a seed XGBoost model for staging.

Purpose:
    predictor.py loads a model from GCS on startup. No model exists until
    the training pipeline runs (Phase 5+). This script creates a minimal
    but structurally valid model (same feature schema, trivial weights) so
    the predictor pod can start and pass health checks for Phase 4 validation.

    The seed model is not accurate — it is a fixture. DO NOT use it in production.

Usage:
    pip install xgboost scikit-learn numpy pandas google-cloud-storage
    python scripts/seed_model.py

    GCS output:
        gs://project-6db0f664-1423-47cb-86d-ml-artifacts/models/staging/seed/model.xgb
        gs://project-6db0f664-1423-47cb-86d-ml-artifacts/models/staging/seed/preprocessor.pkl
"""

import logging
import os
import pickle
import sys
import tempfile

import numpy as np
import pandas as pd
from google.cloud import storage
from sklearn.preprocessing import LabelEncoder

logging.basicConfig(level=logging.INFO, format="%(levelname)s — %(message)s")
log = logging.getLogger(__name__)

# ── Target configuration ───────────────────────────────────────────────────────

PROJECT_ID  = "project-6db0f664-1423-47cb-86d"
BUCKET_NAME = f"{PROJECT_ID}-ml-artifacts"
GCS_PREFIX  = "models/staging/seed"

# Must exactly match trainer/model.py feature lists
NUMERIC_FEATURES = [
    "event_count_7d", "purchase_count_7d", "search_count_7d",
    "page_view_7d", "total_revenue_7d", "unique_sessions_7d",
    "event_count_30d", "purchase_count_30d", "total_revenue_30d",
    "last_active_days_ago", "avg_session_duration_7d",
]
CATEGORICAL_FEATURES = ["country", "plan_tier", "user_cohort"]
ALL_FEATURES  = NUMERIC_FEATURES + CATEGORICAL_FEATURES
LABEL_COLUMN  = "churned_within_30d"

N_SAMPLES = 500   # Minimal synthetic dataset — just enough to fit


def _generate_synthetic_data(n: int) -> pd.DataFrame:
    """Return a synthetic DataFrame with the correct feature schema."""
    rng = np.random.default_rng(seed=42)
    df = pd.DataFrame({
        "event_count_7d":         rng.integers(0, 100, n).astype(float),
        "purchase_count_7d":      rng.integers(0, 10, n).astype(float),
        "search_count_7d":        rng.integers(0, 50, n).astype(float),
        "page_view_7d":           rng.integers(0, 200, n).astype(float),
        "total_revenue_7d":       rng.uniform(0, 500, n),
        "unique_sessions_7d":     rng.integers(0, 30, n).astype(float),
        "event_count_30d":        rng.integers(0, 400, n).astype(float),
        "purchase_count_30d":     rng.integers(0, 40, n).astype(float),
        "total_revenue_30d":      rng.uniform(0, 2000, n),
        "last_active_days_ago":   rng.integers(0, 365, n).astype(float),
        "avg_session_duration_7d": rng.uniform(0, 1800, n),
        "country":                rng.choice(["US", "UK", "DE", "CA", "AU"], n),
        "plan_tier":              rng.choice(["free", "starter", "pro", "enterprise"], n),
        "user_cohort":            rng.choice(["2023-Q1", "2023-Q2", "2024-Q1", "2024-Q2"], n),
        LABEL_COLUMN:             rng.integers(0, 2, n),
    })
    return df


def _fit_seed_model(df: pd.DataFrame):
    """Train a minimal XGBoost model. Returns (model, encoders, medians)."""
    import xgboost as xgb

    encoders: dict = {}
    medians: dict = {}

    df = df.copy()
    for col in NUMERIC_FEATURES:
        medians[col] = float(df[col].median())
        df[col] = df[col].fillna(medians[col])

    for col in CATEGORICAL_FEATURES:
        le = LabelEncoder()
        values = df[col].fillna("__unknown__").unique().tolist() + ["__unknown__"]
        le.fit(values)
        encoders[col] = le
        df[col] = df[col].fillna("__unknown__").apply(
            lambda v: le.transform([v])[0] if v in le.classes_
            else le.transform(["__unknown__"])[0]
        )

    X = df[ALL_FEATURES].values
    y = df[LABEL_COLUMN].values

    model = xgb.XGBClassifier(
        n_estimators=10,           # minimal — this is a fixture, not a real model
        max_depth=3,
        random_state=42,
        eval_metric="logloss",
        tree_method="hist",
    )
    model.fit(X, y)
    log.info("Seed model trained. n_estimators=10, n_features=%d", len(ALL_FEATURES))
    return model, encoders, medians


def _save_artifacts(model, encoders: dict, medians: dict, out_dir: str) -> None:
    model.save_model(os.path.join(out_dir, "model.xgb"))
    with open(os.path.join(out_dir, "preprocessor.pkl"), "wb") as f:
        pickle.dump({"encoders": encoders, "numeric_medians": medians}, f)
    log.info("Artifacts saved to %s", out_dir)


def _upload_to_gcs(local_dir: str, bucket_name: str, prefix: str) -> None:
    client = storage.Client(project=PROJECT_ID)
    bucket = client.bucket(bucket_name)

    for fname in os.listdir(local_dir):
        local_path = os.path.join(local_dir, fname)
        blob_name  = f"{prefix}/{fname}"
        blob = bucket.blob(blob_name)
        blob.upload_from_filename(local_path)
        log.info("Uploaded gs://%s/%s", bucket_name, blob_name)


def main() -> None:
    log.info("Generating %d synthetic training samples...", N_SAMPLES)
    df = _generate_synthetic_data(N_SAMPLES)

    log.info("Fitting seed model...")
    model, encoders, medians = _fit_seed_model(df)

    with tempfile.TemporaryDirectory() as tmpdir:
        _save_artifacts(model, encoders, medians, tmpdir)
        log.info("Uploading to gs://%s/%s ...", BUCKET_NAME, GCS_PREFIX)
        _upload_to_gcs(tmpdir, BUCKET_NAME, GCS_PREFIX)

    log.info("Done. Predictor AIP_STORAGE_URI = gs://%s/%s", BUCKET_NAME, GCS_PREFIX)


if __name__ == "__main__":
    main()
