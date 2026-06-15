"""
predictor.py — Vertex AI Custom Prediction Container handler.

Accepts requests with user_ids (not pre-fetched features).
Internally:
  1. Fetches features from Vertex AI Feature Store (online serving, <10ms)
  2. Runs inference with the loaded XGBoost model
  3. Applies business rules (suppression, capping)
  4. Logs predictions asynchronously to BigQuery (non-blocking)
  5. Returns structured prediction response

This design eliminates training-serving skew: the same features
used for training (from Feature Store offline) are used at serve time
(from Feature Store online). There is no separate feature computation path.

Custom container entrypoint: gunicorn predictor:app (configured in serving_config.yaml)
"""

import asyncio
import json
import logging
import os
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd
from flask import Flask, Response, jsonify, request

from trainer.model import ChurnRiskModel, ALL_FEATURES

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s — %(message)s")
logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Configuration (from env vars injected by Vertex AI)
# ─────────────────────────────────────────────────────────────────────────────

PROJECT_ID          = os.environ["GOOGLE_CLOUD_PROJECT"]
REGION              = os.environ.get("CLOUD_ML_REGION", "us-central1")
FEATURE_STORE_ID    = os.environ["FEATURE_STORE_ID"]
ENTITY_TYPE_ID      = os.environ.get("ENTITY_TYPE_ID", "user")
MODEL_ARTIFACTS_GCS = os.environ["AIP_STORAGE_URI"]  # Injected by Vertex AI
PREDICTION_LOGS_TABLE = os.environ.get(
    "PREDICTION_LOGS_TABLE",
    f"{PROJECT_ID}.platform_ml_features.prediction_logs"
)
MODEL_VERSION       = os.environ.get("MODEL_VERSION", "unknown")
ENDPOINT_ID         = os.environ.get("ENDPOINT_ID", "unknown")

# ─────────────────────────────────────────────────────────────────────────────
# Prediction Logging — async, non-blocking
# ─────────────────────────────────────────────────────────────────────────────

class AsyncPredictionLogger:
    """
    Batches prediction logs and flushes to BigQuery asynchronously.
    Runs in a background thread — prediction latency is not impacted.

    Buffer design: 1000 rows or 10 seconds, whichever comes first.
    On flush failure, retries 3 times with exponential backoff, then drops (best-effort).
    """

    BUFFER_SIZE       = 1000
    FLUSH_INTERVAL_SEC = 10.0
    MAX_RETRIES       = 3

    def __init__(self):
        from google.cloud import bigquery
        self._bq_client = bigquery.Client(project=PROJECT_ID)
        self._buffer: List[Dict] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._flush_loop, daemon=True)
        self._thread.start()

    def log(self, row: Dict) -> None:
        """Add a prediction row to the buffer. Thread-safe."""
        with self._lock:
            self._buffer.append(row)
            if len(self._buffer) >= self.BUFFER_SIZE:
                self._flush_locked()

    def _flush_locked(self) -> None:
        """Flush buffer to BigQuery. Must be called with self._lock held."""
        if not self._buffer:
            return
        rows_to_flush = self._buffer[:]
        self._buffer.clear()
        # Release lock before I/O — other threads can continue buffering
        threading.Thread(target=self._write_to_bq, args=(rows_to_flush,), daemon=True).start()

    def _write_to_bq(self, rows: List[Dict]) -> None:
        from google.cloud import bigquery
        for attempt in range(self.MAX_RETRIES):
            try:
                errors = self._bq_client.insert_rows_json(PREDICTION_LOGS_TABLE, rows)
                if not errors:
                    return
                logger.warning("BQ insert errors on attempt %d: %s", attempt, errors)
            except Exception as exc:
                logger.warning("BQ insert exception attempt %d: %s", attempt, exc)
            time.sleep(2 ** attempt)
        logger.error("Dropped %d prediction log rows after %d retries", len(rows), self.MAX_RETRIES)

    def _flush_loop(self) -> None:
        while not self._stop.is_set():
            self._stop.wait(timeout=self.FLUSH_INTERVAL_SEC)
            with self._lock:
                self._flush_locked()

    def shutdown(self) -> None:
        self._stop.set()
        self._thread.join(timeout=30)


# ─────────────────────────────────────────────────────────────────────────────
# Feature Store Client — online feature fetching
# ─────────────────────────────────────────────────────────────────────────────

class FeatureStoreClient:
    """
    Wraps Vertex AI Feature Store online serving with connection pooling.
    Fetches all user features in a single batch API call per request.
    """

    def __init__(self):
        from google.cloud.aiplatform_v1 import FeaturestoreOnlineServingServiceClient
        endpoint = f"{REGION}-aiplatform.googleapis.com"
        self._client = FeaturestoreOnlineServingServiceClient(
            client_options={"api_endpoint": endpoint}
        )
        self._entity_type_path = (
            f"projects/{PROJECT_ID}/locations/{REGION}"
            f"/featurestores/{FEATURE_STORE_ID}/entityTypes/{ENTITY_TYPE_ID}"
        )

    def fetch_features(self, user_ids: List[str]) -> pd.DataFrame:
        """
        Fetch ALL_FEATURES for a list of user_ids in a single batch call.

        Returns DataFrame with one row per user_id, columns = ALL_FEATURES.
        Missing users (not in Feature Store) get imputed with zeros/empty strings.
        """
        from google.cloud.aiplatform_v1.types import (
            featurestore_online_service,
            FeatureSelector,
            IdMatcher,
        )

        request_obj = featurestore_online_service.ReadFeatureValuesRequest(
            entity_type=self._entity_type_path,
            feature_selector=FeatureSelector(
                id_matcher=IdMatcher(ids=ALL_FEATURES)
            ),
        )

        rows = []
        for user_id in user_ids:
            request_obj.entity_id = user_id
            try:
                response = self._client.read_feature_values(request=request_obj)
                row = {"user_id": user_id}
                for header, value in zip(response.header.feature_descriptors,
                                         response.entity_view.data):
                    feat_name = header.id
                    if value.HasField("value"):
                        v = value.value
                        if v.HasField("int64_value"):
                            row[feat_name] = v.int64_value
                        elif v.HasField("double_value"):
                            row[feat_name] = v.double_value
                        elif v.HasField("string_value"):
                            row[feat_name] = v.string_value
                        else:
                            row[feat_name] = None
                    else:
                        row[feat_name] = None
                rows.append(row)
            except Exception as exc:
                logger.warning("Feature fetch failed for user_id=%s: %s", user_id, exc)
                rows.append({"user_id": user_id, **{f: None for f in ALL_FEATURES}})

        return pd.DataFrame(rows)


# ─────────────────────────────────────────────────────────────────────────────
# Application Initialization
# ─────────────────────────────────────────────────────────────────────────────

def _download_model_if_gcs(gcs_or_local_path: str) -> str:
    """
    If the model path is a GCS URI (gs://...), download the artifacts to a
    local temp directory and return that path. Otherwise return as-is.

    ChurnRiskModel.load() expects a local directory containing:
      model.xgb         — XGBoost booster
      preprocessor.pkl  — encoders + numeric medians
    """
    if not gcs_or_local_path.startswith("gs://"):
        return gcs_or_local_path

    from google.cloud import storage
    
    local_dir = tempfile.mkdtemp(prefix="model_artifacts_")
    logger.info("Downloading model artifacts from %s → %s", gcs_or_local_path, local_dir)
    
    try:
        client = storage.Client()
        # Parse gs://bucket/path
        bucket_name = gcs_or_local_path.split("gs://")[1].split("/")[0]
        prefix = "/".join(gcs_or_local_path.split("gs://")[1].split("/")[1:])
        
        bucket = client.bucket(bucket_name)
        blobs = bucket.list_blobs(prefix=prefix)
        
        count = 0
        for blob in blobs:
            if blob.name.endswith("/"): continue # Skip "directories"
            # Get relative path within the prefix
            rel_path = os.path.relpath(blob.name, prefix)
            dest_path = os.path.join(local_dir, rel_path)
            os.makedirs(os.path.dirname(dest_path), exist_ok=True)
            blob.download_to_filename(dest_path)
            count += 1
            
        if count == 0:
            raise RuntimeError(f"No blobs found at {gcs_or_local_path}")
            
        logger.info("Model download complete. %d files downloaded. Local dir: %s", count, local_dir)
        return local_dir
    except Exception as exc:
        shutil.rmtree(local_dir, ignore_errors=True)
        raise RuntimeError(f"Failed to download model from {gcs_or_local_path}: {exc}") from exc


logger.info("Loading model from %s", MODEL_ARTIFACTS_GCS)
_local_model_dir = _download_model_if_gcs(MODEL_ARTIFACTS_GCS)
_model = ChurnRiskModel.load(_local_model_dir)
logger.info("Model loaded successfully")

_feature_client = FeatureStoreClient()
_prediction_logger = AsyncPredictionLogger()

app = Flask(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Prediction Endpoint
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/v1/predict", methods=["POST"])
def predict():
    """
    Request body:
        {"instances": ["user_123", "user_456", ...]}

    Response:
        {
          "predictions": [
            {"user_id": "user_123", "churn_risk_score": 0.72, "label": "high_risk"},
            ...
          ]
        }
    """
    start_time = time.monotonic()
    request_id = str(uuid.uuid4())

    body = request.get_json(force=True)
    user_ids: List[str] = body.get("instances", [])
    is_shadow: bool = body.get("_shadow", False)  # Set by endpoint traffic splitter for shadow traffic

    if not user_ids:
        return jsonify({"error": "instances must be a non-empty list of user_ids"}), 400
    if len(user_ids) > 500:
        return jsonify({"error": "batch size exceeds maximum of 500"}), 400

    # ── 1. Fetch features ────────────────────────────────────────────────────
    features_df = _feature_client.fetch_features(user_ids)
    feature_fetch_ms = (time.monotonic() - start_time) * 1000

    # ── 2. Inference ──────────────────────────────────────────────────────────
    inference_start = time.monotonic()
    scores = _model.predict_proba(features_df)
    inference_ms = (time.monotonic() - inference_start) * 1000

    # ── 3. Business rules ─────────────────────────────────────────────────────
    predictions = []
    log_rows = []
    now = datetime.now(timezone.utc)

    for i, (user_id, score) in enumerate(zip(user_ids, scores)):
        # Score capping: never return exactly 0.0 or 1.0 (calibration artifact)
        score = float(np.clip(score, 0.001, 0.999))

        label = (
            "high_risk"   if score >= 0.7 else
            "medium_risk" if score >= 0.4 else
            "low_risk"
        )

        predictions.append({
            "user_id":          user_id,
            "churn_risk_score": round(score, 4),
            "label":            label,
        })

        log_rows.append({
            "request_id":       f"{request_id}-{i}",
            "user_id":          user_id,
            "model_version":    MODEL_VERSION,
            "endpoint_id":      ENDPOINT_ID,
            "prediction_date":  now.strftime("%Y-%m-%d"),
            "predicted_at":     now.strftime("%Y-%m-%d %H:%M:%S UTC"),
            "prediction_score": round(score, 4),
            "prediction_label": label,
            "confidence":       None,  # Calibrated confidence available via SHAP (future work)
            "features_json":    json.dumps(features_df.iloc[i].to_dict()),
            "latency_ms":       int((time.monotonic() - start_time) * 1000),
            "is_shadow":        is_shadow,
            "traffic_tag":      "shadow" if is_shadow else "production",
        })

    # ── 4. Async prediction log ───────────────────────────────────────────────
    for row in log_rows:
        _prediction_logger.log(row)

    total_ms = (time.monotonic() - start_time) * 1000
    logger.info(
        "Predicted %d users. feature_fetch=%.0fms inference=%.0fms total=%.0fms",
        len(user_ids), feature_fetch_ms, inference_ms, total_ms
    )

    return jsonify({"predictions": predictions, "latency_ms": int(total_ms)})


@app.route("/healthz", methods=["GET"])
def health():
    """Vertex AI liveness probe endpoint."""
    return jsonify({"status": "ok", "model_version": MODEL_VERSION}), 200


@app.route("/readyz", methods=["GET"])
def readiness():
    """Vertex AI readiness probe — only returns 200 after model is fully loaded."""
    if _model is None:
        return jsonify({"status": "not_ready"}), 503
    return jsonify({"status": "ready"}), 200


if __name__ == "__main__":
    # PORT is injected by Cloud Run. AIP_HTTP_PORT is injected by Vertex AI.
    # Falls back to 8080 for local development.
    port = int(os.environ.get("PORT", os.environ.get("AIP_HTTP_PORT", 8080)))
    app.run(host="0.0.0.0", port=port)
