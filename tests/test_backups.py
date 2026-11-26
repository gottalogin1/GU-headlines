"""The nightly backups: settings, the stats page's list, and the download.

The backup service itself (scripts/backup-service.sh, run by Docker) is
tested by the Docker Compose smoke test in .github/workflows/ci.yml, which
backs up, exports and restores a running site."""

from __future__ import annotations

import base64
import dataclasses
import io
import os
import re
import tarfile
from datetime import time
from pathlib import Path

import pytest
import yaml
from conftest import ROOT
from fastapi.testclient import TestClient

from guheadlines.settings import Settings, _backup_time, load_settings
from guheadlines.web import backups

PASSWORD = "a long stats password"


def make_backup(folder: Path, name: str = "2026-11-26-0300", photos: int = 3) -> Path:
    """A backup folder as scripts/backup-service.sh makes it."""
    path = folder / name
    (path / "photos" / "images" / "2026" / "11").mkdir(parents=True)
    (path / "config" / "sponsor").mkdir(parents=True)
    (path / "database.sql.gz").write_bytes(os.urandom(3000))
    (path / "env").write_text("POSTGRES_PASSWORD=secret\n")
    (path / "config" / "sponsor" / "README.md").write_text("Put the picture here.\n")
    for n in range(photos):
        (path / "photos" / "images" / "2026" / "11" / f"p{n}.webp").write_bytes(os.urandom(700 + n))
    # A long name needs the GNU form's extra header.
    long_dir = path / "photos" / ("x" * 90)
    long_dir.mkdir()
    (long_dir / ("y" * 60 + ".webp")).write_bytes(b"long")
    (path / "info").write_text(
        f"name={name}\nmade=2026-11-26T03:00:04+10:00\n"
        "parts=database photos settings certificates\nphotos=3\nbytes=2254857830\n"
    )
    return path


# ---------------------------------------------------------------- settings


def test_backup_time_setting():
    assert _backup_time(None, time(3, 0)) == time(3, 0)
    assert _backup_time("", time(3, 0)) is None
    assert _backup_time("  ", time(3, 0)) is None
    assert _backup_time("02:30", time(3, 0)) == time(2, 30)
    assert _backup_time("2:30", time(3, 0)) == time(2, 30)
    assert _backup_time("23:59", time(3, 0)) == time(23, 59)
    # Not a time like 03:00: the default, as the backup service reads it.
    for wrong in ("24:00", "3", "3 am", "03:60", "noon"):
        assert _backup_time(wrong, time(3, 0)) == time(3, 0)


def test_backup_settings_are_read(monkeypatch):
    for name in ("BACKUP_TIME", "BACKUP_KEEP_DAYS", "BACKUP_DIR"):
        monkeypatch.delenv(name, raising=False)
    settings = load_settings()
    assert settings.backup_time == time(3, 0)
    assert settings.backup_keep_days == 14
    assert settings.backup_dir == ROOT / "backups" / "site"
    monkeypatch.setenv("BACKUP_TIME", "")
    monkeypatch.setenv("BACKUP_KEEP_DAYS", "0")
    monkeypatch.setenv("BACKUP_DIR", "/data/backups")
    settings = load_settings()
    assert settings.backup_time is None
    assert settings.backup_keep_days == 1
    assert settings.backup_dir == Path("/data/backups")
    monkeypatch.setenv("BACKUP_KEEP_DAYS", "9999")
    assert load_settings().backup_keep_days == 365


def test_env_example_has_the_backup_settings_with_their_defaults():
    lines = (ROOT / ".env.example").read_text().splitlines()
    assert "BACKUP_TIME=03:00" in lines
    assert f"BACKUP_KEEP_DAYS={Settings.backup_keep_days}" in lines
    assert Settings.backup_time == time(3, 0)


def test_compose_runs_the_backup_service():
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    backup = compose["services"]["backup"]
    # The database's own image: its pg_dump matches the database's version.
    assert backup["image"] == compose["services"]["db"]["image"]
    assert backup["entrypoint"] == ["sh", "/site/scripts/backup-service.sh"]
    assert backup["command"] == ["daemon"]
    assert ".:/site:ro" in backup["volumes"]
    assert "./backups:/backups" in backup["volumes"]
    assert backup["environment"]["BACKUP_TIME"] == "${BACKUP_TIME-03:00}"
    assert "profiles" not in backup  # always on
    # The website sees the backups read-only, for the stats page.
    web = compose["services"]["web"]
    assert "./backups/site:/data/backups:ro" in web["volumes"]
    assert web["environment"]["BACKUP_DIR"] == "/data/backups"
    assert "./backups/site" not in str(compose["services"]["worker"]["volumes"])
    # The scripts that the service and the docs name exist.
    assert (ROOT / "scripts" / "backup-service.sh").is_file()
    assert (ROOT / "scripts" / "site-backup.sh").is_file()


def test_backup_parts_are_the_same_in_both_scripts():
    service = (ROOT / "scripts" / "backup-service.sh").read_text()
    host = (ROOT / "scripts" / "site-backup.sh").read_text()
    order = re.search(r'^RESTORE_ORDER="([^"]+)"', service, re.M)[1].split()
    parts = re.search(r'^ALL_PARTS="([^"]+)"', host, re.M)[1].split()
    assert sorted(order) == sorted(parts) == sorted(backups.PART_NAMES)
    assert order[0] == "database" and order[-1] == "settings"


# ------------------------------------------------------------------- reading


def test_backups_are_listed_newest_first(tmp_path):
    make_backup(tmp_path, "2026-11-25-0300")
    make_backup(tmp_path, "2026-11-26-0300")
    (tmp_path / ".new-2026-11-27-0300").mkdir()  # still being made
    (tmp_path / "2026-11-24-0300").write_text("not a folder")
    (tmp_path / "2026-11-23-0300").symlink_to(tmp_path / "2026-11-25-0300")
    found = backups.find(tmp_path)
    assert [b.name for b in found] == ["2026-11-26-0300", "2026-11-25-0300"]
    newest = found[0]
    assert newest.made.isoformat() == "2026-11-26T03:00:04+10:00"
    assert newest.size_text == "2.1 GB"
    assert newest.file_name == "site-backup-2026-11-26-0300.tar"
    assert newest.parts_text == "database, photos, settings (.env), https certificates"
    assert backups.find(tmp_path / "missing") == []


def test_a_backup_without_info_is_still_listed(tmp_path):
    make_backup(tmp_path).joinpath("info").unlink()
    (backup,) = backups.find(tmp_path)
    assert backup.made.isoformat() == "2026-11-26T03:00:00+10:00"
    assert backup.size == 0 and backup.parts == []


def test_status_file(tmp_path):
    assert backups.read_status(tmp_path) is None
    (tmp_path / "status").write_text(
        "result=failed\ntime=1795699439\nmessage=Not enough disk space: 1.0 GB free.\n"
        "last_ok=1795600000\nlast_ok_name=2026-11-25-0300\nused_kb=4208\nfree_kb=1048576\n"
    )
    status = backups.read_status(tmp_path)
    assert status.failed
    assert status.message == "Not enough disk space: 1.0 GB free."
    assert status.time.timestamp() == 1795699439
    assert status.last_ok.timestamp() == 1795600000
    assert status.used == 4208 * 1024 and status.free == 1 << 30
    (tmp_path / "status").write_text("result=ok\nlast_ok=\n")
    status = backups.read_status(tmp_path)
    assert not status.failed and status.last_ok is None and status.used == 0


def test_sizes_and_clock():
    assert backups.human_size(500) == "0 KB"
    assert backups.human_size(4_177_803) == "4 MB"
    assert backups.human_size(5 << 30) == "5.0 GB"
    assert backups.clock_text(time(3, 0)) == "3:00 AM"
    assert backups.clock_text(time(15, 5)) == "3:05 PM"
    assert backups.clock_text(time(0, 30)) == "12:30 AM"
    assert backups.clock_text(None) == ""


def test_downloads_need_https_and_a_long_password():
    site = dataclasses.replace(load_settings(), stats_password=PASSWORD)
    assert backups.download_problem(site, "https") == ""
    assert "https" in backups.download_problem(site, "http")
    short = dataclasses.replace(site, stats_password="short1")
    assert "at least 12" in backups.download_problem(short, "https")


# ------------------------------------------------------------- the download


def _untar(data: bytes) -> dict[str, tarfile.TarInfo]:
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        return {member.name: member for member in tar.getmembers()}


def test_the_download_is_a_tar_of_the_whole_backup(tmp_path):
    folder = make_backup(tmp_path)
    (folder / "photos" / "link.webp").symlink_to("/etc/passwd")  # never followed
    size, body = backups.prepare(tmp_path, "2026-11-26-0300")
    data = b"".join(body)
    # The size is known before it is sent, so the browser shows the progress.
    assert len(data) == size
    members = _untar(data)
    top = "site-backup-2026-11-26-0300"
    assert members[top].isdir()
    for path in folder.rglob("*"):
        name = f"{top}/{path.relative_to(folder).as_posix()}"
        if path.is_symlink():
            assert name not in members
            continue
        assert name in members, name
        if path.is_file():
            assert members[name].size == path.stat().st_size
            assert members[name].mode == path.stat().st_mode & 0o7777
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        db = tar.extractfile(f"{top}/database.sql.gz").read()
        long_name = f"{top}/photos/{'x' * 90}/{'y' * 60}.webp"
        assert tar.extractfile(long_name).read() == b"long"
    assert db == (folder / "database.sql.gz").read_bytes()


def test_only_real_backups_can_be_downloaded(tmp_path):
    make_backup(tmp_path)
    (tmp_path / "2026-11-25-0300").symlink_to(tmp_path / "2026-11-26-0300")
    for name in ("2026-11-25-0300", "2026-11-24-0300", "../etc", "status", ""):
        assert backups.prepare(tmp_path, name) is None


def test_a_file_that_shrinks_stops_the_download(tmp_path):
    folder = make_backup(tmp_path)
    size, body = backups.prepare(tmp_path, "2026-11-26-0300")
    (folder / "database.sql.gz").write_bytes(b"short")
    with pytest.raises(OSError):
        b"".join(body)


# ------------------------------------------------------- the stats page


def _login(password: str = PASSWORD) -> dict:
    return {"authorization": "Basic " + base64.b64encode(f"me:{password}".encode()).decode()}


@pytest.fixture()
def stats_site(clean_db, tmp_path, monkeypatch):
    from guheadlines.web import app as web

    site = dataclasses.replace(web.settings, stats_password=PASSWORD, backup_dir=tmp_path)
    monkeypatch.setattr(web, "settings", site)
    monkeypatch.setitem(web.templates.env.globals, "settings", site)
    sent = []
    monkeypatch.setattr(web.alerts, "send", lambda settings, alert: sent.append(alert))
    client = TestClient(web.app, base_url="https://testserver")
    client.alerts = sent
    client.folder = tmp_path
    return client


def test_stats_page_lists_the_backups(stats_site):
    page = stats_site.get("/stats", headers=_login()).text
    assert 'id="backups"' in page
    assert "No backups yet. The first one is made a few minutes after the site starts" in page
    assert "Every night at 3:00 AM (Guam time)" in page

    make_backup(stats_site.folder)
    (stats_site.folder / "status").write_text(
        "result=ok\ntime=1795699439\nmessage=Backed up in 4 seconds.\nlast_ok=1795699439\n"
        "used_kb=4208\nfree_kb=62250708\n"
    )
    page = stats_site.get("/stats", headers=_login()).text
    assert 'href="/stats/backups/site-backup-2026-11-26-0300.tar" download>Download</a>' in page
    assert "2.1&nbsp;GB" in page
    assert "Space used by the backups: 4 MB (59.4 GB free on the disk)" in page
    assert "The last backup failed" not in page

    (stats_site.folder / "status").write_text(
        "result=failed\ntime=1795699439\nmessage=Could not back up the database: boom\n"
        "last_ok=1795600000\nused_kb=4208\nfree_kb=1000\n"
    )
    page = stats_site.get("/stats", headers=_login()).text
    assert '<span class="error">⚠ The last backup failed.</span> <a href="#backups">' in page
    assert "Could not back up the database: boom" in page


def test_stats_page_says_when_nightly_backups_are_off(stats_site, monkeypatch):
    from guheadlines.web import app as web

    site = dataclasses.replace(web.settings, backup_time=None)
    monkeypatch.setitem(web.templates.env.globals, "settings", site)
    page = stats_site.get("/stats", headers=_login()).text
    assert "Nightly backups are off (BACKUP_TIME is empty in .env)" in page


def test_download_a_backup(stats_site):
    make_backup(stats_site.folder)
    url = "/stats/backups/site-backup-2026-11-26-0300.tar"
    assert stats_site.get(url).status_code == 401
    assert stats_site.get(url, headers=_login("wrong")).status_code == 401
    assert stats_site.alerts == []

    response = stats_site.get(url, headers={**_login(), "accept-encoding": "gzip"})
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/x-tar"
    assert response.headers["content-disposition"] == (
        'attachment; filename="site-backup-2026-11-26-0300.tar"'
    )
    assert response.headers["cache-control"] == "private, no-store"
    assert "content-encoding" not in response.headers  # not squeezed again
    assert int(response.headers["content-length"]) == len(response.content)
    assert "site-backup-2026-11-26-0300/env" in _untar(response.content)
    # Each download is posted in Discord.
    (alert,) = stats_site.alerts
    assert alert.title.endswith("a backup was downloaded")
    assert "site-backup-2026-11-26-0300.tar" in alert.message

    for wrong in ("site-backup-2026-11-25-0300.tar", "2026-11-26-0300", "status", "..%2Fenv"):
        assert stats_site.get(f"/stats/backups/{wrong}", headers=_login()).status_code == 404


def test_no_download_over_http_or_with_a_short_password(stats_site, monkeypatch):
    from guheadlines.web import app as web

    make_backup(stats_site.folder)
    url = "/stats/backups/site-backup-2026-11-26-0300.tar"
    stats_site.base_url = "http://testserver"
    response = stats_site.get(url, headers=_login())
    assert response.status_code == 403 and "https" in response.text
    page = stats_site.get("/stats", headers=_login()).text
    assert "Downloads work only over https" in page
    assert url not in page

    stats_site.base_url = "https://testserver"
    short = dataclasses.replace(web.settings, stats_password="short1")
    monkeypatch.setattr(web, "settings", short)
    response = stats_site.get(url, headers=_login("short1"))
    assert response.status_code == 403 and "at least 12" in response.text
    assert stats_site.alerts == []


def test_no_backup_pages_while_stats_are_off(stats_site, monkeypatch):
    from guheadlines.web import app as web

    make_backup(stats_site.folder)
    monkeypatch.setattr(web, "settings", dataclasses.replace(web.settings, stats_password=""))
    url = "/stats/backups/site-backup-2026-11-26-0300.tar"
    assert stats_site.get(url, headers=_login()).status_code == 404
