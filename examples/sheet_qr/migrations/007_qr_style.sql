-- QR visual style (colors, logo scale). box_size/border already exist.
ALTER TABLE smartqr_configs
  ADD COLUMN IF NOT EXISTS qr_fill_color TEXT NOT NULL DEFAULT '#000000';
ALTER TABLE smartqr_configs
  ADD COLUMN IF NOT EXISTS qr_back_color TEXT NOT NULL DEFAULT '#FFFFFF';
ALTER TABLE smartqr_configs
  ADD COLUMN IF NOT EXISTS qr_logo_scale DOUBLE PRECISION NOT NULL DEFAULT 0.22;
