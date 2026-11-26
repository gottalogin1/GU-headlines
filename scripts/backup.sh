#!/bin/sh
# Nightly database backup with rotation (and the mailboxes, when your own mail
# server is on). Run from the project directory, e.g. cron:
#   15 3 * * * cd /opt/GU-headlines && ./scripts/backup.sh >> backups/backup.log 2>&1
#
# Restore into an empty database:
#   gunzip -c backups/guheadlines-YYYYmmdd-HHMM.sql.gz | docker compose exec -T db \
#       psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"
set -eu

KEEP_DAYS="${KEEP_DAYS:-30}"
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

# Your own mail server (COMPOSE_PROFILES=mail in .env, see docs/email-server.md):
# the mailboxes and the mail server's settings (mailboxes, passwords, the DKIM
# key). At most one a day (scripts/auto-update.sh runs this script before
# each update too), kept 7 days, as they can be big. How to put one back:
# docs/email-server.md#looking-after-it
if env_value COMPOSE_PROFILES | tr ',' '\n' | grep -qx '[[:space:]]*mail[[:space:]]*' &&
    [ -z "$(find "$DIR" -name 'mail-*.tar.gz' -mmin -1200)" ]; then
    MAIL_OUT="$DIR/mail-$STAMP.tar.gz"
    docker compose run --rm --no-deps -T --entrypoint tar mailserver \
        czf - -C / --exclude=tmp/docker-mailserver/fail2ban-jail.cf var/mail tmp/docker-mailserver \
        > "$MAIL_OUT.partial"
    mv "$MAIL_OUT.partial" "$MAIL_OUT"
    echo "$(date '+%F %T') wrote $MAIL_OUT ($(du -h "$MAIL_OUT" | cut -f1))"
    find "$DIR" -name 'mail-*.tar.gz' -mtime +"${MAIL_KEEP_DAYS:-7}" -delete
fi
