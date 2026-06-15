# ADR-004: ML Serving Strategy — Online Endpoints, Traffic Splitting, and Prediction Logging

**Status:** Accepted  
**Date:** 2026-04-21

---

## Context

The ML platform must serve real-time predictions for downstream product features (recommendation
ranking, churn risk scoring, fraud detection). The serving layer must provide:
- Sub-100ms p99 latency for online inference
- Safe model rollout (canary + shadow traffic) without service disruption
- Prediction logging for model monitoring and retraining triggers
- Feature consistency between training and serving (no training-serving skew)

---

## Decision

**Vertex AI Online Endpoints** for real-time serving, with traffic splitting for canary rollout,
shadow traffic for challenger models, and async prediction logging to BigQuery.

---

## Serving Architecture

```
Calling Service (e.g., recommendation API)
    │
    │ REST: POST /v1/projects/.../endpoints/ENDPOINT_ID:predict
    │ gRPC: PredictionServiceStub.Predict()
    ▼
┌─────────────────────────────────────────────────────────────┐
│              Vertex AI Online Endpoint                       │
│                                                              │
│   ┌──────────────────────────────┐  ┌─────────────────────┐ │
│   │  Model A (production, 90%)   │  │  Model B (canary,   │ │
│   │  n1-standard-4, min 2 reps   │  │  10%)               │ │
│   └──────────────────────────────┘  └─────────────────────┘ │
│                                                              │
│   [Shadow traffic to Model C — async, no response impact]   │
└─────────────────────────────────────────────────────────────┘
    │
    │ Async log (Pub/Sub → Dataflow → BigQuery)
    ▼
┌─────────────────────────────────────────────────────────────┐
│  BigQuery: prediction_logs                                   │
│  (request_id, model_version, features, prediction,          │
│   latency_ms, timestamp)                                     │
└─────────────────────────────────────────────────────────────┘
    │
    │ Scheduled query (daily drift check)
    ▼
┌─────────────────────────────────────────────────────────────┐
│  Drift Alert (Cloud Monitoring metric)                       │
│  → triggers retraining pipeline if accuracy < threshold     │
└─────────────────────────────────────────────────────────────┘
```

---

## Traffic Management Strategy

### Stage 1: Initial Deployment
- 100% traffic → Model v1 (production)
- Shadow traffic → Model v2 (logs predictions, not served to clients)
- Evaluate v2 accuracy vs. v1 on logged shadow predictions for 48 hours

### Stage 2: Canary Rollout (if v2 metrics pass evaluation gate)
- 90% traffic → Model v1
- 10% traffic → Model v2
- Monitor: latency p99, error rate, downstream business metrics
- Duration: 72 hours minimum

### Stage 3: Full Promotion (if canary metrics pass)
- 100% traffic → Model v2
- Model v1 retained in registry (available for instant rollback)
- Shadow deployment of Model v3 begins (if in development)

### Rollback Trigger Conditions
- p99 latency > 150ms for 5-minute window
- Error rate > 0.5% for 2-minute window
- Model accuracy drop > 5% vs. baseline (detected via prediction log analysis)
- Manual trigger by on-call engineer

---

## Training-Serving Skew Elimination

The most common silent failure in production ML is training-serving skew: the features used
during training differ from the features served at inference time due to separate computation paths.

**Solution**: Single source of truth via Vertex AI Feature Store.

```
Training Path:
  BigQuery raw events
      │
      ▼ (daily batch ingestion job)
  Vertex AI Feature Store (offline store)
      │
      ▼ (Vertex AI dataset creation — time-windowed snapshot)
  Training Dataset (point-in-time correct)
      │
      ▼
  Model Training

Serving Path:
  Online request → user_id
      │
      ▼ (Feature Store online read, <10ms)
  Vertex AI Feature Store (online store — same features, same computation)
      │
      ▼
  Model Inference
```

**Key constraint**: Feature computation logic lives in ONE place — the feature ingestion job.
Neither the training pipeline nor the serving predictor computes features independently.

---

## Custom Predictor Design

Vertex AI supports custom prediction containers, used here to:
1. Accept batch of user_ids (not pre-computed features) — feature fetch happens inside predictor
2. Log request metadata asynchronously (non-blocking)
3. Apply business rules post-prediction (e.g., suppress predictions for recently churned users)
4. Return structured response with prediction + confidence + explanation

```python
# vertex_ai/serving/predictor.py (simplified)
class ChurnRiskPredictor(PredictionHandler):
    def predict(self, request):
        user_ids = request["instances"]
        features = self.feature_store_client.read_feature_values(user_ids)
        predictions = self.model.predict(features.to_numpy())
        self._log_predictions_async(user_ids, features, predictions)
        return {"predictions": predictions.tolist()}
```

---

## Consequences

**What becomes easier:**
- Instant rollback (one API call to shift traffic back to stable model)
- Shadow testing eliminates production risk during model evaluation
- Training-serving skew is structurally impossible (single feature computation path)
- Prediction logs enable continuous model monitoring without separate tooling

**What becomes harder:**
- Feature Store adds latency to online serving (~5-8ms for online read)
- Custom predictor container requires Docker build + Artifact Registry push in CI
- Traffic splitting config must be tracked in Terraform to avoid drift

**What we'll need to revisit:**
- If prediction latency SLO tightens to <50ms, evaluate embedding features in model weights
- If request volume exceeds 10K rps, evaluate gRPC streaming vs. REST for batch inference
- If explainability becomes a compliance requirement, enable Vertex Explainable AI (SHAP values)

---

## Action Items

1. [x] Build `vertex_ai/serving/predictor.py` with Feature Store client
2. [x] Build `vertex_ai/training/pipeline.py` with evaluation gate
3. [x] Create `BigQuery: prediction_logs` schema with drift monitoring query
4. [x] Terraform: endpoint with traffic split config and autoscaling
5. [ ] Load test endpoint to 1K rps; validate p99 < 100ms
6. [ ] Configure Vertex AI Model Monitoring for feature drift detection
