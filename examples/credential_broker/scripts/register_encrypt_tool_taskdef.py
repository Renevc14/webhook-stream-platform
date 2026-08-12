#!/usr/bin/env python3
"""
Prepare a new ECS task definition JSON for encrypt-tool.

Typical setup (your model): each sensitive value is its own Secrets Manager secret,
referenced in the task definition with ``secrets[].valueFrom`` = full ARN. Non-secret
tunables live under ``environment``.

This script strips read-only fields from ``describe-task-definition`` and optionally:
  - sets a new container image
  - ensures ``SUPABASE_JWT_SECRET`` is present, either by full ARN (--add-jwt-secret-arn)
    or by deriving from a *JSON-key* style ``APP_CRYPTO_KEY`` valueFrom containing
    ``:APP_CRYPTO_KEY::`` (legacy bundle pattern).

Usage:
  python scripts/register_encrypt_tool_taskdef.py --region eu-west-2 --family encrypt-tool
  python scripts/register_encrypt_tool_taskdef.py --region eu-west-2 --family encrypt-tool \\
      --image 123.dkr.ecr.eu-west-2.amazonaws.com/encrypt-tool:latest \\
      --add-jwt-secret-arn arn:aws:secretsmanager:eu-west-2:123:secret:encrypt-tool/supabase-jwt-secret-XXXXXX

Prints register-task-definition JSON to stdout.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys


READ_ONLY = frozenset(
    {
        "taskDefinitionArn",
        "revision",
        "status",
        "requiresAttributes",
        "compatibilities",
        "registeredAt",
        "registeredBy",
        "deregisteredAt",
    }
)


def main() -> int:
    ap = argparse.ArgumentParser(description="Merge SUPABASE_JWT_SECRET into encrypt-tool ECS task definition")
    ap.add_argument("--region", required=True)
    ap.add_argument("--family", required=True)
    ap.add_argument("--image", default=None, help="Optional new container image URI")
    ap.add_argument(
        "--add-jwt-secret-arn",
        default=None,
        metavar="ARN",
        help="Full Secrets Manager ARN for SUPABASE_JWT_SECRET (plain string secret, not JSON key)",
    )
    args = ap.parse_args()

    raw = subprocess.check_output(
        [
            "aws",
            "ecs",
            "describe-task-definition",
            "--task-definition",
            args.family,
            "--region",
            args.region,
            "--output",
            "json",
        ],
        text=True,
    )
    data = json.loads(raw)
    td = data["taskDefinition"]
    for k in READ_ONLY:
        td.pop(k, None)

    cds = td.get("containerDefinitions") or []
    if not cds:
        print("No containerDefinitions", file=sys.stderr)
        return 1
    c0 = cds[0]
    if args.image:
        c0["image"] = args.image

    secrets = list(c0.get("secrets") or [])
    have = {s.get("name") for s in secrets}

    if "SUPABASE_JWT_SECRET" not in have:
        if args.add_jwt_secret_arn:
            secrets.append({"name": "SUPABASE_JWT_SECRET", "valueFrom": args.add_jwt_secret_arn.strip()})
        else:
            crypto = next((s for s in secrets if s.get("name") == "APP_CRYPTO_KEY"), None)
            if crypto and ":APP_CRYPTO_KEY::" in (crypto.get("valueFrom") or ""):
                vf = crypto["valueFrom"]
                secrets.append(
                    {"name": "SUPABASE_JWT_SECRET", "valueFrom": vf.replace(":APP_CRYPTO_KEY::", ":SUPABASE_JWT_SECRET::")}
                )
            else:
                print(
                    "SUPABASE_JWT_SECRET is missing from the task definition.\n"
                    "  Add it in the ECS console (secrets → valueFrom = full ARN), or re-run with:\n"
                    "  --add-jwt-secret-arn arn:aws:secretsmanager:REGION:ACCOUNT:secret:name-SUFFIX",
                    file=sys.stderr,
                )
                return 1

    c0["secrets"] = secrets
    print(json.dumps(td))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
