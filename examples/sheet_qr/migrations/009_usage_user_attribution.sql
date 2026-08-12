-- Attribute usage to the person who generated the code.
--
-- The Hub's billing reports are per creator (see get_creator_*_usage_for_month
-- for the other tools), so a usage row without a user cannot be attributed.
-- Nullable on purpose: rows written before this migration have no owner, and
-- inventing one would be worse than leaving it unknown.
ALTER TABLE smartqr_usage_log
    ADD COLUMN IF NOT EXISTS user_id UUID;

CREATE INDEX IF NOT EXISTS idx_smartqr_usage_org_user
    ON smartqr_usage_log (organization_id, user_id, created_at DESC);

-- Cycle windows are queried by (org, status, created_at) when metering.
CREATE INDEX IF NOT EXISTS idx_smartqr_usage_org_status_created
    ON smartqr_usage_log (organization_id, status, created_at DESC);
