# Encrypt-tool

Validates Smartsheet tokens (region-aware), encrypts with AES-GCM, writes to Supabase.

- **Docs:** [PRODUCTION_PUSH_INVENTORY.md](./PRODUCTION_PUSH_INVENTORY.md) (**start here for full prod checklist**), [SECURITY_AND_OPERATIONS.md](./SECURITY_AND_OPERATIONS.md), [AWS_PRODUCTION.md](./AWS_PRODUCTION.md), [ROLLOUT.md](./ROLLOUT.md), [ECS_REVISION_CHECKLIST.md](./ECS_REVISION_CHECKLIST.md)
- **Tests:** `.\scripts\run_encrypt_tool_tests.ps1` or `./scripts/run_encrypt_tool_tests.sh`
- **Smoke (live):** set `ENCRYPT_SMOKE_BASE_URL` (+ `ENCRYPT_SMOKE_JWT` if JWT on), then `python scripts/smoke_encrypt_tool.py`

```bash
pip install -r requirements.txt
python app.py   # :3051
```
