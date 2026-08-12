-- Optional write-timestamp column + IANA timezone (Prefill form / generation audit)
ALTER TABLE smartqr_configs
    ADD COLUMN IF NOT EXISTS timestamp_column_id BIGINT,
    ADD COLUMN IF NOT EXISTS timezone TEXT NOT NULL DEFAULT 'UTC';
