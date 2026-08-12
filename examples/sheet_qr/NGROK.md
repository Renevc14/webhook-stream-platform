# Local / ngrok testing for SmartQR
#
# 1. Start stack:
#    docker compose -f backend/smart-qr/docker-compose.yml up --build -d
#
# 2. Publish API (Smartsheet callbacks + optional remote Hub):
#    ngrok http 3021
#    export WEBHOOK_PUBLIC_BASE_URL=https://YOUR_SUBDOMAIN.ngrok-free.app
#    export SMARTSHEET_ACCESS_TOKEN=...
#    export SMARTSHEET_MODE=live
#    docker compose -f backend/smart-qr/docker-compose.yml up -d smart-qr
#
# 3. Hub frontend (same machine):
#    REACT_APP_SMARTQR_BACKEND_URL=http://localhost:3021 npm start
#    open http://localhost:3000/smart-qr
#
# 4. Create config → webhook auto-registers when WEBHOOK_PUBLIC_BASE_URL + token are set.
#    Confirm server logs show status=ENABLED (not DISABLED_VERIFICATION_FAILED).
#    Edit a watched cell on the sheet → QR regenerates in target column.
#
# Callback path: Smartsheet → ngrok → POST /webhook/smartsheet/{org_id}
#
# Challenge response must be JSON {"smartsheetHookResponse": "..."} plus header
# Smartsheet-Hook-Response. Plain-text echo causes DISABLED_VERIFICATION_FAILED
# with "Invalid JSON in response body".
#
# If ngrok was down during enable, Smartsheet disables the webhook — re-register
# with ngrok healthy (recreate the config, or POST /api/v1/configs/{id}/register-webhook).
#
# Important: enable must not block the FastAPI event loop (SDK runs in a thread).
# Otherwise Smartsheet's challenge cannot be answered and you get Request timed out.
