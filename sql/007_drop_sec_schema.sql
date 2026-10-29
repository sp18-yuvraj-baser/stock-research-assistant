-- The SEC-filings store (companies, filings, facts, chunks) belongs to a
-- retired product scope; the live data is deliberately discarded.
DROP VIEW IF EXISTS facts_current;
-- CASCADE drops the foreign keys and indexes that tie these tables together.
DROP TABLE IF EXISTS chunks, facts, filings, companies CASCADE;
-- pgvector was only used by chunks.embedding.
DROP EXTENSION IF EXISTS vector;

-- The CSV import feature is being removed. The tables stay (the data is the
-- user's), but the chat agent's read-only role no longer sees them.
REVOKE SELECT ON research_imports, research_stocks FROM sra_reader;
