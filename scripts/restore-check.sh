#!/usr/bin/env bash
# An untested backup is not a backup. Restore the latest dump into a throwaway
# database, assert the tables and at least the crawl_runs table are present, then
# drop it. Exits non-zero if the restore does not verify.
set -euo pipefail

: "${POSTGRES_MIGRATOR_USER:?set POSTGRES_MIGRATOR_USER}"
: "${POSTGRES_MIGRATOR_PASSWORD:?set POSTGRES_MIGRATOR_PASSWORD}"
HOST="${POSTGRES_HOST:-127.0.0.1}"
PORT="${POSTGRES_PORT:-5432}"
BACKUP_DIR="${BACKUP_DIR:-./backups}"

LATEST="$(ls -1t "$BACKUP_DIR"/assay_*.dump 2>/dev/null | head -n1 || true)"
if [[ -z "$LATEST" ]]; then
  echo "No backup found in $BACKUP_DIR" >&2
  exit 1
fi

CHECK_DB="assay_restore_check_$$"
export PGPASSWORD="$POSTGRES_MIGRATOR_PASSWORD"
PSQL=(psql --host="$HOST" --port="$PORT" --username="$POSTGRES_MIGRATOR_USER" -v ON_ERROR_STOP=1)

cleanup() {
  "${PSQL[@]}" --dbname=postgres -c "DROP DATABASE IF EXISTS ${CHECK_DB};" >/dev/null 2>&1 || true
}
trap cleanup EXIT

echo "Restoring ${LATEST} -> ${CHECK_DB}"
"${PSQL[@]}" --dbname=postgres -c "CREATE DATABASE ${CHECK_DB};"
pg_restore --host="$HOST" --port="$PORT" --username="$POSTGRES_MIGRATOR_USER" \
  --dbname="$CHECK_DB" --no-owner "$LATEST"

COUNT="$("${PSQL[@]}" --dbname="$CHECK_DB" -tAc \
  "SELECT count(*) FROM information_schema.tables WHERE table_name IN ('listing_snapshots','crawl_runs');")"
if [[ "$COUNT" -ne 2 ]]; then
  echo "Restore verification FAILED: expected 2 core tables, found ${COUNT}" >&2
  exit 1
fi

echo "Restore verified: both core tables present."
