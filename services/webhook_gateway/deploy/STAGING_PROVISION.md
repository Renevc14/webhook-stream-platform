# Webhook Gateway — staging provision checklist (SH-172 / SH-174)

Account `262252232181` · region `eu-west-2` · profile `smarthub-staging`.

This is **+1 shared ECS service**, same VPC/ALB/Redis as the other staging backends.
It does **not** exist yet until these steps run (post SH-147 soak).

## 1. ECR

```bash
aws ecr create-repository --repository-name stg-webhook-gateway   --region eu-west-2 --profile smarthub-staging
```

## 2. Secrets Manager

Create `stg/webhook-gateway-env` JSON from `stg-webhook-gateway-env.example.json`
(reuse staging `REDIS_URL` / `APP_CRYPTO_KEY` / Supabase staging keys — same crypto
as i2c so `imagetocell_webhooks.shared_secret` decrypts).

## 3. CloudWatch log group

`/ecs/smarthub-stg/webhook-gateway` (90 days).

## 4. Build + push

From repo root:

```bash
aws ecr get-login-password --profile smarthub-staging --region eu-west-2   | docker login --username AWS --password-stdin 262252232181.dkr.ecr.eu-west-2.amazonaws.com
docker buildx build --platform linux/amd64   -t 262252232181.dkr.ecr.eu-west-2.amazonaws.com/stg-webhook-gateway:latest   -f services/webhook-gateway/Dockerfile --load .
docker push 262252232181.dkr.ecr.eu-west-2.amazonaws.com/stg-webhook-gateway:latest
```

## 5. Task definition + service

Fill `__SECRET_ARN__` in `ecs-task-definition.staging.template.json`, register,
create service on an existing staging cluster (or `stg-webhook-gateway-cluster`),
SG with ingress from ALB on **3020**, egress to Redis + Supabase.

## 6. ALB + DNS

Target group port 3020 → health `/health`.
Host e.g. `webhooks.prodactivelabs.com` (or path rule).
Set `SMARTSHEET_WEBHOOK_CALLBACK_BASE_URL` to that origin (no trailing slash).

## 7. Cutover (SH-175)

Only after SH-156 smoke. Dry-run first (default), then pilot orgs with `--apply`:

```bash
.\backend\image-to-cell-3\venv\Scripts\python.exe `
  backend\image-to-cell-3\scripts\sh175_webhook_url_cutover.py `
  --base-url https://webhooks.prodactivelabs.com

.\backend\image-to-cell-3\venv\Scripts\python.exe `
  backend\image-to-cell-3\scripts\sh175_webhook_url_cutover.py `
  --base-url https://webhooks.prodactivelabs.com `
  --org-id <uuid> --apply
```

Until cutover, Smartsheet still hits i2c directly; gateway secret sync
(SH-173 MVP) keeps Redis `webhook-secrets:{org}:smart-image-to-cell` warm
for dual-run / shadow tests. Register / renew / zombie cleanup stay on i2c
until a later lifecycle port.
