-- The user's watchlist, edited from the chat ("add TCS to my watchlist").
-- Single-user local app: one list, keyed by the listing.

CREATE TABLE IF NOT EXISTS watchlist (
  instrument_key TEXT PRIMARY KEY REFERENCES instruments(instrument_key),
  ticker         TEXT NOT NULL,
  added_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

GRANT SELECT ON watchlist TO sra_reader;
