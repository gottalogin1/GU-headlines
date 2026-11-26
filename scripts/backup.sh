#!/bin/sh
# A quick backup of the database only, kept for 7 days. scripts/auto-update.sh
# runs it before each update, so an update that goes wrong can be undone.
# Run it from the GU-headlines folder:
#   ./scripts/backup.sh
#
# The whole site (database, photos, settings, mail) is backed up every night
# by the "backup" service; see scripts/site-backup.sh and
# docs/maintenance.md#backups.
#
# Restore into an empty database:
#   gunzip -c backups/guheadlines-YYYYmmdd-HHMM.sql.gz | docker compose exec -T db \
#       psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"
set -eu

KEEP_DAYS="${KEEP_DAYS:-7}"
DIR="${BACKUP_DIR:-./backups}"
mkdir -p "$DIR"

# Read just the two names from .env. Sourcing it would run values with spaces,
# like SITE_NAME=GU Headlines, as commands.
env_value() {
    [ -f .env ] && sed -n "s/^$1=//p" .env | tail -n 1 | tr -d "\r\"'" || true
}
USER_NAME="$(env_value POSTGRES_USER)"
USER_NAME="${USER_NAME:-guheadlines}"
DB_NAME="$(env_value POSTGRES_DB)"
DB_NAME="${DB_NAME:-guheadlines}"
STAMP="$(date +%Y%m%d-%H%M)"
OUT="$DIR/guheadlines-$STAMP.sql.gz"

docker compose exec -T db pg_dump -U "$USER_NAME" -d "$DB_NAME" --no-owner --clean --if-exists \
    | gzip -9 > "$OUT.partial"
mv "$OUT.partial" "$OUT"
echo "$(date '+%F %T') wrote $OUT ($(du -h "$OUT" | cut -f1))"

find "$DIR" -name 'guheadlines-*.sql.gz' -mtime +"$KEEP_DAYS" -delete
