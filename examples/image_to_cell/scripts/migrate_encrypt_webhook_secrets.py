"""SH-108 — backfill script: encrypt every plaintext webhook ``shared_secret``.

Before SH-108, the Smartsheet webhook ``shared_secret`` was stored in plain text
in ``imagetocell_webhooks.shared_secret``. The code fix in
``WebhookRepository`` (a) encrypts every new write and (b) reads legacy
plaintext rows transparently during a rollout window. This script closes the
gap by encrypting the existing rows in place.

The script is **idempotent**: rows that are already encrypted are detected by
attempting to decrypt them and skipped. Safe to run repeatedly without
side effects.

Usage
-----

    # Activate the i2c-3 venv first so smarthub_core / supabase-py are in PATH.
    cd backend/image-to-cell-3
    source venv/Scripts/activate

    # Required env vars (use staging or production credentials):
    #   SUPABASE_URL
    #   SUPABASE_SERVICE_ROLE_KEY
    #   APP_CRYPTO_KEY
    python scripts/migrate_encrypt_webhook_secrets.py

    # Dry run — see what would change without writing anything:
    python scripts/migrate_encrypt_webhook_secrets.py --dry-run

Reports
-------

The script prints a summary at the end:

    SH-108 backfill complete
      total rows seen           : N
      already encrypted (skip)  : N
      plaintext encrypted now   : N
      empty / null secrets      : N
      errors (kept as-is)       : N

If `errors > 0`, those rows were left untouched and need manual inspection —
they are NOT a deploy blocker (the dual-read in `WebhookRepository` keeps them
working), but they should be looked at after the script run.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

# Make the i2c-3 `src/` importable when running this script directly.
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Load .env (same file the FastAPI app reads via pydantic_settings) so the
# script picks up SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY / APP_CRYPTO_KEY
# without the operator having to export them manually. An explicit
# --env-file flag overrides the default lookup.
try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - dotenv ships with pydantic_settings
    load_dotenv = None  # type: ignore[assignment]

from smarthub_core.crypto import CryptoError, decrypt_token, encrypt_token  # noqa: E402

from src.config.constants import TABLE_NAMES  # noqa: E402
from src.services.supabase.client import get_supabase_service  # noqa: E402


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("sh108-backfill")


def _looks_encrypted(value: str) -> bool:
    """Decrypt-probe: True iff the value is already an AES-GCM ciphertext."""
    try:
        decrypt_token(value)
        return True
    except CryptoError:
        return False


def backfill(dry_run: bool = False) -> int:
    db = get_supabase_service()
    log.info("Fetching all rows from %s ...", TABLE_NAMES.WEBHOOKS)

    result = db.execute_with_retry(
        lambda: db.table(TABLE_NAMES.WEBHOOKS)
            .select("worksheet_id, shared_secret")
            .execute(),
        "SH-108 list webhooks",
    )
    rows = result.data or []
    log.info("Loaded %d rows", len(rows))

    total = len(rows)
    already = 0
    encrypted_now = 0
    empty = 0
    errors = 0

    for row in rows:
        worksheet_id = row.get("worksheet_id")
        secret = row.get("shared_secret")

        if not secret:
            empty += 1
            continue

        if _looks_encrypted(secret):
            already += 1
            continue

        # Plaintext → encrypt and write back.
        try:
            ciphertext = encrypt_token(secret)
        except Exception as e:
            log.error(
                "[worksheet=%s] encryption failed: %s — leaving row untouched",
                worksheet_id, e,
            )
            errors += 1
            continue

        if dry_run:
            log.info("[DRY-RUN] worksheet=%s would be encrypted", worksheet_id)
            encrypted_now += 1
            continue

        try:
            db.execute_with_retry(
                lambda ws=worksheet_id, ct=ciphertext: db.table(TABLE_NAMES.WEBHOOKS)
                    .update({"shared_secret": ct})
                    .eq("worksheet_id", ws)
                    .execute(),
                f"SH-108 encrypt secret worksheet={worksheet_id}",
            )
            encrypted_now += 1
            log.info("[worksheet=%s] encrypted", worksheet_id)
        except Exception as e:
            log.error(
                "[worksheet=%s] DB update failed: %s — leaving row untouched",
                worksheet_id, e,
            )
            errors += 1

    log.info("")
    log.info("SH-108 backfill complete%s", " (DRY RUN)" if dry_run else "")
    log.info("  total rows seen           : %d", total)
    log.info("  already encrypted (skip)  : %d", already)
    log.info("  plaintext encrypted now   : %d", encrypted_now)
    log.info("  empty / null secrets      : %d", empty)
    log.info("  errors (kept as-is)       : %d", errors)

    return 0 if errors == 0 else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would change without writing to the DB.",
    )
    parser.add_argument(
        "--env-file",
        default=str(ROOT / ".env"),
        help="Path to the .env file to load (default: backend/image-to-cell-3/.env).",
    )
    args = parser.parse_args()

    # Load env vars from the .env file the FastAPI app uses, so the operator
    # can run the script with just the file the rest of the service already
    # has configured. If python-dotenv is missing or the file does not exist
    # we silently fall through to whatever is already in the process env.
    if load_dotenv is not None:
        env_path = Path(args.env_file)
        if env_path.is_file():
            load_dotenv(env_path, override=False)
            log.info("Loaded env from %s", env_path)
        else:
            log.warning("Env file not found at %s — relying on process env", env_path)

    # Sanity-check required env vars early so failures are explicit.
    missing = [
        v for v in ("SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY", "APP_CRYPTO_KEY")
        if not os.getenv(v)
    ]
    if missing:
        log.error("Missing required env vars: %s", ", ".join(missing))
        return 2

    return backfill(dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
