-- Indian-equities (NSE/BSE) live-quote universe via Upstox. A separate
-- universe from companies/filings/facts/chunks: Upstox does not cover US
-- SEC filers, and these tickers have no XBRL facts or filing text here.

CREATE TABLE IF NOT EXISTS instruments (
  instrument_key  TEXT PRIMARY KEY,     -- Upstox's key, e.g. 'NSE_EQ|INE467B01029'
  ticker          TEXT NOT NULL,
  exchange        TEXT NOT NULL,        -- NSE_EQ, BSE_EQ
  name            TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS instruments_ticker_exchange_uidx
  ON instruments (ticker, exchange);

-- Append-only audit log of what get_live_quote returned. The model never
-- reads this table to answer a question -- it always calls Upstox live --
-- so a stale row here can never be mistaken for the current price. Exists
-- for debugging/smoke-testing only.
CREATE TABLE IF NOT EXISTS quotes (
  id             BIGSERIAL PRIMARY KEY,
  instrument_key TEXT NOT NULL REFERENCES instruments(instrument_key),
  fetched_at     TIMESTAMPTZ NOT NULL,
  last_price     NUMERIC,
  currency       TEXT NOT NULL DEFAULT 'INR'
);

CREATE INDEX IF NOT EXISTS quotes_instrument_fetched_idx
  ON quotes (instrument_key, fetched_at DESC);

-- sra_reader needs SELECT on instruments to resolve ticker -> instrument_key
-- when answering a question. It does not need quotes: no tool reads that
-- table to answer; only the owner connection writes the audit log.
GRANT SELECT ON instruments TO sra_reader;
