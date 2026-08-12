"""Strip describe-task-definition JSON for register-task-definition; optional new image on container 0."""
from __future__ import annotations

import json
import sys

DROP = (
    "taskDefinitionArn",
    "revision",
    "status",
    "requiresAttributes",
    "compatibilities",
    "registeredAt",
    "registeredBy",
    "deregisteredAt",
)


def main() -> None:
    data = json.load(sys.stdin)
    td = data["taskDefinition"]
    for k in DROP:
        td.pop(k, None)
    if len(sys.argv) >= 2:
        new_image = sys.argv[1].strip()
        if new_image and td.get("containerDefinitions"):
            td["containerDefinitions"][0]["image"] = new_image
    json.dump(td, sys.stdout, indent=2)


if __name__ == "__main__":
    main()
