-- Idempotent billing key for stream replay / force retries.
-- A stable operation_key (config:row:payload_hash) may be written at most once
-- per organization so a reclaim cannot create a second invoice row.

ALTER TABLE smartqr_usage_log
    ADD COLUMN IF NOT EXISTS operation_key TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS uq_smartqr_usage_org_operation
    ON smartqr_usage_log (organization_id, operation_key)
    WHERE operation_key IS NOT NULL;
