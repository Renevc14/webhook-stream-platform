FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
EXPOSE 3051
# Scale workers with CPU on ECS: set GUNICORN_WORKERS (2×vCPU is a starting point on Fargate).
ENV GUNICORN_WORKERS=4
ENV GUNICORN_THREADS=2
ENV GUNICORN_TIMEOUT=120
ENV GUNICORN_GRACEFUL_TIMEOUT=30
ENV GUNICORN_KEEP_ALIVE=5
CMD ["sh", "-c", "exec gunicorn --bind 0.0.0.0:3051 --workers \"${GUNICORN_WORKERS}\" --threads \"${GUNICORN_THREADS}\" --timeout \"${GUNICORN_TIMEOUT}\" --graceful-timeout \"${GUNICORN_GRACEFUL_TIMEOUT}\" --keep-alive \"${GUNICORN_KEEP_ALIVE}\" app:app"]
