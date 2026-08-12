# Webhook Stream Platform

Event-driven architecture for multi-tenant SaaS tools that react to spreadsheet webhooks.

A shared **webhook gateway** acknowledges provider callbacks in milliseconds, publishes versioned events to **Redis Streams**, and lets each tool run as an independent **stream worker** — with shared crypto, credentials, rate limits, billing caps, and DLQ handling.

```
Internet
   │
   ▼
┌──────────────────────┐
│  Webhook Gateway     │  verify signature · challenge · XADD · 200 OK
└──────────┬───────────┘
           │ Redis Streams (per tool + DLQ)
           ▼
┌──────────────────────┐
│  platform_core       │  worker base · token cache · billing · Smartsheet client
└──────────┬───────────┘
           │
     ┌─────┴──────┐
     ▼            ▼
 image_to_cell  sheet_qr   (+ credential_broker for OAuth/token vault)
```

## What's in this repo

| Path | Role |
|------|------|
| [`packages/platform_core`](packages/platform_core) | Shared Python library: AES-GCM crypto, Redis (streams, locks, cache-aside), worker base, bolt metering, Smartsheet client |
| [`services/webhook_gateway`](services/webhook_gateway) | Stateless ingress: HMAC verify → challenge → enqueue → respond |
| [`examples/image_to_cell`](examples/image_to_cell) | Mature worker: attachment → processed cell image |
| [`examples/sheet_qr`](examples/sheet_qr) | Clean reference worker: QR generation + streams + billing |
| [`examples/credential_broker`](examples/credential_broker) | OAuth / encrypted token broker used by tools |
| [`docs/architecture`](docs/architecture) | Target architecture, migration plan, dependency map |
| [`docs/development`](docs/development) | Coding standards, constraints, templates |
| [`docs/runbooks`](docs/runbooks) | Webhook platform cutover notes |

## Design principles

1. **Fast path vs slow path** — webhook handlers never do heavy I/O; workers own processing
2. **Redis Streams + DLQ** — durable fan-out, retries with backoff, orphan reclaim
3. **One core library** — tools share contracts instead of forking webhook stacks
4. **Org isolation** — every data access scoped by `organization_id`
5. **Loop-safe writes** — Change-Agent header on provider writes to avoid infinite webhooks
6. **Ciphertext-only cache** — tokens in Redis are never stored as plaintext
7. **Metering with hold/release** — Postgres cycle baseline + Redis concurrency gate for usage caps

## Stack

Python 3.11 · FastAPI · Redis Streams · Supabase (PostgreSQL) · Smartsheet API · AWS ECS/Fargate (deploy examples)

## Portfolio naming

Folder names are portfolio-neutral. Some Python import paths still use historical package names (`smarthub_core`, `smart_qr`) so the archive stays coherent without a mass rename.

| Folder | Meaning |
|--------|---------|
| `platform_core` | Shared platform library |
| `webhook_gateway` | Shared ingress service |
| `image_to_cell` | Image → cell pipeline worker |
| `sheet_qr` | QR code worker |
| `credential_broker` | Token / OAuth broker |

## Interview pitch

> We split time-sensitive webhook reception from CPU-bound work. A shared gateway verifies signatures and enqueues versioned events onto per-tool Redis streams. Workers plug into a common core for credentials, rate limits, billing, and DLQ — so adding a tool means writing a consumer, not another webhook stack.

## Status

Private case-study archive. Not a drop-in production deploy of a live product — treat secrets, hosts, and org IDs as untrusted until re-sanitized for any wider share.
