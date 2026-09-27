#!/bin/sh
# Nightly database backup with rotation. Run from the project directory, e.g. cron:
#   15 3 * * * cd /opt/GU-headlines && ./scripts/backup.sh >> backups/backup.log 2>&1
#
# Restore into an empty database:
#   gunzip -c backups/guheadlines-YYYYmmdd-HHMM.sql.gz | docker compose exec -T db \
#       psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"
set -eu

KEEP_DAYS="${KEEP_DAYS:-30}"
DIR="${BACKUP_DIR:-./backups}"
mkdir -p "$DIR"

# shellcheck disable=SC1091
[ -f .env ] && . ./.env
USER_NAME="${POSTGRES_USER:-guheadlines}"
DB_NAME="${POSTGRES_DB:-guheadlines}"
STAMP="$(date +%Y%m%d-%H%M)"
OUT="$DIR/guheadlines-$STAMP.sql.gz"

docker compose exec -T db pg_dump -U "$USER_NAME" -d "$DB_NAME" --no-owner --clean --if-exists \
    | gzip -9 > "$OUT.partial"
mv "$OUT.partial" "$OUT"
echo "$(date '+%F %T') wrote $OUT ($(du -h "$OUT" | cut -f1))"

find "$DIR" -name 'guheadlines-*.sql.gz' -mtime +"$KEEP_DAYS" -delete
