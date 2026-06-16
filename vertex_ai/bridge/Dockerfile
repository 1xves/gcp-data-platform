FROM python:3.11-slim

# Security: run as non-root
RUN groupadd --gid 1000 bridge && \
    useradd --uid 1000 --gid 1000 --no-create-home bridge

WORKDIR /app

# Install dependencies first (cached layer if requirements.txt unchanged)
COPY vertex_ai/bridge/requirements.txt requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copy bridge service source
COPY vertex_ai/bridge/ .

USER bridge

# Cloud Run injects PORT (default 8080)
ENV PORT=8080

CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port $PORT --workers 1"]
