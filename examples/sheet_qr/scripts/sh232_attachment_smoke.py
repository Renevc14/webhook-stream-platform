"""SH-232 — prove the default_headers crash and exercise row attachments.

Two modes:

1. ``sdk`` (default, no network token required beyond constructing the client)
   Reproduces the staging TypeError on ``client.default_headers[...] = ...`` and
   shows ``with_change_agent`` is the supported hook.

2. ``live`` — requires ``SMARTSHEET_ACCESS_TOKEN`` (and optional
   ``SMARTQR_SMOKE_SHEET_ID`` / ``SMARTQR_SMOKE_ROW_ID``). Uploads a tiny PNG
   through ``LiveSmartsheet.upload_row_attachment``, lists it, then deletes it.

Usage (from repo root)::

    PYTHONPATH=backend/smart-qr/src python backend/smart-qr/scripts/sh232_attachment_smoke.py
    PYTHONPATH=backend/smart-qr/src python backend/smart-qr/scripts/sh232_attachment_smoke.py live
"""

from __future__ import annotations

import argparse
import asyncio
import io
import os
import sys
from pathlib import Path
from typing import Any, Callable

# Load backend/smart-qr/.env without printing values.
_ENV_PATH = Path(__file__).resolve().parents[1] / ".env"
if _ENV_PATH.is_file():
    for line in _ENV_PATH.read_text(encoding="utf-8").splitlines():
        if not line or line.lstrip().startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key and key not in os.environ:
            os.environ[key] = value.strip().strip('"').strip("'")


def _sdk_crash_repro() -> None:
    import smartsheet

    client = smartsheet.Smartsheet("dummy-token-for-local-repro")
    client.errors_as_exceptions(True)
    print("SDK: constructed client")

    crashed = False
    try:
        client.default_headers["Smartsheet-Change-Agent"] = "smart-qr-v1"
    except TypeError as exc:
        crashed = True
        print(f"SDK: OLD PATH TypeError (expected): {exc}")
    if not crashed:
        raise SystemExit("FAIL: expected TypeError assigning into default_headers")

    client.with_change_agent("smart-qr-v1")
    agent = getattr(client, "_change_agent", None)
    if agent != "smart-qr-v1":
        raise SystemExit(f"FAIL: with_change_agent did not stick (got {agent!r})")
    print("SDK: with_change_agent set _change_agent=smart-qr-v1")

    # Attach with a dummy sheet should fail at the API layer, not headers.
    try:
        client.Attachments.attach_file_to_row(
            1,
            1,
            ("smartqr-smoke.png", io.BytesIO(b"\x89PNG\r\n\x1a\n"), "image/png"),
        )
        raise SystemExit("FAIL: dummy-token attach unexpectedly succeeded")
    except TypeError as exc:
        raise SystemExit(f"FAIL: still a TypeError after fix path: {exc}") from exc
    except Exception as exc:  # noqa: BLE001 — any API/auth error is success here
        msg = str(exc)
        if "default_headers" in msg:
            raise SystemExit(f"FAIL: error still mentions default_headers: {msg}") from exc
        print(f"SDK: got past headers; API/auth error as expected: {type(exc).__name__}")


class _PassthroughCore:
    """Minimal core envelope around a real SDK client for live smoke."""

    def __init__(self, sdk: Any) -> None:
        self._sdk = sdk
        self.org_id = "smoke-org"

    async def call_with_retry_async(
        self, operation: Callable[[Any], Any], *, description: str = "", **_k: Any
    ) -> Any:
        print(f"live: {description}")
        return await asyncio.to_thread(operation, self._sdk)


async def _live_roundtrip() -> None:
    import smartsheet

    from smart_qr.adapters.smartsheet.client import LiveSmartsheet

    token = (os.environ.get("SMARTSHEET_ACCESS_TOKEN") or "").strip()
    if not token:
        raise SystemExit(
            "FAIL: SMARTSHEET_ACCESS_TOKEN is empty — set it in the environment "
            "or backend/smart-qr/.env to run live mode"
        )

    sheet_id = int(os.environ.get("SMARTQR_SMOKE_SHEET_ID", "8587348425002884"))
    row_id = int(os.environ.get("SMARTQR_SMOKE_ROW_ID", "8199104658538372"))
    column_id = int(os.environ.get("SMARTQR_SMOKE_COLUMN_ID", "3747629175705476"))

    # Minimal valid PNG (1x1).
    png = (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
        b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f"
        b"\x00\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
    )
    filename = f"smartqr-{column_id}-{row_id}-sh232-smoke.png"

    sdk = smartsheet.Smartsheet(token)
    sdk.errors_as_exceptions(True)
    live = LiveSmartsheet(_PassthroughCore(sdk), "smart-qr-v1", cache_scope="smoke")

    print(f"live: upload sheet={sheet_id} row={row_id} file={filename}")
    attachment_id = await live.upload_row_attachment(
        sheet_id, row_id, png, "smart-qr-v1", filename
    )
    print(f"live: uploaded attachment_id={attachment_id}")

    exists = await live.row_attachment_exists(sheet_id, row_id, attachment_id)
    if not exists:
        raise SystemExit("FAIL: uploaded attachment not listed on the row")
    print("live: list confirms attachment on row")

    # Replace path used by process_row: delete owned id, upload again.
    await live.delete_row_attachment(sheet_id, attachment_id, "smart-qr-v1")
    if await live.row_attachment_exists(sheet_id, row_id, attachment_id):
        raise SystemExit("FAIL: first attachment still present after delete")

    second_id = await live.upload_row_attachment(
        sheet_id, row_id, png, "smart-qr-v1", filename
    )
    if second_id == attachment_id:
        print("live: note — Smartsheet reused attachment id on re-upload")
    if not await live.row_attachment_exists(sheet_id, row_id, second_id):
        raise SystemExit("FAIL: replacement attachment not listed on the row")
    print(f"live: replacement upload ok attachment_id={second_id}")

    await live.delete_row_attachment(sheet_id, second_id, "smart-qr-v1")
    if await live.row_attachment_exists(sheet_id, row_id, second_id):
        raise SystemExit("FAIL: replacement attachment still present after delete")
    print("live: cleaned up replacement attachment")
    print("live: PASS")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "mode",
        nargs="?",
        default="sdk",
        choices=("sdk", "live", "both"),
        help="sdk = crash repro; live = real attach/list/delete; both = sdk then live",
    )
    args = parser.parse_args()

    if args.mode in ("sdk", "both"):
        _sdk_crash_repro()
        print("sdk: PASS")
    if args.mode in ("live", "both"):
        asyncio.run(_live_roundtrip())


if __name__ == "__main__":
    main()
    sys.exit(0)
