ALTER TABLE market_news_articles ADD COLUMN IF NOT EXISTS article_payload jsonb NOT NULL DEFAULT '{}';
ALTER TABLE market_news_events ADD COLUMN IF NOT EXISTS event_payload jsonb NOT NULL DEFAULT '{}';
ALTER TABLE market_event_reactions ADD COLUMN IF NOT EXISTS reaction_payload jsonb NOT NULL DEFAULT '{}';
CREATE TABLE IF NOT EXISTS market_news_source_health (
    source text PRIMARY KEY, state text NOT NULL, last_attempt_at timestamptz NOT NULL,
    last_success_at timestamptz, last_error text, consecutive_failures integer NOT NULL DEFAULT 0,
    latest_article_count integer NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS market_runtime_health (
    component text PRIMARY KEY, payload jsonb NOT NULL, updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS news_pending_reactions ON market_event_reactions(reaction_quality);
