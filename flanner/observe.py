"""Knowing what flanner did, without telling anybody else.

Observability for a local tool is a different problem from observability for
a service, and the difference is not one of scale.

**Nothing is ever shipped.** The whole promise of this tool is that plan
contents stay on the machine that wrote them, so there is no aggregator, no
endpoint, and no telemetry — not as a cost decision, as the product. What
this module does is make the information *available when somebody asks*.

Three questions, three shapes:

**"Why was that slow?"** Answered by `step()`, which records where the time
went and prints a breakdown under `--verbose`. Collected always because it
costs a `perf_counter` call; printed only when asked.

**"What has the agent been doing?"** Answered by `tool_call()`. The MCP
server is the one surface where nobody is watching a terminal: an agent
calls a tool, gets a dict back, and may retry or work around a failure with
the person none the wiser. Those go to a file, because on that surface
stdout *is* the protocol and a stray print corrupts it.

**"What do I put in the bug report?"** Answered by `flanner doctor
--report`, which uses the ledger below plus the store's own state.

## What is never recorded

Plan bodies, file contents, and anything a person typed into a plan. Names,
ids, counts, durations and outcomes only. A local log is not a secret, but
it is copied into issues and pasted into chats, and the first time somebody
finds a paragraph of their design doc in one is the last time they trust
this tool with anything.
"""

from __future__ import annotations

import logging
import os
import re
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: Where the MCP server records what an agent asked it to do. Overridable
#: mostly so tests do not write into a real home directory.
LOG_PATH_ENV = "FLANNER_LOG"

#: One file, capped, with one old copy kept. A tool that grows a log without
#: limit has taken on a job nobody asked it to do.
MAX_LOG_BYTES = 2_000_000
LOG_BACKUPS = 1


@dataclass
class _Ledger:
    """What this invocation spent its time on.

    Per-process rather than per-request, because a CLI invocation *is* the
    request. Nothing here survives the process, which is the point: a
    breakdown is for the run you just did.
    """

    steps: list[tuple[str, float]] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)

    def total_ms(self) -> float:
        return sum(ms for _, ms in self.steps)


_ledger = _Ledger()


@contextmanager
def step(name: str, **detail: Any) -> Iterator[None]:
    """Time a part of the work, for the `--verbose` breakdown.

    Named for what a person would call it — "fetch artifacts", not
    `_fetch_artifacts` — because the breakdown is read by somebody wondering
    where two seconds went, not by somebody reading the source.

    Timed even when the body raises: a step that fails slowly is the
    interesting one, and losing its number is losing the answer.
    """
    started = time.perf_counter()
    try:
        yield
    finally:
        elapsed = (time.perf_counter() - started) * 1000
        _ledger.steps.append((name, round(elapsed, 1)))
        for key, value in detail.items():
            if isinstance(value, int):
                _ledger.counts[key] = _ledger.counts.get(key, 0) + value


def count(**items: int) -> None:
    """Record how many of something happened, for the breakdown.

    Counts matter as much as times. Fourteen artifacts in 1.8 seconds and
    one artifact in 1.8 seconds are different problems and look identical
    if only the duration is kept.
    """
    for key, value in items.items():
        _ledger.counts[key] = _ledger.counts.get(key, 0) + value


def breakdown() -> list[str]:
    """The `--verbose` report: where the time went, widest column aligned.

    Empty when nothing was timed, so a command that does no measured work
    prints nothing rather than an empty heading.
    """
    if not _ledger.steps:
        return []

    width = max(len(name) for name, _ in _ledger.steps)
    lines = [f"  {name:<{width}}  {ms:>8.1f} ms" for name, ms in _ledger.steps]
    if len(_ledger.steps) > 1:
        lines.append(f"  {'total':<{width}}  {_ledger.total_ms():>8.1f} ms")
    if _ledger.counts:
        tally = ", ".join(f"{k}={v}" for k, v in sorted(_ledger.counts.items()))
        lines.append(f"  ({tally})")
    return lines


def reset() -> None:
    """Start a fresh ledger. For tests, and for a long-running process."""
    _ledger.steps.clear()
    _ledger.counts.clear()


# --- the MCP surface --------------------------------------------------------


def _log_path() -> Path | None:
    """Where tool calls are recorded, or None if nowhere.

    Defaults to the flanner home rather than to off. This is the one surface
    with no human watching, so a failure that leaves no trace is a failure
    nobody can investigate — and unlike a one-shot command, the server is
    already long-running and already owns files there.
    """
    override = os.environ.get(LOG_PATH_ENV, "").strip()
    if override:
        return Path(override)
    home = os.environ.get("FLANNER_HOME")
    return (Path(home) if home else Path.home() / ".flanner") / "mcp.log"


_tool_logger: logging.Logger | None = None


def _tool_log() -> logging.Logger | None:
    """A rotating file logger, built once, or None if it cannot be built.

    Never raises. Logging is not worth failing a tool call over: an agent
    that could not create a plan because the log directory was read-only
    would be a worse tool than one that did not log at all.
    """
    global _tool_logger
    if _tool_logger is not None:
        return _tool_logger

    path = _log_path()
    if path is None:
        return None
    try:
        from logging.handlers import RotatingFileHandler

        path.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            path, maxBytes=MAX_LOG_BYTES, backupCount=LOG_BACKUPS, encoding="utf-8"
        )
        handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
        built = logging.getLogger("flanner.mcp")
        built.setLevel(logging.INFO)
        built.propagate = False  # stdout is the MCP transport
        built.handlers = [handler]
    except Exception as e:  # noqa: BLE001 - the promise above is "never raises"
        # Broad on purpose, and this is the one place in the package where
        # that is right. The caller is an agent mid-tool-call: failing to
        # create somebody's plan because a log path was unusable would be a
        # strictly worse tool than one that did not log at all.
        #
        # `OSError` covers the cases anybody has hit — an unwritable
        # directory, a file where a directory should be. It is broader than
        # that because the cost of being wrong is asymmetric: a missed log
        # line is nothing, and a failed plan write is the product.
        logger.debug("no MCP log (%s); continuing without one", e)
        return None

    _tool_logger = built
    return _tool_logger


def _safe(value: Any) -> str:
    """One field, with anything that could carry content left out.

    Truncated hard and stripped of newlines. A tool argument is a name or an
    id in every case worth logging, and the ones that are not — a plan body
    — are excluded by the caller rather than trimmed here.
    """
    text = str(value).replace("\n", " ").replace("\r", " ")
    return text if len(text) <= 60 else text[:57] + "..."


def tool_call(tool: str, *, ms: float, ok: bool, error: str = "", **fields: Any) -> None:
    """Record one MCP tool call: what, how long, and how it went.

    The surface where nobody is watching. An agent that gets an error back
    may retry, route around it, or quietly give up, and today none of that
    leaves a trace — there are thirty places in `server.py` that return an
    error dict and not one of them says so anywhere a person will look.

    Arguments are recorded by the caller and deliberately not introspected
    here: `content` is a plan body and must never be written down.

    The first parameter is `tool`, not `name`, because several tools take an
    argument called `name` — and a signature that collides with the data it
    is given is one that fails on exactly the calls worth logging.
    """
    log = _tool_log()
    if log is None:
        return
    parts = [f"tool={tool}", f"ms={ms:.1f}", "ok" if ok else "failed"]
    parts.extend(
        f"{key}={_safe(value)}" for key, value in fields.items() if value not in (None, "")
    )
    if error:
        parts.append(f"error={_safe(error)}")
    log.info(" ".join(parts))


#: A tool log line that names its client: the logger's local time, then the call.
_CLIENT_LINE = re.compile(
    r"^(?P<when>\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d+) tool=\S+ .*?\bclient=(?P<client>\S+)"
)


def last_calls() -> dict[str, datetime]:
    """When each MCP client last called a tool, in UTC, keyed by its own name.

    Read from the tool log and its one rotated copy. A log that cannot be
    read is an empty answer rather than an error, because this feeds a
    status line. Reads the whole file each time.
    """
    # ponytail: rereads up to two 2 MB files per call; keep a cursor if a
    # page ever calls this in a loop.
    path = _log_path()
    if path is None:
        return {}
    seen: dict[str, datetime] = {}
    for candidate in (path.with_name(path.name + ".1"), path):
        try:
            text = candidate.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            match = _CLIENT_LINE.match(line)
            if match is None:
                continue
            try:
                local = datetime.strptime(match["when"], "%Y-%m-%d %H:%M:%S,%f")
            except ValueError:
                continue
            when = local.astimezone(timezone.utc).replace(tzinfo=None)
            if when > seen.get(match["client"], datetime.min):
                seen[match["client"]] = when
    return seen


def served(operation: str, *, ms: float, ok: bool, peer: str = "", reason: str = "") -> None:
    """Record one request answered for another machine.

    The second unattended surface. `flanner peer serve` runs for hours
    answering devices nobody is watching it answer, and until now it kept no
    record at all — so "did their laptop ever reach mine?" had no answer
    short of running it again and hoping.

    The refusal reason is the valuable half. A peer being turned away for a
    stale entitlement and one being turned away for a bad signature look
    identical from the other end, and only this side knows which it was.

    Artifact ids and device ids are recorded; artifact *contents* never are.
    """
    log = _tool_log()
    if log is None:
        return
    parts = [f"served={_safe(operation)}", f"ms={ms:.1f}", "ok" if ok else "refused"]
    if peer:
        parts.append(f"peer={_safe(peer)}")
    if reason:
        parts.append(f"reason={_safe(reason)}")
    log.info(" ".join(parts))
