-- User-imported research evidence for the local Indian-equity dashboard.
-- This is additive: existing Upstox tables continue to work unchanged.

CREATE TABLE IF NOT EXISTS research_imports (
  id              BIGSERIAL PRIMARY KEY,
  imported_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  source_name     TEXT NOT NULL DEFAULT 'Screener CSV',
  source_file     TEXT NOT NULL,
  row_count       INT NOT NULL CHECK (row_count > 0)
);

CREATE TABLE IF NOT EXISTS research_stocks (
  import_id       BIGINT NOT NULL REFERENCES research_imports(id) ON DELETE CASCADE,
  symbol          TEXT NOT NULL,
  exchange        TEXT NOT NULL CHECK (exchange IN ('NSE', 'BSE')),
  company_name    TEXT NOT NULL,
  sector          TEXT NOT NULL,
  market_cap      NUMERIC NOT NULL,
  pe              NUMERIC NOT NULL,
  roe             NUMERIC NOT NULL,
  roce            NUMERIC NOT NULL,
  debt_to_equity  NUMERIC NOT NULL,
  sales_growth    NUMERIC NOT NULL,
  profit_growth   NUMERIC NOT NULL,
  source_url      TEXT NOT NULL,
  exported_at     TIMESTAMPTZ NOT NULL,
  raw_values      JSONB NOT NULL,
  PRIMARY KEY (import_id, symbol, exchange)
);

CREATE INDEX IF NOT EXISTS research_stocks_latest_lookup_idx
  ON research_stocks (symbol, exchange, import_id DESC);
CREATE INDEX IF NOT EXISTS research_stocks_sector_idx
  ON research_stocks (import_id, sector);

GRANT SELECT ON research_imports, research_stocks TO sra_reader;
