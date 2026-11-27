"""scripts/harden-server.sh, run with stand-ins for the programs it calls
(apt-get, dpkg, systemctl, ufw, sshd, journalctl, ...). The system's settings
go to a folder of the test (HARDEN_ETC), never to the real /etc."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")

STUBS = {
    "id": 'echo "${FAKE_UID:-0}"',
    # A package is installed when its name is a line of installed.txt.
    "dpkg": 'grep -qx "$2" "$FAKE/installed.txt" 2>/dev/null',
    "apt-get": """
echo "apt-get $*" >> "$FAKE/calls.txt"
if [ "$1" = install ]; then
    for name in "$@"; do
        case $name in -*|install) ;; *) echo "$name" >> "$FAKE/installed.txt" ;; esac
    done
fi
""",
    "systemctl": """
echo "systemctl $*" >> "$FAKE/calls.txt"
case "$1" in
    is-active) grep -qx "${3:-$2}" "$FAKE/active.txt" 2>/dev/null ;;
    restart|reload) [ "${2:-}" = fail2ban ] && echo fail2ban >> "$FAKE/active.txt"; true ;;
    *) true ;;
esac
""",
    "ufw": """
echo "ufw $*" >> "$FAKE/calls.txt"
case "$1" in
    status)
        if [ -f "$FAKE/ufw-on" ]; then echo "Status: active"; else echo "Status: inactive"; fi
        ;;
    --force) touch "$FAKE/ufw-on" ;;
esac
""",
    # ssh's settings: passwords are off once our file is there.
    "sshd": """
dropin="$HARDEN_ETC/ssh/sshd_config.d/10-guheadlines.conf"
case "$1" in
    -T)
        for p in ${SSH_PORTS:-22}; do echo "port $p"; done
        echo "kbdinteractiveauthentication no"
        answer=yes
        [ -f "$dropin" ] && answer=no
        echo "passwordauthentication $answer"
        ;;
    -t) [ -z "${SSHD_REFUSES:-}" ] ;;
esac
""",
    "journalctl": 'cat "$FAKE/journal.txt" 2>/dev/null; true',
    "who": 'echo "root     pts/0        2026-11-27 08:00 (203.0.113.5)"',
    "getent": 'echo "root:x:0:0:root:$FAKE/root:/bin/bash"',
    "openssl": "echo 0123456789abcdef0123456789abcdef",
    "docker": 'echo "docker $*" >> "$FAKE/calls.txt"',
    "ip": 'echo "1.1.1.1 via 198.51.100.1 dev eth0 src 198.51.100.7 uid 0"',
}

KEY_LOGIN = (
    "Nov 27 08:00:01 server sshd[812]: Accepted publickey for root from 203.0.113.5"
    " port 50122 ssh2: ED25519 SHA256:abc\n"
)
PASSWORD_LOGIN = (
    "Nov 27 08:00:01 server sshd[812]: Accepted password for root from 203.0.113.5"
    " port 50122 ssh2\n"
)


class Server:
    """A copy of the script in a folder of its own, with a .env, the stand-ins
    and a pretend /etc."""

    def __init__(self, tmp: Path, env: dict[str, str] | None = None):
        self.root = tmp / "site"
        (self.root / "scripts").mkdir(parents=True)
        shutil.copy(ROOT / "scripts" / "harden-server.sh", self.root / "scripts")
        self.fake = tmp / "fake"
        bin_dir = self.fake / "bin"
        bin_dir.mkdir(parents=True)
        for name, body in STUBS.items():
            stub = bin_dir / name
            stub.write_text("#!/usr/bin/env bash\n" + body)
            stub.chmod(0o755)
        self.etc = tmp / "etc"
        (self.etc / "ssh").mkdir(parents=True)
        (self.etc / "ssh" / "sshd_config").write_text(
            "Include /etc/ssh/sshd_config.d/*.conf\nPort 22\n"
        )
        (self.fake / "root" / ".ssh").mkdir(parents=True)
        (self.fake / "version").write_text("Linux version 6.8.0-49-generic (buildd@ubuntu)\n")
        lines = {"POSTGRES_PASSWORD": "pw", "STATS_PASSWORD": "short1", **(env or {})}
        self.env_file = self.root / ".env"
        self.env_file.write_text("".join(f"{k}={v}\n" for k, v in lines.items()))
        self.env_file.chmod(0o644)
        self.env = {
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "FAKE": str(self.fake),
            "HARDEN_ETC": str(self.etc),
            "PROC_VERSION": str(self.fake / "version"),
            "AUTH_LOG": str(self.fake / "auth.log"),
            "HOME": str(tmp),
            "SSH_CONNECTION": "203.0.113.5 50122 198.51.100.7 22",
        }

    def login(self, line: str) -> None:
        (self.fake / "journal.txt").write_text(line)

    def add_key(self) -> None:
        (self.fake / "root" / ".ssh" / "authorized_keys").write_text(
            "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAA me@laptop\n"
        )

    def run(self, *args: str, **env: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["bash", str(self.root / "scripts" / "harden-server.sh"), *args],
            env={**self.env, **env},
            capture_output=True,
            text=True,
            timeout=60,
            stdin=subprocess.DEVNULL,
        )

    def calls(self) -> str:
        path = self.fake / "calls.txt"
        return path.read_text() if path.exists() else ""

    @property
    def dropin(self) -> Path:
        return self.etc / "ssh" / "sshd_config.d" / "10-guheadlines.conf"


@pytest.fixture()
def server(tmp_path):
    return Server(tmp_path)


def test_everything_is_done_when_this_session_signed_in_with_a_key(server):
    server.login(KEY_LOGIN)
    result = server.run()
    assert result.returncode == 0, result.stderr
    out = result.stdout
    calls = server.calls()
    # 1. Automatic updates.
    assert "apt-get install -y -qq unattended-upgrades" in calls
    auto = (server.etc / "apt" / "apt.conf.d" / "20auto-upgrades").read_text()
    assert 'APT::Periodic::Unattended-Upgrade "1";' in auto
    # 2. Fail2Ban, for the port ssh listens on.
    jail = (server.etc / "fail2ban" / "jail.d" / "guheadlines-sshd.local").read_text()
    assert "port = 22\n" in jail and "maxretry = 5" in jail and "bantime = 1h" in jail
    assert "systemctl restart fail2ban" in calls
    # 3. The firewall: ssh is allowed before it is turned on.
    assert calls.index("ufw allow 22/tcp comment ssh") < calls.index("ufw --force enable")
    for rule in ("80/tcp", "443/tcp", "443/udp"):
        assert f"ufw allow {rule}" in calls
    assert "ufw allow 25/tcp" not in calls  # no mail server
    assert "ufw default deny incoming" in calls
    # 4. ssh with keys only.
    text = server.dropin.read_text()
    assert "PasswordAuthentication no\n" in text
    assert "KbdInteractiveAuthentication no\n" in text
    assert "PermitRootLogin prohibit-password\n" in text
    assert "systemctl reload ssh" in calls
    assert "turned on: ssh takes keys only" in out
    # 5. The short STATS_PASSWORD is replaced and applied.
    assert "STATS_PASSWORD=0123456789abcdef0123456789abcdef\n" in server.env_file.read_text()
    assert "docker compose up -d" in calls
    assert "Your new STATS_PASSWORD (also in .env): 0123456789abcdef0123456789abcdef" in out
    # 6. .env is private.
    assert server.env_file.stat().st_mode & 0o777 == 0o600
    # What is left to do by hand.
    assert "two-step sign-in" in out and "GitHub" in out and "PayPal" in out
    assert "ssh-keygen" not in out  # the key works already


def test_a_password_sign_in_keeps_ssh_passwords_on_and_explains_the_key(server):
    server.login(PASSWORD_LOGIN)
    result = server.run()
    assert result.returncode == 0, result.stderr
    assert not server.dropin.exists()
    assert "you signed in with a password, not a key" in result.stdout
    assert "systemctl reload ssh" not in server.calls()
    # The other steps still happen.
    assert "ufw --force enable" in server.calls()
    # How to make the key, with this server's address filled in.
    assert "ssh-keygen -t ed25519" in result.stdout
    assert "ssh root@198.51.100.7" in result.stdout
    assert "./scripts/harden-server.sh" in result.stdout


def test_unknown_sign_in_without_a_keyboard_keeps_passwords_on(server):
    server.add_key()
    result = server.run()  # nothing in the log, and no one to ask
    assert not server.dropin.exists()
    assert "did not show that your key works" in result.stdout


def test_under_sudo_the_terminal_address_is_used(server):
    server.login(KEY_LOGIN)
    result = server.run(SSH_CONNECTION="")
    assert server.dropin.exists(), result.stdout


def test_ssh_that_refuses_the_change_is_left_as_it_was(server):
    server.login(KEY_LOGIN)
    result = server.run(SSHD_REFUSES="1")
    assert not server.dropin.exists()
    assert "ssh did not accept the change, so it was undone" in result.stdout
    assert "systemctl reload ssh" not in server.calls()


def test_check_changes_nothing(server):
    server.login(KEY_LOGIN)
    result = server.run("--check")
    assert result.returncode == 0, result.stderr
    # Only questions: no installs, no firewall rules, no restarts.
    changes = [c for c in server.calls().splitlines() if "is-active" not in c]
    assert changes in ([], ["ufw status"]), changes
    assert not server.dropin.exists()
    assert not (server.etc / "apt").exists()
    assert "STATS_PASSWORD=short1\n" in server.env_file.read_text()
    assert server.env_file.stat().st_mode & 0o777 == 0o644
    out = result.stdout
    assert "off: ssh still takes passwords" in out
    assert "only 6 characters" in out
    assert ".env can be read by other users (permissions 644)" in out


def test_a_second_run_leaves_everything_as_it_is(server):
    server.login(KEY_LOGIN)
    server.run()
    (server.fake / "calls.txt").unlink()
    result = server.run()
    assert result.returncode == 0, result.stderr
    calls = server.calls()
    assert "apt-get" not in calls and "ufw allow" not in calls and "docker" not in calls
    assert "systemctl reload ssh" not in calls
    out = result.stdout
    assert out.count("✓") >= 6
    assert "long enough (32 characters)" in out
    assert "Your new STATS_PASSWORD" not in out


def test_custom_ssh_port_and_mail_server(tmp_path):
    server = Server(tmp_path, {"COMPOSE_PROFILES": "mail", "STATS_PASSWORD": ""})
    server.login(KEY_LOGIN)
    result = server.run(SSH_PORTS="2222", SSH_CONNECTION="203.0.113.5 50122 198.51.100.7 2222")
    calls = server.calls()
    assert "ufw allow 2222/tcp comment ssh" in calls
    assert "ufw allow 22/tcp" not in calls
    for p in (25, 465, 587, 993):
        assert f"ufw allow {p}/tcp comment mail server" in calls
    jail = (server.etc / "fail2ban" / "jail.d" / "guheadlines-sshd.local").read_text()
    assert "port = 2222\n" in jail
    # An empty STATS_PASSWORD stays empty: the private pages are off.
    assert "STATS_PASSWORD=\n" in server.env_file.read_text()
    assert "nothing to guess" in result.stdout


def test_allow_ssh_passwords_undoes_the_ssh_step(server):
    server.login(KEY_LOGIN)
    server.run()
    assert server.dropin.exists()
    result = server.run("--allow-ssh-passwords")
    assert result.returncode == 0, result.stderr
    assert not server.dropin.exists()
    assert "ssh takes passwords again" in result.stdout


def test_it_refuses_to_run_on_windows_or_without_root(server):
    (server.fake / "version").write_text("Linux version 5.15.167.4-microsoft-standard-WSL2\n")
    result = server.run()
    assert result.returncode == 1 and "This is Windows (WSL)" in result.stderr
    (server.fake / "version").write_text("Linux version 6.8.0\n")
    result = server.run(FAKE_UID="1000")
    assert result.returncode == 1 and "Run it as root" in result.stderr
    assert server.calls() == ""
