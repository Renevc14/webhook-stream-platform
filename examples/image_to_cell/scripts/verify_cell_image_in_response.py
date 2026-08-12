"""
SH-105 diagnostic — verify Smartsheet returns cell.image in the row payload.

Usage (from backend/image-to-cell-3, with the venv active and .env loaded):

    venv/Scripts/python.exe scripts/verify_cell_image_in_response.py \
        --sheet 696662740651908 \
        --row 1844623607529348

The script reaches Smartsheet through the same SmartsheetService the
backend uses (same token, same SDK), runs:
    - get_sheet(sheet_id, include=["attachments"])    # current backend path
    - get_sheet(sheet_id, include=["attachments", "objectValue"])
    - get_row(sheet_id, row_id, include=["attachments"])
    - get_row(sheet_id, row_id, include=["attachments", "objectValue"])
and prints, for the row of interest, a per-cell summary:
    columnId | has image? | image.id | image.altText
plus the full raw JSON of any cell that has an `image` key.

If the "image" key is missing in the base call but present with
"objectValue", we know SH-105 needs the include flag added.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Dict, Iterable, List


def _summarize_cells(cells: Iterable[Dict[str, Any]], row_label: str) -> None:
    print(f"\n--- {row_label} ---")
    found_any_image = False
    for cell in cells:
        col_id = cell.get("columnId") or cell.get("column_id")
        image = cell.get("image")
        has_image = bool(image)
        if has_image:
            found_any_image = True
            img_id = image.get("id") if isinstance(image, dict) else None
            alt = image.get("altText") if isinstance(image, dict) else None
            print(f"  col={col_id}  image=YES  id={img_id}  altText={alt!r}")
        else:
            print(f"  col={col_id}  image=NO")
    if not found_any_image:
        print("  (no cell exposed an 'image' field in this call)")


def _row_to_dict(row: Any) -> Dict[str, Any]:
    if hasattr(row, "to_dict"):
        return row.to_dict()
    return dict(row)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sheet", type=int, required=True)
    parser.add_argument("--row", type=int, required=True)
    args = parser.parse_args()

    # Lazy imports so the script also works when run from the repo root.
    from src.services.smartsheet.client import get_smartsheet_for_organization
    from src.services.supabase.client import get_supabase_service
    from src.services.supabase.config_repo import ConfigRepository

    db = get_supabase_service()
    cfg_repo = ConfigRepository(db)

    configs = cfg_repo.get_active_webhook_configs(args.sheet)
    if not configs:
        print(f"ERROR: no active configs for sheet {args.sheet}", file=sys.stderr)
        return 2
    org_id = configs[0].organization_id
    print(f"Using org={org_id} (from config on sheet {args.sheet})")

    ss = get_smartsheet_for_organization(org_id)

    # Single-include probe (the production path) is enough to answer the
    # SH-105 question now that we know cell.image is returned by default.
    row_response = ss.sheets.get_row(args.sheet, args.row, include=["attachments"])
    row_dict = _row_to_dict(row_response)

    _summarize_cells(row_dict.get("cells", []), "get_row  include=attachments")

    cell_image_ids = set()
    print("\n--- full cell.image dump ---")
    for cell in row_dict.get("cells", []):
        img = cell.get("image")
        if img:
            print(f"  col={cell.get('columnId')}  cell.image={json.dumps(img)}")
            for k in ("id", "imageId"):
                v = img.get(k) if isinstance(img, dict) else None
                if v:
                    cell_image_ids.add(str(v))

    print("\n--- row attachments (raw) ---")
    attachments = row_dict.get("attachments") or []
    if not attachments:
        print("  (no attachments on this row)")
    matched = []
    for att in attachments:
        att_id = att.get("id")
        att_name = att.get("name")
        att_image_id = att.get("imageId") or att.get("image_id")
        marker = ""
        if att_image_id and str(att_image_id) in cell_image_ids:
            marker = "   <-- MATCHES cell.image.id (SH-105 T1 would fire)"
            matched.append((att_id, att_name, att_image_id))
        print(
            f"  attachment id={att_id}  name={att_name!r}  "
            f"imageId={att_image_id!r}{marker}"
        )
        # Dump full attachment JSON so we can inspect ALL fields
        print("    full JSON: " + json.dumps(att))

    print("\n--- SH-105 verdict ---")
    if not cell_image_ids:
        print(
            "  No cell.image in the row payload at all. SH-105 has no data to act on\n"
            "  in this scenario — either the cells are empty or the include flag is wrong."
        )
    elif not attachments:
        print(
            "  Cells have images but the row reports zero attachments. T1 cannot fire\n"
            "  because there is no attachment to compare. (Default's columns_with_images\n"
            "  exclusion still protects against next-available routing into a filled cell.)"
        )
    elif matched:
        print(
            f"  OK: {len(matched)} attachment(s) carry an imageId that equals a cell.image.id.\n"
            "  T1 will fire for those attachments — fix works."
        )
    else:
        print(
            "  Cells have images and the row has attachments, BUT no attachment.imageId\n"
            "  matches any cell.image.id. T1 cannot fire on this scenario.\n"
            "  Likely cause: Smartsheet does not expose imageId on row attachments by default\n"
            "  (it may live in a separate response field, or only on 'sheet image attachments'\n"
            "  endpoint). SH-105 still protects via:\n"
            "    - default mode's columns_with_images exclusion (no overwrite, no duplicate)\n"
            "    - replace+scan defensive skip\n"
            "  but the T1 same-image fast path is unreachable for our backfill data."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
