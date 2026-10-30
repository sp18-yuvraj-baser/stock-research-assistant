-- Mutual-fund NAVs from AMFI (`sra ingest funds`) and a response cache for
-- the optional BharatStock API, whose free plan allows 60 requests a day.

CREATE TABLE IF NOT EXISTS funds (
  scheme_code  TEXT PRIMARY KEY,     -- AMFI code, also used by api.mfapi.in
  name         TEXT NOT NULL,
  fund_house   TEXT,
  category     TEXT,                 -- e.g. 'Equity Scheme - Flexi Cap Fund'
  plan         TEXT,                 -- 'Direct Plan' / 'Regular Plan'
  option       TEXT,                 -- 'Growth', 'IDCW Payout', ...
  isin         TEXT,
  nav          NUMERIC,
  nav_date     DATE,
  fetched_at   TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS funds_name_idx ON funds (lower(name));

CREATE TABLE IF NOT EXISTS bharatstock_cache (
  request     TEXT PRIMARY KEY,      -- path plus sorted query string
  payload     JSONB NOT NULL,
  fetched_at  TIMESTAMPTZ NOT NULL
);

GRANT SELECT ON funds TO sra_reader;
