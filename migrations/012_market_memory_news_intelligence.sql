ALTER TABLE market_memory_days
    DROP CONSTRAINT IF EXISTS market_memory_days_source_quality_check;
ALTER TABLE market_memory_days
    ADD COLUMN IF NOT EXISTS data_quality TEXT,
    ADD COLUMN IF NOT EXISTS causal_evidence_grade TEXT,
    ADD COLUMN IF NOT EXISTS timing_precision TEXT,
    ADD COLUMN IF NOT EXISTS close_change_percent NUMERIC,
    ADD COLUMN IF NOT EXISTS open_to_close_percent NUMERIC,
    ADD COLUMN IF NOT EXISTS special_flags JSONB NOT NULL DEFAULT '[]',
    ADD COLUMN IF NOT EXISTS causal_summary TEXT,
    ADD COLUMN IF NOT EXISTS pattern_lesson TEXT,
    ADD COLUMN IF NOT EXISTS memory_payload JSONB NOT NULL DEFAULT '{}';
ALTER TABLE market_memory_days
    ADD CONSTRAINT market_memory_days_source_quality_v2_check
    CHECK (source_quality IN ('COMPLETE','PARTIAL','HIGH','MEDIUM','LIMITED_CAUSALITY','LOW','STORED_APPLICATION_DATA')) NOT VALID;

ALTER TABLE market_memory_events
    ADD COLUMN IF NOT EXISTS published_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS first_seen_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS knowledge_phase TEXT,
    ADD COLUMN IF NOT EXISTS event_tags JSONB NOT NULL DEFAULT '[]',
    ADD COLUMN IF NOT EXISTS evidence_grade TEXT,
    ADD COLUMN IF NOT EXISTS timing_precision TEXT,
    ADD COLUMN IF NOT EXISTS predictive_eligible BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS context_only BOOLEAN NOT NULL DEFAULT TRUE,
    ADD COLUMN IF NOT EXISTS directional_context TEXT NOT NULL DEFAULT 'UNKNOWN',
    ADD COLUMN IF NOT EXISTS reaction JSONB NOT NULL DEFAULT '{}',
    ADD COLUMN IF NOT EXISTS special_flags JSONB NOT NULL DEFAULT '[]',
    ADD COLUMN IF NOT EXISTS content_hash TEXT,
    ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now();
CREATE INDEX IF NOT EXISTS idx_memory_events_known_at ON market_memory_events(known_at);
CREATE INDEX IF NOT EXISTS idx_memory_events_category ON market_memory_events(category);
CREATE INDEX IF NOT EXISTS idx_memory_events_predictive ON market_memory_events(predictive_eligible) WHERE predictive_eligible;

CREATE TABLE IF NOT EXISTS market_news_articles (
    article_id TEXT PRIMARY KEY,
    content_hash TEXT NOT NULL UNIQUE,
    headline TEXT NOT NULL,
    source TEXT NOT NULL,
    url TEXT NOT NULL,
    published_at TIMESTAMPTZ,
    first_seen_at TIMESTAMPTZ NOT NULL,
    ingested_at TIMESTAMPTZ NOT NULL,
    affected_symbols JSONB NOT NULL DEFAULT '[]',
    category TEXT NOT NULL,
    tags JSONB NOT NULL DEFAULT '[]',
    relevance TEXT NOT NULL,
    relevance_score NUMERIC NOT NULL,
    raw_metadata JSONB NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_news_articles_first_seen ON market_news_articles(first_seen_at DESC);
CREATE INDEX IF NOT EXISTS idx_news_articles_category ON market_news_articles(category);

CREATE TABLE IF NOT EXISTS market_news_events (
    event_id TEXT PRIMARY KEY,
    category TEXT NOT NULL,
    summary TEXT NOT NULL,
    session_date DATE NOT NULL,
    first_known_at TIMESTAMPTZ,
    published_at TIMESTAMPTZ,
    knowledge_phase TEXT NOT NULL,
    predictive_eligible BOOLEAN NOT NULL,
    context_only BOOLEAN NOT NULL,
    evidence_grade TEXT NOT NULL,
    timing_precision TEXT NOT NULL,
    directional_context TEXT NOT NULL,
    relevance TEXT NOT NULL,
    tags JSONB NOT NULL DEFAULT '[]',
    special_flags JSONB NOT NULL DEFAULT '[]',
    source_metadata JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_news_events_first_known ON market_news_events(first_known_at DESC);
CREATE INDEX IF NOT EXISTS idx_news_events_category ON market_news_events(category);

CREATE TABLE IF NOT EXISTS market_event_sources (
    event_id TEXT NOT NULL REFERENCES market_news_events(event_id) ON DELETE CASCADE,
    article_id TEXT NOT NULL REFERENCES market_news_articles(article_id) ON DELETE CASCADE,
    PRIMARY KEY (event_id, article_id)
);

CREATE TABLE IF NOT EXISTS market_event_reactions (
    event_id TEXT PRIMARY KEY REFERENCES market_news_events(event_id) ON DELETE CASCADE,
    reference_at TIMESTAMPTZ,
    reference_price NUMERIC,
    return_5m NUMERIC,
    return_10m NUMERIC,
    return_20m NUMERIC,
    return_30m NUMERIC,
    max_up_excursion NUMERIC,
    max_down_excursion NUMERIC,
    volatility_expansion NUMERIC,
    big_move_outcomes JSONB NOT NULL DEFAULT '{}',
    reaction_quality TEXT NOT NULL,
    measured_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
