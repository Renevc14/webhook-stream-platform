# encrypt-tool — shared token crypto service (Flask)

See root `CLAUDE.md` and @docs/tools/TOOL_REGISTRY.md.

## Run commands (Windows)

- Python: `.\backend\encrypt-tool\venv\Scripts\python.exe`
- Dev port: **3051**

## Mode

REST API for encrypt/decrypt Smartsheet tokens. Uses `APP_CRYPTO_KEY` shared across all 9 backends.

## Tables (read)

`organizations`, `organization_ss_api_base`, `smartsheet_tokens`

## Notes

Migration planned to `smarthub_core.crypto` in-process library. Never rotate `APP_CRYPTO_KEY` without coordinated restart of all backends.
