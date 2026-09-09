#!/usr/bin/env bash
# Runs once, as the Postgres superuser, on first container boot. Creates the
# least-privilege application login role. Table-level privileges are granted by
# the Alembic migration (which runs as the schema owner, after the tables exist).
set -euo pipefail

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
  DO \$\$
  BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '${ASSAY_APP_USER}') THEN
      CREATE ROLE ${ASSAY_APP_USER} LOGIN PASSWORD '${ASSAY_APP_PASSWORD}';
    END IF;
  END
  \$\$;
  GRANT CONNECT ON DATABASE ${POSTGRES_DB} TO ${ASSAY_APP_USER};
  GRANT USAGE ON SCHEMA public TO ${ASSAY_APP_USER};
EOSQL
