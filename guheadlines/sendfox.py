"""Newsletter sign-ups: the sign-up box on the website adds a reader's email
address to a SendFox list, through SendFox's API (https://api.sendfox.com).

The box is a plain form that the browser sends to this site (/newsletter); the
server passes the address on to SendFox with SENDFOX_API_TOKEN. So the pages
load no SendFox script, the reader's browser talks only to this site, and the
site's security rules (Content-Security-Policy) stay as strict as before.

SENDFOX_LIST names the list that new subscribers join: its name as SendFox
shows it, or its number. Empty: the account's only list. `guheadlines
sendfox-check` shows the account's lists and the one that sign-ups join.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass

import httpx

from .settings import Settings

log = logging.getLogger(__name__)

API = "https://api.sendfox.com"
TIMEOUT = 10.0
# The list's number is looked up again after this many seconds, in case the
# list was renamed or made again in SendFox.
LIST_CACHE_SECONDS = 3600
MAX_LIST_PAGES = 20


class SignupError(Exception):
    """SendFox could not add the address. The message says why, for the log."""


class InvalidEmail(SignupError):
    """SendFox does not accept the address."""


@dataclass(frozen=True)
class SendFoxList:
    id: int
    name: str


def _client(settings: Settings, transport: httpx.BaseTransport | None) -> httpx.Client:
    return httpx.Client(
        base_url=API,
        headers={
            "Authorization": f"Bearer {settings.sendfox_api_token}",
            "Accept": "application/json",
            "User-Agent": settings.user_agent,
        },
        timeout=httpx.Timeout(TIMEOUT),
        transport=transport,
    )


def _json(response: httpx.Response):
    if response.status_code == 401:
        raise SignupError(
            "SendFox refused SENDFOX_API_TOKEN (401). Make a new token in SendFox and put it"
            " in .env"
        )
    if response.status_code == 402:
        raise SignupError("SendFox says the plan of the account does not allow this (402)")
    if response.status_code >= 400:
        raise SignupError(f"SendFox answered {response.status_code}: {response.text[:200]}")
    try:
        return response.json()
    except ValueError as exc:
        raise SignupError("SendFox answered with something that is not JSON") from exc


def account(settings: Settings, transport: httpx.BaseTransport | None = None) -> dict:
    """The SendFox account of the token (its email address and name)."""
    try:
        with _client(settings, transport) as client:
            data = _json(client.get("/me"))
    except httpx.HTTPError as exc:
        raise SignupError(f"SendFox could not be reached: {exc}") from exc
    return data if isinstance(data, dict) else {}


def lists(settings: Settings, transport: httpx.BaseTransport | None = None) -> list[SendFoxList]:
    """All the lists of the account. SendFox gives them a page at a time."""
    found: list[SendFoxList] = []
    try:
        with _client(settings, transport) as client:
            for page in range(1, MAX_LIST_PAGES + 1):
                data = _json(client.get("/lists", params={"page": page}))
                rows = data.get("data", []) if isinstance(data, dict) else data
                for row in rows if isinstance(rows, list) else []:
                    if isinstance(row, dict) and isinstance(row.get("id"), int):
                        found.append(SendFoxList(row["id"], str(row.get("name") or "")))
                last = data.get("last_page", 1) if isinstance(data, dict) else 1
                if not isinstance(last, int) or page >= last:
                    break
    except httpx.HTTPError as exc:
        raise SignupError(f"SendFox could not be reached: {exc}") from exc
    return found


def pick_list(wanted: str, available: list[SendFoxList]) -> SendFoxList | None:
    """The list that SENDFOX_LIST names: its number, or its name (capitals and
    extra spaces do not matter). Empty: the only list, if there is one.
    Raises SignupError when the list is not there."""
    wanted = " ".join(wanted.split())
    if not wanted:
        if len(available) == 1:
            return available[0]
        if not available:
            return None  # no list yet: the address is still added to the contacts
        names = ", ".join(f'"{x.name}"' for x in available)
        raise SignupError(f"the account has several lists ({names}); put one in SENDFOX_LIST")
    for item in available:
        if wanted == str(item.id) or wanted.casefold() == " ".join(item.name.split()).casefold():
            return item
    names = ", ".join(f'"{x.name}" ({x.id})' for x in available) or "none"
    raise SignupError(f'SendFox has no list "{wanted}" (SENDFOX_LIST). Its lists: {names}')


_list_lock = threading.Lock()
_list_cache: dict[tuple[str, str], tuple[float, SendFoxList | None]] = {}


def signup_list(
    settings: Settings, transport: httpx.BaseTransport | None = None
) -> SendFoxList | None:
    """The list that sign-ups join, looked up at most once an hour."""
    key = (settings.sendfox_api_token, settings.sendfox_list)
    with _list_lock:
        cached = _list_cache.get(key)
    if cached and time.monotonic() - cached[0] < LIST_CACHE_SECONDS:
        return cached[1]
    picked = pick_list(settings.sendfox_list, lists(settings, transport))
    with _list_lock:
        _list_cache[key] = (time.monotonic(), picked)
    return picked


def add_contact(
    settings: Settings, email: str, transport: httpx.BaseTransport | None = None
) -> None:
    """Add one address to the list. SendFox keeps one contact per address, so
    signing up twice does no harm. Raises InvalidEmail or SignupError."""
    picked = signup_list(settings, transport)
    body: dict = {"email": email}
    if picked is not None:
        body["lists"] = [picked.id]
    try:
        with _client(settings, transport) as client:
            response = client.post("/contacts", json=body)
    except httpx.HTTPError as exc:
        raise SignupError(f"SendFox could not be reached: {exc}") from exc
    if response.status_code == 422:
        raise InvalidEmail(f"SendFox did not accept the address: {response.text[:200]}")
    if response.status_code == 404 and picked is not None:
        # The list was deleted since it was looked up: look again next time.
        with _list_lock:
            _list_cache.clear()
    _json(response)
