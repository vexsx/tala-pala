# Multi-stage build for the prediction service (python:3.12-slim, non-root).
FROM python:3.12-slim AS builder

WORKDIR /build
COPY requirements.txt .
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir --upgrade pip \
    && /opt/venv/bin/pip install --no-cache-dir -r requirements.txt

FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd -r goldpred && useradd -r -g goldpred -d /app goldpred

COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    MODELS_DIR=/app/models \
    PREDICTION_PORT=8500

WORKDIR /app
COPY app ./app
COPY data_samples ./data_samples
RUN mkdir -p /app/models && chown -R goldpred:goldpred /app

USER goldpred
EXPOSE 8500

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://localhost:8500/internal/health || exit 1

CMD ["sh", "-c", "uvicorn --factory app.main:get_app --host 0.0.0.0 --port ${PREDICTION_PORT}"]
