# Phase 1 — local testing manual

> Self-contained guide for exercising the Stability Package (SH-99, SH-93,
> SH-100, SH-101, SH-102, SH-103, SH-135) against the local Docker Redis
> before AWS ElastiCache lands. This version assumes you're comfortable
> with Python + Smartsheet but fuzzy on Docker / Redis / how the local
> stack hangs together — every step explains **what** the command does
> and **why** we're running it.

---

## 0 · What Phase 1 actually changes (and why we test locally)

Phase 1 is a rewrite of the parts of i2c-3 where multiple worker
processes used to keep their own private state (dedup caches, token
caches, rate limit counters, bolt-cap counters). When the production
deploy ran with 4 ECS tasks, each task had its own copy of that state,
so a webhook retry that landed on a different task could re-process the
same image and double-bill.

The fix is to put that state in **Redis** — a shared, fast key/value
store that all tasks talk to. Phase 1 covers seven tickets that
together migrate every piece of cross-process state out of Python
memory and into Redis:

| Ticket | One-sentence summary |
|---|---|
| SH-99 | Webhook dedup keys live in Redis instead of in-memory dicts |
| SH-93 | A new `SmartsheetClient` factory wires our app to the smarthub_core async client |
| SH-100 | The three Smartsheet wrappers (sheets, attachments, webhooks) become `async def` |
| SH-101 | The core processing layer becomes async-first (no more thread pool) |
| SH-102 | The legacy `SmartsheetService` + threading code is deleted |
| SH-103 | Bolt-cap enforcement uses Redis atomic `INCRBY` so two workers can't both spend the same bolt |
| SH-135 | The bolt-cap counter automatically resets when a new billing cycle starts |

**Why local first.** AWS ElastiCache is the production target, but
provisioning it takes time and money. Running the same Redis image
locally lets us:

- Reproduce the production semantics (Redis is Redis — the same Lua
  scripts run identically on local 7-alpine and on managed ElastiCache).
- Iterate fast (sub-second turnaround vs deploy-to-staging cycles).
- Make sure the Phase 1 work doesn't break anything *before* we burn
  AWS minutes on a smoke test that should have failed locally.

The four things this manual does **not** cover — and why — are at the
bottom (section 9).

---

## 1 · The tools we use (plain-English primer)

If you already know Docker / Redis / `.env`, skip to section 2. If
you're hazy on any of them, this section gives you just enough mental
model to follow the rest of the manual without surprises.

### 1.1 Docker (and what a "container" actually is)

Docker is software that runs on your machine and gives you **isolated
mini-environments** called *containers*. A container is just a Linux
process running on your laptop's kernel — but it has its own filesystem
(it sees only the files Docker hands it), its own network interfaces
(it sees only the ports Docker maps for it), and its own
"installed software" (whatever was inside the image it was built from).

You can think of a container as a tiny VM that boots in under a second
and doesn't waste RAM on its own operating system.

**Why we use one for Redis.** We could install Redis directly on
Windows/Mac/Linux, but:

1. Versions drift. The Redis on your machine might be 6.2 while
   production runs 7.4. Lua scripts and TTL behaviour can differ.
2. Uninstalling is messy. With a container, "uninstall" = `docker rm`.
3. Different projects might need different Redises (you already saw
   this — your `iot-redis` container is for a different project).

The image we pull is **`redis:7-alpine`** — the official Redis 7.4
build on Alpine Linux (a tiny ~5 MB Linux distro). That's the same
image family AWS ElastiCache 7.x speaks, so the behaviour we observe
locally matches production.

### 1.2 Ports, and why we run Redis on `:6380` instead of `:6379`

Redis listens on TCP port **6379** *inside the container*. Outside the
container — on your laptop — that port is invisible by default.
Docker has to "publish" it for you.

The flag `-p 6380:6379` means:

```
                  host port (your laptop)
                   │
                  ┌▼────┐                ┌──────┐
       client ──►│:6380│──── Docker ────►│:6379│ Redis process
                  └─────┘                 └──────┘
                                          container port
```

Anything that connects to `localhost:6380` on your machine actually
talks to the Redis process inside the container.

We chose **6380** because your machine already had a different Redis
container (`iot-redis`) holding `:6379`. Two processes cannot bind the
same port at the same time. By publishing `smarthub-redis` on `:6380`
we coexist with it.

Production doesn't have this problem — ElastiCache exposes a single
endpoint inside the AWS VPC.

### 1.3 Redis itself (what it actually does)

Redis is an **in-memory key/value store**. Conceptually it's a giant
Python `dict` running in a separate process that any application can
talk to over TCP.

Two properties matter for Phase 1:

1. **It's fast.** Sub-millisecond reads and writes because everything
   lives in RAM. That's why we use it for hot paths like "does this
   attachment_id already have a dedup key?".
2. **It supports atomic Lua scripts.** When you send Redis a Lua
   script, the server executes it from beginning to end with no other
   client able to interleave reads or writes. This is what makes
   `atomic_deduct` actually atomic — the "check the consumed counter,
   compare to the limit, then INCRBY" sequence happens as one
   indivisible unit even if 100 clients send the same call at the
   same millisecond.

Everything we store in Redis has a "key" (a string like
`bolt-cap:org-uuid:consumed`) and a "value" (a string, number, list,
or hash). Most keys also have a **TTL** — a time-to-live in seconds
after which Redis evicts the key automatically. We use TTL for
dedup keys (cleared after 10 min so memory doesn't grow forever) but
NOT for the bolt-cap consumed counter (must survive the whole
billing cycle).

**Persistence.** Our local container does not persist to disk — if you
`docker rm` it, every key is gone. That's fine for testing: in
section 5 you'll see that every key gets re-seeded from Postgres
(the source of truth) on first use.

### 1.4 `.env` — your local configuration file

`.env` is a plain-text file that lives at the root of `image-to-cell-3/`
and holds environment variables for local development. Each line is a
`NAME=value` pair. When the FastAPI app boots, it reads `.env` and
populates `os.environ` so the rest of the code can `os.getenv("X")`.

The variables that matter for this manual:

```
SUPABASE_URL=https://kkncqjelucyhgsmuinqb.supabase.co
SUPABASE_SERVICE_ROLE_KEY=<secret>
APP_CRYPTO_KEY=<32-byte hex secret>
REDIS_URL=redis://localhost:6380/0
REDIS_DEDUP_TTL_S=600
```

- **`SUPABASE_URL`** + **`SUPABASE_SERVICE_ROLE_KEY`** — what database
  the app talks to. There is no local Supabase; we use the cloud-hosted
  one. That means any test that writes to Postgres (e.g. registering a
  webhook) will leave real rows in the real DB. Use a sandbox
  organization when possible.
- **`APP_CRYPTO_KEY`** — the AES-256 key SH-108 uses to encrypt webhook
  shared secrets. If this key changes, every previously-encrypted
  secret becomes garbage.
- **`REDIS_URL`** — the URL pattern is `redis://<host>:<port>/<db>`.
  Our local Redis is at `localhost:6380` and we use database `0` (Redis
  has 16 numbered databases by default, like Postgres schemas; we only
  ever use `0`).
- **`REDIS_DEDUP_TTL_S`** — how many seconds the SH-99 dedup keys live.
  600 = 10 minutes, same as the legacy in-memory cache TTL.

In production these variables come from AWS Secrets Manager (via
ECS task definition); local dev reads them from this file.

### 1.5 Uvicorn and FastAPI

**FastAPI** is the web framework our `src/api/app.py` is built on. It
defines the URL routes (`/webhook/smartsheet`, `/configs`, etc.) and
the lifecycle hooks (`lifespan`) that start the lifecycle manager and
the DLQ processor.

**Uvicorn** is the ASGI server that actually listens on a port,
accepts HTTP requests, and hands them to FastAPI. When you run
`uvicorn src.api.app:create_app --factory --port 3011`, uvicorn:

1. Imports `src.api.app`.
2. Calls `create_app()` to get the FastAPI instance.
3. Triggers the `lifespan` startup hook (which wires up Redis, the
   webhook lifecycle, etc.).
4. Starts listening on `localhost:3011`.
5. For each HTTP request, runs the matching route handler inside the
   asyncio event loop.

The `--reload` flag makes uvicorn restart automatically when you save
a `.py` file. That makes local iteration fast but means every restart
re-runs the startup hook and re-warms the connection pools.

### 1.6 Where Smartsheet's webhook calls actually arrive

When Smartsheet fires a webhook, it makes an HTTPS POST to the
**callback URL** we registered with them. In production that URL is
something like `https://smart-image-to-cell.prodactivelabs.com/webhook/smartsheet`.

Locally, your laptop is not on the public internet, so Smartsheet
cannot reach `localhost:3011` directly. We use **ngrok** — a service
that opens a tunnel from a public HTTPS URL (e.g.
`https://unacclimatized-greyson-joylessly.ngrok-free.dev`) to your
local `localhost:3011`. The URL is stored in the
`WEBHOOK_CALLBACK_URL` env var when we register webhooks.

**If you haven't started ngrok**, the per-ticket "live check" steps
that involve real Smartsheet webhooks will not fire. You can still
exercise the dedup / atomic_deduct / cycle rollover paths against
Redis directly using the standalone Python scripts in this manual —
they don't need Smartsheet.

### 1.7 The mental model in one picture

```
   ┌──────────────────────────────┐      ┌──────────────────────────┐
   │  Your laptop (uvicorn)       │      │  Supabase Cloud          │
   │  ┌────────────────────────┐  │      │  • smartsheet_tokens     │
   │  │ FastAPI app            │  │      │  • imagetocell_webhooks  │
   │  │  - routes              │◄─┼──────┤  • imagetocell_usage_log │
   │  │  - lifespan            │  │ HTTPS │  • subscription_status   │
   │  │  - workers             │  │      └──────────────────────────┘
   │  └──────┬─────────────────┘  │
   │         │ TCP                 │      ┌──────────────────────────┐
   │  ┌──────▼─────────────────┐  │      │  Smartsheet API + Hooks  │
   │  │ Docker container       │  │      │  api.smartsheet.com      │
   │  │  smarthub-redis        │◄─┼──────┤  ngrok tunnel ────────►  │
   │  │  redis:7-alpine        │  │ HTTPS │  → localhost:3011        │
   │  │  port 6379 internal    │  │      └──────────────────────────┘
   │  │  → :6380 on your host  │  │
   │  └────────────────────────┘  │
   └──────────────────────────────┘
```

Four moving parts: **uvicorn**, **Redis (Docker)**, **Supabase
(remote)**, **Smartsheet (remote, reached via ngrok)**. The rest of
this manual exercises each pair.

---

## 2 · Pre-flight (every command annotated)

These four checks should all pass before you start. If one fails,
fix it before continuing — the rest of the manual depends on them.

### 2.1 Confirm the Redis container is up

```bash
docker ps --filter "name=smarthub-redis" --format "{{.Names}} {{.Status}} {{.Ports}}"
```

What this does:

- **`docker ps`** lists running containers (without `-a` it only shows
  the running ones).
- **`--filter "name=smarthub-redis"`** narrows the list to ours.
- **`--format "..."`** picks the three columns we care about.

You should see something like:

```
smarthub-redis Up 6 hours 0.0.0.0:6380->6379/tcp
```

The trailing `0.0.0.0:6380->6379/tcp` is the port mapping we discussed
in 1.2 — host port 6380 forwards to container port 6379.

**If the container is missing** (no output): you've never created it,
or you ran `docker rm` at some point. Recreate it:

```bash
docker run -d --name smarthub-redis -p 6380:6379 redis:7-alpine
```

- **`-d`** runs detached (in the background) so your shell prompt
  comes back.
- **`--name smarthub-redis`** names the container so you can
  `docker start/stop/exec` it by name later.
- **`-p 6380:6379`** publishes the port (see 1.2).
- **`redis:7-alpine`** is the image — Docker downloads it from Docker
  Hub if you don't have it cached.

**If it's stopped** (status starts with `Exited`): start it.

```bash
docker start smarthub-redis
```

### 2.2 Confirm Redis itself is responding

```bash
docker exec smarthub-redis redis-cli -p 6379 ping
```

What this does:

- **`docker exec`** runs a command *inside* a container — like
  `ssh`-ing into a tiny VM.
- **`smarthub-redis`** is the container name (from step 2.1).
- **`redis-cli`** is Redis' built-in command-line client (already
  installed inside the `redis:7-alpine` image).
- **`-p 6379`** tells the CLI which port to connect to *inside* the
  container — that's why we use 6379, not 6380.
- **`ping`** is the Redis health check command.

Expected reply: `PONG`. Anything else means the Redis process inside
the container has crashed — `docker logs smarthub-redis` to see why.

### 2.3 Confirm `.env` is configured

```bash
cd backend/image-to-cell-3
grep -E "^REDIS_URL|^REDIS_DEDUP_TTL_S" .env
```

Expected output:

```
REDIS_URL=redis://localhost:6380/0
REDIS_DEDUP_TTL_S=600
```

If `REDIS_URL` is missing or points at the wrong port, uvicorn will
log a `SH-99 REDIS_URL not configured` warning at boot and fall back
to in-memory dedup — most of this manual won't observe what it should.

### 2.4 Confirm you're on the right git branch

```bash
git log --oneline -10
```

You should see the SH-99 → SH-135 commits in order:

```
3773710 SH-135 auto-reset bolt-cap consumed counter at cycle rollover
dcbbd13 SH-103 (part 2) integrate atomic_deduct into the bolt cap
ec81ef6 SH-103 (part 1) add atomic_deduct helper ...
e45b71f SH-102 drop legacy SmartsheetService path ...
640d6c5 SH-101 migrate core processing layer to async-first
c8983fa SH-100 migrate WebhookService ...
767259f SH-100 migrate AttachmentService ...
7682450 SH-100 migrate SheetService ...
d412c2e SH-93 add SmartsheetClient factory ...
37d64a3 SH-99 migrate webhook dedup caches to Redis
```

If this list is wrong, you're on a different branch:

```bash
git checkout SH-135-bolt-cap-cycle-rollover
```

`SH-135-bolt-cap-cycle-rollover` is the top of the Phase 1 stack — it
has every previous ticket merged in via the stacked branches.

---

## 3 · Boot the server

### 3.1 Run the unit suite once for a baseline

```bash
cd backend/image-to-cell-3
venv/Scripts/python.exe -m pytest tests/unit/ -q
```

- **`venv/Scripts/python.exe`** is the Python interpreter inside your
  `venv/` virtual environment. Using the absolute path avoids any
  ambiguity about which Python is running.
- **`-m pytest`** runs `pytest` as a module (more reliable than the
  `pytest` shell shim).
- **`tests/unit/`** is the test directory.
- **`-q`** is "quiet mode" — one line per test result.

Expected result: **193 passed, 2 failed**. The two failures are
`test_temp_file_manager.py::test_create_temp_file_path_traversal_blocked`
and the Windows-style variant. These predate Phase 1 (we noted them on
every prior PR). **Anything else failing means a Phase 1 regression
slipped in — stop and investigate.**

### 3.2 Start uvicorn

In one terminal:

```bash
cd backend/image-to-cell-3
venv/Scripts/uvicorn.exe src.api.app:create_app --factory --port 3011 --reload
```

- **`src.api.app:create_app`** tells uvicorn "import the `create_app`
  callable from the `src.api.app` module".
- **`--factory`** says "this is a factory function — call it to get
  the FastAPI instance" (as opposed to a top-level `app` global).
- **`--port 3011`** is the port the server listens on. Choose any
  free port; the rest of the manual assumes 3011.
- **`--reload`** auto-restarts on `.py` file changes.

### 3.3 What you should see in the boot log

The first ~30 seconds should print a block like this (timestamps will
differ):

```
2026-06-04 04:13:23 | INFO  | src.api.app | I2C CORS allow_origins (6): [...]
INFO:     Started server process [12345]
INFO:     Waiting for application startup.
2026-06-04 04:13:23 | INFO  | src.api.app | ===================== IMAGE-TO-CELL v3 STARTING =====================
2026-06-04 04:13:23 | INFO  | src.api.app | [STARTUP] Environment: development
2026-06-04 04:13:23 | INFO  | src.api.app | [STARTUP] SH-99 Redis dedup wired url=redis://localhost:6380
2026-06-04 04:13:23 | INFO  | src.api.app | [STARTUP] Starting webhook lifecycle manager...
2026-06-04 04:13:23 | INFO  | src.api.app | [STARTUP] Starting DLQ processor...
2026-06-04 04:13:23 | INFO  | src.api.app | ========================== STARTUP COMPLETE ==========================
2026-06-04 04:13:24 | INFO  | smarthub_core.db.supabase | supabase: initialized url=https://...
INFO:     Application startup complete.
INFO:     Uvicorn running on http://0.0.0.0:3011 (Press CTRL+C to quit)
```

Two lines to watch for:

- **`SH-99 Redis dedup wired url=redis://localhost:6380`** — the Redis
  connection succeeded. If you see `SH-99 REDIS_URL not configured`
  or `SH-99 Redis unavailable at startup` instead, fix .env or the
  container (back to section 2) and restart.
- **`STARTUP COMPLETE`** — the lifespan hook finished. The server is
  now accepting requests.

### 3.4 Open a Redis "watch" in a second terminal

While the server runs, keep an interactive Redis session open so you
can see keys land in real time:

```bash
docker exec -it smarthub-redis redis-cli -p 6379
```

- **`-it`** allocates an interactive TTY — without this, the prompt
  hangs.

Inside the CLI you can run commands like:

```
127.0.0.1:6379> KEYS *
1) "..."
127.0.0.1:6379> MONITOR
OK
1717520123.456789 [0 172.17.0.1:54321] "PING"
1717520124.123456 [0 172.17.0.1:54321] "SET" "..."
^C   # to stop the MONITOR stream
127.0.0.1:6379> exit
```

- **`KEYS *`** lists every key in the current database. Fine for local
  testing; **never run on production Redis** — it's O(N) and can lock
  the server.
- **`MONITOR`** streams every command Redis receives in real time.
  Great for debugging. Stop with Ctrl-C.

---

## 4 · Per-ticket verification

Each block is independent — run them in any order. The goal is to make
each ticket's signature observable in Redis / logs / DB.

### 4.1 SH-99 — Webhook dedup is cross-process

**What this ticket changes.** Before SH-99, when the WebhookHandler
received a Smartsheet attachment-created event, it remembered the
attachment_id in two Python dicts: `_processing_cache` (so the same
event from the same task didn't fire twice) and `_completed_attachments`
(so a later event for an already-finished attachment was skipped). These
dicts lived in the Python process — when a webhook retry was
load-balanced to a different ECS task, the dicts there were empty and
the work ran a second time.

SH-99 moves both dicts to Redis keys:

- **`dedup:i2c:processing:<attachment_id>:<row_id>`** — written via
  `SET NX EX 600`. The first task to receive the event gets `True`
  back and processes; every other task sees `False` and skips.
- **`dedup:i2c:completed:<attachment_id>`** — written after a row
  finishes processing. Subsequent events for the same attachment skip
  silently.

#### Static check (no Smartsheet needed)

```bash
docker exec smarthub-redis redis-cli -p 6379 KEYS "dedup:i2c:*"
```

Right after a fresh `FLUSHDB`, this returns empty. After a webhook
fires, you should see at least one of each key:

```
1) "dedup:i2c:processing:8674628157869956:1014096047570820"
2) "dedup:i2c:completed:8674628157869956"
```

Check their TTL:

```bash
docker exec smarthub-redis redis-cli -p 6379 TTL dedup:i2c:completed:8674628157869956
# (integer) 587
```

That's the remaining time-to-live in seconds. We seeded it at 600
(from `REDIS_DEDUP_TTL_S`), so anything between 1 and 600 is healthy.
`-1` would mean "no TTL" (bug) and `-2` would mean "key doesn't exist"
(typo).

#### Live check

Upload an image to a row in a configured Smartsheet worksheet. Within
~5 s, the server log should print:

```
[WEBHOOK:RECEIVE] (sheet=..., webhook=...) STEP 2: Verifying signature
[WEBHOOK:RECEIVE] ... OK: Webhook acknowledged events=1
[WEBHOOK:PROCESS] ... OK: Webhook processed processed=1, skipped=0, failed=0
```

Then `KEYS dedup:i2c:*` returns the matching attachment_id.

#### Graceful degradation (negative test)

This is the most reassuring check that the new path doesn't fail
catastrophically when Redis dies. From a second terminal:

```bash
docker stop smarthub-redis
```

Now upload another image. The server log should print:

```
[SH-99] try_acquire_processing Redis error ... — falling back to in-memory dedup
```

— and process the image normally. SH-99 is designed so that a Redis
outage degrades the dedup back to per-task semantics (the legacy
behaviour) without dropping work. Restart the container before
moving on:

```bash
docker start smarthub-redis
```

### 4.2 SH-93 — `get_smartsheet_client` factory

**What this ticket changes.** Before SH-93, every Smartsheet call went
through the legacy `get_smartsheet_for_organization(org_id)` factory
which built a `SmartsheetService` with a per-process token cache and a
per-process rate limiter. Four ECS tasks meant 4 separate token caches
that all expired independently after restart, and 4 separate rate
limiters that could each issue 275 calls/min for a cluster total of
1100/min against Smartsheet's 300/min ceiling.

SH-93 adds `get_smartsheet_client(org_id)` which returns a
`smarthub_core.platforms.smartsheet.SmartsheetClient` — a shared
async client that puts the encrypted token in Redis under
**`ss-token:<org>:<slug>`** and tracks the rate-limit window in Redis
under **`ratelimit:ss-api:<org>:<slug>`**. All four ECS tasks see the
same cache and the same rate-limit window.

#### Static check (no Smartsheet needed)

```bash
venv/Scripts/python.exe -c "
import asyncio
from smarthub_core.db.redis import RedisClient
from src.services.redis_client import set_redis_client
from src.services.smartsheet.client import get_smartsheet_client

async def main():
    rc = RedisClient('redis://localhost:6380/0')
    await rc.start()
    set_redis_client(rc)
    c = get_smartsheet_client('test-org-uuid')
    print(f'client built: org={c.org_id} slug={c.product_slug}')
    await rc.close()

asyncio.run(main())
"
```

Expected output:

```
client built: org=test-org-uuid slug=smart-image-to-cell
```

What the script does:

1. Creates a `RedisClient` instance pointed at our local container.
2. `set_redis_client(rc)` installs it on the module-level singleton —
   uvicorn's startup hook normally does this, but a standalone Python
   `-c` invocation needs to do it manually.
3. Calls the factory and prints the org/slug to confirm the wiring.

The slug `smart-image-to-cell` comes from the `PRODUCT_SLUG` setting
and identifies which row of `smartsheet_tokens` (the Postgres table)
holds this org's token.

#### Live check (token + rate limit visible in Redis)

These keys only appear AFTER a real Smartsheet call resolves a token.
Fire any webhook or trigger a backfill, then:

```bash
docker exec smarthub-redis redis-cli -p 6379 KEYS "ss-token:*"
# 1) "ss-token:<org_id>:smart-image-to-cell"

docker exec smarthub-redis redis-cli -p 6379 GET ss-token:<org_id>:smart-image-to-cell
# "<ciphertext blob — >80 chars, looks like base64>"
```

**Critical check**: the value must look like ciphertext (long
base64-ish blob with a version prefix), not a 26-char ASCII string.
A 26-char ASCII string here means SH-108's encryption regressed —
**stop, escalate, don't ship to AWS.**

The rate-limit key only appears under burst load:

```bash
docker exec smarthub-redis redis-cli -p 6379 KEYS "ratelimit:ss-api:*"
# 1) "ratelimit:ss-api:<org_id>:smart-image-to-cell"

docker exec smarthub-redis redis-cli -p 6379 ZCARD ratelimit:ss-api:<org_id>:smart-image-to-cell
# (integer) 12       # how many calls in the current 60-s window
```

It's a Redis sorted set (ZSET), not a string — that's why you use
`ZCARD` to count entries instead of `GET`.

### 4.3 SH-100 — Smartsheet wrappers async

**What this ticket changes.** The three wrapper classes
(`SheetService`, `AttachmentService`, `SmartsheetWebhookService`)
used to be `def` methods that wrapped sync SDK calls in a thread
pool. After SH-100 every IO method is `async def` and routes through
`SmartsheetClient.call_with_retry_async`.

#### Static check

```bash
venv/Scripts/python.exe -c "
import inspect
from src.services.smartsheet.sheets import SheetService
from src.services.smartsheet.attachments import AttachmentService
from src.services.smartsheet.webhooks import SmartsheetWebhookService

cases = [
    ('SheetService.get_sheet', SheetService.get_sheet),
    ('AttachmentService.download', AttachmentService.download),
    ('SmartsheetWebhookService.register_webhook', SmartsheetWebhookService.register_webhook),
]
for name, fn in cases:
    print(f'{name}: {\"async\" if inspect.iscoroutinefunction(fn) else \"SYNC (BAD)\"}')
"
```

Expected output:

```
SheetService.get_sheet: async
AttachmentService.download: async
SmartsheetWebhookService.register_webhook: async
```

`inspect.iscoroutinefunction(fn)` is True if `fn` is `async def`. We
sample one method per wrapper; if these three are async, the rest
are too (SH-100 migrated them all in one slice each — none half-done).

#### Live check

Trigger a backfill on any worksheet with an active config:

```bash
curl -X POST http://localhost:3011/configs/backfill \
  -H 'Content-Type: application/json' \
  -H 'x-organization-id: <your-org-uuid>' \
  -d '{"worksheet_id": <your-worksheet-id>}'
```

Watch the log:

```
Backfill requested for worksheet ...
Starting backfill for worksheet ...
[default.should_process_row] row=... skip — ...
Backfill completed for worksheet ...: 0 processed, N skipped, 0 failed
```

The key sign of SH-100/101 is what you **don't** see: no
`loop.run_in_executor` traces, no thread-pool worker warnings.

### 4.4 SH-101 — Core async-first

**What this ticket changes.** The core processor used to be `def`
functions that scheduled per-row work onto a `ThreadPoolExecutor`.
After SH-101 they're `async def` and use `asyncio.gather` with an
`asyncio.Semaphore` to bound concurrency.

The most direct symptom is what's **gone** from the codebase. Run:

```bash
grep -rE --include='*.py' '\bThreadPoolExecutor\(' src/
```

Expected: nothing. Any matches are SH-102 comments documenting the
removal (we keep one or two as docstring breadcrumbs so future readers
understand why the threading code disappeared).

### 4.5 SH-102 — Legacy SmartsheetService path is gone

**What this ticket changes.** With SH-100 and SH-101 routing every
Smartsheet call through the new `SmartsheetClient`, the legacy
`SmartsheetService` class and the `get_smartsheet_for_organization`
factory are dead code. SH-102 deletes them.

#### Static checks

```bash
grep -rE --include='*.py' '\bget_smartsheet_for_organization\(' src/
# expect: nothing

grep -rE --include='*.py' 'class SmartsheetService' src/
# expect: nothing

grep -n '_executor\|_db_semaphore' src/workers/webhook_handler.py
# expect: only the SH-102 comment that documents the removal
```

If any of these returns real code (not just comments), SH-102 was
incomplete — the legacy path is still alive somewhere.

#### Health endpoint

```bash
curl http://localhost:3011/health
# {"status":"ok","service":"image-to-cell-v3"}
```

`/health` exercises the cleanest possible request path. A 200 here
proves the legacy path isn't needed even for the lightest endpoint.

### 4.6 SH-103 — Distributed bolt-cap atomic deduct

**What this ticket changes.** The bolt-cap enforcement before SH-103
worked like this: read the consumed-bolts count from Postgres into a
30-second per-process cache, compare to the cap, decide whether to
process. Under burst load (BAU mass upload), 100 workers might all
read `consumed=500, cap=1000` and each decide "yep, 500 to spare, go"
— collectively spending 1500 before any of them writes back.

SH-103 keeps the consumed counter in Redis under
**`bolt-cap:<org>:consumed`** and uses an atomic Lua script
(`atomic_deduct`) that reads the consumed, compares to the limit,
and INCRBYs the consumed counter in a single indivisible operation.
Two workers can no longer both read 500 and both INCRBY 500 — one
of them sees 1000 and is denied.

#### Static check — helper exists

```bash
venv/Scripts/python.exe -c "
from smarthub_core.db.redis import RedisClient
print('atomic_deduct:', hasattr(RedisClient, 'atomic_deduct'))
print('atomic_deduct_with_cycle:', hasattr(RedisClient, 'atomic_deduct_with_cycle'))
"
# atomic_deduct: True
# atomic_deduct_with_cycle: True
```

#### Concurrency invariant — the proof that it's actually atomic

This is the script that makes the atomicity contract observable. Run
it against your local Redis:

```bash
venv/Scripts/python.exe -c "
import asyncio
from smarthub_core.db.redis import RedisClient

async def main():
    rc = RedisClient('redis://localhost:6380/0')
    await rc.start()
    # Clean slate
    await rc.raw.delete('demo:consumed', 'demo:limit')
    # Set a cap of 50 — only 50 of 100 callers should succeed
    await rc.raw.set('demo:limit', '50')

    async def one():
        ok, _ = await rc.atomic_deduct('demo:consumed', 'demo:limit', 1)
        return ok

    # Fire 100 deduct calls in parallel
    results = await asyncio.gather(*[one() for _ in range(100)])
    print(f'allowed={sum(results)}/100  (expect 50)')
    consumed = (await rc.raw.get('demo:consumed')).decode()
    print(f'consumed={consumed}  (expect 50)')

    # Clean up
    await rc.raw.delete('demo:consumed', 'demo:limit')
    await rc.close()

asyncio.run(main())
```

Expected output:

```
allowed=50/100  (expect 50)
consumed=50  (expect 50)
```

What's happening line by line:

1. `RedisClient('redis://localhost:6380/0').start()` connects to the
   local container.
2. `delete` and `set` wipe any previous state and seed the cap at 50.
3. `asyncio.gather(*[one() for _ in range(100)])` schedules 100
   `atomic_deduct` calls into the event loop. They run "in parallel"
   in the sense that Python is awaiting all of them concurrently;
   Redis serialises them.
4. Redis executes each Lua script atomically. The first 50 callers
   each see `consumed < limit`, INCRBY 1, return `(True, 49→0)`. The
   next 50 see `consumed = 50`, return `(False, 0)`.
5. The Python script tallies the `True`s — should be exactly 50.

**If you get anything other than 50/100**, the serialisation is
broken. That would be a Lua-script bug or a Redis behaviour change.
Stop and investigate.

#### Live check — bolt-cap keys during a real backfill

Trigger a backfill on a worksheet with many rows. While it runs:

```bash
docker exec smarthub-redis redis-cli -p 6379 KEYS "bolt-cap:*"
# 1) "bolt-cap:<org_id>:consumed"
# 2) "bolt-cap:<org_id>:limit"
# 3) "bolt-cap:<org_id>:cycle_id"     # SH-135

docker exec smarthub-redis redis-cli -p 6379 GET "bolt-cap:<org_id>:consumed"
# "450"      # bolts spent so far this cycle

docker exec smarthub-redis redis-cli -p 6379 GET "bolt-cap:<org_id>:limit"
# "21000"    # cap for this cycle

docker exec smarthub-redis redis-cli -p 6379 TTL "bolt-cap:<org_id>:limit"
# (integer) 47          # close to 60 — refreshed on every reserve call

docker exec smarthub-redis redis-cli -p 6379 TTL "bolt-cap:<org_id>:consumed"
# (integer) -1          # NOT TTL'd — persists across the cycle (correct)
```

The `-1` on consumed is intentional. The counter must survive across
the cycle until SH-135's rollover logic resets it (next section).

### 4.7 SH-135 — Auto-reset at cycle rollover

**What this ticket changes.** SH-103's consumed counter never expired,
so when an org's billing cycle rolled over to a new month, the Redis
counter still had last month's total and blocked the org earlier than
entitled. SH-135 adds a third key — **`bolt-cap:<org>:cycle_id`** —
that stores the last seen cycle identifier (the
`subscription_status.current_cycle_start` timestamp). A new helper
`atomic_deduct_with_cycle` extends the Lua script: before evaluating
the deduct, it compares the stored cycle_id with the expected one
and atomically resets the consumed counter to the new cycle's baseline
if they differ.

#### Simulated rollover (no Postgres needed)

```bash
venv/Scripts/python.exe -c "
import asyncio
from smarthub_core.db.redis import RedisClient

async def main():
    rc = RedisClient('redis://localhost:6380/0')
    await rc.start()
    await rc.raw.delete('demo:c', 'demo:l', 'demo:cid')
    await rc.raw.set('demo:l', '300')      # cap = 300 bolts (= 20 images at 15 bolts each)

    # ------------------------------------------------------------------
    # 1. First call ever — counter starts fresh in cycle '2026-06-01'
    # ------------------------------------------------------------------
    ok, _ = await rc.atomic_deduct_with_cycle('demo:c', 'demo:l', 'demo:cid', 15, '2026-06-01', 0)
    print(f'June first call: ok={ok} consumed={(await rc.raw.get(\"demo:c\")).decode()}')

    # ------------------------------------------------------------------
    # 2. 20 more calls in June — should saturate the 300-bolt cap.
    #    First call already used 15, so 20 more × 15 = 300 + 15 = 315 → 20th is denied.
    # ------------------------------------------------------------------
    for _ in range(20):
        await rc.atomic_deduct_with_cycle('demo:c', 'demo:l', 'demo:cid', 15, '2026-06-01', 0)
    next_ok, _ = await rc.atomic_deduct_with_cycle('demo:c', 'demo:l', 'demo:cid', 15, '2026-06-01', 0)
    print(f'June 21st call (should deny): ok={next_ok}')

    # ------------------------------------------------------------------
    # 3. Rollover to July — same Redis counter, different expected cycle_id.
    #    The Lua script resets consumed to baseline=0 BEFORE evaluating the deduct.
    # ------------------------------------------------------------------
    ok, _ = await rc.atomic_deduct_with_cycle('demo:c', 'demo:l', 'demo:cid', 15, '2026-07-01', 0)
    consumed = (await rc.raw.get('demo:c')).decode()
    cycle = (await rc.raw.get('demo:cid')).decode()
    print(f'July first call (rollover): ok={ok} consumed={consumed} cycle={cycle}')

    await rc.raw.delete('demo:c', 'demo:l', 'demo:cid')
    await rc.close()

asyncio.run(main())
"
```

Expected output:

```
June first call: ok=True consumed=15
June 21st call (should deny): ok=False
July first call (rollover): ok=True consumed=15 cycle=2026-07-01
```

**The third line is the proof of SH-135.** Without the rollover
detection, the July call would see `consumed=300` from June and be
denied. With SH-135, the Lua script detects the cycle mismatch,
atomically resets `consumed=0`, then INCRBYs the 15 for this call,
ending at 15. Atomic — even if 100 concurrent workers all hit the
same rollover moment, only one of them executes the reset and the
rest see the post-reset state.

#### Live check during real traffic

During normal operation:

```bash
docker exec smarthub-redis redis-cli -p 6379 GET "bolt-cap:<org_id>:cycle_id"
# "2026-06-04T00:00:00+00:00"     # matches subscription_status.current_cycle_start
```

The format depends on Postgres; what matters is that the same string
is stored across consecutive calls and changes only when Postgres
reports a new cycle.

If `cycle_id` is missing entirely during normal operation, the
subscription RPC isn't yielding one for this org. The helper safely
falls back to the cycle-less `atomic_deduct` (still atomic, just
loses the rollover detection until the RPC returns a cycle).

---

## 5 · End-to-end golden path

This is the integration test for the whole stack. Run it after the
per-ticket checks pass.

### 5.1 Reset Redis to a known empty state

```bash
docker exec smarthub-redis redis-cli -p 6379 FLUSHDB
# OK
```

What this does:

- **`FLUSHDB`** deletes every key in the current Redis database (we
  only use database 0). The container itself keeps running.
- **Safe locally** because every Phase 1 key either gets re-seeded
  from Postgres on first use (`bolt-cap:*`, `ss-token:*`) or has a
  source of truth elsewhere (`imagetocell_usage_log` for billing,
  `imagetocell_webhooks` for HMAC secrets).
- **Never run on production** Redis without coordinating with
  everyone — you'd evict every cache hot path at once.

### 5.2 Restart uvicorn so the boot log is fresh

Stop the running uvicorn (Ctrl-C) and restart it:

```bash
cd backend/image-to-cell-3
venv/Scripts/uvicorn.exe src.api.app:create_app --factory --port 3011 --reload
```

Confirm again that `SH-99 Redis dedup wired` and `STARTUP COMPLETE`
print in the log.

### 5.3 Upload an image to a configured Smartsheet row

In the Smartsheet UI, drop a fresh image into a row of a worksheet
that has an active i2c-3 config. Within ~5 s, the server log should
print:

```
[WEBHOOK:RECEIVE] (sheet=..., webhook=...) STEP 2: Verifying signature
[WEBHOOK:RECEIVE] ... OK: Webhook acknowledged events=1
[WEBHOOK:PROCESS] ... STEP 3: Processing 1 row groups in parallel
[PROCESS:ROW] ... OK: Images processed images=1
```

And **no** `legacy plaintext webhook secret` warnings (those would
indicate an SH-108 regression).

### 5.4 The Redis snapshot — every Phase 1 surface populated

```bash
docker exec smarthub-redis redis-cli -p 6379 KEYS "*"
```

Expected output (in some order):

```
1) "dedup:i2c:processing:<attachment_id>:<row_id>"   # SH-99
2) "dedup:i2c:completed:<attachment_id>"             # SH-99
3) "ss-token:<org_id>:smart-image-to-cell"           # SH-93
4) "ratelimit:ss-api:<org_id>:smart-image-to-cell"   # SH-93
5) "bolt-cap:<org_id>:consumed"                      # SH-103
6) "bolt-cap:<org_id>:limit"                         # SH-103
7) "bolt-cap:<org_id>:cycle_id"                      # SH-135
```

**Seven keys total.** If any of these is missing, the corresponding
ticket has a bug that the static checks did not catch. Escalate
before pushing to AWS.

---

## 6 · Troubleshooting

| Symptom | What it means | Fix |
|---|---|---|
| `SH-99 REDIS_URL not configured` warning at boot | `.env` not in CWD, or REDIS_URL line is missing/typo'd | Run from `backend/image-to-cell-3/` and confirm `REDIS_URL=redis://localhost:6380/0` exists |
| `SH-99 Redis unavailable at startup` warning at boot | The container isn't running, or it's bound to a different port | `docker start smarthub-redis` and verify with `docker ps` |
| `[SH-99] legacy plaintext webhook secret encountered` repeats | Some rows in `imagetocell_webhooks` are still plaintext from before SH-108 backfill | Re-run `scripts/migrate_encrypt_webhook_secrets.py` |
| `SH-93: SmartsheetClient requires Redis` raised on any Smartsheet call | Redis was up at start but is now unreachable | `docker start smarthub-redis` then restart uvicorn |
| `bolt_cap: SH-103 Redis surface unavailable` in logs | Connection severed mid-call — the helper fell back to the legacy peek | Harmless once Redis is back; flag for AWS smoke once provisioned |
| Backfill runs but produces 0 processed / 0 skipped | Insufficient bolts, or cycle rolled over and consumed counter never reset (pre-SH-135 behaviour) | Check `GET bolt-cap:<org>:cycle_id` matches `subscription_status.current_cycle_start`. If not, SH-135 should fix on next call — wait one cycle |
| `port is already allocated` when (re)creating `smarthub-redis` | Another container is already on `:6380` | `docker ps` to find it; if it's an old `smarthub-redis` from before, `docker rm -f smarthub-redis` and recreate |

---

## 7 · Reset / cleanup between runs

Three levels of "reset", least to most destructive:

```bash
# 1. Clear all Redis keys but keep the container running.
#    Use between test runs of section 4 / section 5.
docker exec smarthub-redis redis-cli -p 6379 FLUSHDB

# 2. Restart the Redis process inside the container.
#    Equivalent to FLUSHDB plus drops any pending replies in flight.
#    Almost never needed.
docker restart smarthub-redis

# 3. Destroy the container and recreate it.
#    Use only when you want a guaranteed-clean Docker state, e.g. after
#    a Docker upgrade or if you've been experimenting with port mappings.
docker rm -f smarthub-redis
docker run -d --name smarthub-redis -p 6380:6379 redis:7-alpine
```

All three are safe locally for the reasons listed in section 5.1.

---

## 8 · What this manual does NOT test

These behaviours only show up on real AWS infrastructure and have to
wait for SH-119:

- **Multi-AZ failover.** ElastiCache primary dies, the reader is
  promoted. Local Redis is single-node — no concept of failover. The
  Phase 1 client code uses smarthub_core's connection pool which
  should reconnect transparently, but proving it requires forcing
  the failover in AWS.
- **TLS in transit.** Production uses `rediss://` (note the double
  `s`). Local is plaintext `redis://`. The client library handles
  both, but the cert chain is only meaningful in AWS.
- **AUTH tokens.** Production ElastiCache requires an AUTH token
  injected from Secrets Manager. Local has no auth.
- **BAU burst scale.** Thousands of webhooks per minute across
  multiple orgs simultaneously — only reproducible by pointing the
  Bulk Attachment Uploader at a real Smartsheet account.

When ElastiCache is provisioned in staging, follow
[`docs/infrastructure/I2C_PHASE_1_STAGING_DEPLOY.md`](../../../docs/infrastructure/I2C_PHASE_1_STAGING_DEPLOY.md)
to wire `REDIS_URL` in Secrets Manager and redeploy ECS, then re-run **section 5
(End-to-end golden path)** against staging instead of Docker.
