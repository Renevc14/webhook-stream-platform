"""
Move I2C_ADMIN_API_KEY from plain environment (wrong: ECS leaves ARN as literal text)
into secrets[] so the key value is injected like SUPABASE_JWT_SECRET.

Reads register-ready task definition JSON on stdin; writes JSON on stdout.
"""
from __future__ import annotations

import json
import sys


def main() -> None:
    td = json.load(sys.stdin)
    for c in td.get("containerDefinitions") or []:
        env = c.get("environment") or []
        secrets = list(c.get("secrets") or [])
        new_env = []
        admin_arn = None
        for e in env:
            name = e.get("name")
            val = (e.get("value") or "").strip()
            # ECS does not resolve Secrets Manager ARNs in plain environment — value must be in secrets[].
            if name == "I2C_ADMIN_API_KEY" and val.startswith("arn:aws:secretsmanager:"):
                admin_arn = val
                continue
            new_env.append(e)
        if admin_arn:
            secrets = [s for s in secrets if s.get("name") != "I2C_ADMIN_API_KEY"]
            secrets.append({"name": "I2C_ADMIN_API_KEY", "valueFrom": admin_arn})
            c["environment"] = new_env
            c["secrets"] = secrets
    json.dump(td, sys.stdout, indent=2)


if __name__ == "__main__":
    main()
