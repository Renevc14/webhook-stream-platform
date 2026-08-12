# Build from the REPO ROOT so we can include shared/smarthub_core/ in the build context.
#
#   docker buildx build --platform linux/amd64 \
#     -t <tag> \
#     -f backend/image-to-cell-3/Dockerfile \
#     .
#
# Building from backend/image-to-cell-3/ will fail because COPY needs to reach
# shared/smarthub_core/.

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONPATH=/app

ARG BUILD_DATE=unknown
ARG VCS_REF=unknown
LABEL build_date="${BUILD_DATE}" vcs_ref="${VCS_REF}"

RUN apt-get update && apt-get install -y \
    gcc \
    g++ \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install the shared smarthub_core library first. Its sources change less
# often than i2c-3 itself, so this layer caches well across rebuilds.
COPY shared/smarthub_core /opt/smarthub_core
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir /opt/smarthub_core

# Install i2c-3 Python dependencies (smarthub_core is already installed).
COPY backend/image-to-cell-3/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Copy only the runtime sources. Everything else (scripts, .env, logs, venv,
# CHANGELOG, tests) stays out of the image. No .dockerignore needed because
# COPY statements are explicit.
COPY backend/image-to-cell-3/src /app/src

RUN useradd -m -u 1000 appuser && chown -R appuser:appuser /app
USER appuser

HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD curl -f http://localhost:3011/health || exit 1

EXPOSE 3011

CMD ["python", "-m", "src.main"]
