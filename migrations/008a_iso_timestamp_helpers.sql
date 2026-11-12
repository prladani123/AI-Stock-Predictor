-- Explicit ISO inputs only: no timezone-dependent or relative dates.
CREATE OR REPLACE FUNCTION market_iso_timestamp(value text) RETURNS timestamptz
LANGUAGE plpgsql IMMUTABLE STRICT AS $$
BEGIN
  IF value !~ '^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$' THEN
    RAISE EXCEPTION 'Expected offset-aware ISO timestamp: %', value;
  END IF;
  RETURN value::timestamptz;
END $$;
CREATE OR REPLACE FUNCTION market_iso_date(value text) RETURNS date
LANGUAGE plpgsql IMMUTABLE STRICT AS $$
BEGIN
  IF value !~ '^\d{4}-\d{2}-\d{2}$' THEN
    RAISE EXCEPTION 'Expected ISO date: %', value;
  END IF;
  RETURN make_date(substring(value,1,4)::integer, substring(value,6,2)::integer, substring(value,9,2)::integer);
END $$;
