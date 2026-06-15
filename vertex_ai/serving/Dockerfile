###############################################################################
# Predictor — Cloud Run / GKE serving container
#
# Runs the Flask + gunicorn prediction service.
# Loads XGBoost model from GCS on startup using native storage library.
# Calls Vertex AI Feature Store for online feature fetching.
#
# Cloud Run: PORT env var is injected at runtime. Gunicorn binds to $PORT.
# GKE:       PORT defaults to 8080 via the Cloud Run convention fallback.
###############################################################################

FROM python:3.11-slim AS base

# Install OS-level dependencies (only libomp for XGBoost)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libomp-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# ── Python dependencies ────────────────────────────────────────────────────────
COPY vertex_ai/serving/requirements.txt requirements-serving.txt
RUN pip install --no-cache-dir -r requirements-serving.txt

# ── Application code ───────────────────────────────────────────────────────────
# Copy everything first, then change ownership to non-root user
COPY vertex_ai/serving/predictor.py predictor.py
COPY vertex_ai/training/trainer/ trainer/

# ── Security: non-root user ────────────────────────────────────────────────────
# Use UID/GID 1000 to match GKE default fsGroup/runAsUser for consistency
RUN groupadd -r predictor -g 1000 && \
    useradd -r -g predictor -u 1000 -d /app predictor && \
    chown -R predictor:predictor /app

# Writable /tmp for model artifacts (GKE readOnlyRootFilesystem=true)
RUN mkdir -p /tmp && chown predictor:predictor /tmp

USER predictor

# ── Runtime ────────────────────────────────────────────────────────────────────
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOME=/tmp

EXPOSE 8080

# gunicorn: bind to $PORT (Cloud Run injects PORT; falls back to 8080 for GKE/local).
# sync workers handle one request at a time each — safe with the background BQ logging thread.
CMD ["sh", "-c", \
     "gunicorn predictor:app \
       --bind 0.0.0.0:${PORT:-8080} \
       --workers ${GUNICORN_WORKERS:-2} \
       --timeout ${REQUEST_TIMEOUT:-30} \
       --log-level ${LOG_LEVEL:-INFO} \
       --access-logfile - \
       --error-logfile -"]
