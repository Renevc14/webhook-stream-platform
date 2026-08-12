"""
CORS allowlist for hub → Image-to-Cell (mirrors Attachment Counter idea).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.config.settings import Settings


def build_i2c_allowed_origins(settings: "Settings") -> list[str]:
    localhost_origins = [
        "http://localhost:3000",
        "http://127.0.0.1:3000",
    ]
    production_hub = [
        "https://prodactivelabs.com",
        "https://www.prodactivelabs.com",
        "https://app.prodactivelabs.com",
        "https://smart-image-to-cell.prodactivelabs.com",
    ]
    cors_origin_env = (settings.cors_origin or "").strip()
    env_origins = (
        [o.strip() for o in cors_origin_env.split(",") if o.strip()] if cors_origin_env else []
    )

    env_lower = (settings.environment or "").strip().lower()
    strict = settings.i2c_cors_strict

    if env_lower == "production" and strict:
        merged = list(dict.fromkeys(env_origins + production_hub))
        out = [o for o in merged if "localhost" not in o and "127.0.0.1" not in o]
    elif env_origins:
        out = list(dict.fromkeys(env_origins + production_hub + localhost_origins))
    else:
        out = list(dict.fromkeys(production_hub + localhost_origins))

    for required in (
        "https://www.prodactivelabs.com",
        "https://prodactivelabs.com",
    ):
        if required not in out:
            out.append(required)
    return out
