"""Launch a one-off ECS task that inspects (or resets) the shared bolt-cap keys.

Local operator helper for staging: the counters live in the VPC-only Redis, so
the only way to read them without an SSM session is to borrow the SmartQR task
definition and override its command.

    python scripts/boltcap_oneoff.py <org_id> [--reset]
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile

CLUSTER = "stg-smart-qr-cluster"
TASK_DEF = "stg-smart-qr:3"
CONTAINER = "smart-qr"
SUBNETS = ["subnet-0afbd069383af4b67", "subnet-0afa34c2182cda8e3"]
SECURITY_GROUPS = ["sg-02bffc5e5deaf7c5c"]
AWS = ["--profile", "smarthub-staging", "--region", "eu-west-2"]


def remote_program(org: str, reset: bool) -> str:
    keys = [f"bolt-cap:{org}:{suffix}" for suffix in ("consumed", "limit", "cycle_id")]
    lines = [
        "import asyncio, os",
        "import redis.asyncio as redis_asyncio",
        "async def main():",
        "    c = redis_asyncio.from_url(os.environ['REDIS_URL'], decode_responses=True)",
        f"    for k in {keys!r}:",
        "        print('BOLTCAP', k, repr(await c.get(k)), 'ttl', await c.ttl(k), flush=True)",
    ]
    if reset:
        lines += [
            f"    await c.delete({keys[0]!r})",
            f"    print('BOLTCAP reset', {keys[0]!r}, repr(await c.get({keys[0]!r})), flush=True)",
        ]
    lines += ["    await c.aclose()", "asyncio.run(main())"]
    return "\n".join(lines)


def main() -> int:
    positional = [a for a in sys.argv[1:] if not a.startswith("-")]
    if not positional:
        print(__doc__)
        return 2
    org = positional[0]
    reset = "--reset" in sys.argv[1:]

    overrides = {
        "containerOverrides": [
            {
                "name": CONTAINER,
                "command": ["python", "-c", remote_program(org, reset)],
            }
        ]
    }
    network = {
        "awsvpcConfiguration": {
            "subnets": SUBNETS,
            "securityGroups": SECURITY_GROUPS,
            "assignPublicIp": "DISABLED",
        }
    }

    paths = []
    for payload in (overrides, network):
        handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        json.dump(payload, handle)
        handle.close()
        paths.append("file://" + handle.name.replace(os.sep, "/"))

    result = subprocess.run(
        [
            "aws", "ecs", "run-task",
            "--cluster", CLUSTER,
            "--task-definition", TASK_DEF,
            "--launch-type", "FARGATE",
            "--overrides", paths[0],
            "--network-configuration", paths[1],
            *AWS,
            "--output", "json",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(result.stderr.strip())
        return result.returncode
    arn = json.loads(result.stdout)["tasks"][0]["taskArn"]
    print(arn)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
