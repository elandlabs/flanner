"""What the web UI holds between requests, in this process's memory only.

Three things a page cannot do inside one request: a check that takes ten
seconds, a scan or a test run that takes minutes, and saying afterwards
what a button did. Each is kept here, and a restart forgets all of it.

Nothing here is written to disk, and nothing here imports the rest of
Curb: the work to run is handed in.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Hashable
from dataclasses import dataclass, field, replace
from typing import Any

#: One operating-system prompt at a time: a second would open over the first.
APPROVAL = threading.Lock()
#: How many browsers' unread messages are kept. More, and the oldest goes.
MAX_SAID = 50


def _why(failure: Exception) -> str:
    """A failure in words a page may show: never the file it names.

    An operating-system error carries the path it failed on, and that can be
    a credential's place. So the reason is kept and the path dropped, and any
    other message that holds a path gives way to the error's kind.
    """
    # In full for the person, in the terminal that runs the web UI. Never on a page.
    logging.getLogger(__name__).warning("curb: background work failed", exc_info=failure)
    said = failure.strerror if isinstance(failure, OSError) and failure.strerror else str(failure)
    return said if said and "/" not in said and "\\" not in said else type(failure).__name__


# --- slow reads ---------------------------------------------------------------------


@dataclass
class Held:
    """An answer worked out off the request, and how old it is."""

    value: Any = None
    #: When the value was worked out, and when the work last finished at all.
    at: float | None = None
    tried: float | None = None
    busy: bool = False
    error: str | None = None
    stale: bool = False


class Memo:
    """Slow reads, done in the background and kept.

    A page shows the last answer while the next one is worked out, so a
    check that walks the home folder never holds a request.
    """

    def __init__(self) -> None:
        self._held: dict[Hashable, Held] = {}
        self._threads: list[threading.Thread] = []
        self._lock = threading.Lock()

    def get(self, key: Hashable, work: Callable[[], Any], *, max_age: float) -> Held:
        """The answer held for `key`, and a fresh one started if it is old."""
        with self._lock:
            held = self._held.setdefault(key, Held())
            old = held.tried is None or held.stale or time.time() - held.tried > max_age
            if old and not held.busy:
                held.busy, held.stale = True, False
                self._threads = [t for t in self._threads if t.is_alive()]
                thread = threading.Thread(target=self._run, args=(key, work), daemon=True)
                self._threads.append(thread)
                thread.start()
            return replace(held)

    def _run(self, key: Hashable, work: Callable[[], Any]) -> None:
        try:
            value, error = work(), None
        except Exception as failure:  # noqa: BLE001 - said on the page, never a crash
            value, error = None, _why(failure)
        with self._lock:
            held = self._held.setdefault(key, Held())
            held.tried = time.time()
            if error is None:
                held.value, held.at = value, held.tried
            held.error, held.busy = error, False

    def stale(self) -> None:
        """Something changed: every answer is worked out again when next asked for."""
        with self._lock:
            for held in self._held.values():
                held.stale = True

    def settle(self) -> None:
        """Wait for the work in flight. For tests."""
        for thread in list(self._threads):
            thread.join()


@dataclass
class Job:
    """One long task at a time, with its real progress: a scan, or a test run."""

    state: str = "idle"  # idle, running, done or failed
    done: int = 0
    total: int = 0
    label: str = ""
    started: float | None = None
    finished: float | None = None
    error: str | None = None
    #: What the last run that finished produced. A failed run leaves it.
    result: Any = None
    _thread: threading.Thread | None = field(default=None, repr=False)

    @property
    def running(self) -> bool:
        return self.state == "running"

    def start(self, work: Callable[[Job], Any], *, label: str = "") -> bool:
        """Run `work` in the background. False when a run is already going."""
        if self.running:
            return False
        self.state, self.done, self.total, self.label = "running", 0, 0, label
        self.started, self.finished, self.error = time.time(), None, None
        self._thread = threading.Thread(target=self._run, args=(work,), daemon=True)
        self._thread.start()
        return True

    def step(self, done: int, total: int, label: str | None = None) -> None:
        self.done, self.total = done, total
        if label is not None:
            self.label = label

    def _run(self, work: Callable[[Job], Any]) -> None:
        try:
            self.result = work(self)
            self.state = "done"
        except Exception as failure:  # noqa: BLE001 - said on the page, never a crash
            self.error = _why(failure)
            self.state = "failed"
        self.finished = time.time()

    def settle(self) -> None:
        """Wait for the run in flight. For tests."""
        if self._thread is not None:
            self._thread.join()


# --- quick reads ----------------------------------------------------------------------

_kept: dict[Hashable, tuple[float, Any]] = {}


def kept(key: Hashable, work: Callable[[], Any], *, seconds: float) -> Any:
    """A quick read, asked for at most once in `seconds`."""
    now = time.monotonic()
    held = _kept.get(key)
    if held is None or now - held[0] > seconds:
        held = (now, work())
        _kept[key] = held
    return held[1]


def changed() -> None:
    """Settings or stored state changed: nothing kept is trusted any longer."""
    _kept.clear()
    MEMO.stale()


# --- what a button did ----------------------------------------------------------------

_said: dict[str, tuple[str, str]] = {}


def say(browser: str | None, text: str, tone: str = "success") -> None:
    """Keep one message for a browser's next page. Tone: success, error or info.

    Kept here rather than in the address, so no link can put words in
    Curb's mouth.
    """
    if not browser:
        return
    _said.pop(browser, None)
    _said[browser] = (tone, text)
    while len(_said) > MAX_SAID:
        _said.pop(next(iter(_said)))


def heard(browser: str | None) -> tuple[str, str] | None:
    """That browser's message, once."""
    return _said.pop(browser, None) if browser else None


MEMO = Memo()
SCAN = Job()
TESTS = Job()


def reset() -> None:
    """Forget everything, as a restart does. For tests, and after `forget`."""
    global MEMO, SCAN, TESTS
    MEMO.settle()
    SCAN.settle()
    TESTS.settle()
    MEMO, SCAN, TESTS = Memo(), Job(), Job()
    _kept.clear()
    _said.clear()
