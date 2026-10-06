CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE TABLE IF NOT EXISTS reports (
  id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  text         TEXT NOT NULL CHECK (char_length(text) BETWEEN 1 AND 500),
  location     TEXT NOT NULL,
  lat          DOUBLE PRECISION NOT NULL CHECK (lat BETWEEN -90 AND 90),
  lng          DOUBLE PRECISION NOT NULL CHECK (lng BETWEEN -180 AND 180),
  need_type    TEXT NOT NULL CHECK (need_type IN ('rescue','medical','supplies')),
  urgency      TEXT NOT NULL CHECK (urgency IN ('low','moderate','high','critical')),
  source       TEXT NOT NULL DEFAULT 'citizen' CHECK (source = 'citizen'),
  "timestamp"  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  metadata     JSONB NOT NULL DEFAULT '{}'::jsonb,
  client_token TEXT UNIQUE  -- duplicate-submission guard
);
CREATE INDEX IF NOT EXISTS reports_urgency_ts_idx ON reports (urgency, "timestamp" DESC);
CREATE INDEX IF NOT EXISTS reports_need_type_idx ON reports (need_type);

-- Upgrade path for databases created before need_type became rescue/medical/supplies.
-- Idempotent: a no-op on fresh installs. Existing rows are left untouched (NOT VALID only
-- enforces the new list for new/updated rows); see README for the optional remap of old values.
DO $$
DECLARE c record;
BEGIN
  IF to_regclass('reports') IS NULL THEN RETURN; END IF;
  FOR c IN SELECT conname FROM pg_constraint
           WHERE conrelid = 'reports'::regclass AND contype = 'c'
             AND pg_get_constraintdef(oid) LIKE '%need_type%' AND pg_get_constraintdef(oid) NOT LIKE '%supplies%'
  LOOP
    EXECUTE format('ALTER TABLE reports DROP CONSTRAINT %I', c.conname);
  END LOOP;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid = 'reports'::regclass AND contype = 'c'
                 AND pg_get_constraintdef(oid) LIKE '%need_type%' AND pg_get_constraintdef(oid) LIKE '%supplies%') THEN
    ALTER TABLE reports ADD CONSTRAINT reports_need_type_check
      CHECK (need_type IN ('rescue','medical','supplies')) NOT VALID;
  END IF;
END $$;
