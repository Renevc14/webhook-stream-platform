-- Allow Custom link (advanced template) content mode.
-- template column already exists from 001_smartqr_init.sql.

ALTER TABLE smartqr_configs
    DROP CONSTRAINT IF EXISTS smartqr_configs_content_mode_check;

ALTER TABLE smartqr_configs
    ADD CONSTRAINT smartqr_configs_content_mode_check
    CHECK (content_mode IN ('identify', 'form_prefill', 'open_row', 'advanced'));

COMMENT ON COLUMN smartqr_configs.template IS
    'URL/text template with {Column Name} placeholders; used when content_mode=advanced.';
