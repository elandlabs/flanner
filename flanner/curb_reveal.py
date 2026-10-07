"""Names and locations in the web UI: one browser, five minutes, after a yes (Curb PRD §11.3).

The Curb pages hide credential names and secret locations, as the terminal
does. A person can show them in the page. The page gives a four-digit code;
the operating system's prompt shows the same code; a yes makes that one
browser's requests carry names for five minutes.

The browser is known by a random token in a cookie that scripts cannot
read. Tokens, codes and expiries live only in this process's memory, so a
restart ends every reveal. No two waiting reveals share a code, so a prompt
that shows the code on a person's page can only be the one that page asked
for.
"""

from __future__ import annotations

import hashlib
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from . import curb_approval

REVEAL_SECONDS = 300
CODE_SECONDS = 120
COOKIE = "flanner_curb_view"
#: More waiting codes than this and the oldest goes: nobody opens twenty dialogs.
MAX_WAITING = 20


class NotShown(PermissionError):
    """Names stay hidden. The message says why, in words for the page."""


@dataclass
class _View:
    code: str | None = None
    code_until: float = 0.0
    shown_until: float = 0.0


def unavailable(presence: curb_approval.Presence | None) -> str | None:
    """Why this machine cannot show names in the page, or None if it can."""
    if presence is None:
        return "no approval method on this machine"
    if not getattr(presence, "shows_reason", True):
        return "this system's prompt cannot show the code"
    return None


class Reveals:
    """Every browser's reveal state, in memory only."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self.clock = clock
        self._views: dict[str, _View] = {}
        self._lock = threading.Lock()

    @staticmethod
    def new_token() -> str:
        return secrets.token_urlsafe(32)

    def _prune(self, now: float) -> None:
        dead = [t for t, v in self._views.items() if v.code_until < now and v.shown_until < now]
        for token in dead:
            del self._views[token]
        waiting = sorted(
            (v.code_until, t) for t, v in self._views.items() if v.code and v.shown_until < now
        )
        for _, token in waiting[: max(0, len(waiting) - MAX_WAITING)]:
            del self._views[token]

    def code(self, token: str) -> str:
        """A fresh code for this browser's next request, unlike any other waiting one."""
        with self._lock:
            now = self.clock()
            self._prune(now)
            taken = {v.code for t, v in self._views.items() if v.code and t != token}
            code = f"{secrets.randbelow(9000) + 1000}"
            while code in taken:
                code = f"{secrets.randbelow(9000) + 1000}"
            view = self._views.setdefault(token, _View())
            view.code, view.code_until = code, now + CODE_SECONDS
            return code

    def seconds_left(self, token: str | None) -> int:
        """How long this browser still sees names. Zero when it does not."""
        if not token:
            return 0
        with self._lock:
            view = self._views.get(token)
            return max(0, int(view.shown_until - self.clock())) if view else 0

    def hide(self, token: str | None) -> None:
        with self._lock:
            if token:
                self._views.pop(token, None)

    def show(self, token: str, broker: curb_approval.Broker) -> None:
        """Ask the operating system, naming the code. Raises NotShown unless it says yes."""
        with self._lock:
            view = self._views.get(token)
            code = view.code if view and view.code_until >= self.clock() else None
        if code is None:
            raise NotShown("the code expired, so ask again")
        reason = unavailable(broker.presence)
        if reason:
            raise NotShown(reason)
        change = curb_approval.change_hash(
            {"reveal": hashlib.sha256(token.encode("utf-8")).hexdigest(), "code": code}
        )
        try:
            grant = broker.request(
                f"show names and locations in the flanner page. Code {code}", change
            )
            if grant is None:
                raise NotShown("not approved, so names stay hidden")
            broker.redeem(grant, change)
        except curb_approval.Paused as stop:
            raise NotShown(str(stop)) from None
        except curb_approval.NoGrant as refusal:
            raise NotShown(str(refusal)) from None
        with self._lock:
            held = self._views.setdefault(token, _View())
            held.code, held.code_until = None, 0.0
            held.shown_until = self.clock() + REVEAL_SECONDS


#: The web process's reveals. A restart empties it, which ends every reveal.
REVEALS = Reveals()
