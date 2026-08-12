# Build from repo root:
#   docker build -f backend/smart-qr/Dockerfile -t smart-qr .
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/app/src

RUN apt-get update && apt-get install -y gcc curl && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY shared/smarthub_core /opt/smarthub_core
RUN pip install --no-cache-dir --upgrade pip && pip install --no-cache-dir /opt/smarthub_core

COPY backend/smart-qr/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY backend/smart-qr/src /app/src
COPY backend/smart-qr/migrations /app/migrations

RUN useradd -m -u 1000 appuser && chown -R appuser:appuser /app
USER appuser

HEALTHCHECK --interval=20s --timeout=5s --start-period=30s --retries=3 \
  CMD curl -f http://localhost:3021/health || exit 1

EXPOSE 3021
CMD ["python", "-m", "smart_qr.main"]
