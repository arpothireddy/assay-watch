-- One-time setup for a Neon-hosted database. Run this once in the Neon SQL
-- editor (or via psql), connected as the project's default role -- Neon's
-- default role has the privileges needed to create roles within its own
-- project. It plays the same part as scripts/db-init/00-create-app-role.sh
-- does for the self-hosted docker-compose stack: create the least-privilege
-- login role that the app connects as. Table-level grants are then applied
-- automatically by migration 0001 the first time `alembic upgrade head` runs
-- as the migrator (Neon's default role).
--
-- Replace the two placeholders below before running.

CREATE ROLE assay_app LOGIN PASSWORD 'REPLACE_WITH_A_GENERATED_PASSWORD';

GRANT CONNECT ON DATABASE "REPLACE_WITH_YOUR_NEON_DB_NAME" TO assay_app;
GRANT USAGE ON SCHEMA public TO assay_app;
