"""What changed since last time, and whether a newer release exists.

Two questions a person has after `uv tool upgrade flanner`, and neither
had an answer before this: what did I just get, and am I behind?

Both are answered from a small json file beside the store. It is read on
every command, so this module imports nothing of flanner's and nothing
that costs anything to import. It also runs before the database is
opened, which is why it keeps its own idea of where the home directory
is rather than reaching for one.

The network half is off until somebody says otherwise. A tool that
promises nothing is sent unless you turn it on cannot quietly start
announcing itself to pypi.org once a day, so the check is asked for at `init`,
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

#: How often the notice is repeated. Somebody who has chosen not to
#: upgrade today should not be told again by every command they run.
TELL_EVERY = timedelta(hours=24)

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
    if not isinstance(last, str) or not last:
        return None
    # Only forwards. Pinning back to an older version is a decision
    # somebody made, not news to announce, and on a machine whose
    # metadata disagrees with itself - an editable install whose
    # dist-info was never refreshed - announcing both directions would
    # fire on every other command forever.
    return last if is_newer(current, last) else None


def remember_version(current: str) -> None:
    """Record the highest version this machine has run.

    The highest, not the last. An editable install whose dist-info was
    never refreshed reports one version from the command and another
    from an import of the same source, so `last` would flip on every
    other run and announce an upgrade each time it flipped up. A
    high-water mark announces each release once, whatever order the
    entry points are used in.
    """
    state = read_state()
    seen = state.get("version")
    if isinstance(seen, str) and seen and not is_newer(current, seen):
        return
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


def known_newer(current: str) -> str | None:
    """A newer version, if the cache already knows of one. Never any network.

    This is what a command calls. Asking PyPI on the command somebody is
    waiting on would make them pay for the answer, and the answer is worth
    nothing to them right now: the next command shows it just as well.
    """
    if update_check_consent() is not True:
        return None
    latest = read_state().get("latest")
    if not isinstance(latest, str):
        return None
    return latest if is_newer(latest, current) else None


def _due(state: dict[str, Any], key: str, every: timedelta, now: datetime) -> bool:
    """Whether `key` is missing or older than `every`. A bad value is due."""
    stamp = state.get(key)
    if not isinstance(stamp, str):
        return True
    try:
        return datetime.fromisoformat(stamp) <= now - every
    except ValueError:
        return True


def due_to_tell(now: datetime | None = None) -> bool:
    """Whether the notice has gone unsaid long enough to say again.

    Once a day, not once a command. Somebody who has chosen not to upgrade
    today should not be told again by every command they run.
    """
    return _due(read_state(), "told_at", TELL_EVERY, now or datetime.now(timezone.utc))


def mark_told(now: datetime | None = None) -> None:
    state = read_state()
    state["told_at"] = (now or datetime.now(timezone.utc)).isoformat()
    write_state(state)


def refresh_in_background(now: datetime | None = None) -> bool:
    """Start a detached check, if one is due. Returns whether one was started.

    Out of process and never waited on. A thread would be killed when a
    fast command exits, and waiting for the reply would charge somebody a
    round trip for news they did not ask for. The answer lands in the
    cache and the next command reads it.

    `attempted_at` is written here rather than by the child, so a machine
    with no network tries once a day instead of on every command.
    """
    if update_check_consent() is not True:
        return False
    now = now or datetime.now(timezone.utc)
    state = read_state()
    if not _due(state, "checked_at", CHECK_EVERY, now):
        return False
    if not _due(state, "attempted_at", CHECK_EVERY, now):
        return False
    state["attempted_at"] = now.isoformat()
    write_state(state)
    return _spawn_check()


def _spawn_check() -> bool:
    """Run `fetch_and_store` in a child that outlives this command."""
    return spawn_detached("from flanner import release; release.fetch_and_store()")


def spawn_detached(code: str) -> bool:
    """Run `code` in a Python child that outlives this command, and do not wait.

    `code` is always a literal from this package, never anything a person
    or a file supplied.
    """
    import subprocess
    import sys

    if not sys.executable:
        return False
    flags = 0
    if hasattr(subprocess, "CREATE_NO_WINDOW"):  # Windows: no console flash
        flags |= subprocess.CREATE_NO_WINDOW
    if hasattr(subprocess, "DETACHED_PROCESS"):
        flags |= subprocess.DETACHED_PROCESS
    try:
        subprocess.Popen(  # noqa: S603 - sys.executable and a literal from this package
            [sys.executable, "-c", code],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=flags,
            start_new_session=True,
        )
    except OSError:
        return False
    return True


def fetch_and_store(now: datetime | None = None) -> str | None:
    """Ask PyPI and record the answer. The entry point of the detached child."""
    latest = _fetch_latest()
    if latest is None:
        return None
    state = read_state()
    state["latest"] = latest
    state["checked_at"] = (now or datetime.now(timezone.utc)).isoformat()
    write_state(state)
    return latest


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
