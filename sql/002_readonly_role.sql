-- Grants for the role the agent's run_sql tool connects as. Least privilege is
-- enforced by these grants, not by inspecting the SQL string the model emits.
-- The role itself is created by `sra migrate`, which holds the password.

-- No CREATE on the schema: the role cannot make temp tables or functions.
REVOKE ALL ON SCHEMA public FROM sra_reader;
GRANT USAGE ON SCHEMA public TO sra_reader;

GRANT SELECT ON companies, filings, facts, chunks, facts_current TO sra_reader;

-- A runaway query cannot occupy the demo machine, and no statement the model
-- emits can write even if it reaches the server.
ALTER ROLE sra_reader SET statement_timeout = '5s';
ALTER ROLE sra_reader SET idle_in_transaction_session_timeout = '10s';
ALTER ROLE sra_reader SET default_transaction_read_only = on;
