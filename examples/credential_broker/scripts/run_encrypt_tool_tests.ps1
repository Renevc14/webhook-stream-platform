# Run unit tests + optional smoke (set ENCRYPT_SMOKE_BASE_URL to hit a live service).
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)

Write-Host "== pytest (encrypt-tool) =="
python -m pip install -q -r requirements.txt
python -m pytest tests -q
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

if ($env:ENCRYPT_SMOKE_BASE_URL) {
  Write-Host "== smoke_encrypt_tool.py =="
  python scripts/smoke_encrypt_tool.py
  exit $LASTEXITCODE
}

Write-Host "Skip smoke (set ENCRYPT_SMOKE_BASE_URL to run)."
exit 0
