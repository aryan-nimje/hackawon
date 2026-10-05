CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE TABLE IF NOT EXISTS reports (
  id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  text         TEXT NOT NULL CHECK (char_length(text) BETWEEN 1 AND 500),
  location     TEXT NOT NULL,
  lat          DOUBLE PRECISION NOT NULL CHECK (lat BETWEEN -90 AND 90),
  lng          DOUBLE PRECISION NOT NULL CHECK (lng BETWEEN -180 AND 180),
  need_type    TEXT NOT NULL CHECK (need_type IN ('rescue','medical','shelter','food_water','other')),
  urgency      TEXT NOT NULL CHECK (urgency IN ('low','moderate','high','critical')),
  source       TEXT NOT NULL DEFAULT 'citizen' CHECK (source = 'citizen'),
  "timestamp"  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  metadata     JSONB NOT NULL DEFAULT '{}'::jsonb,
  client_token TEXT UNIQUE  -- duplicate-submission guard
);
CREATE INDEX IF NOT EXISTS reports_urgency_ts_idx ON reports (urgency, "timestamp" DESC);
CREATE INDEX IF NOT EXISTS reports_need_type_idx ON reports (need_type);
