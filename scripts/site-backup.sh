#!/usr/bin/env bash
# Backs up the whole site, and puts a backup back. Run it in the GU-headlines
# folder on the server.
#
# Every night at BACKUP_TIME (3:00 AM Guam time unless .env says otherwise),
# the "backup" part of the site backs everything up by itself: the database,
# the photos, your settings (.env), the config folder, Caddy's https
# certificates, and the mailboxes when your own mail server is on. Backups
# are kept for BACKUP_KEEP_DAYS days (14 unless .env says otherwise), in
# backups/site. You don't need this script for that.
#
#   ./scripts/site-backup.sh now                back up now
#   ./scripts/site-backup.sh list               the backups on this server
#   ./scripts/site-backup.sh export             back up now, as one file to copy
#                                               to another computer
#   ./scripts/site-backup.sh export NAME        backup NAME as one file
#   ./scripts/site-backup.sh export [NAME] -    the same file, written to the screen
#                                               (to send it straight to your own
#                                               computer through ssh, see the docs)
#   ./scripts/site-backup.sh restore NAME       put backup NAME back (a name from
#                                               "list", such as 2026-11-26-0300)
#   ./scripts/site-backup.sh restore FILE.tar   put back a backup file (from
#                                               "export", or downloaded from /stats)
#
# restore puts back every part of the backup except your settings (.env),
# unless you name the parts to put back after the name:
#   database      stories, jobs, events, bids, listings, bookings, visitor counts
#   photos        the saved photos
#   settings      your .env file (the one you have now is kept as a copy in backups/)
#   sponsor       the sponsor picture in config/sponsor
#   mail          the mailboxes, accounts, aliases and DKIM key (own mail server)
#   webmail       the webmail's settings and contacts
#   certificates  Caddy's https certificates
# For example: ./scripts/site-backup.sh restore 2026-11-26-0300 database
# On a new server without a .env file yet, the settings come from the backup.
# Add --yes to skip the question before restoring.
#
# More: docs/maintenance.md#backups

# This script needs bash. When it is started with "sh", start it again with bash.
if [ -z "${BASH_VERSION:-}" ]; then
    exec bash "$0" "$@"
fi
set -uo pipefail

cd "$(dirname "$0")/.." || exit 1

ALL_PARTS="database photos settings sponsor mail webmail certificates"

die() {
    echo "$*" >&2
    exit 1
}

usage() {
    sed -n '/^#   \.\/scripts/,/^# More:/p' "$0" | sed 's/^# \{0,1\}//' >&2
    exit 2
}

# One value from .env, read the way the other scripts read it.
env_value() {
    [ -f .env ] || return 0
    sed -n "s/^$1=//p" .env | tail -n 1 | tr -d "\r\"'" | sed 's/[[:space:]]#.*$//; s/[[:space:]]*$//'
}

# "database, photos and certificates"
words() {
    local text=$1
    shift
    while [ $# -gt 1 ]; do
        text="$text, $1"
        shift
    done
    [ $# -eq 1 ] && text="$text and $1"
    echo "$text"
}

mail_on() {
    env_value COMPOSE_PROFILES | tr ',' '\n' | grep -qx '[[:space:]]*mail[[:space:]]*'
}

# run [--no-deps] [-v ...] COMMAND...: run scripts/backup-service.sh inside
# Docker (the "backup" service), with the database started when needed.
run() {
    local options=()
    while [ $# -gt 0 ]; do
        case $1 in
            --no-deps) options+=("$1") ;;
            -v) options+=("$1" "$2") && shift ;;
            *) break ;;
        esac
        shift
    done
    # Files it makes for you (an exported backup) belong to you.
    docker compose run --rm -T -e "HOST_OWNER=$(id -u):$(id -g)" "${options[@]}" backup "$@"
}

check_ready() {
    [ -f .env ] || die "There is no .env file in $PWD. Run this in the GU-headlines folder that runs the site."
    docker compose version >/dev/null 2>&1 || die "Docker Compose does not work here. Is Docker installed and running?"
    docker compose ps >/dev/null 2>&1 ||
        die "\"docker compose ps\" does not work for this user. Run this as the user that starts the site."
}

# 2026-11-26-0300 from 2026-11-26-0300, site-backup-2026-11-26-0300.tar or
# backups/site/2026-11-26-0300/
backup_name() {
    local name=${1%/}
    name=${name##*/}
    name=${name#site-backup-}
    name=${name%.tar}
    [[ $name =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}-[0-9]{4}$ ]] ||
        die "\"$1\" is not the name of a backup (such as 2026-11-26-0300) or a backup file. To see the backups: ./scripts/site-backup.sh list"
    echo "$name"
}

newest() {
    run --no-deps list | awk '/^  [0-9]{4}-/ { print $1; exit }'
}

cmd_export() {
    local name="" to_screen="" file
    for arg in "$@"; do
        if [ "$arg" = - ]; then to_screen=yes; else name=$(backup_name "$arg") || exit 1; fi
    done
    if [ -n "$to_screen" ]; then
        [ -t 1 ] && die "Send it into a file, for example: ./scripts/site-backup.sh export - > site-backup.tar"
        [ -n "$name" ] || name=$(newest)
        [ -n "$name" ] || die "There is no backup yet. Make one: ./scripts/site-backup.sh now"
        run --no-deps tar "$name"
        return
    fi
    if [ -z "$name" ]; then
        run now || exit 1
        name=$(newest)
    fi
    echo "Writing backup $name as one file..."
    file=$(run --no-deps export "$name") || exit 1
    echo
    echo "Done: $file ($(du -h "$file" 2>/dev/null | cut -f1))"
    echo "It holds your passwords (the .env file), so keep it private."
    echo "To copy it to your own computer, run this there (with your server's address):"
    echo "  scp you@your-server:$PWD/$file ."
    echo "It is deleted from the server after 3 days, as it is big. To delete it now:"
    echo "  rm $file"
}

# Make .env from the settings in a backup file (a new server has none yet,
# and Docker Compose needs it before it does anything).
env_from_file() {
    local member
    member=$(tar -tf "$1" 2>/dev/null | sed -n '/^[^/]*\/env$/{p;q;}')
    [ -n "$member" ] || die "There is no .env file here, and the backup has no settings to make one from. Make one first: cp .env.example .env (see docs/getting-started.md)"
    (umask 077 && tar -xOf "$1" "$member" >.env) || die "Could not take the settings out of $1."
    echo "There was no .env file here, so it was made from the settings in the backup."
}

cmd_restore() {
    local source="" yes="" name info when available part wanted=() new_env="" stamp status
    for arg in "$@"; do
        case $arg in
            -y | --yes) yes=yes ;;
            *)
                if [ -z "$source" ]; then
                    source=$arg
                elif [[ " $ALL_PARTS " == *" $arg "* ]]; then
                    wanted+=("$arg")
                else
                    die "\"$arg\" is not a part of a backup. The parts: $ALL_PARTS"
                fi
                ;;
        esac
    done
    [ -n "$source" ] || usage

    if [ -f "$source" ]; then
        if [ ! -f .env ]; then
            env_from_file "$source"
            new_env=yes
        fi
        check_ready
        echo "Reading $source into backups/site (a big backup takes a few minutes)..."
        name=$(run --no-deps import <"$source") || exit 1
    else
        name=$(backup_name "$source") || exit 1
        check_ready
    fi
    info=$(run --no-deps describe "$name") || exit 1
    when=$(sed -n 's/^when=//p' <<<"$info")
    available=$(sed -n 's/^parts=//p' <<<"$info")

    if [ ${#wanted[@]} -eq 0 ]; then
        for part in $available; do
            [ "$part" = settings ] || wanted+=("$part")
        done
    else
        for part in "${wanted[@]}"; do
            [[ " $available " == *" $part "* ]] || die "The backup from $when has no $part. It has: $available"
        done
    fi
    # On a new server, the settings were put in place already (above).
    if [ -n "$new_env" ]; then
        local others=()
        for part in "${wanted[@]}"; do [ "$part" = settings ] || others+=("$part"); done
        wanted=("${others[@]}")
    fi
    [ ${#wanted[@]} -gt 0 ] || die "Nothing to put back."
    if [[ " ${wanted[*]} " == *" settings "* ]] && { [ ! -w .env ] || [ ! -w backups ]; }; then
        die "To put back the settings, run this as the user that owns .env and the backups folder (or as root)."
    fi

    echo
    echo "This puts back the $(words "${wanted[@]}") from the backup of $when (Guam time)."
    echo "What the site has now in these parts is replaced, and anything newer than"
    echo "the backup is lost. The site is offline for a few minutes."
    if [ -z "$yes" ]; then
        [ -t 0 ] || die "Add --yes to restore without being asked."
        read -r -p "Type yes to go on: " answer
        [ "$answer" = yes ] || die "Nothing was changed."
    fi

    local stop=(web worker caddy)
    mail_on && stop+=(mailserver webmail)
    echo "Stopping the site..."
    docker compose stop "${stop[@]}" >/dev/null 2>&1
    run -v "$PWD/config/sponsor:/site/config/sponsor" restore "$name" "${wanted[@]}"
    status=$?
    if [ $status -eq 0 ] && [[ " ${wanted[*]} " == *" settings "* ]]; then
        stamp=$(date +%Y%m%d-%H%M)
        if cp -p .env "backups/env-before-restore-$stamp" &&
            cat "backups/env-from-backup-$name" >.env; then
            rm -f "backups/env-from-backup-$name"
            echo "Your settings are now the ones from the backup. The ones you had: backups/env-before-restore-$stamp"
        else
            echo "Could not put the backup's settings in .env. The database already uses the backup's"
            echo "password, so do it now: cp backups/env-from-backup-$name .env"
        fi
    fi
    echo "Starting the site again..."
    if ! docker compose up -d; then
        echo
        echo "The site did not start again (see the messages above). Try: docker compose up -d"
        [ $status -eq 0 ] && echo "The backup itself was put back."
        exit 1
    fi
    echo
    if [ $status -ne 0 ]; then
        echo "The restore did not finish (see the message above). The parts it had not reached are as they were."
        exit $status
    fi
    echo "Done. The site is back with the $(words "${wanted[@]}") from $when. It can take a minute to answer."
    echo "To check: docker compose ps"
    if [[ " $available " == *" settings "* ]] && [[ " ${wanted[*]} " != *" settings "* ]] && [ -z "$new_env" ]; then
        echo "Your settings (.env) were not changed. To put back the backup's settings too:"
        echo "  ./scripts/site-backup.sh restore $name settings"
    fi
}

case ${1:-} in
    now)
        check_ready
        run now
        ;;
    list)
        check_ready
        run --no-deps list
        ;;
    export)
        check_ready
        shift
        cmd_export "$@"
        ;;
    restore)
        shift
        cmd_restore "$@"
        ;;
    *) usage ;;
esac
