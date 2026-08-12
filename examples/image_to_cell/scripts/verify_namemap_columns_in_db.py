"""
Diagnostic — verify that `namemap_column1..10` exist in `imagetocell_config`
and that the frontend's "WHERE filename CONTAINS X" patterns are stored
where the backend can read them.

Usage (from backend/image-to-cell-3, with venv active and .env loaded):

    venv/Scripts/python.exe scripts/verify_namemap_columns_in_db.py
        [--config-id <UUID>]

If --config-id is provided, dumps that specific config's name_map columns.
Otherwise, queries the most recent `name map` config and dumps it.

What we want to confirm:
  1. The columns `namemap_column1..namemap_column10` EXIST on the table
  2. For your test config 'r1', `namemap_column1` = "dog" and
     `namemap_column2` = "horse" (as configured in the UI)
  3. The backend can SELECT them (so the fix is purely additive in the
     Pydantic model + matcher).

If the columns don't exist OR are empty, we have a different problem
than the one we hypothesised — frontend isn't saving them, or there's a
schema gap we need a migration for.
"""

from __future__ import annotations

import argparse
import json
import sys


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--config-id",
        help="Specific config UUID to inspect; omit to get the most recent name_map config",
    )
    args = parser.parse_args()

    from src.services.supabase.client import get_supabase_service

    db = get_supabase_service()
    select_columns = (
        "id, configuration_name, config_type, "
        # Image columns (already known to work)
        "image1_column_name, image2_column_name, image3_column_name, "
        "image4_column_name, image5_column_name, image6_column_name, "
        "image7_column_name, image8_column_name, image9_column_name, "
        "image10_column_name, "
        # The fields the frontend writes for name_map — verifying they
        # exist and carry the user's "CONTAINS" patterns.
        "namemap_column1, namemap_column2, namemap_column3, "
        "namemap_column4, namemap_column5, namemap_column6, "
        "namemap_column7, namemap_column8, namemap_column9, namemap_column10"
    )

    try:
        if args.config_id:
            response = (
                db.table("imagetocell_config")
                .select(select_columns)
                .eq("id", args.config_id)
                .limit(1)
                .execute()
            )
            if not response.data:
                # Fallback: see if there are ANY rows in the table at all.
                any_rows = (
                    db.table("imagetocell_config")
                    .select("id, configuration_name, config_type, created_at")
                    .order("created_at", desc=True)
                    .limit(5)
                    .execute()
                )
                print(f"Config {args.config_id} not found. Most recent 5 configs:")
                for r in (any_rows.data or []):
                    print(f"  - {r['id']}  name={r.get('configuration_name')!r}  type={r.get('config_type')!r}  created_at={r.get('created_at')}")
                return 1
        else:
            # Try a few variants of config_type (the enum is lowercase but
            # historical configs may use other casings).
            response = None
            for ct in ("name map", "name_map", "Name Map", "name-map"):
                response = (
                    db.table("imagetocell_config")
                    .select(select_columns)
                    .eq("config_type", ct)
                    .order("created_at", desc=True)
                    .limit(1)
                    .execute()
                )
                if response.data:
                    print(f"(matched config_type={ct!r})")
                    break
    except Exception as e:
        msg = str(e)
        if "namemap_column" in msg or "column" in msg.lower():
            print(
                "ERROR: columns `namemap_column*` do NOT exist on imagetocell_config.\n"
                "The frontend writes them but the table schema is missing them — we\n"
                "need a DB migration to add them BEFORE doing the backend matcher fix.\n"
                f"\nUnderlying error: {msg}",
                file=sys.stderr,
            )
            return 2
        raise

    rows = response.data or []
    if not rows:
        print("No matching config found.", file=sys.stderr)
        return 1

    cfg = rows[0]
    print(f"\nConfig {cfg['id']}  ({cfg['configuration_name']!r}, type={cfg['config_type']})\n")
    print("Image columns (Smartsheet column names):")
    for i in range(1, 11):
        name = cfg.get(f"image{i}_column_name")
        if name:
            print(f"  image{i}_column_name = {name!r}")

    print("\nname_map patterns (frontend `WHERE filename CONTAINS`):")
    any_set = False
    for i in range(1, 11):
        pattern = cfg.get(f"namemap_column{i}")
        if pattern:
            any_set = True
        marker = " ← " if pattern else ""
        print(f"  namemap_column{i} = {pattern!r}{marker}")

    if not any_set:
        print(
            "\nALL namemap_column* values are NULL. The frontend either failed to\n"
            "save them, or this config was created before name_map was added.\n"
            "Need to inspect the create-config flow."
        )
        return 3

    print("\nVerdict: columns exist and your patterns are stored. The fix is purely")
    print("backend (add fields to model + use them in matcher). No DB migration needed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
