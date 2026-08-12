-- Optional printable QR attachments and database-level target ownership.
--
-- Cell images remain mandatory. When attach_to_row is enabled, SmartQR also
-- uploads the same PNG as a row attachment and tracks only the attachment it
-- owns, so user-managed attachments are never deleted.

ALTER TABLE smartqr_configs
    ADD COLUMN IF NOT EXISTS attach_to_row BOOLEAN NOT NULL DEFAULT FALSE;

ALTER TABLE smartqr_row_states
    ADD COLUMN IF NOT EXISTS attachment_id BIGINT,
    ADD COLUMN IF NOT EXISTS attachment_payload_hash TEXT;

-- The API already rejects a second config targeting the same sheet column.
-- Enforce the same invariant in Postgres to close the concurrent-create race.
CREATE UNIQUE INDEX IF NOT EXISTS uq_smartqr_configs_org_sheet_target
    ON smartqr_configs (organization_id, worksheet_id, target_column_id);

COMMENT ON COLUMN smartqr_configs.attach_to_row IS
    'Also upload the generated cell image as a printable Smartsheet row attachment.';
COMMENT ON COLUMN smartqr_row_states.attachment_id IS
    'Smartsheet attachment owned by SmartQR; never use it to delete other attachments.';
COMMENT ON COLUMN smartqr_row_states.attachment_payload_hash IS
    'Payload hash represented by attachment_id; enables attachment-only retries without rebilling.';
