#!/usr/bin/env bash
# Looks after your own mail server: the "mailserver" (docker-mailserver) and
# "webmail" (Roundcube) parts of docker-compose.yml. Run it in the
# GU-headlines folder on the server. The whole story, step by step, is in
# docs/email-server.md.
#
#   ./scripts/mail.sh check                  is the server ready for mail? (changes nothing)
#   ./scripts/mail.sh setup                  first start: your mailbox, the site's mailbox,
#                                            the DKIM key, then the DNS records to add
#   ./scripts/mail.sh dns                    the DNS records to add, and which are in place
#   ./scripts/mail.sh add NAME@DOMAIN        make a mailbox (asks for its password)
#   ./scripts/mail.sh password NAME@DOMAIN   give a mailbox a new password
#   ./scripts/mail.sh remove NAME@DOMAIN     delete a mailbox and all its mail
#   ./scripts/mail.sh alias FROM@DOMAIN TO   send mail for FROM@DOMAIN on to TO
#   ./scripts/mail.sh unalias FROM@DOMAIN TO stop that
#   ./scripts/mail.sh list                   the mailboxes and aliases
#   ./scripts/mail.sh test YOU@EXAMPLE.COM   send a test email through the mail server
#   ./scripts/mail.sh unban 203.0.113.5      unblock an address that Fail2Ban blocked
#   ./scripts/mail.sh update                 download and start new versions of the mail
#                                            server and webmail (auto-update.sh does this weekly)
#   ./scripts/mail.sh logs                   the mail server's latest messages

# This script needs bash. When it is started with "sh", start it again with bash.
if [ -z "${BASH_VERSION:-}" ]; then
    exec bash "$0" "$@"
fi
set -uo pipefail

cd "$(dirname "$0")/.." || exit 1

# The path where Caddy keeps the certificate (inside its container), and the
# same in the mail server (docker-compose.yml mounts Caddy's storage there).
CERT_DIR="/data/caddy/certificates/acme-v02.api.letsencrypt.org-directory"

ok() { echo "  OK    $*"; }
bad() { echo "  FIX   $*"; }
maybe() { echo "  ??    $*"; }
die() {
    echo "$*" >&2
    exit 1
}

# One value from .env, read the way backup.sh and auto-update.sh read it.
env_value() {
    [ -f .env ] || return 0
    sed -n "s/^$1=//p" .env | tail -n 1 | tr -d "\r\"'" | sed 's/[[:space:]]#.*$//; s/[[:space:]]*$//'
}

# mail.guamheadlines.com, from MAIL_HOSTNAME
mail_host() {
    env_value MAIL_HOSTNAME | tr '[:upper:]' '[:lower:]' | sed 's#^https\{0,1\}://##; s#/.*$##'
}

# guamheadlines.com: the mail server's name without its first part. The
# addresses are name@guamheadlines.com.
mail_domain() {
    local host
    host=$(mail_host)
    echo "${host#*.}"
}

profile_on() {
    env_value COMPOSE_PROFILES | tr ',' '\n' | grep -qx '[[:space:]]*mail[[:space:]]*'
}

running() {
    [ -n "$(docker compose ps -q --status running mailserver 2>/dev/null)" ]
}

# The setup command of docker-mailserver: in the running mail server, or in a
# short-lived copy of it when it is not running.
# It gets no keyboard input (passwords go in as arguments), except for
# "dms_ask", which may ask a question.
dms() {
    if running; then
        docker compose exec -T mailserver setup "$@" </dev/null
    else
        docker compose "${QUIET[@]}" run --rm --no-deps -T mailserver setup "$@" </dev/null
    fi
}

dms_ask() {
    local tty=()
    [ -t 0 ] || tty=(-T)
    if running; then
        docker compose exec "${tty[@]}" mailserver setup "$@"
    else
        docker compose "${QUIET[@]}" run --rm --no-deps "${tty[@]}" mailserver setup "$@"
    fi
}

# Without "Container ... Creating" lines, where Docker Compose knows how.
QUIET=()
docker compose --progress quiet version >/dev/null 2>&1 && QUIET=(--progress quiet)

# The server's own IPv4 address: the one it sends from.
server_ip() {
    ip -4 route get 1.1.1.1 2>/dev/null | sed -n 's/.* src \([0-9.]*\).*/\1/p' | head -n 1
}

has_dig() { command -v dig >/dev/null; }

# lookup TYPE NAME: the answers, one per line, without quotes.
lookup() {
    dig +short "$1" "$2" 2>/dev/null | tr -d '"'
}

require_env() {
    [ -f .env ] || die "There is no .env file here. Run this in the GU-headlines folder on the server."
    local host
    host=$(mail_host)
    if [ -z "$host" ]; then
        die "MAIL_HOSTNAME is empty in .env. Set it, for example MAIL_HOSTNAME=mail.guamheadlines.com (see docs/email-server.md)."
    fi
    case $host in
        *.*.*) ;;
        *) die "MAIL_HOSTNAME=$host needs three parts, like mail.guamheadlines.com." ;;
    esac
    profile_on || die "COMPOSE_PROFILES=mail is missing in .env, so Docker does not start the mail server. Add that line (see docs/email-server.md)."
}

# Which program uses PORT on this computer (empty when none).
port_user() {
    ss -ltnpH "( sport = :$1 )" 2>/dev/null | head -n 1
}

check() {
    local host domain ip seen ptr reply listed mem
    host=$(mail_host)
    domain=$(mail_domain)
    ip=$(server_ip)
    echo "Checking the mail server setup for ${host:-<MAIL_HOSTNAME is empty>}"
    echo

    echo "Settings in .env"
    if [ -f .env ]; then ok ".env is here"; else bad "there is no .env file here: run this in the GU-headlines folder"; fi
    if profile_on; then ok "COMPOSE_PROFILES=mail"; else bad "add the line COMPOSE_PROFILES=mail to .env"; fi
    if [ -n "$host" ]; then ok "MAIL_HOSTNAME=$host (addresses are name@$domain)"; else bad "set MAIL_HOSTNAME, for example mail.guamheadlines.com"; fi
    echo

    echo "This server"
    if [ -n "$ip" ]; then ok "its address is $ip"; else maybe "could not find its address (the ip command did not answer)"; fi
    for port in 25 465 587 993; do
        seen=$(port_user "$port")
        if running; then
            ok "port $port: the mail server answers there"
        elif [ -z "$seen" ]; then
            ok "port $port is free for the mail server"
        else
            bad "port $port is used by another program: $seen"
            echo "        A mail program that came with Linux, most likely. To stop it for good:"
            echo "          sudo systemctl disable --now postfix exim4 sendmail 2>/dev/null"
        fi
    done
    if timeout 10 bash -c 'exec 3<>/dev/tcp/gmail-smtp-in.l.google.com/25' 2>/dev/null; then
        ok "it can reach other mail servers on port 25 (Gmail answered)"
    else
        bad "it cannot reach other mail servers on port 25. Your server company blocks it:"
        echo "        ask them to open outgoing port 25 (docs/email-server.md, step 1)."
    fi
    mem=$(free -m 2>/dev/null | awk '/^Mem:/ {print $7}')
    if [ -n "$mem" ] && ! running && [ "$mem" -lt 600 ]; then
        bad "only $mem MB of memory is free. The mail server and webmail use about 350 MB; leave 600 MB free."
    elif [ -n "$mem" ]; then
        ok "$mem MB of memory is free"
    fi
    echo

    echo "DNS"
    if [ -n "$host" ]; then
        seen=$(getent ahostsv4 "$host" 2>/dev/null | awk '{print $1}' | sort -u | tr '\n' ' ')
        if [ -z "$seen" ]; then
            bad "$host does not point anywhere yet: add its A record (./scripts/mail.sh dns)"
        elif [ -n "$ip" ] && ! printf ' %s' "$seen" | grep -q " $ip "; then
            bad "$host points at $seen instead of this server ($ip)"
        else
            ok "$host points at this server"
        fi
    fi
    if ! has_dig; then
        maybe "the other DNS checks need the dig program: sudo apt install dnsutils"
    elif [ -n "$ip" ]; then
        ptr=$(dig +short -x "$ip" 2>/dev/null | sed 's/\.$//' | head -n 1)
        if [ "$ptr" = "$host" ]; then
            ok "reverse DNS: $ip points back at $host"
        else
            bad "reverse DNS: $ip points back at ${ptr:-nothing}, not $host."
            echo "        Ask your server company to set it (docs/email-server.md, step 4)."
        fi
        reply=$(echo "$ip" | awk -F. '{print $4"."$3"."$2"."$1}')
        for list in zen.spamhaus.org b.barracudacentral.org; do
            listed=$(dig +short "$reply.$list" A 2>/dev/null | head -n 1)
            case $listed in
                "") ok "$ip is not on the $list block list" ;;
                127.255.255.*) maybe "$list did not answer this server's DNS; check on https://mxtoolbox.com/blacklists.aspx" ;;
                127.*) bad "$ip is on the $list block list ($listed). See docs/email-server.md#block-lists" ;;
                *) maybe "$list gave an odd answer ($listed)" ;;
            esac
        done
    fi
    echo

    echo "Mail server"
    if running; then
        ok "it is running"
        if docker compose exec -T caddy test -f "$CERT_DIR/$host/$host.crt" 2>/dev/null; then
            ok "Caddy has its certificate"
        else
            bad "Caddy has no certificate for $host yet. See: docker compose logs --tail 30 caddy"
        fi
    elif profile_on; then
        maybe "it is not running. Start it with ./scripts/mail.sh setup (first time) or docker compose up -d"
    else
        maybe "it is off (no COMPOSE_PROFILES=mail in .env)"
    fi
}

# The DKIM public key for the DNS record, from the mail server.
dkim_record() {
    local domain=$1
    docker compose exec -T mailserver sh -c "cat /tmp/docker-mailserver/rspamd/dkim/$domain-mail.public.dns.txt 2>/dev/null" | tr -d '\r'
}

make_dkim() {
    local domain=$1
    if [ -n "$(dkim_record "$domain")" ]; then
        echo "The DKIM key for $domain is already there."
        return 0
    fi
    echo "Making the DKIM key for $domain..."
    docker compose exec -T mailserver setup config dkim domain "$domain" >/dev/null 2>&1 || return 1
    [ -n "$(dkim_record "$domain")" ]
}

# row TYPE NAME VALUE STATUS
row() {
    printf '  %-5s %-20s %s\n' "$1" "$2" "$3"
    [ -n "${4:-}" ] && printf '  %-5s %-20s %s\n' "" "" "$4"
    echo
}

# in_dns TYPE NAME WANTED: prints whether the record is in place.
in_dns() {
    local found
    has_dig || return 0
    found=$(lookup "$1" "$2" | tr -d ' ' | sed 's/\.$//')
    if printf '%s\n' "$found" | grep -qiF -- "$(printf '%s' "$3" | tr -d ' ')"; then
        echo "(in place)"
    elif [ -n "$found" ]; then
        echo "(different now: $(printf '%s' "$found" | head -n 1 | cut -c1-60))"
    else
        echo "(missing)"
    fi
}

dns() {
    require_env
    local host domain ip label dkim spf dmarc
    host=$(mail_host)
    domain=$(mail_domain)
    ip=$(server_ip)
    label=${host%".$domain"}
    spf="v=spf1 mx ~all"
    dmarc="v=DMARC1; p=none; rua=mailto:postmaster@$domain"
    if running; then
        dkim=$(dkim_record "$domain")
    fi

    echo "Add these records where you manage the DNS of $domain (your domain"
    echo "registrar, or Cloudflare). \"@\" means $domain itself. Keep the records"
    echo "that are there already for the website."
    has_dig || echo "(Install dig to see which ones are in place: sudo apt install dnsutils)"
    echo
    row A "$label" "${ip:-<the IP address of this server>}" "$(in_dns A "$host" "$ip")"
    row MX "@" "$host   (priority 10)" "$(in_dns MX "$domain" "$host")"
    row TXT "@" "$spf" "$(in_dns TXT "$domain" "v=spf1 mx")"
    row TXT "$label" "v=spf1 a ~all" "$(in_dns TXT "$host" "v=spf1 a")"
    if [ -n "${dkim:-}" ]; then
        row TXT "mail._domainkey" "$dkim" "$(in_dns TXT "mail._domainkey.$domain" "${dkim:0:40}")"
    else
        row TXT "mail._domainkey" "<the DKIM key: start the mail server, then run ./scripts/mail.sh dns again>"
    fi
    row TXT "_dmarc" "$dmarc" "$(in_dns TXT "_dmarc.$domain" "v=DMARC1")"
    echo "  Remove any other MX record of $domain: mail for your addresses must"
    echo "  come only here. Do not add an AAAA record for $label."
    echo "  On Cloudflare, set \"Proxy status\" to \"DNS only\" (grey cloud) for $label."
    echo
    echo "Reverse DNS (set by your server company, not at the registrar):"
    echo "  $ip  ->  $host"
    if has_dig && [ -n "$ip" ]; then
        local ptr
        ptr=$(dig +short -x "$ip" 2>/dev/null | sed 's/\.$//' | head -n 1)
        if [ "$ptr" = "$host" ]; then echo "  (in place)"; else echo "  (now: ${ptr:-nothing}; see docs/email-server.md, step 4)"; fi
    fi
}

ask_password() {
    local first second
    while :; do
        read -r -s -p "Password for $1 (at least 12 characters): " first || die $'\nNo password given.'
        echo
        if [ "${#first}" -lt 12 ]; then
            echo "That is shorter than 12 characters. Try again."
            continue
        fi
        read -r -s -p "The same password again: " second || die $'\nNo password given.'
        echo
        [ "$first" = "$second" ] && break
        echo "The two passwords are not the same. Try again."
    done
    PASSWORD=$first
}

valid_address() {
    [[ $1 =~ ^[A-Za-z0-9._+-]+@[A-Za-z0-9.-]+\.[A-Za-z]+$ ]]
}

has_account() {
    dms email list 2>/dev/null | grep -qi -- "\* $1 "
}

add() {
    require_env
    local address=${1:-}
    valid_address "$address" || die "Write the whole address, for example: ./scripts/mail.sh add sanjay@$(mail_domain)"
    if has_account "$address"; then
        echo "$address is already a mailbox. To change its password: ./scripts/mail.sh password $address"
        return 0
    fi
    ask_password "$address"
    dms email add "$address" "$PASSWORD" && echo "Made the mailbox $address."
}

setup() {
    require_env
    local host domain me site_box site_password siteName waited out
    host=$(mail_host)
    domain=$(mail_domain)

    if [ -z "$(getent ahostsv4 "$host" 2>/dev/null)" ]; then
        echo "$host does not point at this server yet. Add this DNS record first, then run"
        echo "this again (it can take a few minutes to an hour to work):"
        echo "  A   ${host%".$domain"}   $(server_ip)"
        exit 1
    fi
    for port in 25 465 587 993; do
        if [ -n "$(port_user "$port")" ] && ! running; then
            die "Port $port is used by another program. Run ./scripts/mail.sh check to see which, and how to stop it."
        fi
    done

    echo "1. Your own mailbox: the address you read in the webmail. Postmaster and"
    echo "   abuse mail for $domain comes there too."
    while :; do
        read -r -p "   Your address (for example yourname@$domain): " me || die $'\nNo address given.'
        valid_address "$me" && [[ ${me,,} == *@"$domain" ]] && break
        echo "   Write an address that ends in @$domain."
    done
    if has_account "$me"; then
        echo "   $me is already there."
    else
        ask_password "$me"
        dms email add "$me" "$PASSWORD" >/dev/null || die "Could not make $me."
        echo "   Made $me."
    fi
    for alias in postmaster abuse; do
        if ! dms alias list 2>/dev/null | grep -qi -- "\* $alias@$domain "; then
            dms alias add "$alias@$domain" "$me" >/dev/null && echo "   Mail to $alias@$domain goes to $me."
        fi
    done
    echo

    site_box="alerts@$domain"
    echo "2. The site's own mailbox, $site_box: the site signs in with it to send"
    echo "   email (bid alerts). Replies and bounced emails land there; you can read"
    echo "   them in the webmail."
    if has_account "$site_box"; then
        echo "   $site_box is already there. Its password is SMTP_PASSWORD in .env."
    else
        site_password=$(tr -dc 'A-Za-z0-9' </dev/urandom | head -c 32)
        dms email add "$site_box" "$site_password" >/dev/null || die "Could not make $site_box."
        siteName=$(env_value SITE_NAME)
        echo "   Made it. Put these lines in .env (replace the empty SMTP_ and MAIL_FROM"
        echo "   lines if .env has them), then run: docker compose up -d"
        echo
        echo "     SMTP_HOST=$host"
        echo "     SMTP_PORT=587"
        echo "     SMTP_USER=$site_box"
        echo "     SMTP_PASSWORD=$site_password"
        echo "     MAIL_FROM=${siteName:-GU Headlines} <$site_box>"
        echo
        echo "   This password is shown only now. If you lose it, give the mailbox a new"
        echo "   one with: ./scripts/mail.sh password $site_box"
    fi
    echo

    echo "3. Starting the mail server and the webmail..."
    if ! out=$(docker compose up -d 2>&1); then
        printf '%s\n' "$out" | tail -n 15
        die "docker compose up did not work (see above)."
    fi
    waited=0
    until docker compose exec -T caddy test -f "$CERT_DIR/$host/$host.crt" 2>/dev/null; do
        if [ "$waited" -ge 180 ]; then
            echo "   Caddy has no certificate for $host after 3 minutes. See what it says:"
            echo "     docker compose logs --tail 30 caddy"
            echo "   The mail server starts by itself once the certificate is there. Then run"
            echo "   ./scripts/mail.sh dns for the DNS records."
            exit 1
        fi
        sleep 5
        waited=$((waited + 5))
    done
    echo "   Caddy has the certificate for $host."
    waited=0
    until [ "$(docker inspect -f '{{.State.Health.Status}}' "$(docker compose ps -q mailserver)" 2>/dev/null)" = healthy ]; do
        if [ "$waited" -ge 300 ]; then
            die "   The mail server did not start within 5 minutes. See: docker compose logs --tail 50 mailserver"
        fi
        sleep 5
        waited=$((waited + 5))
    done
    echo "   The mail server runs. Webmail: https://$host"
    make_dkim "$domain" >/dev/null || die "Could not make the DKIM key. See: docker compose logs --tail 50 mailserver"
    echo

    echo "4. The DNS records"
    echo
    dns
    echo
    echo "Next: add the DNS records, ask your server company for the reverse DNS, and"
    echo "send a test email. docs/email-server.md tells how, step by step."
}

test_mail() {
    require_env
    local to=${1:-} user password
    valid_address "$to" || die "Write the address to send to, for example: ./scripts/mail.sh test you@gmail.com"
    running || die "The mail server is not running. Start it with: docker compose up -d"
    user=$(env_value SMTP_USER)
    password=$(env_value SMTP_PASSWORD)
    if [ -z "$user" ] || [ -z "$password" ]; then
        read -r -p "Send from which of your mailboxes? " user || die $'\nNo mailbox given.'
        read -r -s -p "Its password: " password || die $'\nNo password given.'
        echo
    fi
    echo "Sending a test email from $user to $to..."
    if docker compose exec -T mailserver swaks --silent 2 --to "$to" --from "$user" \
        --server 127.0.0.1 --port 587 -tls --auth LOGIN --auth-user "$user" --auth-password "$password" \
        --header "Subject: Test email from $(mail_host)" \
        --body "This is a test email from your own mail server. If it reached the inbox, sending works."; then
        echo "The mail server took it and is delivering it. Look in the inbox of $to (and in its"
        echo "spam folder). If it does not come within a few minutes, see what happened with:"
        echo "  ./scripts/mail.sh logs"
    else
        echo "The mail server did not take it (see above). A wrong password? Then:"
        echo "  ./scripts/mail.sh password $user"
        return 1
    fi
}

update_images() {
    require_env
    docker compose pull --quiet mailserver webmail || return 1
    docker compose up -d mailserver webmail
}

main() {
    local command=${1:-}
    shift || true
    case $command in
        check) check ;;
        setup) setup ;;
        dns) dns ;;
        add) add "${1:-}" ;;
        password)
            require_env
            valid_address "${1:-}" || die "Write the whole address: ./scripts/mail.sh password NAME@DOMAIN"
            ask_password "$1"
            dms email update "$1" "$PASSWORD" && echo "New password set for $1."
            ;;
        remove)
            require_env
            valid_address "${1:-}" || die "Write the whole address: ./scripts/mail.sh remove NAME@DOMAIN"
            dms_ask email del "$1"
            ;;
        alias)
            require_env
            if ! valid_address "${1:-}" || ! valid_address "${2:-}"; then
                die "Write both addresses: ./scripts/mail.sh alias FROM@DOMAIN TO@DOMAIN"
            fi
            dms alias add "$1" "$2" && echo "Mail to $1 now goes to $2."
            ;;
        unalias)
            require_env
            dms alias del "${1:-}" "${2:-}"
            ;;
        list)
            require_env
            echo "Mailboxes:"
            dms email list
            echo "Aliases:"
            dms alias list
            ;;
        test) test_mail "${1:-}" ;;
        unban)
            require_env
            [ -n "${1:-}" ] || die "Write the address to unblock: ./scripts/mail.sh unban 203.0.113.5"
            docker compose exec -T mailserver setup fail2ban unban "$1"
            ;;
        update) update_images ;;
        logs) docker compose logs --tail 80 mailserver webmail ;;
        *)
            sed -n '2,24p' "$0" | sed 's/^# \{0,1\}//'
            [ -z "$command" ] || return 2
            ;;
    esac
}

main "$@"
