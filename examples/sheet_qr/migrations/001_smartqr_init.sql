-- SmartQR owned schema (Postgres / Supabase compatible)
CREATE EXTENSION IF NOT EXISTS "pgcrypto";

CREATE TABLE IF NOT EXISTS smartqr_configs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id UUID NOT NULL,
    worksheet_id BIGINT NOT NULL,
    name TEXT NOT NULL DEFAULT 'SmartQR config',
    content_mode TEXT NOT NULL DEFAULT 'identify'
        CHECK (content_mode IN ('identify', 'form_prefill', 'open_row')),
    form_base_url TEXT,
    content_column_ids BIGINT[] NOT NULL DEFAULT '{}',
    content_column_titles TEXT[] NOT NULL DEFAULT '{}',
    template TEXT,
    form_label_overrides JSONB NOT NULL DEFAULT '{}'::jsonb,
    watched_column_ids BIGINT[] NOT NULL DEFAULT '{}',
    target_column_id BIGINT NOT NULL,
    write_mode TEXT NOT NULL DEFAULT 'replace'
        CHECK (write_mode IN ('replace', 'fill_if_empty')),
    code_type TEXT NOT NULL DEFAULT 'qr'
        CHECK (code_type IN ('qr', 'code128', 'ean')),
    qr_ecc TEXT NOT NULL DEFAULT 'H',
    qr_box_size INT NOT NULL DEFAULT 4,
    qr_border INT NOT NULL DEFAULT 2,
    pause BOOLEAN NOT NULL DEFAULT FALSE,
    runner_user_id UUID,
    last_errors JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_smartqr_configs_org
    ON smartqr_configs (organization_id);
CREATE INDEX IF NOT EXISTS idx_smartqr_configs_worksheet
    ON smartqr_configs (organization_id, worksheet_id);

CREATE TABLE IF NOT EXISTS smartqr_webhooks (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id UUID NOT NULL,
    worksheet_id BIGINT NOT NULL,
    webhook_id BIGINT,
    shared_secret_encrypted TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    runner_user_id UUID,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_smartqr_webhooks_org
    ON smartqr_webhooks (organization_id);

CREATE TABLE IF NOT EXISTS smartqr_row_states (
    config_id UUID NOT NULL REFERENCES smartqr_configs(id) ON DELETE CASCADE,
    row_id BIGINT NOT NULL,
    payload_hash TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (config_id, row_id)
);

CREATE TABLE IF NOT EXISTS smartqr_usage_log (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id UUID NOT NULL,
    config_id UUID,
    row_id BIGINT,
    code_type TEXT,
    bolts INT NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'success',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_smartqr_usage_org
    ON smartqr_usage_log (organization_id, created_at DESC);
