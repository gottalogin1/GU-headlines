"""scripts/server-check.sh, run with stand-ins for the programs it calls (df,
docker, openssl, getent, ip and curl, which posts to Discord)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
WEBHOOK = "https://discord.com/api/webhooks/1234567890/abc-DEF_123"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")

STUBS = {
    # Each posted message is one line in discord.jsonl.
    "curl": """
while [ $# -gt 0 ]; do
    if [ "$1" = --data-binary ]; then printf '%s\\n' "$2" >> "$FAKE/discord.jsonl"; fi
    shift
done
[ -z "${CURL_FAILS:-}" ]
""",
    "df": """
echo "Filesystem 1024-blocks Used Available Capacity Mounted on"
cat "$FAKE/df.txt"
""",
    "docker": """
case "$1 $2" in
    "image prune") echo "Total reclaimed space: ${IMAGES_FREED:-0B}" ;;
    "builder prune") printf 'ID\\tRECLAIMABLE\\nTotal:\\t%s\\n' "${CACHE_FREED:-0B}" ;;
    *) echo "" ;;
esac
""",
    "openssl": """
case "$1" in
    s_client) cat > /dev/null; [ -n "${CERT_END:-}" ] && echo "-----BEGIN CERTIFICATE-----" ;;
    x509) grep -q BEGIN && echo "notAfter=$CERT_END" ;;
esac
""",
    "getent": """
for list in ${LISTED:-}; do
    case "$2" in *".$list") echo "127.0.0.2 STREAM $2"; exit 0 ;; esac
done
for list in ${REFUSED:-}; do
    case "$2" in *".$list") echo "127.255.255.254 STREAM $2"; exit 0 ;; esac
done
exit 2
""",
    "ip": 'echo "1.1.1.1 via 203.0.113.1 dev eth0 src 203.0.113.5 uid 0"',
}


class Server:
    """A copy of the script in a folder of its own, with a .env and the stand-ins."""

    def __init__(self, tmp: Path, env: dict[str, str]):
        self.root = tmp / "site"
        (self.root / "scripts").mkdir(parents=True)
        shutil.copy(ROOT / "scripts" / "server-check.sh", self.root / "scripts")
        self.fake = tmp / "fake"
        bin_dir = self.fake / "bin"
        bin_dir.mkdir(parents=True)
        for name, body in STUBS.items():
            stub = bin_dir / name
            stub.write_text("#!/usr/bin/env bash\n" + body)
            stub.chmod(0o755)
        lines = {"DISCORD_WEBHOOK_URL": WEBHOOK, "SITE_DOMAIN": "guamheadlines.com", **env}
        (self.root / ".env").write_text("".join(f"{k}={v}\n" for k, v in lines.items()))
        self.disk(50)
        self.memory(40)
        self.env = {
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "FAKE": str(self.fake),
            "MEMINFO": str(self.fake / "meminfo"),
            "REBOOT_FLAG": str(self.fake / "reboot-required"),
            "HOME": str(tmp),
        }

    def disk(self, percent: int) -> None:
        size = 50 * 1048576  # 50 GB in kilobytes
        used = size * percent // 100
        (self.fake / "df.txt").write_text(f"/dev/vda1 {size} {used} {size - used} {percent}% /\n")

    def memory(self, percent: int) -> None:
        total = 4 * 1048576  # 4 GB
        free = total * (100 - percent) // 100
        (self.fake / "meminfo").write_text(
            f"MemTotal: {total} kB\nMemFree: 1000 kB\nMemAvailable: {free} kB\n"
        )

    def run(self, *args: str, **env: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["bash", str(self.root / "scripts" / "server-check.sh"), *args],
            env={**self.env, **env},
            capture_output=True,
            text=True,
            timeout=60,
        )

    def round(self, **env: str) -> subprocess.CompletedProcess:
        result = self.run("--round", **env)
        assert result.returncode == 0, result.stdout + result.stderr
        return result

    def posted(self) -> list[dict]:
        path = self.fake / "discord.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines()]

    def texts(self) -> list[str]:
        return [message["content"] for message in self.posted()]

    def forget_times(self) -> None:
        """As if the last block list and certificate checks were long ago."""
        state = self.root / "backups" / ".server-check"
        for name in ("lists-checked", "certs-checked"):
            (state / name).unlink(missing_ok=True)


def cert_end(days: float) -> str:
    """An openssl end date, `days` from now."""
    moment = datetime.now(timezone.utc) + timedelta(days=days)
    return moment.strftime("%b %d %H:%M:%S %Y GMT")


@pytest.fixture()
def server(tmp_path):
    return Server(tmp_path, {})


def test_by_hand_it_shows_the_readings_and_posts_nothing_when_all_is_well(server):
    result = server.run(CERT_END=cert_end(60))
    assert result.returncode == 0, result.stderr
    assert "Disk /: 50% full, 25.0 GB free of 50.0 GB (alert at 90%)" in result.stdout
    assert "Memory: 40% in use, 2457 MB free of 4096 MB (alert at 90%)" in result.stdout
    assert "Certificate of the site (guamheadlines.com): ends" in result.stdout
    assert "in 59 days" in result.stdout or "in 60 days" in result.stdout
    assert "Restart needed: no" in result.stdout
    assert server.posted() == []


def test_a_full_disk_is_posted_once_and_again_when_it_has_room(server):
    server.disk(91)
    server.round()
    server.round()
    assert len(server.posted()) == 1
    message = server.posted()[0]
    assert message["content"].startswith("💾 **guamheadlines.com: disk 91% full**\n")
    assert "4.5 GB free of 50.0 GB" in message["content"]
    assert message["allowed_mentions"] == {"parse": []}
    assert message["flags"] == 4  # with a phone notification
    server.disk(89)  # not enough room yet: no flapping around the limit
    server.round()
    assert len(server.posted()) == 1
    server.disk(80)
    server.round()
    assert server.texts()[1].startswith("✅ **guamheadlines.com: disk space OK again**")
    assert server.posted()[1]["flags"] == 4 | 4096  # quiet


def test_memory_must_stay_high_for_three_rounds(server):
    server.memory(95)
    server.round()
    server.round()
    assert server.posted() == []
    server.round()
    server.round()
    assert len(server.posted()) == 1
    assert server.texts()[0].startswith("🧠 **guamheadlines.com: memory 95% in use**")
    server.memory(50)
    server.round()
    assert server.texts()[1].startswith("✅ **guamheadlines.com: memory OK again**")


def test_a_short_memory_peak_is_not_posted(server):
    server.memory(95)
    server.round()
    server.round()
    server.memory(50)
    server.round()
    server.memory(95)
    server.round()
    server.round()
    assert server.posted() == []


def test_a_check_can_be_turned_off(tmp_path):
    server = Server(tmp_path, {"DISK_ALERT_PERCENT": "0", "MEMORY_ALERT_PERCENT": "0"})
    server.disk(99)
    server.memory(99)
    for _ in range(4):
        server.round()
    assert server.posted() == []


def test_a_certificate_that_ends_soon(server):
    soon = cert_end(9.5)
    server.round(CERT_END=soon)
    assert len(server.posted()) == 1
    text = server.texts()[0]
    assert text.startswith("🔒 **guamheadlines.com: the site's certificate ends on ")
    assert "(in 9 days)" in text
    assert "docker compose logs --tail 50 caddy" in text
    # Checked every 6 hours, and posted once.
    server.round(CERT_END=soon)
    server.forget_times()
    server.round(CERT_END=soon)
    assert len(server.posted()) == 1
    server.forget_times()
    server.round(CERT_END=cert_end(89))
    assert server.texts()[1].startswith("✅ **guamheadlines.com: the site's certificate is renewed")


def test_no_certificate_check_without_a_domain(tmp_path):
    server = Server(tmp_path, {"SITE_DOMAIN": ""})
    result = server.run(CERT_END=cert_end(2))
    assert "Certificate" not in result.stdout
    assert server.posted() == []


def test_the_mail_server_certificate_and_block_lists_need_the_mail_server(server):
    result = server.run(CERT_END=cert_end(60), LISTED="bl.spamcop.net")
    assert "mail server" not in result.stdout
    assert "Block lists" not in result.stdout
    assert server.posted() == []


@pytest.fixture()
def mail_server(tmp_path):
    return Server(tmp_path, {"COMPOSE_PROFILES": "mail", "MAIL_HOSTNAME": "mail.guamheadlines.com"})


def test_block_lists(mail_server):
    result = mail_server.run(CERT_END=cert_end(60), REFUSED="zen.spamhaus.org")
    assert "Certificate of the mail server (mail.guamheadlines.com)" in result.stdout
    assert "Block lists, for the mail server's address 203.0.113.5:" in result.stdout
    assert "  not on bl.spamcop.net" in result.stdout
    assert "  zen.spamhaus.org did not answer this server's DNS" in result.stdout
    assert mail_server.posted() == []

    mail_server.forget_times()
    mail_server.round(LISTED="bl.spamcop.net zen.spamhaus.org")
    assert len(mail_server.posted()) == 1
    text = mail_server.texts()[0]
    assert text.startswith("🚫 **guamheadlines.com: the mail server is on a spam block list**")
    assert (
        "203.0.113.5 is on:\n• zen.spamhaus.org: removal at https://check.spamhaus.org/\n" in text
    )
    assert "• bl.spamcop.net: removal at https://www.spamcop.net/bl.shtml\n" in text

    # Once a day only, and each listing is posted once.
    mail_server.round(LISTED="bl.spamcop.net zen.spamhaus.org")
    mail_server.forget_times()
    mail_server.round(LISTED="bl.spamcop.net zen.spamhaus.org")
    assert len(mail_server.posted()) == 1

    # Spamhaus does not answer this time: that is no reason to say it's off.
    mail_server.forget_times()
    mail_server.round(LISTED="bl.spamcop.net", REFUSED="zen.spamhaus.org")
    assert len(mail_server.posted()) == 1

    mail_server.forget_times()
    mail_server.round(LISTED="zen.spamhaus.org")
    assert mail_server.texts()[1] == (
        "✅ **guamheadlines.com: the mail server is off the block list**\n"
        "The server's IP address 203.0.113.5 is no longer on: bl.spamcop.net."
    )


def test_a_block_list_alert_that_could_not_be_sent_is_tried_again(mail_server):
    mail_server.round(LISTED="bl.spamcop.net", CURL_FAILS="1")
    mail_server.round(LISTED="bl.spamcop.net")
    assert len(mail_server.posted()) == 2  # the failed try, then the one that went
    mail_server.round(LISTED="bl.spamcop.net")
    assert len(mail_server.posted()) == 2


def test_cleanup_by_hand(server):
    result = server.run("--clean", IMAGES_FREED="1.2GB", CACHE_FREED="300MB")
    assert result.returncode == 0, result.stderr
    assert "Deleted old versions of the program (1.2GB) and old build files (300MB)." in (
        result.stdout
    )
    assert server.texts() == [
        "🧹 **guamheadlines.com: weekly cleanup**\n"
        "Deleted old versions of the program (1.2GB) and old build files (300MB). "
        "The disk now has 25.0 GB free."
    ]
    # Nothing to delete: nothing posted.
    server.run("--clean")
    assert len(server.posted()) == 1


def test_auto_update_runs_the_server_checks():
    script = (ROOT / "scripts" / "auto-update.sh").read_text()
    assert "bash scripts/server-check.sh --round" in script


def test_env_example_has_the_server_check_settings():
    text = (ROOT / ".env.example").read_text()
    for line in (
        "DISK_ALERT_PERCENT=90",
        "MEMORY_ALERT_PERCENT=90",
        "CERT_ALERT_DAYS=14",
        "REPORT_TIME=8:00",
        "DAILY_REPORT=true",
        "WEEKLY_REPORT_DAY=monday",
    ):
        assert f"\n{line}\n" in text, line


def test_private_files_are_made_readable_by_their_owner_only(server):
    env_file = server.root / ".env"
    env_file.chmod(0o644)
    (server.root / "backups").mkdir(mode=0o755, exist_ok=True)
    (server.root / "backups").chmod(0o755)
    result = server.round(CERT_END=cert_end(60))
    assert env_file.stat().st_mode & 0o777 == 0o600
    assert (server.root / "backups").stat().st_mode & 0o777 == 0o700
    assert "made .env private: only its owner can read it now (it was 644)" in result.stdout
    assert "made backups private" in result.stdout
    # Already private: nothing to do, nothing said.
    assert "private" not in server.round(CERT_END=cert_end(60)).stdout
    assert server.posted() == []


def test_a_restart_for_linux_updates_is_posted_once(server):
    (server.fake / "reboot-required").write_text("*** System restart required ***\n")
    server.round(CERT_END=cert_end(60))
    server.round(CERT_END=cert_end(60))
    assert len(server.texts()) == 1
    assert server.texts()[0].startswith("🔁 **guamheadlines.com: the server needs a restart**")
    assert "`sudo reboot`" in server.texts()[0]
    assert "Restart needed: yes" in server.run(CERT_END=cert_end(60)).stdout
    # After the restart the flag is gone; a later one is posted again.
    (server.fake / "reboot-required").unlink()
    server.round(CERT_END=cert_end(60))
    (server.fake / "reboot-required").write_text("")
    server.round(CERT_END=cert_end(60))
    assert len(server.texts()) == 2


def test_auto_update_brings_security_updates_of_all_programs_weekly():
    script = (ROOT / "scripts" / "auto-update.sh").read_text()
    assert 'local services="db caddy backup"' in script
    assert "docker compose build --pull --quiet" in script
    assert "\n    update_images\n" in script
    assert "update_mail" not in script


def test_database_backups_are_private():
    assert "\numask 077\n" in (ROOT / "scripts" / "backup.sh").read_text()
