"""The optional mail server and webmail in docker-compose.yml (docs/email-server.md)."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
COMPOSE = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
SERVICES = COMPOSE["services"]
MAIL_SERVICES = ("mailserver", "webmail")


def test_mail_services_start_only_with_the_mail_profile():
    for name in MAIL_SERVICES:
        assert SERVICES[name]["profiles"] == ["mail"]
    for name, service in SERVICES.items():
        if name not in MAIL_SERVICES:
            assert "profiles" not in service, name


def test_mail_services_get_no_site_secrets():
    # .env holds the database password and the PayPal and other keys.
    for name in MAIL_SERVICES:
        assert "env_file" not in SERVICES[name], name


def test_webmail_is_reached_only_through_caddy():
    assert "ports" not in SERVICES["webmail"]
    caddyfile = (ROOT / "caddy" / "Caddyfile").read_text()
    assert "reverse_proxy webmail:80" in caddyfile


def test_without_mail_hostname_caddy_asks_for_no_certificate():
    address = SERVICES["caddy"]["environment"]["WEBMAIL_ADDRESS"]
    assert address == "${MAIL_HOSTNAME:-http://mail.invalid}"


def test_mail_server_reads_the_lets_encrypt_certificate_caddy_gets():
    env = SERVICES["mailserver"]["environment"]
    issuer_dir = "acme-v02.api.letsencrypt.org-directory"
    assert env["SSL_TYPE"] == "manual"
    assert f"/caddy-data/caddy/certificates/{issuer_dir}/" in env["SSL_CERT_PATH"]
    assert "caddy_data:/caddy-data:ro" in SERVICES["mailserver"]["volumes"]
    # Caddy must not switch to another issuer for this name, or the path
    # changes and the mail server loses its certificate.
    caddyfile = (ROOT / "caddy" / "Caddyfile").read_text()
    webmail_block = caddyfile.split("{$WEBMAIL_ADDRESS} {", 1)[1]
    assert re.search(r"tls \{\s*issuer acme\s*\}", webmail_block)
    script = (ROOT / "scripts" / "mail.sh").read_text()
    assert f'CERT_DIR="/data/caddy/certificates/{issuer_dir}"' in script


def test_site_and_webmail_reach_the_mail_server_by_its_own_name():
    aliases = SERVICES["mailserver"]["networks"]["default"]["aliases"]
    assert aliases == ["${MAIL_HOSTNAME:-mail.localhost}"]
    env = SERVICES["webmail"]["environment"]
    assert env["ROUNDCUBEMAIL_DEFAULT_HOST"] == "ssl://${MAIL_HOSTNAME:-mail.localhost}"
    assert env["ROUNDCUBEMAIL_SMTP_SERVER"] == "tls://${MAIL_HOSTNAME:-mail.localhost}"


def test_fail2ban_never_blocks_the_containers_that_sign_in():
    jail = (ROOT / "mail" / "fail2ban-jail.cf").read_text()
    ignored = re.search(r"^ignoreip = (.+)$", jail, re.M).group(1).split()
    for name in ("webmail", "web", "worker"):
        assert name in ignored
        assert name in SERVICES
    assert (
        "./mail/fail2ban-jail.cf:/tmp/docker-mailserver/fail2ban-jail.cf:ro"
        in (SERVICES["mailserver"]["volumes"])
    )


def test_mail_volumes_are_declared():
    for volume in ("mail_data", "mail_state", "mail_logs", "mail_config", "webmail_db"):
        assert volume in COMPOSE["volumes"]


def test_env_example_has_the_mail_settings_off():
    lines = (ROOT / ".env.example").read_text().splitlines()
    assert "COMPOSE_PROFILES=" in lines
    assert "MAIL_HOSTNAME=" in lines
