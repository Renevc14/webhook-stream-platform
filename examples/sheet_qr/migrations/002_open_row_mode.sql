-- Allow Open row in Smartsheet (row permalink) content mode
ALTER TABLE smartqr_configs
    DROP CONSTRAINT IF EXISTS smartqr_configs_content_mode_check;

ALTER TABLE smartqr_configs
    ADD CONSTRAINT smartqr_configs_content_mode_check
    CHECK (content_mode IN ('identify', 'form_prefill', 'open_row'));
