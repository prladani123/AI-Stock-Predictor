CREATE TABLE IF NOT EXISTS dataset_revisions (
    id uuid PRIMARY KEY,
    created_at timestamptz NOT NULL,
    session_start date NOT NULL,
    session_end date NOT NULL,
    row_count bigint NOT NULL CHECK (row_count >= 0),
    quality_summary jsonb NOT NULL,
    source_job_ids uuid[] NOT NULL,
    checksum char(64) NOT NULL,
    status text NOT NULL CHECK (status IN ('CANDIDATE', 'ACCEPTED', 'REJECTED')),
    decided_at timestamptz,
    decision_reason text,
    CHECK (session_end >= session_start),
    CHECK (
      (status = 'CANDIDATE' AND decided_at IS NULL)
      OR (status IN ('ACCEPTED', 'REJECTED') AND decided_at IS NOT NULL)
    )
);

CREATE INDEX IF NOT EXISTS dataset_revisions_checksum_idx
    ON dataset_revisions(checksum);

CREATE TABLE IF NOT EXISTS dataset_revision_instruments (
    revision_id uuid NOT NULL REFERENCES dataset_revisions(id) ON DELETE RESTRICT,
    instrument_id bigint NOT NULL REFERENCES canonical_instruments(id) ON DELETE RESTRICT,
    PRIMARY KEY (revision_id, instrument_id)
);

CREATE TABLE IF NOT EXISTS dataset_revision_candles (
    revision_id uuid NOT NULL REFERENCES dataset_revisions(id) ON DELETE RESTRICT,
    instrument_id bigint NOT NULL,
    candle_at timestamptz NOT NULL,
    open numeric(18,8) NOT NULL,
    high numeric(18,8) NOT NULL,
    low numeric(18,8) NOT NULL,
    close numeric(18,8) NOT NULL,
    volume bigint NOT NULL,
    open_interest bigint,
    source_job_id uuid NOT NULL REFERENCES historical_import_jobs(id) ON DELETE RESTRICT,
    PRIMARY KEY (revision_id, instrument_id, candle_at),
    FOREIGN KEY (revision_id, instrument_id)
      REFERENCES dataset_revision_instruments(revision_id, instrument_id) ON DELETE RESTRICT,
    CHECK (open > 0 AND high > 0 AND low > 0 AND close > 0),
    CHECK (volume >= 0 AND (open_interest IS NULL OR open_interest >= 0)),
    CHECK (high >= GREATEST(open, high, low, close)),
    CHECK (low <= LEAST(open, high, low, close))
);

CREATE OR REPLACE FUNCTION reject_revision_content_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'dataset revision contents are immutable';
END;
$$;

DROP TRIGGER IF EXISTS immutable_revision_candles ON dataset_revision_candles;
CREATE TRIGGER immutable_revision_candles
BEFORE UPDATE OR DELETE ON dataset_revision_candles
FOR EACH ROW EXECUTE FUNCTION reject_revision_content_mutation();

DROP TRIGGER IF EXISTS immutable_revision_instruments ON dataset_revision_instruments;
CREATE TRIGGER immutable_revision_instruments
BEFORE UPDATE OR DELETE ON dataset_revision_instruments
FOR EACH ROW EXECUTE FUNCTION reject_revision_content_mutation();

CREATE OR REPLACE FUNCTION enforce_revision_state_transition()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'dataset revisions are immutable';
    END IF;
    IF OLD.status <> 'CANDIDATE' THEN
        RAISE EXCEPTION 'decided dataset revisions are immutable';
    END IF;
    IF NEW.id <> OLD.id
       OR NEW.created_at <> OLD.created_at
       OR NEW.session_start <> OLD.session_start
       OR NEW.session_end <> OLD.session_end
       OR NEW.row_count <> OLD.row_count
       OR NEW.quality_summary <> OLD.quality_summary
       OR NEW.source_job_ids <> OLD.source_job_ids
       OR NEW.checksum <> OLD.checksum
       OR NEW.status NOT IN ('ACCEPTED', 'REJECTED') THEN
        RAISE EXCEPTION 'only a CANDIDATE acceptance/rejection transition is allowed';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS immutable_dataset_revision ON dataset_revisions;
CREATE TRIGGER immutable_dataset_revision
BEFORE UPDATE OR DELETE ON dataset_revisions
FOR EACH ROW EXECUTE FUNCTION enforce_revision_state_transition();

INSERT INTO schema_migrations(version) VALUES ('002_accepted_datasets')
ON CONFLICT (version) DO NOTHING;
