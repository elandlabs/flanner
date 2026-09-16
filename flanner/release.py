"""What changed since last time, and whether a newer release exists.

Two questions a person has after `uv tool upgrade flanner`, and neither
had an answer before this: what did I just get, and am I behind?

Both are answered from a small json file beside the store. It is read on
every command, so this module imports nothing of flanner's and nothing
that costs anything to import. It also runs before the database is
opened, which is why it keeps its own idea of where the home directory
is rather than reaching for one.

The network half is off until somebody says otherwise. A tool whose
sidebar says nothing leaves your disk cannot quietly start announcing
itself to pypi.org once a day, so the check is asked for at `init`,
recorded, and skippable forever. Nothing about the machine, the
repository or the plans is sent: it is a GET of a public json document.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

#: Beside data.db, not inside it: this is read before the store opens.
STATE_FILE = "state.json"

#: A public document that changes on release day. Asking more often than
#: this costs somebody a request and tells them nothing new.
PYPI_URL = "https://pypi.org/pypi/flanner/json"
CHECK_EVERY = timedelta(hours=24)

#: Short on purpose. This runs inside a command somebody is waiting on,
#: and being told about a release is never worth making them wait.
TIMEOUT_SECONDS = 2.0


def _home() -> Path:
    """Where flanner keeps its files. Read directly, see the module docstring."""
    return Path(os.environ.get("FLANNER_HOME", Path.home() / ".flanner"))


def read_state() -> dict[str, Any]:
    """The state file, or an empty one. A corrupt file is treated as empty.

    Deliberately forgiving: nothing in here is worth failing a command
    over, and the next write repairs it.
    """
    try:
        loaded = json.loads((_home() / STATE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def write_state(state: dict[str, Any]) -> None:
    """Save the state file, or give up quietly if the home is not writable."""
    try:
        home = _home()
        home.mkdir(parents=True, exist_ok=True)
        (home / STATE_FILE).write_text(json.dumps(state, indent=2), encoding="utf-8")
    except OSError:
        return


def upgraded_from(current: str) -> str | None:
    """The version this machine ran last, when it is not the one running now.

    None on a first install. Arriving is not upgrading, and somebody who
    has just installed flanner does not want release notes for a version
    they never had.
    """
    last = read_state().get("version")
    if not isinstance(last, str) or not last or last == current:
        return None
    return last


def remember_version(current: str) -> None:
    """Record the version that ran, so the next change is noticed once."""
    state = read_state()
    state["version"] = current
    write_state(state)


def update_check_consent() -> bool | None:
    """Whether the PyPI check was allowed, or None if nobody has been asked."""
    answer = read_state().get("update_check")
    return answer if isinstance(answer, bool) else None


def set_update_check_consent(allowed: bool) -> None:
    state = read_state()
    state["update_check"] = allowed
    write_state(state)


def _parts(version: str) -> tuple[int, ...]:
    """The leading numbers of a version, for comparing two of them.

    A version this cannot parse returns empty, and an empty one never
    compares as newer. Failing towards saying nothing is the right way
    round: a wrong "you are behind" is worse than a missed release.
    """
    numbers: list[int] = []
    for piece in version.split("."):
        digits = ""
        for char in piece:
            if not char.isdigit():
                break
            digits += char
        if not digits:
            break
        numbers.append(int(digits))
    return tuple(numbers)


def is_newer(candidate: str, than: str) -> bool:
    left, right = _parts(candidate), _parts(than)
    return bool(left) and bool(right) and left > right


def newer_release(current: str, *, now: datetime | None = None) -> str | None:
    """The version on PyPI when it is newer than this one, else None.

    Answers from the cache when it was filled recently, so a person
    running several commands pays for at most one request a day. Every
    failure - no consent, no network, a slow mirror, a reply that is not
    what was expected - is silent. Not being told about a release is a
    smaller harm than an error nobody can act on.
    """
    if update_check_consent() is not True:
        return None
    now = now or datetime.now(timezone.utc)
    state = read_state()
    latest = state.get("latest")
    checked = state.get("checked_at")
    fresh = False
    if isinstance(checked, str):
        try:
            fresh = datetime.fromisoformat(checked) > now - CHECK_EVERY
        except ValueError:
            fresh = False
    if not fresh:
        latest = _fetch_latest()
        if latest is None:
            return None
        state["latest"] = latest
        state["checked_at"] = now.isoformat()
        write_state(state)
    if not isinstance(latest, str):
        return None
    return latest if is_newer(latest, current) else None


def _fetch_latest() -> str | None:
    """The newest version PyPI lists, or None for any failure at all."""
    import urllib.request

    try:
        request = urllib.request.Request(  # noqa: S310 - literal https constant above
            PYPI_URL, headers={"Accept": "application/json"}
        )
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as reply:  # noqa: S310
            payload = json.loads(reply.read().decode("utf-8"))
    except Exception:  # noqa: BLE001 - a version check must never fail a command
        return None
    version = payload.get("info", {}).get("version")
    return version if isinstance(version, str) else None
