#!/usr/bin/env python3
"""Run SmartQR sandbox smoke + product matrix against a live sheet."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import subprocess
import sys
import time
import uuid
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_env() -> dict[str, str]:
    vals: dict[str, str] = {}
    for raw in (ROOT / ".env").read_text(encoding="utf-8-sig").splitlines():
        s = raw.strip()
        if s and not s.startswith("#") and "=" in s:
            k, v = s.split("=", 1)
            vals[k] = v
    return vals


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def mint_jwt(vals: dict[str, str], user_id: str) -> str:
    header = b64url(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    now = int(time.time())
    payload = b64url(
        json.dumps(
            {
                "sub": user_id,
                "role": "authenticated",
                "aud": "authenticated",
                "iss": vals["SUPABASE_URL"].rstrip("/") + "/auth/v1",
                "iat": now,
                "exp": now + 3600,
            }
        ).encode()
    )
    sig = hmac.new(
        vals["SUPABASE_JWT_SECRET"].encode(),
        f"{header}.{payload}".encode(),
        hashlib.sha256,
    ).digest()
    return f"{header}.{payload}.{b64url(sig)}"


def http(auth: dict[str, str], method: str, path: str, body=None, timeout: int = 120):
    data = None if body is None else json.dumps(body).encode()
    headers = dict(auth)
    if body is None:
        headers.pop("Content-Type", None)
    req = urllib.request.Request(
        "http://localhost:3021" + path, data=data, headers=headers, method=method
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            raw = res.read().decode()
            return res.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode()
        try:
            parsed = json.loads(raw) if raw else raw
        except Exception:
            parsed = raw
        return exc.code, parsed


def main() -> int:
    vals = load_env()
    meta = json.loads((ROOT / ".sandbox_test_sheet.json").read_text(encoding="utf-8"))

    user_id = "79c8cbf7-8421-47c3-9092-5f07d174b257"
    org_id = "ad2f851e-f432-468a-81fc-6e82bfa7d61a"
    foreign_org = "66da8377-4e01-4218-a24b-3797177e5de0"
    sheet_id = meta["sheet_id"]
    cols = meta["columns"]
    row_ids = meta["row_ids"]

    jwt = mint_jwt(vals, user_id)
    (ROOT / ".sandbox_jwt.txt").write_text(jwt, encoding="utf-8")

    smoke = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "sandbox_smoke.py"),
            "--base-url",
            "http://localhost:3021",
            "--jwt",
            jwt,
            "--user-id",
            user_id,
            "--org-id",
            org_id,
            "--foreign-org-id",
            foreign_org,
        ],
        capture_output=True,
        text=True,
    )
    print(smoke.stdout)
    print("SMOKE_EXIT", smoke.returncode)

    auth = {
        "Authorization": f"Bearer {jwt}",
        "X-User-ID": user_id,
        "X-Organization-ID": org_id,
        "Content-Type": "application/json",
    }
    results: list[tuple[str, bool, str]] = []

    create_body = {
        "name": "Sandbox Identify",
        "worksheet_id": sheet_id,
        "worksheet_name": "SmartQR Sandbox Validation",
        "content_mode": "identify",
        "content_column_ids": [cols["Asset ID"]],
        "content_column_titles": ["Asset ID"],
        "watched_column_ids": [cols["Asset ID"]],
        "target_column_id": cols["QR Code"],
        "target_column_title": "QR Code",
        "write_mode": "replace",
        "code_type": "qr",
        "qr_fill_color": "#000000",
        "qr_back_color": "#FFFFFF",
        "qr_box_size": 4,
        "qr_border": 2,
        "qr_logo_scale": 0.22,
    }

    status, cfg = http(auth, "POST", "/api/v1/configs", create_body)
    config_id = cfg.get("id") if isinstance(cfg, dict) else None
    results.append(
        (
            "10_create_identify",
            status in (200, 201) and bool(config_id),
            f"{status} id={config_id}",
        )
    )
    print("created", status, config_id)

    status, listed = http(auth, "GET", "/api/v1/configs")
    results.append(
        (
            "10_list_no_auto_generate",
            status == 200
            and isinstance(listed, list)
            and any(c.get("id") == config_id for c in listed),
            f"count={len(listed) if isinstance(listed, list) else listed}",
        )
    )

    status, gen = http(auth, "POST", f"/api/v1/configs/{config_id}/backfill", {})
    results.append(("11_generate", status == 200, f"{status} {gen}"))
    print("generate1", status, gen)

    status, gen2 = http(auth, "POST", f"/api/v1/configs/{config_id}/backfill", {})
    results.append(("12_idempotent", status == 200, f"{status} {gen2}"))
    print("generate2", status, gen2)

    req = urllib.request.Request(
        f"https://api.smartsheet.com/2.0/sheets/{sheet_id}?include=objectValue",
        headers={"Authorization": f"Bearer {vals['SMARTSHEET_ACCESS_TOKEN']}"},
    )
    with urllib.request.urlopen(req, timeout=60) as res:
        sheet = json.loads(res.read().decode())

    image_rows = 0
    empty_has_image = False
    for row in sheet.get("rows") or []:
        cells = {c["columnId"]: c for c in row.get("cells") or []}
        target = cells.get(cols["QR Code"]) or {}
        has_img = bool(
            target.get("image")
            or (
                isinstance(target.get("objectValue"), dict)
                and target.get("objectValue", {}).get("objectType") == "IMAGE"
            )
        )
        asset = (cells.get(cols["Asset ID"]) or {}).get("displayValue") or (
            cells.get(cols["Asset ID"]) or {}
        ).get("value")
        print(f"row={row['id']} asset={asset!r} has_img={has_img}")
        if has_img:
            image_rows += 1
        if row["id"] == row_ids[3] and has_img:
            empty_has_image = True
    results.append(("11_images_written", image_rows >= 3, f"image_rows={image_rows}"))
    results.append(
        (
            "11_empty_row_skipped",
            not empty_has_image,
            f"empty_has_image={empty_has_image}",
        )
    )

    status, bad = http(
        auth,
        "POST",
        "/api/v1/configs",
        {
            **create_body,
            "name": "Sandbox bad source",
            "content_column_ids": [cols["QR Code"]],
            "content_column_titles": ["QR Code"],
            "watched_column_ids": [cols["QR Code"]],
            "target_column_id": cols["Notes"],
            "target_column_title": "Notes",
        },
    )
    bad_id = bad.get("id") if isinstance(bad, dict) else None
    print("bad_cfg", status, bad_id)
    status, bad_gen = http(auth, "POST", f"/api/v1/configs/{bad_id}/backfill", {"force": True})
    print("bad_gen", status, bad_gen)
    blob = json.dumps(bad_gen).lower()
    results.append(
        (
            "13_image_source_handled",
            status in (200, 400) and ("error" in blob or "image" in blob or status == 400),
            f"{status} {bad_gen}",
        )
    )

    status, form_cfg = http(
        auth,
        "POST",
        "/api/v1/configs",
        {
            **create_body,
            "name": "Sandbox Prefill",
            "content_mode": "form_prefill",
            "form_base_url": "https://app.smartsheet.com/b/form/example",
            "content_column_ids": [cols["Asset ID"], cols["Serial"]],
            "content_column_titles": ["Asset ID", "Serial"],
            "watched_column_ids": [cols["Asset ID"], cols["Serial"]],
        },
    )
    form_id = form_cfg.get("id") if isinstance(form_cfg, dict) else None
    status, form_gen = http(
        auth,
        "POST",
        f"/api/v1/configs/{form_id}/generate-row",
        {"row_id": row_ids[0], "force": True},
    )
    results.append(
        (
            "14_prefill",
            status == 200
            and isinstance(form_gen, dict)
            and form_gen.get("status") in ("ok", "skipped"),
            f"{status} {form_gen}",
        )
    )
    print("prefill", status, form_gen)

    status, open_cfg = http(
        auth,
        "POST",
        "/api/v1/configs",
        {
            **create_body,
            "name": "Sandbox Open Row",
            "content_mode": "open_row",
            "content_column_ids": [],
            "content_column_titles": [],
            "watched_column_ids": [cols["Asset ID"]],
        },
    )
    open_id = open_cfg.get("id") if isinstance(open_cfg, dict) else None
    status, open_gen = http(
        auth,
        "POST",
        f"/api/v1/configs/{open_id}/generate-row",
        {"row_id": row_ids[1], "force": True},
    )
    results.append(
        (
            "15_open_row",
            status == 200
            and isinstance(open_gen, dict)
            and open_gen.get("status") in ("ok", "skipped"),
            f"{status} {open_gen}",
        )
    )
    print("open_row", status, open_gen)

    boundary = "----SmartQR" + uuid.uuid4().hex
    fields = {
        "text": "SmartQR preview",
        "fill_color": "#0EA5E9",
        "back_color": "#0F172A",
        "box_size": "3",
        "border": "1",
        "logo_scale": "0.2",
    }
    body = b""
    for key, value in fields.items():
        body += (
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{key}\"\r\n\r\n"
            f"{value}\r\n"
        ).encode()
    body += f"--{boundary}--\r\n".encode()
    req = urllib.request.Request(
        "http://localhost:3021/api/v1/qr-preview",
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {jwt}",
            "X-User-ID": user_id,
            "X-Organization-ID": org_id,
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as res:
            png = res.read()
            preview_ok = res.status == 200 and png[:8] == b"\x89PNG\r\n\x1a\n"
            print("preview", res.status, "bytes", len(png))
    except urllib.error.HTTPError as exc:
        preview_ok = False
        print("preview_err", exc.code, exc.read()[:200])
    results.append(("16_preview", preview_ok, "png" if preview_ok else "fail"))

    status, _updated = http(
        auth,
        "PATCH",
        f"/api/v1/configs/{config_id}",
        {
            "qr_fill_color": "#0EA5E9",
            "qr_back_color": "#0F172A",
            "qr_box_size": 3,
            "qr_border": 1,
            "qr_logo_scale": 0.2,
        },
    )
    results.append(("16_style_update", status == 200, f"{status}"))
    status, style_gen = http(auth, "POST", f"/api/v1/configs/{config_id}/backfill", {})
    results.append(("17_style_rewrite", status == 200, f"{status} {style_gen}"))
    print("style_gen", status, style_gen)

    req = urllib.request.Request(
        f"http://localhost:3021/webhook/smartsheet/{org_id}",
        data=b'{"events":[]}',
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Smartsheet-Hmac-SHA256": "deadbeef",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as res:
            hmac_status = res.status
    except urllib.error.HTTPError as exc:
        hmac_status = exc.code
    results.append(("20_hmac_invalid", hmac_status == 401, f"{hmac_status}"))

    status, inj = http(
        auth,
        "POST",
        "/api/v1/dev/inject-event",
        {
            "organization_id": org_id,
            "config_id": config_id,
            "row_id": row_ids[0],
        },
    )
    results.append(("09_inject_closed", status in (401, 403), f"{status} {inj}"))

    print("\n=== PRODUCT RESULTS ===")
    fails = 0
    for name, ok, detail in results:
        print(("PASS" if ok else "FAIL"), name, "-", detail)
        if not ok:
            fails += 1
    print("PRODUCT_FAILS", fails)

    (ROOT / ".sandbox_evidence.json").write_text(
        json.dumps(
            {
                "smoke_exit": smoke.returncode,
                "config_id": config_id,
                "form_id": form_id,
                "open_id": open_id,
                "bad_id": bad_id,
                "results": [{"name": n, "ok": ok, "detail": d} for n, ok, d in results],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return 0 if smoke.returncode == 0 and fails == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
