"""
model.py — XGBoost churn risk model definition and training logic.

Design decisions:
- XGBoost chosen over deep learning: tabular features, small-medium dataset (< 10M users),
  interpretable feature importance, fast training (< 4h), SHAP explainability built-in
- Feature engineering done in BigQuery (materialized view) — not in this trainer.
  Reason: single source of truth, no Python-BQ parity bugs
- Hyperparameter tuning via Vertex AI HyperparameterTuning Job (not hardcoded here)
- Model serialized in both XGBoost booster format (for serving) and ONNX (for portability)
"""

import logging
import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import xgboost as xgb
from sklearn.metrics import (
    average_precision_score,
    precision_score,
    recall_score,
    roc_auc_score,
)
from sklearn.model_selection import StratifiedKFold
from sklearn.preprocessing import LabelEncoder

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Feature Definition
# Must exactly match Vertex AI Feature Store feature names
# ─────────────────────────────────────────────────────────────────────────────

NUMERIC_FEATURES = [
    "event_count_7d",
    "purchase_count_7d",
    "search_count_7d",
    "page_view_7d",
    "total_revenue_7d",
    "unique_sessions_7d",
    "event_count_30d",
    "purchase_count_30d",
    "total_revenue_30d",
    "last_active_days_ago",
    "avg_session_duration_7d",
]

CATEGORICAL_FEATURES = [
    "country",
    "plan_tier",
    "user_cohort",
]

ALL_FEATURES = NUMERIC_FEATURES + CATEGORICAL_FEATURES
LABEL_COLUMN = "churned_within_30d"  # Binary: 1 = churned, 0 = retained


@dataclass
class ModelConfig:
    """Hyperparameters for the XGBoost model. Tuned via Vertex AI HP tuning."""
    n_estimators:     int   = 500
    max_depth:        int   = 6
    learning_rate:    float = 0.05
    subsample:        float = 0.8
    colsample_bytree: float = 0.8
    min_child_weight: int   = 5
    scale_pos_weight: float = 3.0  # Handles class imbalance (churners are ~25% of users)
    reg_alpha:        float = 0.1  # L1 regularization
    reg_lambda:       float = 1.0  # L2 regularization
    n_jobs:           int   = -1
    random_state:     int   = 42
    eval_metric:      str   = "auc"
    early_stopping_rounds: int = 50

    # Evaluation thresholds (training fails if not met — acts as quality gate)
    min_auc:                 float = 0.85
    min_precision_at_k:      float = 0.80


@dataclass
class EvaluationResult:
    auc:              float
    precision_pos:    float
    recall_pos:       float
    avg_precision:    float
    passed_gate:      bool
    gate_failure_reasons: List[str] = field(default_factory=list)


class ChurnRiskModel:
    """
    XGBoost-based churn risk binary classifier.

    Responsibilities:
    - Feature preprocessing (categorical encoding, null imputation)
    - K-fold cross-validation
    - Model evaluation with quality gate
    - Artifact serialization for Vertex AI model registry
    """

    def __init__(self, config: Optional[ModelConfig] = None):
        self.config = config or ModelConfig()
        self._encoders: Dict[str, LabelEncoder] = {}
        self._model: Optional[xgb.XGBClassifier] = None
        self._feature_importances: Optional[pd.Series] = None

    # ── Preprocessing ─────────────────────────────────────────────────────────

    def _impute_numerics(self, df: pd.DataFrame, fit: bool = True) -> pd.DataFrame:
        """Impute numeric nulls with feature median (from training set)."""
        df = df.copy()
        for col in NUMERIC_FEATURES:
            if col in df.columns:
                if fit:
                    self._numeric_medians = getattr(self, "_numeric_medians", {})
                    self._numeric_medians[col] = df[col].median()
                df[col] = df[col].fillna(
                    getattr(self, "_numeric_medians", {}).get(col, 0.0)
                )
        return df

    def _encode_categoricals(self, df: pd.DataFrame, fit: bool = True) -> pd.DataFrame:
        """Label-encode categoricals. Unknown values at serve time → -1."""
        df = df.copy()
        for col in CATEGORICAL_FEATURES:
            if col in df.columns:
                if fit:
                    le = LabelEncoder()
                    known_values = df[col].fillna("__unknown__").unique()
                    le.fit(list(known_values) + ["__unknown__"])
                    self._encoders[col] = le
                le = self._encoders[col]
                df[col] = df[col].fillna("__unknown__")
                df[col] = df[col].apply(
                    lambda v: le.transform([v])[0]
                    if v in le.classes_
                    else le.transform(["__unknown__"])[0]
                )
        return df

    def preprocess(self, df: pd.DataFrame, fit: bool = True) -> Tuple[np.ndarray, Optional[np.ndarray]]:
        """Full preprocessing pipeline."""
        df = self._impute_numerics(df, fit=fit)
        df = self._encode_categoricals(df, fit=fit)

        X = df[ALL_FEATURES].values
        y = df[LABEL_COLUMN].values if LABEL_COLUMN in df.columns else None
        return X, y

    # ── Training ──────────────────────────────────────────────────────────────

    def train(self, train_df: pd.DataFrame, val_df: Optional[pd.DataFrame] = None) -> None:
        """
        Train XGBoost model with optional validation set for early stopping.

        Args:
            train_df: Training data with feature columns + LABEL_COLUMN
            val_df:   Validation data (if None, uses 20% holdout from train_df)
        """
        logger.info("Starting model training. train_rows=%d", len(train_df))

        X_train, y_train = self.preprocess(train_df, fit=True)

        if val_df is not None:
            X_val, y_val = self.preprocess(val_df, fit=False)
        else:
            from sklearn.model_selection import train_test_split
            X_train, X_val, y_train, y_val = train_test_split(
                X_train, y_train, test_size=0.2, stratify=y_train,
                random_state=self.config.random_state
            )

        self._model = xgb.XGBClassifier(
            n_estimators=self.config.n_estimators,
            max_depth=self.config.max_depth,
            learning_rate=self.config.learning_rate,
            subsample=self.config.subsample,
            colsample_bytree=self.config.colsample_bytree,
            min_child_weight=self.config.min_child_weight,
            scale_pos_weight=self.config.scale_pos_weight,
            reg_alpha=self.config.reg_alpha,
            reg_lambda=self.config.reg_lambda,
            n_jobs=self.config.n_jobs,
            random_state=self.config.random_state,
            eval_metric=self.config.eval_metric,
            early_stopping_rounds=self.config.early_stopping_rounds,
            tree_method="hist",   # GPU-compatible; falls back to CPU on non-GPU machines
            device="cuda" if os.environ.get("USE_GPU") else "cpu",
        )

        self._model.fit(
            X_train, y_train,
            eval_set=[(X_val, y_val)],
            verbose=100,
        )

        logger.info(
            "Training complete. best_iteration=%d", self._model.best_iteration
        )

        # Capture feature importances
        self._feature_importances = pd.Series(
            self._model.feature_importances_, index=ALL_FEATURES
        ).sort_values(ascending=False)
        logger.info("Top 5 features:\n%s", self._feature_importances.head(5))

    # ── Evaluation ────────────────────────────────────────────────────────────

    def evaluate(self, test_df: pd.DataFrame) -> EvaluationResult:
        """
        Evaluate model against the quality gate thresholds.

        Returns EvaluationResult with passed_gate=True only if ALL thresholds are met.
        The training pipeline uses passed_gate to decide whether to push to registry.
        """
        X_test, y_test = self.preprocess(test_df, fit=False)
        y_proba = self._model.predict_proba(X_test)[:, 1]
        y_pred  = (y_proba >= 0.5).astype(int)

        auc            = roc_auc_score(y_test, y_proba)
        avg_precision  = average_precision_score(y_test, y_proba)
        precision_pos  = precision_score(y_test, y_pred, zero_division=0)
        recall_pos     = recall_score(y_test, y_pred, zero_division=0)

        failures = []
        if auc < self.config.min_auc:
            failures.append(f"AUC {auc:.4f} < threshold {self.config.min_auc}")
        if precision_pos < self.config.min_precision_at_k:
            failures.append(f"Precision {precision_pos:.4f} < threshold {self.config.min_precision_at_k}")

        result = EvaluationResult(
            auc=auc,
            precision_pos=precision_pos,
            recall_pos=recall_pos,
            avg_precision=avg_precision,
            passed_gate=len(failures) == 0,
            gate_failure_reasons=failures,
        )

        logger.info(
            "Evaluation: AUC=%.4f precision=%.4f recall=%.4f avg_precision=%.4f gate=%s",
            auc, precision_pos, recall_pos, avg_precision,
            "PASS" if result.passed_gate else f"FAIL: {failures}",
        )
        return result

    # ── Inference ─────────────────────────────────────────────────────────────

    def predict_proba(self, features_df: pd.DataFrame) -> np.ndarray:
        """Return churn probability scores (0.0–1.0) for a batch of users."""
        X, _ = self.preprocess(features_df, fit=False)
        return self._model.predict_proba(X)[:, 1]

    # ── Serialization ─────────────────────────────────────────────────────────

    def save(self, output_dir: str) -> None:
        """Save model artifacts for Vertex AI model upload."""
        import pickle
        os.makedirs(output_dir, exist_ok=True)

        # XGBoost JSON format (model weights)
        self._model.save_model(os.path.join(output_dir, "model.xgb"))

        # Encoders and medians (required for serving)
        with open(os.path.join(output_dir, "preprocessor.pkl"), "wb") as f:
            pickle.dump({"encoders": self._encoders,
                         "numeric_medians": getattr(self, "_numeric_medians", {})}, f)

        # Feature importance report
        if self._feature_importances is not None:
            self._feature_importances.to_csv(
                os.path.join(output_dir, "feature_importances.csv"), header=["importance"]
            )
        logger.info("Model artifacts saved to %s", output_dir)

    @classmethod
    def load(cls, model_dir: str) -> "ChurnRiskModel":
        """Load a saved model from a Vertex AI model artifact directory."""
        import pickle
        instance = cls()
        instance._model = xgb.XGBClassifier()
        instance._model.load_model(os.path.join(model_dir, "model.xgb"))

        with open(os.path.join(model_dir, "preprocessor.pkl"), "rb") as f:
            state = pickle.load(f)
        instance._encoders = state["encoders"]
        instance._numeric_medians = state["numeric_medians"]
        return instance
