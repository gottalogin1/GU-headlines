#!/usr/bin/env bash
# Checks the server itself, and tells you in Discord when something runs low.
# scripts/auto-update.sh runs it in each of its rounds (every 5 minutes), so
# with automatic updates on (docs/maintenance.md#automatic-updates) there is
# nothing to set up.
#
#   ./scripts/server-check.sh          check now, and show the readings
#   ./scripts/server-check.sh --clean  delete Docker's leftovers now (see 5.)
#
# What it watches. The limits are settings in .env (see .env.example):
#   1. Disk space: an alert when a disk is DISK_ALERT_PERCENT full (90%).
#   2. Memory: an alert when MEMORY_ALERT_PERCENT (90%) of the memory has
#      been in use for about 15 minutes (3 rounds in a row).
#   3. The https certificates of the site (SITE_DOMAIN) and, with your own
#      mail server on, of the mail server (MAIL_HOSTNAME). Caddy renews each
#      one by itself about 30 days before it ends. An alert comes when one
#      ends within CERT_ALERT_DAYS days (14), because then renewing has
#      failed for two weeks. Checked every 6 hours.
#   4. Spam block lists: once a day, with your own mail server on, it asks
#      five big block lists whether they list the server's IP address. Mail
#      from a listed address often goes to spam, or is refused.
#   5. Once a week, on Sunday night (Guam time), it deletes what Docker
#      leaves behind: old versions of the program and old build files. The
#      stories, photos, mail and database are never touched.
# Each problem is posted once, and once more when it is fixed. Alerts go to
# the Discord channel of DISCORD_WEBHOOK_URL in .env. What it does is written
# to backups/auto-update.log (through auto-update.sh).
# More: docs/maintenance.md#server-checks

# This script needs bash. When it is started with "sh" (on Ubuntu a smaller
# shell, dash), start it again with bash.
if [ -z "${BASH_VERSION:-}" ]; then
    exec bash "$0" "$@"
fi
set -uo pipefail

cd "$(dirname "$0")/.." || exit 1
# cron starts programs with a short list of folders to find them in.
export PATH="$PATH:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

# What the script remembers between rounds (small text files).
STATE="backups/.server-check"
mkdir -p "$STATE" || exit 1
# Where Linux shows the memory (another file in the tests).
MEMINFO="${MEMINFO:-/proc/meminfo}"
# The block lists: the biggest ones that mail services use and that answer
# anyone for free.
BLOCK_LISTS="zen.spamhaus.org b.barracudacentral.org bl.spamcop.net psbl.surriel.com bl.mailspike.net"
# Set by --round (auto-update.sh): log only what happened, no readings.
ROUND=""

say() { echo "$(date '+%F %T') $*"; }

# A reading, shown when the script is run by hand.
show() {
    [ -n "$ROUND" ] || echo "$*"
}

# One value from .env, read the way backup.sh reads it. A note after " #" is
# left out, as Docker Compose does.
env_value() {
    [ -f .env ] || return 0
    sed -n "s/^$1=//p" .env | tail -n 1 | tr -d "\r\"'" | sed 's/[[:space:]]#.*$//; s/[[:space:]]*$//'
}

# number SETTING DEFAULT: a whole number from .env, or DEFAULT when the
# setting is missing or not a whole number. 0 turns a check off.
number() {
    local value
    value=$(env_value "$1")
    case $value in '' | *[!0-9]*) echo "$2" ;; *) echo "$((10#$value))" ;; esac
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

# Is your own mail server on? (COMPOSE_PROFILES=mail in .env)
mail_on() {
    env_value COMPOSE_PROFILES | tr ',' '\n' | grep -qx '[[:space:]]*mail[[:space:]]*'
}

mail_host() {
    env_value MAIL_HOSTNAME | sed 's#^https\{0,1\}://##; s#/.*$##'
}

json_escape() {
    printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g; s/\t/\\t/g; s/\r//g' |
        awk 'NR > 1 { printf "\\n" } { printf "%s", $0 }'
}

# send_alert EMOJI TITLE MESSAGE [quiet]
#   Posts "EMOJI **TITLE**" and MESSAGE in the Discord channel. With "quiet",
#   the message shows in the channel without a phone notification. Fails
#   when it could not be posted, so the next round tries again.
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

# A file name for a state file about THING (a disk, a certificate).
key_for() {
    printf '%s' "$1" | tr -c 'A-Za-z0-9' '_'
}

# 5242880 (kilobytes) -> 5.0 (GB)
gb() {
    awk -v k="$1" 'BEGIN { printf "%.1f", k / 1048576 }'
}

# "Fri Dec 5" in Guam time, from a date that "date -d" reads.
guam_day() {
    if [ -e /usr/share/zoneinfo/Pacific/Guam ]; then
        TZ=Pacific/Guam date -d "$1" '+%a %b %-d'
    else
        date -u -d "$1" '+%a %b %-d'
    fi
}

# ------------------------------------------------------------------ 1. disk

# The disks to watch: the one of this folder and the one where Docker keeps
# the database, the photos and the mail (often the same disk).
disk_folders() {
    local root
    echo "$PWD"
    root=$(docker info --format '{{.DockerRootDir}}' 2>/dev/null)
    [ -n "$root" ] && [ -d "$root" ] && echo "$root"
}

# One line for each disk: size and free space (in kilobytes), percent used,
# and where it is mounted.
disk_lines() {
    local folders=()
    mapfile -t folders < <(disk_folders)
    df -P -k "${folders[@]}" 2>/dev/null | awk 'NR > 1 { sub("%", "", $5); print $2, $4, $5, $6 }' | sort -u -k4,4
}

check_disk() {
    local limit size free percent mount key
    limit=$(number DISK_ALERT_PERCENT 90)
    [ "$limit" -gt 0 ] || return 0
    while read -r size free percent mount; do
        [ -n "$mount" ] || continue
        show "Disk $mount: $percent% full, $(gb "$free") GB free of $(gb "$size") GB (alert at $limit%)"
        key="$STATE/disk-alerted-$(key_for "$mount")"
        if [ "$percent" -ge "$limit" ]; then
            [ -f "$key" ] && continue
            send_alert "💾" "$(site_label): disk $percent% full" \
                "The server's disk ($mount) has only $(gb "$free") GB free of $(gb "$size") GB. When it is full, the database can't save new stories, and the site can stop."$'\n'"To see what takes the space, run these on the server: \`docker system df\` and \`du -sh backups\`. To free space: \`./scripts/server-check.sh --clean\` deletes Docker's leftovers, and a lower IMAGE_STORAGE_GB in .env keeps fewer photos. More: docs/maintenance.md#disk-space" &&
                touch "$key"
        elif [ -f "$key" ] && [ "$percent" -le $((limit - 3)) ]; then
            send_alert "✅" "$(site_label): disk space OK again" \
                "The disk ($mount) is $percent% full, with $(gb "$free") GB free." quiet &&
                rm -f "$key"
        fi
    done < <(disk_lines)
}

# ---------------------------------------------------------------- 2. memory

check_memory() {
    local limit total free percent count
    limit=$(number MEMORY_ALERT_PERCENT 90)
    [ "$limit" -gt 0 ] || return 0
    # MemAvailable: what programs can still get (Linux gives back its file cache).
    total=$(awk '/^MemTotal:/ {print $2}' "$MEMINFO" 2>/dev/null)
    free=$(awk '/^MemAvailable:/ {print $2}' "$MEMINFO" 2>/dev/null)
    if [ -z "$total" ] || [ -z "$free" ] || [ "$total" -le 0 ]; then
        return 0
    fi
    percent=$(((total - free) * 100 / total))
    show "Memory: $percent% in use, $((free / 1024)) MB free of $((total / 1024)) MB (alert at $limit%)"
    if [ "$percent" -ge "$limit" ]; then
        count=$(($(cat "$STATE/memory-count" 2>/dev/null || echo 0) + 1))
        echo "$count" >"$STATE/memory-count"
        # A short peak (an update builds the program) is no problem.
        [ "$count" -ge 3 ] || return 0
        [ -f "$STATE/memory-alerted" ] && return 0
        send_alert "🧠" "$(site_label): memory $percent% in use" \
            "Only $((free / 1024)) MB of the server's $((total / 1024)) MB of memory has been free for about 15 minutes. When the memory runs out, Linux stops programs, such as the database or the mail server."$'\n'"To see which part uses the most, run this on the server: \`docker stats --no-stream\` A restart frees memory for a while: \`docker compose restart\` If it keeps happening, the server needs more memory (a bigger plan at your server company)." &&
            touch "$STATE/memory-alerted"
    else
        rm -f "$STATE/memory-count"
        if [ -f "$STATE/memory-alerted" ] && [ "$percent" -le $((limit - 5)) ]; then
            send_alert "✅" "$(site_label): memory OK again" \
                "$percent% of the memory is in use, $((free / 1024)) MB free." quiet &&
                rm -f "$STATE/memory-alerted"
        fi
    fi
}

# ---------------------------------------------------------- 3. certificates

# cert_end PORT NAME: the end date of the certificate that this server shows
# on PORT for NAME, such as "Feb 23 12:00:00 2027 GMT". Empty when none.
cert_end() {
    timeout 20 openssl s_client -connect "127.0.0.1:$1" -servername "$2" </dev/null 2>/dev/null |
        openssl x509 -noout -enddate 2>/dev/null | sed -n 's/^notAfter=//p'
}

# check_cert WHAT PORT NAME LIMIT HELP
check_cert() {
    local what=$1 name=$3 limit=$4 end end_s days key when
    key="$STATE/cert-alerted-$(key_for "$what")"
    end=$(cert_end "$2" "$name")
    if [ -z "$end" ]; then
        # The site or the mail server not answering has alerts of its own.
        show "Certificate of the $what ($name): could not be read"
        return 0
    fi
    end_s=$(date -d "$end" +%s 2>/dev/null) || return 0
    days=$(((end_s - $(date +%s)) / 86400))
    show "Certificate of the $what ($name): ends $(guam_day "$end"), in $days days (alert under $limit days)"
    if [ "$days" -lt "$limit" ]; then
        # One alert for each certificate (a renewed one that ends soon too gets its own).
        [ "$(cat "$key" 2>/dev/null)" = "$end_s" ] && return 0
        when="ends on $(guam_day "$end") (in $days days)"
        [ "$days" -lt 0 ] && when="ended on $(guam_day "$end")"
        send_alert "🔒" "$(site_label): the $what's certificate $when" \
            "The https certificate of $name $when. Caddy renews it by itself about 30 days before the end, so renewing has failed. $5"$'\n'"To see why, run this on the server: \`docker compose logs --tail 50 caddy\` (look for \"error\"). Most often the name no longer points at this server in DNS, or port 80 is closed. \`docker compose restart caddy\` makes Caddy try again now." &&
            echo "$end_s" >"$key"
    elif [ -f "$key" ]; then
        send_alert "✅" "$(site_label): the $what's certificate is renewed" \
            "The certificate of $name now runs until $(guam_day "$end")." quiet &&
            rm -f "$key"
    fi
}

check_certs() {
    local limit name port host
    limit=$(number CERT_ALERT_DAYS 14)
    [ "$limit" -gt 0 ] || return 0
    if [ -n "$ROUND" ] && [ -n "$(find "$STATE/certs-checked" -mmin -360 2>/dev/null)" ]; then
        return 0
    fi
    touch "$STATE/certs-checked"
    if ! command -v openssl >/dev/null; then
        say "the certificates are not checked: the openssl program is missing (sudo apt install openssl)"
        return 0
    fi
    name=$(first_domain)
    port=$(env_value HTTPS_PORT)
    if [ -n "$name" ]; then
        check_cert "site" "${port:-443}" "$name" "$limit" \
            "When it ends, browsers show visitors a warning instead of the site."
    fi
    host=$(mail_host)
    if mail_on && [ -n "$host" ]; then
        check_cert "mail server" 993 "$host" "$limit" \
            "When it ends, mail apps refuse to connect, and some mail servers stop delivering. When Caddy has a new one, \`docker compose restart mailserver\` makes the mail server use it."
    fi
}

# ------------------------------------------------------------ 4. block lists

# The IP address that the server sends mail from: its own address, or the
# address of MAIL_HOSTNAME when its own is a private one (a server behind a
# router).
public_ip() {
    local ip
    ip=$(ip -4 route get 1.1.1.1 2>/dev/null | sed -n 's/.* src \([0-9.]*\).*/\1/p' | head -n 1)
    case $ip in
        '' | 10.* | 127.* | 192.168.* | 172.1[6-9].* | 172.2[0-9].* | 172.3[01].* | 100.6[4-9].* | 100.[7-9][0-9].* | 100.1[01][0-9].* | 100.12[0-7].*)
            ip=$(getent ahostsv4 "$(mail_host)" 2>/dev/null | awk 'NR == 1 {print $1}')
            ;;
    esac
    echo "$ip"
}

removal_page() {
    case $1 in
        zen.spamhaus.org) echo "https://check.spamhaus.org/" ;;
        b.barracudacentral.org) echo "https://www.barracudacentral.org/rbl/removal-request" ;;
        bl.spamcop.net) echo "https://www.spamcop.net/bl.shtml" ;;
        psbl.surriel.com) echo "https://psbl.org/" ;;
        *) echo "https://mxtoolbox.com/blacklists.aspx" ;;
    esac
}

# "a b" minus "b c" -> "a"
minus() {
    local item out=""
    for item in $1; do
        [[ " $2 " == *" $item "* ]] || out="$out $item"
    done
    echo "${out# }"
}

check_block_lists() {
    local ip reversed list answer listed="" unknown="" before kept new gone lines=""
    mail_on && [ -n "$(mail_host)" ] || return 0
    if [ -n "$ROUND" ] && [ -n "$(find "$STATE/lists-checked" -mmin -1440 2>/dev/null)" ]; then
        return 0
    fi
    ip=$(public_ip)
    if [ -z "$ip" ]; then
        say "block lists not checked: could not find the server's IP address"
        touch "$STATE/lists-checked"
        return 0
    fi
    reversed=$(echo "$ip" | awk -F. '{print $4"."$3"."$2"."$1}')
    show "Block lists, for the mail server's address $ip:"
    for list in $BLOCK_LISTS; do
        # A listed address has an answer like 127.0.0.2; one that is not listed has none.
        answer=$(getent ahostsv4 "$reversed.$list" 2>/dev/null | awk 'NR == 1 {print $1}')
        case $answer in
            "") show "  not on $list" ;;
            # 127.255.255.x: the list refused to answer (Spamhaus does that for
            # big shared DNS servers). That says nothing about the address.
            127.255.255.*)
                unknown="$unknown $list"
                show "  $list did not answer this server's DNS"
                ;;
            127.*)
                listed="$listed $list"
                show "  ON $list ($answer)"
                ;;
            *)
                unknown="$unknown $list"
                show "  $list gave an odd answer ($answer)"
                ;;
        esac
    done
    before=$(cat "$STATE/block-listed" 2>/dev/null)
    new=$(minus "$listed" "$before")
    # A list that did not answer keeps what it said before.
    kept=$(minus "$before" "$(minus "$before" "$unknown")")
    gone=$(minus "$(minus "$before" "$listed")" "$kept")
    if [ -n "$new" ]; then
        for list in $new; do
            lines="$lines"$'\n'"• $list: removal at $(removal_page "$list")"
        done
        send_alert "🚫" "$(site_label): the mail server is on a spam block list" \
            "The server's IP address $ip is on:$lines"$'\n'"Mail from a listed address often goes to spam, or is refused. Most often a server that had this address before you sent spam. Each list's page tells how to ask for removal (free). More: docs/email-server.md#block-lists" ||
            return 0
    fi
    if [ -n "$gone" ]; then
        send_alert "✅" "$(site_label): the mail server is off the block list" \
            "The server's IP address $ip is no longer on: ${gone// /, }." quiet || return 0
    fi
    echo "$listed $kept" | xargs >"$STATE/block-listed"
    touch "$STATE/lists-checked"
}

# ---------------------------------------------------------------- 5. cleanup

# clean_docker [now]: once a week, on Sunday between 2 and 5 AM Guam time
# (or now), delete old versions of the program that no container uses and
# build files older than a week. Docker keeps them after each update.
clean_docker() {
    local images cache free hour
    command -v docker >/dev/null || return 0
    if [ "${1:-}" != now ]; then
        [ "$(TZ=Pacific/Guam date +%u)" = 7 ] || return 0
        hour=$(TZ=Pacific/Guam date +%-H)
        [ "$hour" -ge 2 ] && [ "$hour" -lt 5 ] || return 0
        [ -n "$(find "$STATE/docker-cleaned" -mmin -$((6 * 1440)) 2>/dev/null)" ] && return 0
    fi
    touch "$STATE/docker-cleaned"
    # Only images without a name: old versions of this program and of the
    # parts it downloads. Images that have a name, also those of other
    # programs on the server, are kept.
    images=$(docker image prune -f 2>&1 | sed -n 's/^Total reclaimed space:[[:space:]]*//p' | tail -n 1)
    cache=$(docker builder prune -f --filter until=168h 2>&1 | sed -n 's/^Total:[[:space:]]*//p' | tail -n 1)
    images=${images:-0B}
    cache=${cache:-0B}
    free=$(df -P -k "$PWD" 2>/dev/null | awk 'NR == 2 {print $4}')
    say "cleanup: deleted old program versions ($images) and old build files ($cache)"
    show "Deleted old versions of the program ($images) and old build files ($cache)."
    if [ "$images" != 0B ] || [ "$cache" != 0B ]; then
        send_alert "🧹" "$(site_label): weekly cleanup" \
            "Deleted old versions of the program ($images) and old build files ($cache).${free:+ The disk now has $(gb "$free") GB free.}" \
            quiet
    fi
    return 0
}

main() {
    case "${1:-}" in
        --round) ROUND=1 ;;
        --clean)
            clean_docker now
            return
            ;;
        "") ;;
        *)
            echo "unknown option: $1 (see the notes at the top of $0)"
            return 2
            ;;
    esac
    # One round at a time.
    exec 8>"$STATE/lock"
    flock -n 8 || return 0
    check_disk
    check_memory
    check_certs
    check_block_lists
    clean_docker
    return 0
}

main "$@"
exit $?
