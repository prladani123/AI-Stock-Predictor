ALTER TABLE canonical_instruments ADD COLUMN IF NOT EXISTS strike numeric(18,6);
-- Verify an unchanged master again without changing its first-capture lineage.
ALTER TABLE instrument_snapshots ADD COLUMN IF NOT EXISTS last_verified_at timestamptz;
INSERT INTO schema_migrations(version) VALUES ('014_live_instrument_metadata') ON CONFLICT DO NOTHING;
