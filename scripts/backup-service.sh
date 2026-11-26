#!/bin/sh
# The "backup" service in docker-compose.yml runs this script. It backs up
# the whole site every night, keeps the backups for BACKUP_KEEP_DAYS days,
# and puts one back when you ask. You don't run this script yourself: use
# scripts/site-backup.sh, which runs it inside Docker for you.
#
# It runs in the postgres:16-alpine image (the database's own image), which
# has pg_dump, psql and the usual small tools (BusyBox). docker-compose.yml
# mounts what it backs up:
#   /site                       the GU-headlines folder, read-only (.env, config/)
#   /backups                    the backups folder (backups/ in GU-headlines)
#   /volumes/media              the saved photos
#   /volumes/certificates       Caddy's https certificates
#   /volumes/mail               the mailboxes (your own mail server)
#   /volumes/mail-settings      the mail server's accounts, aliases and DKIM key
#   /volumes/webmail            the webmail's settings and contacts
#
# Each backup is a folder backups/site/<date>-<time>, for example
# backups/site/2026-11-26-0300 (Guam time). It holds:
#   database.sql.gz        the whole database (pg_dump)
#   photos/                the saved photos. A photo that was in the night
#                          before's backup too is not stored again: both
#                          folders share the one copy on disk (a "hard link").
#   env                    the .env file
#   config/                the config folder
#   certificates.tar.gz    Caddy's certificates
#   mail.tar.gz, mail-settings.tar.gz, webmail.tar.gz   (with your own mail server)
#   README.txt, info       what is in it, and when it was made
#
# Commands (as scripts/site-backup.sh gives them):
#   daemon               wait for BACKUP_TIME every night, then back up
#   now                  back up now
#   list                 the backups, newest first
#   describe NAME        when NAME was made, and what is in it
#   tar NAME             write NAME as one .tar file to the screen (stdout)
#   export NAME          write NAME as one file: backups/site-backup-NAME.tar
#   import               read a .tar file (from stdin) into backups/site
#   restore NAME PART..  put these parts of backup NAME back

set -u

SITE=/site
ROOT=/backups
DIR=$ROOT/site
VOL=/volumes
STATUS=$DIR/status
# The user that runs the website (see the Dockerfile). The backups belong to
# it, so the stats page can offer them for download, and nobody else on the
# server can read them.
WEB_UID=10001
# Guam time: 10 hours ahead of UTC, all year.
GUAM=ChST-10
# A backup is not started when it would leave less free space than this,
# in KB (1 GB): the database stops working when the disk is full.
MIN_FREE_KB=1048576

# "08" -> 8 (the shell reads numbers that start with 0 as octal).
num() { echo "$1" | sed 's/^0*//; s/^$/0/'; }

KEEP_DAYS=${BACKUP_KEEP_DAYS:-14}
case $KEEP_DAYS in '' | *[!0-9]*) KEEP_DAYS=14 ;; esac
KEEP_DAYS=$(num "$KEEP_DAYS")
[ "$KEEP_DAYS" -ge 1 ] || KEEP_DAYS=1
[ "$KEEP_DAYS" -le 365 ] || KEEP_DAYS=365

say() { echo "$(TZ=$GUAM date '+%F %T') $*"; }
die() {
    echo "$*" >&2
    exit 1
}

# 2026-11-26-0300: a backup's name.
valid_name() {
    case $1 in
        [0-9][0-9][0-9][0-9]-[0-1][0-9]-[0-3][0-9]-[0-2][0-9][0-5][0-9]) return 0 ;;
        *) return 1 ;;
    esac
}

# "Thu Nov 26 2026, 3:00 AM" from 2026-11-26-0300
when() {
    TZ=$GUAM date -d "$(echo "$1" | sed 's/^\(....-..-..\)-\(..\)\(..\)$/\1 \2:\3/')" \
        '+%a %b %-d %Y, %-I:%M %p' 2>/dev/null || echo "$1"
}

# "2.1 GB" from a number of KB
human() {
    awk -v kb="$1" 'BEGIN {
        if (kb >= 1048576) printf "%.1f GB", kb / 1048576
        else if (kb >= 1024) printf "%.0f MB", kb / 1024
        else printf "%d KB", kb }'
}

free_kb() { df -Pk "$1" | awk 'NR == 2 { print $4 }'; }

# Who gets the files made for you (an exported backup, the backup's .env):
# the user that ran scripts/site-backup.sh, else the owner of the backups
# folder.
owner() {
    case ${HOST_OWNER:-} in
        [0-9]*:[0-9]*) echo "$HOST_OWNER" ;;
        *) stat -c %u:%g "$ROOT" ;;
    esac
}

# The backups, oldest first.
backups() {
    for path in "$DIR"/*; do
        name=${path##*/}
        valid_name "$name" && [ -d "$path" ] && echo "$name"
    done
    return 0
}

newest() { backups | tail -n 1; }

info_value() { sed -n "s/^$2=//p" "$DIR/$1/info" 2>/dev/null | tail -n 1; }

# BACKUP_TIME (03:00) as seconds after midnight; nothing when it is empty.
# When it is not a time, 03:00 (as the stats page reads it too).
backup_seconds() {
    [ -n "${BACKUP_TIME:-}" ] || return 0
    case $BACKUP_TIME in
        [0-9]:[0-5][0-9] | [0-1][0-9]:[0-5][0-9] | 2[0-3]:[0-5][0-9]) ;;
        *)
            say "BACKUP_TIME=$BACKUP_TIME is not a time like 03:00, so the backups run at 03:00." >&2
            BACKUP_TIME=03:00
            ;;
    esac
    hour=$(num "${BACKUP_TIME%%:*}")
    minute=$(num "${BACKUP_TIME##*:}")
    echo $((hour * 3600 + minute * 60))
}

# One value from a .env file, read the way the other scripts read .env.
env_file_value() {
    sed -n "s/^$2=//p" "$1" | tail -n 1 | tr -d "\r\"'" | sed 's/[[:space:]]#.*$//; s/[[:space:]]*$//'
}

# write_status RESULT MESSAGE [NAME]: the file that the stats page and
# scripts/auto-update.sh read. Anyone on the server may read it: it holds
# no secret.
write_status() {
    last_ok=$(sed -n 's/^last_ok=//p' "$STATUS" 2>/dev/null)
    last_ok_name=$(sed -n 's/^last_ok_name=//p' "$STATUS" 2>/dev/null)
    if [ "$1" = ok ]; then
        last_ok=$(date +%s)
        last_ok_name=$3
    fi
    used=$(du -sk "$DIR" 2>/dev/null | awk '{ print $1 }')
    {
        echo "result=$1"
        echo "time=$(date +%s)"
        echo "message=$(printf '%s' "$2" | tr '\n' ' ' | cut -c 1-500)"
        echo "last_ok=${last_ok:-}"
        echo "last_ok_name=${last_ok_name:-}"
        echo "used_kb=${used:-0}"
        echo "free_kb=$(free_kb "$DIR")"
        echo "keep_days=$KEEP_DAYS"
    } >"$STATUS.new" && chmod 644 "$STATUS.new" && mv "$STATUS.new" "$STATUS"
}

# lock wait|now: one backup or restore at a time.
lock() {
    mkdir -p "$DIR"
    exec 9>"$DIR/.lock"
    if [ "$1" = now ]; then
        flock -n 9 || die "A backup or a restore is running right now. Try again in a few minutes."
    else
        flock -n 9 || {
            echo "A backup is running right now. Waiting for it to finish..." >&2
            flock -w 3600 9 || die "The backup that is running did not finish within an hour."
        }
    fi
}

# Does this volume hold anything?
has_files() { [ -d "$1" ] && [ -n "$(ls -A "$1" 2>/dev/null)" ]; }

# ---------------------------------------------------------------- backing up

copy_photos() { # NEW PREV
    new=$1
    prev=$2
    lists=$(mktemp -d)
    if [ -n "$prev" ] && [ -d "$DIR/$prev/photos" ]; then
        # The photos of the backup before, as links: no space is used for
        # them. Then the photos that changed are swapped for new copies.
        cp -al "$DIR/$prev/photos" "$new/photos" || return 1
    else
        mkdir "$new/photos" || return 1
    fi
    # Files the scraper is still writing end in .tmp; they are skipped.
    (cd "$VOL/media" && find . -type f ! -name '*.tmp' | sort) >"$lists/site" || return 1
    (cd "$new/photos" && find . -type f | sort) >"$lists/backup" || return 1
    # Photos deleted from the site since the backup before.
    comm -13 "$lists/site" "$lists/backup" | while IFS= read -r file; do
        rm -f "$new/photos/$file"
    done
    # New photos, and photos changed since the backup before (rare: a
    # damaged photo that was downloaded again).
    {
        comm -23 "$lists/site" "$lists/backup"
        if [ -n "$prev" ] && [ -f "$DIR/$prev/info" ]; then
            (cd "$VOL/media" && find . -type f ! -name '*.tmp' -newer "$DIR/$prev/info")
        fi
    } | sort -u >"$lists/copy"
    while IFS= read -r file; do
        # Never write into a shared file: that would change the backup before too.
        rm -f "$new/photos/$file"
    done <"$lists/copy"
    if [ -s "$lists/copy" ]; then
        tar -C "$VOL/media" -cf - -T "$lists/copy" | tar -C "$new/photos" -xf - || return 1
    fi
    find "$new/photos" -mindepth 1 -type d -empty -delete
    chown -R "$WEB_UID:$WEB_UID" "$new/photos"
    PHOTOS=$(wc -l <"$lists/site" | tr -d ' ')
    rm -rf "$lists"
}

write_readme() { # FOLDER NAME
    cat >"$1/README.txt" <<EOF
GU Headlines backup, made $(when "$2") (Guam time).

What is in it
  database.sql.gz        The whole database: stories, jobs, events, bids,
                         listings, bookings, subscribers and visitor counts.
  photos/                The saved photos.
  env                    Your settings file (.env), with your passwords and
                         keys. Keep this backup private.
  config/                The config folder: news sites, topics, your sponsor
                         picture.
  certificates.tar.gz    Caddy's https certificates.
  mail.tar.gz            The mailboxes (with your own mail server).
  mail-settings.tar.gz   The mail server's accounts, aliases and DKIM key.
  webmail.tar.gz         The webmail's settings and contacts.
  (A file that is not here had nothing in it to back up.)

How to put it back
  Copy this backup (the .tar file) into the GU-headlines folder on the
  server, and run this there:

    ./scripts/site-backup.sh restore site-backup-$2.tar

  More: docs/maintenance.md#restoring-from-a-backup
EOF
}

backup_now() {
    lock now
    mkdir -p "$DIR"
    chmod 755 "$DIR"
    # Folders left by a backup or import that was stopped half way.
    rm -rf "$DIR"/.new-* "$DIR"/.import-* "$DIR"/.export-*

    name=$(TZ=$GUAM date +%Y-%m-%d-%H%M)
    if [ -e "$DIR/$name" ]; then
        die "There is already a backup from this minute ($name). Try again in a minute."
    fi
    prev=$(newest)
    started=$(date +%s)

    # Is there room? A new backup needs about as much space as the one
    # before without its photos, plus the new photos. The first one stores
    # every photo.
    if [ -n "$prev" ]; then
        need=$(find "$DIR/$prev" -maxdepth 1 -type f -exec du -k {} + | awk '{ s += $1 } END { print s + 0 }')
        need=$((need + 204800))
    else
        need=$(du -sk "$VOL/media" | awk '{ print $1 }')
        need=$((${need:-0} + 524288))
    fi
    free=$(free_kb "$DIR")
    if [ "$((free - need))" -lt "$MIN_FREE_KB" ]; then
        message="Not enough disk space: $(human "$free") free, and the backup needs about $(human "$need") and leaves at least $(human "$MIN_FREE_KB") free. Delete old files, or make BACKUP_KEEP_DAYS smaller."
        say "backup skipped: $message"
        write_status failed "$message"
        return 1
    fi

    NEW=$DIR/.new-$name
    trap 'rm -rf "$NEW"' EXIT
    trap 'exit 1' INT TERM
    mkdir -m 700 "$NEW" || return 1
    say "backing up to backups/site/$name"
    parts="database"

    step="the database"
    pg_dump --no-owner --no-privileges --clean --if-exists --compress=6 \
        -f "$NEW/database.sql.gz" 2>"$NEW/.error" || {
        fail "$step" "$(tail -n 3 "$NEW/.error")"
        return 1
    }

    step="the photos"
    PHOTOS=0
    if [ -d "$VOL/media" ]; then
        copy_photos "$NEW" "$prev" 2>"$NEW/.error" || {
            fail "$step" "$(tail -n 3 "$NEW/.error")"
            return 1
        }
        parts="$parts photos"
    fi

    step="the settings"
    if [ -f "$SITE/.env" ]; then
        cp "$SITE/.env" "$NEW/env" || {
            fail "$step" "could not copy .env"
            return 1
        }
        parts="$parts settings"
    fi
    if [ -d "$SITE/config" ]; then
        cp -a "$SITE/config" "$NEW/config" 2>"$NEW/.error" || {
            fail "the config folder" "$(tail -n 3 "$NEW/.error")"
            return 1
        }
        [ -n "$(find "$NEW/config/sponsor" -type f ! -name README.md 2>/dev/null)" ] &&
            parts="$parts sponsor"
    fi

    # The volumes, each as one .tar.gz. A volume with nothing in it (your own
    # mail server is off) is left out.
    for item in certificates:certificates mail:mail mail-settings:mail webmail:webmail; do
        volume=${item%%:*}
        part=${item##*:}
        has_files "$VOL/$volume" || continue
        step="$volume"
        # fail2ban-jail.cf comes from the mail folder in GU-headlines.
        tar -C "$VOL/$volume" --exclude=./fail2ban-jail.cf -czf "$NEW/$volume.tar.gz" . \
            2>"$NEW/.error" || {
            fail "$step" "$(tail -n 3 "$NEW/.error")"
            return 1
        }
        case " $parts " in *" $part "*) ;; *) parts="$parts $part" ;; esac
    done

    rm -f "$NEW/.error"
    write_readme "$NEW" "$name"
    {
        echo "name=$name"
        echo "made=$(TZ=$GUAM date -d "@$started" '+%Y-%m-%dT%H:%M:%S+10:00')"
        echo "parts=$parts"
        echo "photos=$PHOTOS"
        echo "bytes=$(du -sb "$NEW" | awk '{ print $1 }')"
    } >"$NEW/info"
    # Only the website's user (and root) can open the backup: it holds your
    # passwords.
    chown "$WEB_UID:$WEB_UID" "$NEW" "$NEW"/*
    chown -R "$WEB_UID:$WEB_UID" "$NEW/config" 2>/dev/null
    find "$NEW" -maxdepth 1 -type f -exec chmod 600 {} +
    chmod 700 "$NEW"
    mv "$NEW" "$DIR/$name" || return 1
    trap - EXIT

    prune
    took=$(($(date +%s) - started))
    size=$(($(info_value "$name" bytes) / 1024))
    say "backup done: backups/site/$name ($(human "$size"), $PHOTOS photos, $took seconds)"
    write_status ok "Backed up in $took seconds." "$name"
}

fail() { # STEP ERROR
    message="Could not back up $1: $2"
    say "backup failed: $message"
    write_status failed "$message"
}

# Delete backups older than BACKUP_KEEP_DAYS days, but never the newest one,
# and exported .tar files older than 3 days (they are big).
prune() {
    cutoff=$(TZ=$GUAM date -d "@$(($(date +%s) - KEEP_DAYS * 86400))" +%Y-%m-%d-%H%M | tr -d -)
    keep=$(newest)
    for name in $(backups); do
        [ "$name" = "$keep" ] && continue
        if [ "$(echo "$name" | tr -d -)" -lt "$cutoff" ]; then
            rm -rf "${DIR:?}/$name" && say "deleted the old backup $name"
        fi
    done
    find "$ROOT" -maxdepth 1 -name 'site-backup-*.tar' -mtime +2 -exec rm -f {} + 2>/dev/null
    return 0
}

# ------------------------------------------------------------------- daemon

daemon() {
    trap 'exit 0' INT TERM
    mkdir -p "$DIR"
    target=$(backup_seconds)
    if [ -z "$target" ]; then
        say "BACKUP_TIME is empty, so there are no nightly backups."
        while :; do
            sleep 86400 &
            wait $!
        done
    fi
    say "nightly backups at $(printf '%02d:%02d' $((target / 3600)) $((target % 3600 / 60))) Guam time, kept $KEEP_DAYS days"
    # Just started, and no backup from the last day (a new server, or it was
    # off at backup time): back up in 5 minutes, after the site is up.
    first=yes
    while :; do
        now=$(date +%s)
        delay=$(((target - (now + 36000) % 86400 + 86400) % 86400))
        [ "$delay" -eq 0 ] && delay=86400
        if [ "$first" = yes ] && [ "$delay" -gt 600 ]; then
            last=$(sed -n 's/^last_ok=//p' "$STATUS" 2>/dev/null)
            [ "$((now - ${last:-0}))" -gt 86400 ] && delay=300
        fi
        first=no
        sleep "$delay" &
        wait $!
        # The newest version of this script (an update may have changed it).
        sh "$0" now || true
    done
}

# --------------------------------------------------------- list and export

list() {
    names=$(backups)
    if [ -z "$names" ]; then
        echo "No backups yet."
    else
        echo "Backups in backups/site, newest first:"
        for name in $(echo "$names" | sort -r); do
            bytes=$(info_value "$name" bytes)
            printf '  %-17s %-27s %9s   %s\n' "$name" "$(when "$name")" \
                "$(human $((${bytes:-0} / 1024)))" "$(info_value "$name" parts)"
        done
        echo "Space used by all backups: $(human "$(du -sk "$DIR" | awk '{ print $1 }')")" \
            "(a photo kept by several nights is stored once)."
    fi
    echo "Free disk space: $(human "$(free_kb "$DIR")")."
    if [ "$(sed -n 's/^result=//p' "$STATUS" 2>/dev/null)" = failed ]; then
        echo "The last backup failed: $(sed -n 's/^message=//p' "$STATUS")"
    fi
}

describe() {
    valid_name "$1" && [ -d "$DIR/$1" ] || die "There is no backup named $1 in backups/site."
    echo "when=$(when "$1")"
    echo "parts=$(info_value "$1" parts)"
}

# The backup as one .tar file, with everything in a folder site-backup-NAME.
# The folder is made of links, so it uses no space.
write_tar() {
    valid_name "$1" && [ -d "$DIR/$1" ] || die "There is no backup named $1 in backups/site."
    stage=$DIR/.export-$$
    trap 'rm -rf "$stage"' EXIT
    mkdir -m 700 "$stage" && cp -al "$DIR/$1" "$stage/site-backup-$1" &&
        tar -C "$stage" -cf - "site-backup-$1"
    status=$?
    rm -rf "$stage"
    return $status
}

export_file() {
    out=$ROOT/site-backup-$1.tar
    write_tar "$1" >"$out.partial" || {
        rm -f "$out.partial"
        die "Could not write $out"
    }
    # It belongs to you (the user that ran scripts/site-backup.sh), and only
    # you can read it.
    chown "$(owner)" "$out.partial"
    chmod 600 "$out.partial"
    mv "$out.partial" "$out"
    echo "backups/site-backup-$1.tar"
}

# Read a backup .tar (from stdin) into backups/site. Prints its name.
import() {
    lock wait
    stage=$DIR/.import-$$
    trap 'rm -rf "$stage"' EXIT
    mkdir -m 700 "$stage" || exit 1
    tar -C "$stage" -xf - || die "That file is not a backup, or it is damaged (it could not be unpacked)."
    top=$(ls -A "$stage")
    [ "$(echo "$top" | wc -l)" -eq 1 ] && [ -d "$stage/$top" ] ||
        die "That file is not a backup made by GU Headlines (it should hold one folder)."
    [ -f "$stage/$top/database.sql.gz" ] || [ -d "$stage/$top/photos" ] ||
        die "That file is not a backup made by GU Headlines (no database.sql.gz in it)."
    name=${top#site-backup-}
    valid_name "$name" || name=$(sed -n 's/^name=//p' "$stage/$top/info" 2>/dev/null)
    valid_name "$name" || name=$(TZ=$GUAM date +%Y-%m-%d-%H%M)
    if [ -d "$DIR/$name" ]; then
        echo "This server already has the backup $name; using that one." >&2
    else
        chown -R "$WEB_UID:$WEB_UID" "$stage/$top"
        chmod 700 "$stage/$top"
        mv "$stage/$top" "$DIR/$name" || exit 1
    fi
    echo "$name"
}

# ------------------------------------------------------------------ restore

RESTORE_ORDER="database photos sponsor mail webmail certificates settings"

# empty FOLDER [KEEP]: delete everything in FOLDER (but not KEEP).
empty() {
    find "$1" -mindepth 1 -maxdepth 1 ! -name "${2:-.}" -exec rm -rf {} +
}

restore() {
    name=$1
    shift
    valid_name "$name" && [ -d "$DIR/$name" ] || die "There is no backup named $name in backups/site."
    for part in "$@"; do
        case " $RESTORE_ORDER " in *" $part "*) ;; *) die "unknown part: $part" ;; esac
    done
    lock wait
    from=$DIR/$name
    # The database first: if it can't be put back, nothing else is changed.
    # The settings last, as they change the database's password.
    for part in $RESTORE_ORDER; do
        case " $* " in *" $part "*) ;; *) continue ;; esac
        case $part in
            database)
                [ -f "$from/database.sql.gz" ] || die "This backup has no database."
                # A dump that was cut off would put back only part of the
                # database, so check that it is whole first.
                if ! gzip -t "$from/database.sql.gz" 2>/dev/null ||
                    ! gunzip -c "$from/database.sql.gz" | tail -n 5 | grep -q 'dump complete'; then
                    die "The database in this backup is damaged. Nothing was changed."
                fi
                say "putting back the database (this can take a few minutes)"
                # Everything in one go: if anything fails, the database stays
                # as it was. The tables are emptied first, so tables added by a
                # newer version of the site go too; the site makes them again
                # when it starts.
                {
                    echo "SET client_min_messages TO warning;"
                    echo "DROP SCHEMA IF EXISTS public CASCADE; CREATE SCHEMA public;"
                    gunzip -c "$from/database.sql.gz"
                } | psql -X -q -v ON_ERROR_STOP=1 --single-transaction >/dev/null ||
                    die "Could not put back the database. It is as it was before."
                ;;
            settings)
                [ -f "$from/env" ] || die "This backup has no settings (.env)."
                user=$(env_file_value "$from/env" POSTGRES_USER)
                password=$(env_file_value "$from/env" POSTGRES_PASSWORD)
                if [ "${user:-guheadlines}" != "$PGUSER" ]; then
                    die "The backup's settings use another database user (POSTGRES_USER=${user:-guheadlines}), so they were not put back."
                fi
                # scripts/site-backup.sh puts this copy in place of .env.
                if ! cp "$from/env" "$ROOT/env-from-backup-$name" ||
                    ! chown "$(owner)" "$ROOT/env-from-backup-$name" ||
                    ! chmod 600 "$ROOT/env-from-backup-$name"; then
                    die "Could not copy the backup's settings to backups/."
                fi
                if [ -n "$password" ] && [ "$password" != "$PGPASSWORD" ]; then
                    # The database keeps the password it was made with, so it
                    # gets the one in the backup's settings.
                    printf "ALTER ROLE CURRENT_USER PASSWORD :'password';\n" |
                        psql -X -q -v ON_ERROR_STOP=1 -v password="$password" >/dev/null || {
                        rm -f "$ROOT/env-from-backup-$name"
                        die "Could not give the database the password from the backup's settings."
                    }
                fi
                ;;
            photos)
                [ -d "$from/photos" ] || die "This backup has no photos."
                say "putting back the photos"
                empty "$VOL/media"
                cp -a "$from/photos/." "$VOL/media/" || die "Could not put back all the photos."
                # The website saves new photos here too.
                chown "$WEB_UID:$WEB_UID" "$VOL/media"
                ;;
            sponsor)
                [ -d "$SITE/config/sponsor" ] || die "The config/sponsor folder is missing."
                find "$SITE/config/sponsor" -mindepth 1 -maxdepth 1 ! -name README.md -exec rm -rf {} +
                find "$from/config/sponsor" -mindepth 1 -maxdepth 1 ! -name README.md \
                    -exec cp -a {} "$SITE/config/sponsor/" \;
                ;;
            mail | webmail | certificates)
                volumes=$part
                [ "$part" = mail ] && volumes="mail mail-settings"
                for volume in $volumes; do
                    [ -f "$from/$volume.tar.gz" ] || continue
                    say "putting back $volume"
                    empty "$VOL/$volume" fail2ban-jail.cf
                    tar -C "$VOL/$volume" --numeric-owner -xzf "$from/$volume.tar.gz" ||
                        die "Could not put back $volume."
                done
                ;;
        esac
    done
    say "done"
}

case ${1:-} in
    daemon) daemon ;;
    now) backup_now ;;
    list) list ;;
    describe) describe "${2:-}" ;;
    tar) write_tar "${2:-}" ;;
    export) export_file "${2:-}" ;;
    import) import ;;
    restore)
        shift
        restore "$@"
        ;;
    *) die "usage: $0 daemon|now|list|describe NAME|tar NAME|export NAME|import|restore NAME PART..." ;;
esac
