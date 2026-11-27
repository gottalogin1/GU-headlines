#!/usr/bin/env bash
# Makes the server itself harder to break into. Run it on the server, in the
# GU-headlines folder, as root (on a rented server you usually are root):
#
#   ./scripts/harden-server.sh           do all the steps below
#   ./scripts/harden-server.sh --check   only show where each step stands
#   ./scripts/harden-server.sh --allow-ssh-passwords
#                                        undo step 4: ssh takes passwords again
#
# (Signed in as another user, put "sudo " in front.) It is safe to run again:
# a step that is done already is left alone. docs/security.md explains each
# step, and how to do it by hand.
#
#   1. Automatic Linux security updates (unattended-upgrades). When one needs
#      a restart of the server, the server checks post in Discord.
#   2. Fail2Ban: an address that types 5 wrong ssh passwords within 10
#      minutes is blocked for an hour.
#   3. The firewall (ufw): only ssh, the website (80 and 443) and, with your
#      own mail server, the mail ports are open.
#   4. ssh takes keys only, no passwords. So that you can't lock yourself
#      out, this step runs only when this very ssh session signed in with a
#      key. Otherwise it says how to make a key, and you run the script again.
#   5. STATS_PASSWORD: replaced by a long random one when it is shorter than
#      12 characters. The new one is shown once, at the end.
#   6. .env and the backups folder: readable by their owner only.
#
# What it can't do for you: make the ssh key on your own computer, and turn
# on two-step sign-in at GitHub, PayPal, RackNerd and your other accounts.
# The end of its output lists them.

# This script needs bash. When it is started with "sh" (on Ubuntu a smaller
# shell, dash), start it again with bash.
if [ -z "${BASH_VERSION:-}" ]; then
    exec bash "$0" "$@"
fi
set -uo pipefail

cd "$(dirname "$0")/.." || exit 1
export PATH="$PATH:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

# Where the system's settings are (another folder in the tests).
ETC="${HARDEN_ETC:-/etc}"
PROC_VERSION="${PROC_VERSION:-/proc/version}"
AUTH_LOG="${AUTH_LOG:-/var/log/auth.log}"
SSHD_DROPIN="$ETC/ssh/sshd_config.d/10-guheadlines.conf"
JAIL="$ETC/fail2ban/jail.d/guheadlines-sshd.local"
AUTO_UPGRADES="$ETC/apt/apt.conf.d/20auto-upgrades"

CHECK=""
APT_UPDATED=""
NEED_KEY=""
NEW_PASSWORD=""

die() {
    echo "$*" >&2
    exit 1
}
ok() { echo "   ✓ $*"; }
note() { echo "   • $*"; }
problem() { echo "   ✗ $*"; }

# One value from .env, read the way the other scripts read it.
env_value() {
    [ -f .env ] || return 0
    sed -n "s/^$1=//p" .env | tail -n 1 | tr -d "\r\"'" | sed 's/[[:space:]]#.*$//; s/[[:space:]]*$//'
}

# port SETTING DEFAULT: a port number from .env, else DEFAULT.
port() {
    local value
    value=$(env_value "$1")
    case $value in '' | *[!0-9]*) echo "$2" ;; *) echo "$value" ;; esac
}

mail_on() {
    env_value COMPOSE_PROFILES | tr ',' '\n' | grep -qx '[[:space:]]*mail[[:space:]]*'
}

# The server's own IP address (for the instructions at the end).
server_ip() {
    ip -4 route get 1.1.1.1 2>/dev/null | sed -n 's/.* src \([0-9.]*\).*/\1/p' | head -n 1
}

preflight() {
    [ "$(id -u)" = 0 ] || die "Run it as root (or with sudo): sudo ./scripts/harden-server.sh"
    if grep -qi microsoft "$PROC_VERSION" 2>/dev/null; then
        die "This is Windows (WSL), not the server. Sign in to the server with ssh, and run it there."
    fi
    command -v apt-get >/dev/null || die "This script is for Ubuntu and Debian servers (it uses apt-get)."
    command -v systemctl >/dev/null || die "This script needs systemd (Ubuntu and Debian have it)."
}

installed() {
    dpkg -s "$1" >/dev/null 2>&1
}

# install_packages NAME...: install the ones that are missing (not with --check).
install_packages() {
    local missing=() name
    for name in "$@"; do
        installed "$name" || missing+=("$name")
    done
    [ ${#missing[@]} -eq 0 ] && return 0
    [ -n "$CHECK" ] && return 1
    if [ -z "$APT_UPDATED" ]; then
        DEBIAN_FRONTEND=noninteractive apt-get update -qq >/dev/null 2>&1 || return 1
        APT_UPDATED=1
    fi
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "${missing[@]}" >/dev/null 2>&1
}

# The ports ssh listens on, and the one this ssh session came in on (so the
# firewall never closes the door you are using).
ssh_ports() {
    local session=${SSH_CONNECTION:-} found
    found=$({
        sshd -T 2>/dev/null | awk '$1 == "port" { print $2 }'
        echo "${session##* }"
    } | grep -E '^[0-9]+$' | sort -un)
    echo "${found:-22}"
}

# ------------------------------------------------------------------ steps

step_updates() {
    echo "1. Automatic Linux security updates"
    if installed unattended-upgrades &&
        grep -qs 'APT::Periodic::Unattended-Upgrade "1"' "$AUTO_UPGRADES"; then
        ok "on"
        return
    fi
    if [ -n "$CHECK" ]; then
        problem "off"
        return
    fi
    if ! install_packages unattended-upgrades; then
        problem "could not install unattended-upgrades (try: apt-get update)"
        return
    fi
    mkdir -p "$(dirname "$AUTO_UPGRADES")"
    printf '%s\n' 'APT::Periodic::Update-Package-Lists "1";' \
        'APT::Periodic::Unattended-Upgrade "1";' >"$AUTO_UPGRADES"
    systemctl enable --now unattended-upgrades >/dev/null 2>&1
    ok "turned on: Linux installs its security updates every day by itself"
}

step_fail2ban() {
    echo "2. Fail2Ban (blocks addresses that guess ssh passwords)"
    if installed fail2ban && [ -f "$JAIL" ] && systemctl is-active --quiet fail2ban; then
        ok "on"
        return
    fi
    if [ -n "$CHECK" ]; then
        problem "off"
        return
    fi
    if ! install_packages fail2ban python3-systemd; then
        problem "could not install fail2ban (try: apt-get update)"
        return
    fi
    local ports
    ports=$(ssh_ports | paste -sd, -)
    mkdir -p "$(dirname "$JAIL")"
    cat >"$JAIL" <<EOF
# Made by GU Headlines (scripts/harden-server.sh): an address that types 5
# wrong ssh passwords within 10 minutes is blocked for an hour.
# To unblock one: fail2ban-client set sshd unbanip 203.0.113.5
[sshd]
enabled = true
backend = systemd
port = $ports
maxretry = 5
findtime = 10m
bantime = 1h
EOF
    systemctl enable fail2ban >/dev/null 2>&1
    if systemctl restart fail2ban >/dev/null 2>&1; then
        ok "turned on: 5 wrong ssh passwords within 10 minutes block an address for an hour"
    else
        problem "Fail2Ban did not start. To see why: journalctl -u fail2ban --since today"
    fi
}

step_firewall() {
    echo "3. The firewall (ufw)"
    if systemctl is-active --quiet firewalld 2>/dev/null; then
        note "another firewall (firewalld) is on already: left as it is"
        return
    fi
    if command -v ufw >/dev/null && ufw status 2>/dev/null | grep -q '^Status: active'; then
        ok "on (its rules are left as they are; to see them: ufw status)"
        return
    fi
    if [ -n "$CHECK" ]; then
        problem "off"
        return
    fi
    if ! install_packages ufw; then
        problem "could not install ufw (try: apt-get update)"
        return
    fi
    local ssh http https opened p
    ssh=$(ssh_ports | tr '\n' ' ')
    http=$(port HTTP_PORT 80)
    https=$(port HTTPS_PORT 443)
    ufw default deny incoming >/dev/null
    ufw default allow outgoing >/dev/null
    # ssh first, so the door you are using stays open.
    for p in $ssh; do
        ufw allow "$p/tcp" comment "ssh" >/dev/null
    done
    ufw allow "$http/tcp" comment "website (http)" >/dev/null
    ufw allow "$https/tcp" comment "website (https)" >/dev/null
    ufw allow "$https/udp" comment "website (HTTP/3)" >/dev/null
    opened="ssh (port ${ssh% }), the website ($http and $https)"
    if mail_on; then
        for p in 25 465 587 993; do
            ufw allow "$p/tcp" comment "mail server" >/dev/null
        done
        opened="$opened, the mail server (25, 465, 587, 993)"
    fi
    if ufw --force enable >/dev/null 2>&1; then
        ok "turned on. Open: $opened. Everything else is refused."
    else
        problem "ufw did not start. To see why: ufw status verbose"
    fi
}

passwords_off() {
    sshd -T 2>/dev/null | grep -qx 'passwordauthentication no'
}

# How this ssh session signed in, from ssh's log: publickey, password,
# keyboard-interactive/pam, ... ; nothing when it can't be seen.
session_method() {
    local ip="" client_port="" rest pattern
    if [ -n "${SSH_CONNECTION:-}" ]; then
        read -r ip client_port rest <<<"$SSH_CONNECTION"
    else
        # Under sudo: the address that this terminal came from.
        ip=$(who -m 2>/dev/null | sed -n 's/.*(\([^)]*\)).*/\1/p' | head -n 1)
    fi
    [ -n "$ip" ] || return 0
    pattern="Accepted [a-z/-]+ for [^ ]+ from ${ip//./\\.} port ${client_port:-[0-9]+}( |$)"
    {
        journalctl -q --no-pager --since "-3 days" -u ssh -u sshd 2>/dev/null
        journalctl -q --no-pager --since "-3 days" -t sshd -t sshd-session 2>/dev/null
        cat "$AUTH_LOG" 2>/dev/null
    } | grep -E "$pattern" | tail -n 1 | sed -n 's/.*Accepted \([a-z/-]*\) for .*/\1/p'
}

# The keys that may sign in as the user who runs this script.
keys_file() {
    local home
    home=$(getent passwd "${SUDO_USER:-root}" | cut -d: -f6)
    echo "${AUTHORIZED_KEYS:-${home:-/root}/.ssh/authorized_keys}"
}

has_keys() {
    grep -qE '^[^#]*(ssh-|ecdsa-|sk-)' "$(keys_file)" 2>/dev/null
}

reload_ssh() {
    # A reload keeps the sessions that are open, this one too.
    systemctl reload ssh >/dev/null 2>&1 || systemctl reload sshd >/dev/null 2>&1 ||
        systemctl restart ssh >/dev/null 2>&1 || systemctl restart sshd >/dev/null 2>&1
}

step_ssh() {
    echo "4. ssh: sign in with keys only"
    if passwords_off; then
        ok "on: ssh takes no passwords"
        return
    fi
    if [ -n "$CHECK" ]; then
        problem "off: ssh still takes passwords"
        return
    fi
    local method answer interactive
    method=$(session_method)
    case $method in
        publickey) ;;
        "")
            if [ -t 0 ] && has_keys; then
                read -r -p "   Did you sign in to this server with your ssh key just now? (yes/no) " answer
                if [ "$answer" != yes ]; then
                    note "left as it is: ssh still takes passwords"
                    NEED_KEY=1
                    return
                fi
            else
                note "left as it is: this session did not show that your key works"
                NEED_KEY=1
                return
            fi
            ;;
        *)
            note "left as it is: you signed in with a password, not a key, so turning"
            note "passwords off now could lock you out"
            NEED_KEY=1
            return
            ;;
    esac
    # Older ssh (Ubuntu 20.04) has another name for the "type a code" sign-in.
    interactive=KbdInteractiveAuthentication
    sshd -T 2>/dev/null | grep -q '^kbdinteractiveauthentication ' ||
        interactive=ChallengeResponseAuthentication
    if [ -f "$ETC/ssh/sshd_config" ] &&
        ! grep -qE '^[[:space:]]*Include[[:space:]]+/etc/ssh/sshd_config\.d/' "$ETC/ssh/sshd_config"; then
        sed -i '1i Include /etc/ssh/sshd_config.d/*.conf' "$ETC/ssh/sshd_config"
    fi
    mkdir -p "$(dirname "$SSHD_DROPIN")"
    cat >"$SSHD_DROPIN" <<EOF
# Made by GU Headlines (scripts/harden-server.sh): sign in with ssh keys only.
# To allow passwords again: ./scripts/harden-server.sh --allow-ssh-passwords
PasswordAuthentication no
$interactive no
PermitRootLogin prohibit-password
EOF
    if ! sshd -t >/dev/null 2>&1; then
        rm -f "$SSHD_DROPIN"
        problem "ssh did not accept the change, so it was undone. ssh works as before."
        return
    fi
    reload_ssh
    if passwords_off; then
        ok "turned on: ssh takes keys only. Keep this window open, and check that a"
        ok "new ssh window still lets you in with your key."
    else
        problem "another file in $ETC/ssh/sshd_config.d still turns passwords on"
        problem "(to see which: grep -ri passwordauthentication $ETC/ssh/)"
    fi
}

step_stats_password() {
    echo "5. STATS_PASSWORD (the password of the private pages)"
    if [ ! -f .env ]; then
        note "no .env here: run the script in the GU-headlines folder"
        return
    fi
    local current
    current=$(env_value STATS_PASSWORD)
    if [ -z "$current" ]; then
        note "empty: the private pages are off, so there is nothing to guess"
        return
    fi
    if [ ${#current} -ge 12 ]; then
        ok "long enough (${#current} characters)"
        return
    fi
    if [ -n "$CHECK" ]; then
        problem "only ${#current} characters: it should have at least 12"
        return
    fi
    NEW_PASSWORD=$(openssl rand -hex 16 2>/dev/null)
    [ ${#NEW_PASSWORD} -eq 32 ] || NEW_PASSWORD=$(tr -dc 'a-f0-9' </dev/urandom | head -c 32)
    sed -i "s/^STATS_PASSWORD=.*/STATS_PASSWORD=$NEW_PASSWORD/" .env
    if docker compose up -d >/dev/null 2>&1; then
        ok "replaced by a new one of 32 letters and digits (shown at the end)"
    else
        ok "replaced by a new one (shown at the end). Apply it with: docker compose up -d"
    fi
}

step_private_files() {
    echo "6. Private files (.env and the backups folder)"
    local path mode changed=""
    for path in .env backups; do
        [ -e "$path" ] || continue
        mode=$(stat -c %a "$path" 2>/dev/null) || continue
        [ $((8#$mode & 8#077)) -eq 0 ] && continue
        if [ -n "$CHECK" ]; then
            problem "$path can be read by other users (permissions $mode)"
            changed=1
            continue
        fi
        if [ -d "$path" ]; then chmod 700 "$path"; else chmod 600 "$path"; fi
        changed=1
        ok "$path: only its owner can read it now (it was $mode)"
    done
    [ -n "$changed" ] || ok "only their owner can read them"
}

allow_passwords() {
    if [ ! -f "$SSHD_DROPIN" ]; then
        echo "ssh takes passwords already (or another setting turns them off: see $ETC/ssh/)."
        return 0
    fi
    rm -f "$SSHD_DROPIN"
    sshd -t >/dev/null 2>&1 && reload_ssh
    echo "Done: ssh takes passwords again. Run ./scripts/harden-server.sh to turn them off again."
}

summary() {
    local who address
    who=${SUDO_USER:-root}
    address=$(server_ip)
    address=${address:-YOUR-SERVER-IP}
    echo
    if [ -n "$NEW_PASSWORD" ]; then
        echo "Your new STATS_PASSWORD (also in .env): $NEW_PASSWORD"
        echo "Keep it in a password manager. Your browser asks for it again on /stats."
        echo
    fi
    echo "Still yours to do (the script can't do these for you):"
    if [ -n "$NEED_KEY" ]; then
        cat <<EOF
 - Make an ssh key on your own computer, then run this script again so it
   can turn ssh passwords off. On Windows, in PowerShell:
     ssh-keygen -t ed25519
     type \$env:USERPROFILE\\.ssh\\id_ed25519.pub | ssh $who@$address "mkdir -p ~/.ssh && chmod 700 ~/.ssh && cat >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys"
   Then open a new PowerShell window, sign in with: ssh $who@$address
   (it asks for the key's passphrase, not the server's password), and run:
     cd $(pwd) && ./scripts/harden-server.sh
EOF
    fi
    cat <<EOF
 - Turn on two-step sign-in (a code from an app) at GitHub (the most
   important one: the server installs whatever reaches the main branch),
   PayPal, RackNerd (the client area and the control panel), the company
   where your domain is registered, Google (AdSense), Discord and SendFox.
 - When Discord says the server needs a restart, run: reboot
More: docs/security.md
EOF
}

main() {
    case "${1:-}" in
        --check) CHECK=1 ;;
        --allow-ssh-passwords)
            preflight
            allow_passwords
            return
            ;;
        "") ;;
        *)
            echo "unknown option: $1 (see the notes at the top of $0)"
            return 2
            ;;
    esac
    preflight
    if [ -n "$CHECK" ]; then
        echo "Where each step stands (nothing is changed):"
    else
        echo "Making the server harder to break into. This takes a minute or two."
    fi
    echo
    step_updates
    step_fail2ban
    step_firewall
    step_ssh
    step_stats_password
    step_private_files
    [ -n "$CHECK" ] && return 0
    summary
}

main "$@"
exit $?
