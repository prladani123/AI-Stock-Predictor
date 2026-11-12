-- Unresolved special-session times are not regular-session guesses.
ALTER TABLE trading_sessions DROP CONSTRAINT trading_sessions_status_check;
ALTER TABLE trading_sessions ALTER COLUMN market_open_utc DROP NOT NULL;
ALTER TABLE trading_sessions ALTER COLUMN market_close_utc DROP NOT NULL;
ALTER TABLE trading_sessions ADD CONSTRAINT trading_sessions_status_check
    CHECK (status IN ('OPEN','CLOSED','SPECIAL','UNKNOWN'));
ALTER TABLE trading_sessions ADD CONSTRAINT trading_sessions_resolved_times_check
    CHECK ((status = 'UNKNOWN' AND market_open_utc IS NULL AND market_close_utc IS NULL)
        OR (status <> 'UNKNOWN' AND market_open_utc IS NOT NULL AND market_close_utc IS NOT NULL));
