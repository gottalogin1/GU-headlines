#!/usr/bin/env bash
# Keeps a GU Headlines server up to date, and tells you when the site is in
# trouble. cron (the scheduler built into Linux) runs it every 5 minutes. To
# set that up, run this once in the GU-headlines folder on the server:
#
#   ./scripts/auto-update.sh --install
#
# Other ways to run it:
#   ./scripts/auto-update.sh               one round now, and say what it did
#   ./scripts/auto-update.sh --test-alert  post a test alert in your Discord channel
#   ./scripts/auto-update.sh --uninstall   stop the rounds every 5 minutes
#
# Each round:
#   1. Update. When the main branch on GitHub has new commits (for example
#      after a pull request is merged), it downloads them (git), backs up the
#      database (scripts/backup.sh), then rebuilds and restarts the site
#      (docker compose up -d --build). If the site does not answer within 5
#      minutes, it goes back to the version before and sends an alert. A
#      version that failed is not tried again; the next new commit is.
#   2. Once a week, when your own mail server is on (docs/email-server.md),
#      it downloads new versions of the mail server and the webmail, which
#      bring security fixes, and restarts them.
#   3. Watch. If the site does not answer in two rounds in a row (about 10
#      minutes), it sends an alert, and another when the site answers again.
#      The same when the news has not been checked for 3 hours.
#   4. Server checks (scripts/server-check.sh): disk space, memory, the https
#      certificates and, with your own mail server on, the spam block lists;
#      and once a week it deletes Docker's leftovers.
#
# Alerts are posted to a Discord channel through DISCORD_WEBHOOK_URL in .env
# (see .env.example). Without it, the script still updates and watches, and
# writes what happened to its log, backups/auto-update.log.
# More: docs/maintenance.md#automatic-updates

# This script needs bash. When it is started with "sh" (on Ubuntu a smaller
# shell, dash), start it again with bash.
if [ -z "${BASH_VERSION:-}" ]; then
    exec bash "$0" "$@"
fi
set -uo pipefail

cd "$(dirname "$0")/.." || exit 1
# cron starts programs with a short list of folders to find them in.
export PATH="$PATH:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

BRANCH="main"
LOG="backups/auto-update.log"
# What the script remembers between rounds (small text files).
STATE="backups/.auto-update"
mkdir -p "$STATE" || exit 1

say() { echo "$(date '+%F %T') $*"; }

# One value from .env, read the way backup.sh reads it. Loading .env as a
# script would run values with spaces, like SITE_NAME=GU Headlines, as
# commands. A note after " #" is left out, as Docker Compose does.
env_value() {
    [ -f .env ] || return 0
    sed -n "s/^$1=//p" .env | tail -n 1 | tr -d "\r\"'" | sed 's/[[:space:]]#.*$//; s/[[:space:]]*$//'
}

# guamheadlines.com from SITE_DOMAIN=guamheadlines.com, www.guamheadlines.com
first_domain() {
    env_value SITE_DOMAIN | tr ',' ' ' | awk '{print $1}' | sed 's#^https\{0,1\}://##; s#/.*$##'
}

# The name used in alert titles: the domain, else SITE_NAME.
site_label() {
    local name
    name=$(first_domain)
    [ -n "$name" ] || name=$(env_value SITE_NAME)
    echo "${name:-GU Headlines}"
}

guam_time() {
    if [ -e /usr/share/zoneinfo/Pacific/Guam ]; then
        TZ=Pacific/Guam date -d "$1" '+%a %b %-d, %-I:%M %p (Guam time)'
    else
        date -u -d "$1" '+%a %b %-d, %H:%M UTC'
    fi
}

json_escape() {
    printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g; s/\t/\\t/g; s/\r//g' |
        awk 'NR > 1 { printf "\\n" } { printf "%s", $0 }'
}

# send_alert EMOJI TITLE MESSAGE [quiet]
#   Posts "EMOJI **TITLE**" and MESSAGE in the Discord channel. With "quiet",
#   the message shows in the channel without a phone notification.
send_alert() {
    local url flags=4 body # 4: no preview cards for links
    url=$(env_value DISCORD_WEBHOOK_URL)
    if [ -z "$url" ]; then
        say "alert not sent (DISCORD_WEBHOOK_URL is empty in .env): $2"
        return 0
    fi
    if ! [[ $url =~ ^https://([a-z]+\.)?discord(app)?\.com/api/(v[0-9]+/)?webhooks/[0-9]+/[A-Za-z0-9._-]+/?$ ]]; then
        say "DISCORD_WEBHOOK_URL is not a Discord webhook link like https://discord.com/api/webhooks/...; alert not sent: $2"
        return 1
    fi
    [ "${4:-}" = quiet ] && flags=$((flags | 4096))
    # allowed_mentions: never notify anyone because of text like "@everyone".
    body=$(printf '{"content":"%s","allowed_mentions":{"parse":[]},"flags":%d}' \
        "$(json_escape "$1 **$2**"$'\n'"$3")" "$flags")
    if curl -fsS --max-time 20 -o /dev/null -H "Content-Type: application/json" \
        --data-binary "$body" "$url" 2>"$STATE/send.err"; then
        say "alert sent: $2"
        return 0
    fi
    say "could not send the alert to Discord: $2 ($(tail -n 1 "$STATE/send.err"))"
    return 1
}

# Prints the site's /healthz answer. It asks through Caddy, as a visitor
# would, but without leaving the server. Fails if the site does not answer
# "ok".
healthz() {
    local name port answer
    name=$(first_domain)
    if [ -n "$name" ]; then
        port=$(env_value HTTPS_PORT)
        port=${port:-443}
        answer=$(curl -fsS --max-time 15 --resolve "$name:$port:127.0.0.1" \
            "https://$name:$port/healthz") || return 1
    else
        port=$(env_value HTTP_PORT)
        port=${port:-80}
        answer=$(curl -fsS --max-time 15 "http://127.0.0.1:$port/healthz") || return 1
    fi
    case $answer in
        *'"status":"ok"'*) printf '%s\n' "$answer" ;;
        *) echo "the site answered: $answer" >&2; return 1 ;;
    esac
}

# How long a new version gets to start, in seconds.
START_TIMEOUT="${START_TIMEOUT:-300}"

wait_until_up() {
    local waited=0
    until healthz >/dev/null 2>&1; do
        [ "$waited" -ge "$START_TIMEOUT" ] && return 1
        sleep 5
        waited=$((waited + 5))
    done
}

# switch_to FROM TO: rebuild and restart the site after the files changed
# from commit FROM to commit TO.
switch_to() {
    if ! docker compose up -d --build >"$STATE/compose.out" 2>&1; then
        say "docker compose up failed:"
        tail -n 25 "$STATE/compose.out"
        return 1
    fi
    # Docker does not notice a changed Caddyfile; Caddy reads it when it starts.
    if ! git diff --quiet "$1" "$2" -- caddy/; then
        docker compose restart caddy >/dev/null 2>&1
    fi
    # The same for the mail server's files in mail/ (when the mail server is on).
    if mail_on && ! git diff --quiet "$1" "$2" -- mail/; then
        docker compose restart mailserver webmail >/dev/null 2>&1
    fi
    wait_until_up
}

# Is your own mail server on? (COMPOSE_PROFILES=mail in .env)
mail_on() {
    env_value COMPOSE_PROFILES | tr ',' '\n' | grep -qx '[[:space:]]*mail[[:space:]]*'
}

# Once a week, download the newest versions of the mail server and the
# webmail (security fixes) and restart them when there is one. Mail that
# arrives during the few seconds of the restart waits at the sender and comes
# a little later.
update_mail() {
    mail_on || return 0
    [ -n "$(find "$STATE/mail-updated" -mtime -7 2>/dev/null)" ] && return 0
    if docker compose pull --quiet mailserver webmail >"$STATE/mail-update.out" 2>&1 &&
        docker compose up -d mailserver webmail >>"$STATE/mail-update.out" 2>&1; then
        say "checked the mail server and webmail for new versions"
        touch "$STATE/mail-updated"
        docker image prune -f >/dev/null 2>&1
    else
        say "could not update the mail server: $(tail -n 1 "$STATE/mail-update.out")"
        # Try again in a day, not every 5 minutes.
        touch -d '6 days ago' "$STATE/mail-updated"
    fi
}

# blocked COMMIT MESSAGE: the update to COMMIT can't be done. Alert once for
# each new commit; the update is tried again every round.
blocked() {
    [ "$(cat "$STATE/blocked" 2>/dev/null)" = "$1" ] && return 0
    say "can't update: $2"
    send_alert "⚠️" "Automatic update stopped" "$2" && echo "$1" >"$STATE/blocked"
}

update() {
    local old new branch subject changes note=""
    if ! git fetch --quiet origin "$BRANCH" 2>"$STATE/fetch.err"; then
        say "could not download from GitHub: $(tail -n 1 "$STATE/fetch.err")"
        return 0
    fi
    old=$(git rev-parse HEAD) || return 0
    new=$(git rev-parse FETCH_HEAD) || return 0
    [ "$old" = "$new" ] && return 0
    git merge-base --is-ancestor "$new" "$old" && return 0 # nothing newer
    # This version failed already: wait for the next one.
    [ "$(cat "$STATE/failed" 2>/dev/null)" = "$new" ] && return 0

    branch=$(git symbolic-ref --short -q HEAD)
    if [ "$branch" != "$BRANCH" ]; then
        blocked "$new" "The folder on the server is not on the $BRANCH branch (it is on ${branch:-a single commit}), so it is not updated automatically. To fix it, run this in the folder: \`git checkout $BRANCH\`"
        return 0
    fi
    if ! git merge-base --is-ancestor "$old" "$new"; then
        blocked "$new" "The folder on the server has commits of its own that the $BRANCH branch does not have, so it is not updated automatically. Run \`git status\` in the folder to see them."
        return 0
    fi
    # Download first: if files changed on the server are in the way, nothing
    # else happens, and the next round tries again.
    if ! git merge --ff-only --quiet "$new" >"$STATE/merge.err" 2>&1; then
        blocked "$new" "Files changed on the server are in the way of the update. Git said:"$'\n'"$(head -c 400 "$STATE/merge.err")"$'\n'"See \"If git pull says your local changes would be overwritten\" in docs/maintenance.md. The update is tried again every 5 minutes."
        return 0
    fi
    rm -f "$STATE/blocked"
    subject=$(git log -1 --format=%s "$new")
    changes=$(git log --format='• %s' "$old..$new" | head -n 10)
    say "updating ${old:0:7} -> ${new:0:7}: $subject"
    if ! ./scripts/backup.sh; then
        note=$'\n\nThe database backup before the update failed.'
    fi

    if switch_to "$old" "$new"; then
        say "updated to ${new:0:7}"
        send_alert "🚀" "$(site_label) updated" "Now running:"$'\n'"$changes$note" quiet
        # Old versions of the program take up disk space; stories and photos
        # are never touched by this.
        docker image prune -f >/dev/null 2>&1
        return 0
    fi

    say "the new version did not start in time; going back to ${old:0:7}"
    echo "$new" >"$STATE/failed"
    docker compose logs --tail 60 web worker >"$STATE/failed-update.log" 2>&1
    if git reset --quiet --keep "$old" && switch_to "$new" "$old"; then
        send_alert "⚠️" "Update failed; $(site_label) runs the version before" \
            "The update to \"$subject\" did not start, so the server went back to the version before. The site works. This version is not tried again; the next update is. The site's messages are in backups/.auto-update/failed-update.log on the server.$note"
    else
        send_alert "🚨" "Update failed; $(site_label) is down" \
            "The update to \"$subject\" did not start, and going back to the version before did not help. A database backup from just before the update is in the backups folder. To see what is wrong, run these on the server: \`docker compose ps\` and \`docker compose logs --tail 50 web\`$note" &&
            touch "$STATE/down-alerted"
    fi
}

check_news() {
    local last last_s interval limit age
    last=$(printf '%s' "$1" | sed -n 's/.*"last_scrape":"\([^"]*\)".*/\1/p')
    [ -n "$last" ] || return 0 # a new site: nothing checked yet
    last_s=$(date -d "$last" +%s 2>/dev/null) || return 0
    interval=$(env_value SCRAPE_INTERVAL_MINUTES)
    case $interval in '' | *[!0-9]*) interval=60 ;; esac
    limit=$((interval * 3 > 180 ? interval * 3 : 180)) # minutes
    age=$((($(date +%s) - last_s) / 60))
    if [ "$age" -ge "$limit" ]; then
        [ -f "$STATE/stale-alerted" ] && return 0
        say "the news was last checked $age minutes ago"
        send_alert "⚠️" "$(site_label): news not checked for $((age / 60)) hours" \
            "The news sites were last checked $(guam_time "$last"). The worker may have stopped. To see what it says, run this on the server: \`docker compose logs --tail 50 worker\` To restart it: \`docker compose restart worker\`" &&
            touch "$STATE/stale-alerted"
    elif [ -f "$STATE/stale-alerted" ]; then
        send_alert "✅" "$(site_label): news checked again" \
            "The news sites were checked again $(guam_time "$last")." &&
            rm -f "$STATE/stale-alerted"
    fi
}

watch() {
    local answer count
    if answer=$(healthz 2>"$STATE/healthz.err"); then
        rm -f "$STATE/down-count"
        if [ -f "$STATE/down-alerted" ]; then
            send_alert "✅" "$(site_label) is back" "The site answers again." &&
                rm -f "$STATE/down-alerted"
        fi
        check_news "$answer"
        return 0
    fi
    count=$(($(cat "$STATE/down-count" 2>/dev/null || echo 0) + 1))
    echo "$count" >"$STATE/down-count"
    say "the site did not answer: $(head -c 300 "$STATE/healthz.err")"
    if [ "$count" -ge 2 ] && [ ! -f "$STATE/down-alerted" ]; then
        send_alert "🚨" "$(site_label) is not answering" \
            "The site has not answered for about 10 minutes. The error: $(head -c 300 "$STATE/healthz.err")"$'\n'"To see what is wrong, run these on the server: \`docker compose ps\` and \`docker compose logs --tail 50 web caddy\`" &&
            touch "$STATE/down-alerted"
    fi
}

install_cron() {
    local line
    if ! command -v crontab >/dev/null; then
        echo "cron is not installed. Install it with: sudo apt install cron   then run this again."
        return 1
    fi
    if [ ! -f .env ]; then
        echo "There is no .env file in $PWD. Run this in the folder that runs the site."
        return 1
    fi
    if ! docker compose ps >/dev/null 2>&1; then
        echo "\"docker compose ps\" does not work for this user. Run this as the user that starts the site."
        return 1
    fi
    # "bash" in front: it runs even if the file lost its permission to run.
    line="*/5 * * * * cd \"$PWD\" && bash scripts/auto-update.sh >> $LOG 2>&1"
    {
        crontab -l 2>/dev/null | grep -v 'scripts/auto-update.sh'
        echo "$line"
    } | crontab - || return 1
    echo "Done. Every 5 minutes, the server now updates itself when the $BRANCH branch has"
    echo "new commits, and checks that the site answers. What it does is written to:"
    echo "  $PWD/$LOG"
    if [ -n "$(env_value DISCORD_WEBHOOK_URL)" ]; then
        send_alert "✅" "$(site_label): test alert" \
            "Alerts from the server work. Automatic updates are on." >/dev/null &&
            echo "A test alert was posted in your Discord channel. It shows within a few seconds."
    else
        echo "DISCORD_WEBHOOK_URL is empty in .env, so no alerts are sent. See .env.example."
    fi
}

uninstall_cron() {
    { crontab -l 2>/dev/null | grep -v 'scripts/auto-update.sh' || true; } | crontab - || return 1
    echo "Done. The server no longer updates itself or watches the site."
}

main() {
    case "${1:-}" in
        --install) install_cron; return ;;
        --uninstall) uninstall_cron; return ;;
        --test-alert)
            send_alert "✅" "$(site_label): test alert" "Alerts from the server work."
            return
            ;;
        "") ;;
        *)
            echo "unknown option: $1 (see the notes at the top of $0)"
            return 2
            ;;
    esac

    # One round at a time: a rebuild can take longer than 5 minutes.
    exec 9>"$STATE/lock"
    flock -n 9 || return 0

    update
    update_mail
    watch
    # Its own file, so it can also be run by hand. "bash": see install_cron.
    bash scripts/server-check.sh --round
    if [ -t 1 ]; then # run by hand: say where things stand
        echo "On commit $(git log -1 --format='%h: %s')."
        if healthz >/dev/null 2>&1; then echo "The site answers."; else echo "The site does not answer."; fi
    fi
}

# On one line, so bash has read all of it before an update replaces this file.
main "$@"; exit $?
