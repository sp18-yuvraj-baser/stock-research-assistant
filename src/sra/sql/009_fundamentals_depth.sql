-- Cash flow, company profile (sector) and corporate actions from Upstox's
-- Fundamentals API, stored by `sra upstox ingest-fundamentals`.

-- Cash-flow rows (operating, investing, financing) share the income-statement
-- shape, so they live in the same table under a third statement.
ALTER TABLE fundamental_financials
  DROP CONSTRAINT IF EXISTS fundamental_financials_statement_check;
ALTER TABLE fundamental_financials
  ADD CONSTRAINT fundamental_financials_statement_check
  CHECK (statement IN ('income_statement', 'balance_sheet', 'cash_flow'));

-- One current snapshot per company, overwritten on refresh. The sector label
-- is Upstox's own ('IT - Software'); peers and sector lists group on it.
CREATE TABLE IF NOT EXISTS company_profiles (
  instrument_key TEXT PRIMARY KEY REFERENCES instruments(instrument_key),
  sector         TEXT,
  description    TEXT,
  fetched_at     TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS company_profiles_sector_idx ON company_profiles (sector);

-- Upstox returns roughly the last year of events; storing them on every
-- refresh builds a longer history over time. kind is Upstox's event name
-- lower-cased ('dividend', 'bonus', 'split', ...).
CREATE TABLE IF NOT EXISTS corporate_actions (
  instrument_key TEXT NOT NULL REFERENCES instruments(instrument_key),
  kind           TEXT NOT NULL,
  ex_date        DATE NOT NULL,
  record_date    DATE,
  amount         NUMERIC,
  ratio          TEXT,
  details        JSONB NOT NULL,
  fetched_at     TIMESTAMPTZ NOT NULL,
  PRIMARY KEY (instrument_key, kind, ex_date)
);

GRANT SELECT ON company_profiles, corporate_actions TO sra_reader;
