-- Logo branding per SmartQR config (PNG/JPEG stored as bytes)
ALTER TABLE smartqr_configs
    ADD COLUMN IF NOT EXISTS logo_png BYTEA,
    ADD COLUMN IF NOT EXISTS logo_filename TEXT,
    ADD COLUMN IF NOT EXISTS logo_content_type TEXT;

ALTER TABLE smartqr_configs
    ALTER COLUMN qr_ecc SET DEFAULT 'H';

UPDATE smartqr_configs SET qr_ecc = 'H' WHERE qr_ecc IS DISTINCT FROM 'H';
