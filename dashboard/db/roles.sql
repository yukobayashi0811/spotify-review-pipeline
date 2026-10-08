-- Read-only role for the deployed backend. Run once in the Supabase SQL editor
-- AFTER db/load.py has created the `spotify` schema (re-run the GRANT lines after
-- any reload, because the loader recreates the schema).
--
-- Replace the password before running. Never commit the real password.

CREATE ROLE dashboard_reader WITH LOGIN PASSWORD 'replace-with-a-long-random-password';

GRANT USAGE ON SCHEMA spotify TO dashboard_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA spotify TO dashboard_reader;
ALTER ROLE dashboard_reader SET statement_timeout = '15s';
