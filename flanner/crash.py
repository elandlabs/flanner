"""Crash reports, sent only if somebody turned them on.

A crash on somebody's machine used to be invisible unless they opened an
issue, and most people do not. This is the one way flanner tells its
developers something went wrong, and it is built around what it must never
say.

**What a report is.** The error's type and where in the code it happened:
module, function and line, with file paths relative to the package. Plus
the flanner version, the platform, how flanner was installed, which surface
crashed and the name of the command. That is the whole list, and
`build_report` is an allowlist of exactly those fields.

**What it never is.** The exception message, which often quotes what
somebody typed or a path or a plan. Local variables. Source lines. Command
arguments. Absolute paths, the user name, the host name. Anything that
counts or identifies a person. Nothing here is captured implicitly, which is
why the report is built by hand rather than by an SDK whose defaults can
grow in a later version.

**How it leaves.** Never from the process that crashed. The report is
written to a folder beside the store, and a detached child sends what is
waiting, the same way the release check fetches: out of process, never
waited on, silent when offline. `flanner crash-reports show` prints exactly
what is waiting or was last sent.

Off unless a person said yes at `init` or ran `flanner crash-reports on`.
`DO_NOT_TRACK` and `FLANNER_CRASH_REPORTS=0` turn it off whatever was said.
"""

from __future__ import annotations

import json
import os
import platform
import sys
import traceback
import uuid
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import TracebackType
from typing import Any

from . import release
from .exceptions import FlannerError

#: Where reports go. Empty until the Sentry project exists, and an empty
#: address means reports are written and kept but never sent.
DSN = ""

#: Points reports somewhere else, for testing against a real endpoint
#: before the address above is filled in.
DSN_ENV = "FLANNER_CRASH_REPORTS_DSN"

#: Why reports are off in a build that has no address to send them to.
NOT_AVAILABLE = "not available in this build"

#: `0` or `1` decides without asking, for people who manage machines.
SWITCH_ENV = "FLANNER_CRASH_REPORTS"

STATE_KEY = "crash_reports"
SPOOL = "crash-reports"
LAST_SENT = "last-sent.json"

#: A report is about where a crash happened, and the innermost frames are
#: where. A deep recursion does not need to be sent whole.
MAX_FRAMES = 30
MAX_CHAIN = 3

#: A machine in a crash loop should not send a crash loop.
MAX_PER_DAY = 5
MAX_WAITING = 20
KEEP_FOR = timedelta(days=7)
TIMEOUT_SECONDS = 3.0

INSTALL_KINDS = ("pip", "uv-tool", "pipx", "editable")
SURFACES = ("cli", "mcp", "web")


# --- consent -----------------------------------------------------------------


def _truthy(value: str) -> bool:
    return value.strip().lower() not in ("", "0", "false", "no")


def consent() -> tuple[bool, str]:
    """Whether reports may be sent, and what decided it.

    The environment beats the saved answer, so a machine can be kept quiet
    whatever somebody clicked. `DO_NOT_TRACK` is the convention other tools
    honour; being off is the only thing it can mean here.
    """
    if not dsn():
        # A build with nowhere to send reports: asking would get a yes that
        # did nothing, so the question is never put and nothing is kept.
        return False, NOT_AVAILABLE
    if _truthy(os.environ.get("DO_NOT_TRACK", "")):
        return False, "DO_NOT_TRACK"
    switch = os.environ.get(SWITCH_ENV, "").strip()
    if switch in ("0", "1"):
        return switch == "1", SWITCH_ENV
    saved = release.read_state().get(STATE_KEY)
    if isinstance(saved, bool):
        return saved, "setting"
    return False, "not asked"


def asked() -> bool:
    """Whether a person has answered, either way."""
    return isinstance(release.read_state().get(STATE_KEY), bool)


def set_consent(allowed: bool) -> None:
    """Save the answer. Saying no also drops anything not yet sent."""
    state = release.read_state()
    state[STATE_KEY] = allowed
    release.write_state(state)
    if not allowed:
        clear()


def dsn() -> str:
    return os.environ.get(DSN_ENV, "").strip() or DSN


# --- what counts as a crash --------------------------------------------------


def is_crash(error: BaseException) -> bool:
    """Whether this is a fault in flanner rather than an answer.

    A refusal (`FlannerError`) is flanner saying no on purpose, and a
    Click usage error is a mistyped command. Neither is a bug, and sending
    them would bury the ones that are.
    """
    if not isinstance(error, Exception) or isinstance(error, FlannerError):
        return False
    try:
        import click
    except ImportError:  # pragma: no cover - click is a dependency
        return True
    return not isinstance(error, click.exceptions.ClickException | click.exceptions.Abort)


# --- building a report -------------------------------------------------------


def _package_root() -> Path:
    """The directory holding the `flanner` package, so paths can be relative to it."""
    return Path(__file__).resolve().parent.parent


def _relative_path(filename: str) -> str:
    """A frame's file, with everything that says whose machine this is removed.

    Flanner's own files become `flanner/…`. Other installed packages become
    `site-packages/…`. The standard library becomes `stdlib/<file>`, and
    anything else is reduced to its file name.
    """
    try:
        return Path(filename).resolve().relative_to(_package_root()).as_posix()
    except (ValueError, OSError):
        pass
    normalised = filename.replace("\\", "/")
    for marker in ("/site-packages/", "/dist-packages/"):
        if marker in normalised:
            return "site-packages/" + normalised.split(marker, 1)[1]
    name = normalised.rsplit("/", 1)[-1]
    for base in {sys.base_prefix, sys.prefix}:
        try:
            Path(filename).resolve().relative_to(Path(base).resolve())
        except (ValueError, OSError):
            continue
        return f"stdlib/{name}"
    return f"other/{name}"


def _type_name(kind: type[BaseException]) -> str:
    module = kind.__module__
    return kind.__qualname__ if module == "builtins" else f"{module}.{kind.__qualname__}"


def _frames(tb: TracebackType | None) -> list[dict[str, Any]]:
    """The innermost frames, outermost first, as Sentry expects them."""
    frames = []
    for frame, lineno in traceback.walk_tb(tb):
        module = str(frame.f_globals.get("__name__", ""))
        frames.append(
            {
                "module": module,
                "function": frame.f_code.co_name,
                "filename": _relative_path(frame.f_code.co_filename),
                "lineno": lineno,
                "in_app": module == "flanner" or module.startswith("flanner."),
            }
        )
    return frames[-MAX_FRAMES:]


def _chain(error: BaseException) -> Iterator[BaseException]:
    """This error and what caused it, newest first, as far as MAX_CHAIN."""
    current: BaseException | None = error
    seen = 0
    while current is not None and seen < MAX_CHAIN:
        yield current
        seen += 1
        current = current.__cause__ or (
            None if current.__suppress_context__ else current.__context__
        )


def install_kind() -> str:
    """How flanner was installed. One of INSTALL_KINDS, read from local metadata only."""
    try:
        from importlib.metadata import distribution

        direct = json.loads(distribution("flanner").read_text("direct_url.json") or "{}")
        if direct.get("dir_info", {}).get("editable"):
            return "editable"
    except Exception:  # noqa: BLE001, S110 - a guess, never a failure
        pass
    prefix = Path(sys.prefix).as_posix().lower()
    if "/pipx/" in prefix:
        return "pipx"
    if "/uv/tools/" in prefix:
        return "uv-tool"
    return "pip"


def _version() -> str:
    try:
        from importlib.metadata import version

        return version("flanner")
    except Exception:  # noqa: BLE001 - an unknown version is still a report
        return "unknown"


def build_report(
    error: BaseException, *, surface: str, command: str = "", now: datetime | None = None
) -> dict[str, Any]:
    """The report for this error, and nothing but the allowlisted fields.

    `exception.value` is always empty. The message is the most likely place
    for somebody's content to be, so it is never read at all.
    """
    stamp = (now or datetime.now(timezone.utc)).replace(microsecond=0)
    values = [
        {
            "type": _type_name(type(link)),
            "value": "",
            "stacktrace": {"frames": _frames(link.__traceback__)},
        }
        for link in _chain(error)
    ]
    values.reverse()  # Sentry lists the oldest cause first
    return {
        "event_id": uuid.uuid4().hex,
        "timestamp": stamp.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "platform": "python",
        "level": "error",
        "release": f"flanner@{_version()}",
        "exception": {"values": values},
        "tags": {
            "surface": surface if surface in SURFACES else "cli",
            "command": command,
            "os": f"{platform.system()} {platform.release()}".strip(),
            "python": platform.python_version(),
            "install": install_kind(),
        },
    }


def fingerprint(report: dict[str, Any]) -> str:
    """The same crash, twice: its type and the innermost frame of our own code."""
    newest = report["exception"]["values"][-1]
    ours = [f for f in newest["stacktrace"]["frames"] if f["in_app"]]
    where = f"{ours[-1]['module']}:{ours[-1]['function']}" if ours else ""
    return f"{newest['type']}@{where}"


# --- the waiting folder ------------------------------------------------------


def _spool() -> Path:
    return release._home() / SPOOL


def waiting() -> list[Path]:
    """Reports not yet sent, oldest first."""
    try:
        found = [p for p in _spool().glob("*.json") if p.name != LAST_SENT]
    except OSError:
        return []
    return sorted(found, key=lambda p: p.stat().st_mtime)


def clear() -> int:
    """Delete everything waiting. Returns how many."""
    dropped = 0
    for path in waiting():
        try:
            path.unlink()
            dropped += 1
        except OSError:
            continue
    return dropped


def last_sent() -> dict[str, Any] | None:
    try:
        loaded = json.loads((_spool() / LAST_SENT).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return loaded if isinstance(loaded, dict) else None


def capture(error: BaseException, *, surface: str, command: str = "") -> Path | None:
    """Keep a report for this error, if reports are on and it is a crash.

    Never raises and never touches the network: this runs inside a process
    that is already failing, and must not change how it fails.
    """
    try:
        if not consent()[0] or not is_crash(error):
            return None
        report = build_report(error, surface=surface, command=command)
        folder = _spool()
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{report['event_id']}.json"
        path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        for stale in waiting()[:-MAX_WAITING]:
            stale.unlink(missing_ok=True)
        return path
    except Exception:  # noqa: BLE001 - reporting must never add a second failure
        return None


# --- sending -----------------------------------------------------------------


def send_in_background() -> bool:
    """Start a detached sender if anything is waiting and sending is allowed."""
    if not consent()[0] or not dsn() or not waiting():
        return False
    return release.spawn_detached("from flanner import crash; crash.send_waiting()")


def send_waiting(now: datetime | None = None) -> int:
    """Send what is waiting, within today's limits. The detached child's entry point."""
    if not consent()[0] or not dsn():
        return 0
    now = now or datetime.now(timezone.utc)
    today = now.date().isoformat()
    state = release.read_state()
    sent_today = state.get("crash_sent", {})
    if not isinstance(sent_today, dict) or sent_today.get("date") != today:
        sent_today = {"date": today, "count": 0, "seen": []}

    sent = 0
    for path in waiting():
        try:
            if datetime.fromtimestamp(path.stat().st_mtime, timezone.utc) < now - KEEP_FOR:
                path.unlink(missing_ok=True)
                continue
            report = json.loads(path.read_text(encoding="utf-8"))
            mark = fingerprint(report)
        except (OSError, ValueError, KeyError, IndexError, TypeError):
            path.unlink(missing_ok=True)
            continue
        if mark in sent_today["seen"]:
            path.unlink(missing_ok=True)
            continue
        if sent_today["count"] >= MAX_PER_DAY:
            break
        if not _post(report):
            break  # offline, or refused: keep it for next time
        path.unlink(missing_ok=True)
        (_spool() / LAST_SENT).write_text(json.dumps(report, indent=2), encoding="utf-8")
        sent_today["count"] += 1
        sent_today["seen"].append(mark)
        sent += 1

    state = release.read_state()
    state["crash_sent"] = sent_today
    release.write_state(state)
    return sent


def _parse_dsn(value: str) -> tuple[str, str] | None:
    """The envelope URL and public key in a Sentry DSN, or None if it is not one."""
    from urllib.parse import urlsplit

    parts = urlsplit(value)
    project = parts.path.strip("/")
    if parts.scheme != "https" or not parts.username or not parts.hostname or not project:
        return None
    host = parts.hostname + (f":{parts.port}" if parts.port else "")
    return f"https://{host}/api/{project}/envelope/", parts.username


def _post(report: dict[str, Any]) -> bool:
    """Send one report. False for any failure at all."""
    import urllib.request

    target = _parse_dsn(dsn())
    if target is None:
        return False
    url, key = target
    body = "\n".join(
        [
            json.dumps({"event_id": report["event_id"]}),
            json.dumps({"type": "event"}),
            json.dumps(report),
        ]
    ).encode("utf-8")
    request = urllib.request.Request(  # noqa: S310 - https only, checked in _parse_dsn
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/x-sentry-envelope",
            "X-Sentry-Auth": (
                f"Sentry sentry_version=7, sentry_key={key}, sentry_client=flanner-crash/1"
            ),
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as reply:  # noqa: S310
            return bool(200 <= reply.status < 300)
    except Exception:  # noqa: BLE001 - sending must never fail anything
        return False
