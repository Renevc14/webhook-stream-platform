# Build from the REPO ROOT so we can include shared/smarthub_core/ in the
# build context (same pattern as backend/image-to-cell-3/Dockerfile):
#
#   docker buildx build --platform linux/amd64 \
#     -t <tag> \
#     -f services/webhook-gateway/Dockerfile \
#     .

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONPATH=/app/src

ARG BUILD_DATE=unknown
ARG VCS_REF=unknown
LABEL build_date="${BUILD_DATE}" vcs_ref="${VCS_REF}"

RUN apt-get update && apt-get install -y \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# smarthub_core first — changes less often, caches well.
COPY shared/smarthub_core /opt/smarthub_core
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir /opt/smarthub_core

COPY services/webhook-gateway/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY services/webhook-gateway/src /app/src

RUN useradd -m -u 1000 appuser && chown -R appuser:appuser /app
USER appuser

HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD curl -f http://localhost:3020/health || exit 1

EXPOSE 3020

CMD ["uvicorn", "gateway.main:app", "--host", "0.0.0.0", "--port", "3020"]
