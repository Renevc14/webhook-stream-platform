-- Target column display name for config dashboard / edit UI
ALTER TABLE smartqr_configs
  ADD COLUMN IF NOT EXISTS target_column_title TEXT;
