#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
python -m pip install -q -r requirements.txt
python -m pytest tests -q
if [[ -n "${ENCRYPT_SMOKE_BASE_URL:-}" ]]; then
  python scripts/smoke_encrypt_tool.py
fi
