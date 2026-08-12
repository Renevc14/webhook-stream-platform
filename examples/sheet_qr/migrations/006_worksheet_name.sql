-- Human-readable sheet name for edit/list UI (users do not know worksheet IDs)
ALTER TABLE smartqr_configs
  ADD COLUMN IF NOT EXISTS worksheet_name TEXT;
