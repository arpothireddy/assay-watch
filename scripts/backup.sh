#!/usr/bin/env bash
# Nightly logical backup. The snapshot history is the entire project and cannot
# be reacquired at any price, so this runs on a timer and its restore is tested
# by restore-check.sh. Retention is configurable via BACKUP_RETENTION.
set -euo pipefail

: "${POSTGRES_DB:?set POSTGRES_DB}"
: "${POSTGRES_MIGRATOR_USER:?set POSTGRES_MIGRATOR_USER}"
: "${POSTGRES_MIGRATOR_PASSWORD:?set POSTGRES_MIGRATOR_PASSWORD}"
HOST="${POSTGRES_HOST:-127.0.0.1}"
PORT="${POSTGRES_PORT:-5432}"
BACKUP_DIR="${BACKUP_DIR:-./backups}"
RETENTION="${BACKUP_RETENTION:-30}"

mkdir -p "$BACKUP_DIR"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
OUT="$BACKUP_DIR/assay_${STAMP}.dump"

echo "Backing up ${POSTGRES_DB} -> ${OUT}"
PGPASSWORD="$POSTGRES_MIGRATOR_PASSWORD" pg_dump \
  --host="$HOST" --port="$PORT" --username="$POSTGRES_MIGRATOR_USER" \
  --dbname="$POSTGRES_DB" --format=custom --file="$OUT"

echo "Pruning backups older than ${RETENTION} days"
find "$BACKUP_DIR" -name 'assay_*.dump' -type f -mtime "+${RETENTION}" -delete

echo "Done. Latest: $OUT"
