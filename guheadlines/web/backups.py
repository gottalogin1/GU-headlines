"""The nightly backups, for the private stats page (/stats).

The "backup" service in docker-compose.yml (scripts/backup-service.sh) makes
them: each night a folder backups/site/<date>-<time>, such as
2026-11-26-0300, with the database, the photos, .env and the rest. Its
"info" file says when it was made, its size and what is in it. The service
also writes backups/site/status after each run. The website sees the folder
read-only, at BACKUP_DIR (/data/backups in Docker).

A backup is downloaded as one .tar file, made while it is sent: nothing is
written to disk for it. The .tar holds one folder, site-backup-<name>, the
same as `scripts/site-backup.sh export` makes, so `scripts/site-backup.sh
restore` takes either.
"""

from __future__ import annotations

import os
import re
import stat
import tarfile
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, time, timezone
from pathlib import Path

from ..scraper.text import GUAM_TZ

NAME = re.compile(r"\d{4}-\d{2}-\d{2}-\d{4}")
FILE_NAME = re.compile(r"site-backup-(\d{4}-\d{2}-\d{2}-\d{4})\.tar")
# The parts of a backup, in the words the stats page uses.
PART_NAMES = {
    "database": "database",
    "photos": "photos",
    "settings": "settings (.env)",
    "sponsor": "sponsor picture",
    "certificates": "https certificates",
    "mail": "mailboxes",
    "webmail": "webmail",
}
# A backup holds every password of the site, so the password that opens the
# download must be hard to guess.
MIN_PASSWORD = 12
BLOCK = tarfile.BLOCKSIZE  # 512
CHUNK = 1 << 20


def human_size(size: int) -> str:
    """2254857830 -> "2.1 GB"."""
    if size >= 1 << 30:
        return f"{size / (1 << 30):.1f} GB"
    if size >= 1 << 20:
        return f"{size / (1 << 20):.0f} MB"
    return f"{max(size, 0) // 1024} KB"


def clock_text(at: time | None) -> str:
    """03:00 -> "3:00 AM"; "" for None."""
    if at is None:
        return ""
    return f"{at.hour % 12 or 12}:{at.minute:02d} {'AM' if at.hour < 12 else 'PM'}"


@dataclass
class Backup:
    name: str
    made: datetime
    size: int  # bytes, without the .tar's own headers
    parts: list[str] = field(default_factory=list)

    @property
    def file_name(self) -> str:
        return f"site-backup-{self.name}.tar"

    @property
    def size_text(self) -> str:
        return human_size(self.size)

    @property
    def parts_text(self) -> str:
        return ", ".join(PART_NAMES.get(part, part) for part in self.parts)


@dataclass
class Status:
    result: str  # "ok" or "failed"
    time: datetime | None
    message: str
    last_ok: datetime | None
    used: int  # bytes used by all the backups (a shared photo counts once)
    free: int  # bytes free on the disk

    @property
    def failed(self) -> bool:
        return self.result == "failed"


def _values(path: Path) -> dict[str, str]:
    """The NAME=value lines of a small text file; {} if it can't be read."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")[:20_000]
    except OSError:
        return {}
    values = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            values[key.strip()] = value.strip()
    return values


def _int(value: str | None) -> int:
    try:
        return int(value or 0)
    except ValueError:
        return 0


def _moment(seconds: str | None) -> datetime | None:
    number = _int(seconds)
    return datetime.fromtimestamp(number, timezone.utc) if number > 0 else None


def _made(name: str, made: str | None) -> datetime:
    try:
        return datetime.fromisoformat(made or "")
    except ValueError:
        return datetime.strptime(name, "%Y-%m-%d-%H%M").replace(tzinfo=GUAM_TZ)


def find(folder: Path) -> list[Backup]:
    """The finished backups in folder, newest first."""
    try:
        entries = [entry for entry in os.scandir(folder) if NAME.fullmatch(entry.name)]
    except OSError:
        return []
    found = []
    for entry in entries:
        if not entry.is_dir(follow_symlinks=False):
            continue
        info = _values(Path(entry.path) / "info")
        found.append(
            Backup(
                name=entry.name,
                made=_made(entry.name, info.get("made")),
                size=_int(info.get("bytes")),
                parts=info.get("parts", "").split(),
            )
        )
    return sorted(found, key=lambda b: b.name, reverse=True)


def read_status(folder: Path) -> Status | None:
    values = _values(folder / "status")
    if not values.get("result"):
        return None
    return Status(
        result=values["result"],
        time=_moment(values.get("time")),
        message=values.get("message", ""),
        last_ok=_moment(values.get("last_ok")),
        used=_int(values.get("used_kb")) * 1024,
        free=_int(values.get("free_kb")) * 1024,
    )


def download_problem(settings, scheme: str) -> str:
    """Why backups can't be downloaded here, or "" when they can."""
    if scheme != "https":
        return (
            "Downloads work only over https (with SITE_DOMAIN set), because a backup holds"
            " all your passwords."
        )
    if len(settings.stats_password) < MIN_PASSWORD:
        return (
            f"Downloads need a STATS_PASSWORD of at least {MIN_PASSWORD} letters and digits,"
            " because a backup holds all your passwords. Make a longer one (see .env.example),"
            " then run: docker compose up -d"
        )
    return ""


# ------------------------------------------------------------- the download


@dataclass
class _Member:
    path: Path
    info: tarfile.TarInfo


def _header(info: tarfile.TarInfo) -> bytes:
    # The GNU form: GNU tar, BusyBox, 7-Zip and Windows all read it, also for
    # long names.
    return info.tobuf(tarfile.GNU_FORMAT, "utf-8", "surrogateescape")


def _members(folder: Path, top: str) -> list[_Member]:
    """Every folder and file in folder, as .tar entries under top/. Links and
    other special files are left out (a backup has none)."""
    members = []

    def add(path: Path, name: str, st: os.stat_result) -> None:
        info = tarfile.TarInfo(name)
        info.mode = stat.S_IMODE(st.st_mode)
        info.uid, info.gid = st.st_uid, st.st_gid
        info.mtime = int(st.st_mtime)
        if stat.S_ISDIR(st.st_mode):
            info.type = tarfile.DIRTYPE
        else:
            info.type = tarfile.REGTYPE
            info.size = st.st_size
        members.append(_Member(path, info))

    add(folder, top, folder.lstat())
    for root, dirs, files in os.walk(folder):
        dirs.sort()
        base = Path(root)
        relative = base.relative_to(folder).as_posix()
        prefix = top if relative == "." else f"{top}/{relative}"
        for name in [*sorted(files), *dirs]:
            path = base / name
            st = path.lstat()
            if stat.S_ISDIR(st.st_mode) or stat.S_ISREG(st.st_mode):
                add(path, f"{prefix}/{name}", st)
        # os.walk does not follow links to folders; drop them from the walk.
        dirs[:] = [d for d in dirs if not (base / d).is_symlink()]
    return members


def _padding(size: int) -> int:
    return -size % BLOCK


def tar_size(members: list[_Member]) -> int:
    total = BLOCK * 2  # the two empty blocks that end a .tar
    for member in members:
        total += len(_header(member.info))
        if member.info.isreg():
            total += member.info.size + _padding(member.info.size)
    return total


def prepare(folder: Path, name: str) -> tuple[int, Iterator[bytes]] | None:
    """(the size of the .tar, its bytes) for backup `name` in folder, or None
    when there is no such backup."""
    if not NAME.fullmatch(name):
        return None
    path = folder / name
    if path.is_symlink() or not path.is_dir():
        return None
    members = _members(path, f"site-backup-{name}")
    return tar_size(members), _stream(members)


def _stream(members: list[_Member]) -> Iterator[bytes]:
    for member in members:
        yield _header(member.info)
        if not member.info.isreg():
            continue
        left = member.info.size
        # A file that went missing or changed size (an old backup deleted
        # while it is downloaded) raises, and the download stops unfinished,
        # rather than giving a damaged file that looks whole.
        with open(member.path, "rb") as file:
            while left:
                chunk = file.read(min(CHUNK, left))
                if not chunk:
                    raise OSError(f"{member.path} became shorter during the download")
                left -= len(chunk)
                yield chunk
        if _padding(member.info.size):
            yield b"\0" * _padding(member.info.size)
    yield b"\0" * (BLOCK * 2)
