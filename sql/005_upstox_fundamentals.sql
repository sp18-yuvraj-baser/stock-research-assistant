-- Static/periodic fundamentals for the tracked Indian-equities universe, from
-- Upstox's Fundamentals API. A separate concern from `quotes` (live,
-- per-second): these refresh on an ingest cadence, not per question.

-- Key Ratios has no history in Upstox's API -- one current snapshot per
-- company, overwritten on refresh.
CREATE TABLE IF NOT EXISTS fundamental_ratios (
  instrument_key TEXT NOT NULL REFERENCES instruments(instrument_key),
  name           TEXT NOT NULL,        -- 'P/E', 'P/B', 'ROA', 'ROE', 'ROCE', 'EV/EBITDA'
  company_value  NUMERIC,
  sector_value   NUMERIC,
  fetched_at     TIMESTAMPTZ NOT NULL,
  PRIMARY KEY (instrument_key, name)
);

-- Income-statement and balance-sheet rows share one shape: a named category
-- (revenue, net_profit, total_asset, ...) with a value for one period. One
-- table for both, discriminated by `statement`, rather than two
-- near-identical tables.
CREATE TABLE IF NOT EXISTS fundamental_financials (
  instrument_key TEXT NOT NULL REFERENCES instruments(instrument_key),
  statement      TEXT NOT NULL CHECK (statement IN ('income_statement', 'balance_sheet')),
  time_period    TEXT NOT NULL CHECK (time_period IN ('yearly', 'quarterly')),
  category       TEXT NOT NULL,        -- 'revenue', 'net_profit', 'total_asset', ...
  period         TEXT NOT NULL,        -- Upstox's own label, e.g. 'FY2025', 'Q2FY26'
  value          NUMERIC,
  fetched_at     TIMESTAMPTZ NOT NULL,
  PRIMARY KEY (instrument_key, statement, time_period, category, period)
);

CREATE INDEX IF NOT EXISTS fundamental_financials_lookup_idx
  ON fundamental_financials (instrument_key, statement, time_period, category);

GRANT SELECT ON fundamental_ratios, fundamental_financials TO sra_reader;
