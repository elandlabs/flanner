"""Approvals: a person at this machine says yes to one exact change (Curb PRD §11.2).

Curb asks the operating system to confirm that a person is present, naming
the change. A yes becomes a grant: single use, valid for two minutes, bound
to the hash of the exact change, and held only in this process's memory.
Every Curb write redeems one, so a write without a grant fails.

Methods, preferred first:

- Windows: Windows Hello, then the account password in the Windows Security
  prompt (weaker: a look-alike window could phish it).
- macOS: Touch ID, or the account password, in the system's own prompt.
- Linux desktop: polkit, whose agent asks for a security key or password.

None of them can be answered by typing into the terminal that asked, which
is the point: an agent can run `flanner curb fix`, but cannot say yes. Where
no method exists, Curb stays read-only. Three refused or ignored approvals
within ten minutes pause requests for an hour, and say so on the desktop.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from . import curb_log, curb_store, notify

GRANT_SECONDS = 120
DENIALS, DENIAL_WINDOW, PAUSE = 3, 600, 3600
PROMPT_SECONDS = 120


class NoGrant(PermissionError):
    """A write was attempted without a valid grant for that exact change."""


class Paused(PermissionError):
    """Too many refused approvals: requests wait for the pause to end."""


def change_hash(change: Any) -> str:
    """The hash a grant binds to: canonical JSON of the exact change."""
    text = json.dumps(change, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --- presence methods -----------------------------------------------------------------


class Presence(Protocol):
    name: str
    weak: bool
    #: Whether the prompt shows the reason it was given. The web UI's reveal
    #: needs one that does: its code is in the reason (Curb PRD §11.3).
    shows_reason: bool

    def available(self) -> bool: ...

    def confirm(self, reason: str) -> bool: ...


def _run(
    argv: Sequence[str], env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed argv; the reason travels in the environment
        list(argv),
        capture_output=True,
        text=True,
        timeout=PROMPT_SECONDS,
        check=False,
        env={**os.environ, **(env or {})},
    )


_WINRT = r"""
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$asTask = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
  $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
  $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
function Await($op, [Type]$type) {
  $t = $asTask.MakeGenericMethod($type).Invoke($null, @($op)); $t.Wait(-1) | Out-Null; $t.Result }
$v = [Windows.Security.Credentials.UI.UserConsentVerifier, Windows.Security.Credentials.UI,
  ContentType = WindowsRuntime]
$avail = Await ($v::CheckAvailabilityAsync()) `
  ([Windows.Security.Credentials.UI.UserConsentVerifierAvailability])
if ($env:FLANNER_CURB_ASK -ne '1') { Write-Output "availability:$avail"; exit 0 }
if ($avail -ne 'Available') { Write-Output "result:$avail"; exit 0 }
$r = Await ($v::RequestVerificationAsync($env:FLANNER_CURB_REASON)) `
  ([Windows.Security.Credentials.UI.UserConsentVerificationResult])
Write-Output "result:$r"
"""


@dataclass
class WindowsHello:
    name: str = "Windows Hello"
    weak: bool = False
    shows_reason: bool = True
    run: Callable[..., subprocess.CompletedProcess[str]] = field(default=_run, repr=False)

    def _ask(self, ask: bool, reason: str = "") -> str:
        # Windows PowerShell 5.1: PowerShell 7 has no Windows Runtime projection.
        done = self.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", _WINRT],
            {"FLANNER_CURB_ASK": "1" if ask else "0", "FLANNER_CURB_REASON": reason},
        )
        return done.stdout.strip().rsplit("\n", 1)[-1] if done.returncode == 0 else ""

    def available(self) -> bool:
        return sys.platform == "win32" and self._ask(False) == "availability:Available"

    def confirm(self, reason: str) -> bool:
        return self._ask(True, reason) == "result:Verified"


@dataclass
class WindowsPassword:
    """The account password in the Windows Security prompt, checked by LogonUser."""

    name: str = "the Windows account password"
    weak: bool = True
    shows_reason: bool = True

    def available(self) -> bool:
        return sys.platform == "win32"

    def confirm(self, reason: str) -> bool:  # pragma: no cover - draws a real prompt
        return _windows_password(reason)


_MAC = """
ObjC.import('LocalAuthentication'); ObjC.import('Foundation');
var reason = $.NSProcessInfo.processInfo.environment.objectForKey('FLANNER_CURB_REASON').js;
var ctx = $.LAContext.alloc.init;
if (!ctx.canEvaluatePolicyError(2, null)) { 'unavailable' } else if (!reason) { 'available' }
else {
  var done = false, ok = false;
  ctx.evaluatePolicyLocalizedReasonReply(2, reason, function (success, error) {
    ok = success; done = true; });
  while (!done) { $.NSRunLoop.currentRunLoop.runUntilDate(
    $.NSDate.dateWithTimeIntervalSinceNow(0.1)); }
  ok ? 'verified' : 'denied';
}
"""


@dataclass
class MacOwner:
    """Touch ID, or the account password, in LocalAuthentication's own prompt."""

    name: str = "Touch ID or the account password"
    weak: bool = False
    shows_reason: bool = True
    run: Callable[..., subprocess.CompletedProcess[str]] = field(default=_run, repr=False)

    def _ask(self, reason: str) -> str:
        done = self.run(
            ["osascript", "-l", "JavaScript", "-e", _MAC], {"FLANNER_CURB_REASON": reason}
        )
        return done.stdout.strip() if done.returncode == 0 else ""

    def available(self) -> bool:
        return sys.platform == "darwin" and self._ask("") == "available"

    def confirm(self, reason: str) -> bool:
        return self._ask(reason) == "verified"


@dataclass
class LinuxPolkit:
    """polkit's own agent, on a desktop session: a security key or the password."""

    name: str = "polkit (security key or password)"
    weak: bool = False
    #: polkit shows its action's own text, never the reason.
    shows_reason: bool = False
    run: Callable[..., subprocess.CompletedProcess[str]] = field(default=_run, repr=False)

    def available(self) -> bool:
        desktop = os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")
        return sys.platform.startswith("linux") and bool(desktop) and bool(shutil.which("pkcheck"))

    def confirm(self, reason: str) -> bool:
        # polkit shows its action's own text; the terminal named the change first.
        done = self.run(
            [
                "pkcheck",
                "--action-id",
                "org.freedesktop.policykit.exec",
                "--process",
                str(os.getpid()),
                "--allow-user-interaction",
            ]
        )
        return done.returncode == 0


def methods() -> list[Presence]:
    """Every method this OS offers, preferred first."""
    platform: str = sys.platform  # a plain str, so mypy checks every branch on every OS
    if platform == "win32":
        return [WindowsHello(), WindowsPassword()]
    if platform == "darwin":
        return [MacOwner()]
    return [LinuxPolkit()]


def _dll(name: str) -> Any:  # pragma: no cover - Windows only
    loader: Any = getattr(ctypes, "WinDLL", None)
    return loader(name, use_last_error=True)


def method() -> Presence | None:
    """The best method available here, or None: then Curb stays read-only."""
    for candidate in methods():
        try:
            if candidate.available():
                return candidate
        except (OSError, subprocess.SubprocessError):
            continue
    return None


# --- who asked ----------------------------------------------------------------------


def process_chain(limit: int = 6) -> list[str]:
    """The names of this process and its parents, such as claude → bash → flanner.

    A hint, not proof: a process can be named anything.
    """
    try:
        chain = _windows_chain(limit) if sys.platform == "win32" else _posix_chain(limit)
    except (OSError, ValueError, subprocess.SubprocessError):
        return []
    return list(reversed(chain))


def _posix_chain(limit: int) -> list[str]:
    names, pid = [], os.getpid()
    for _ in range(limit):
        done = subprocess.run(  # noqa: S603 - fixed argv
            ["ps", "-o", "ppid=,comm=", "-p", str(pid)],  # noqa: S607 - ps from PATH
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        parts = done.stdout.strip().split(None, 1)
        if len(parts) != 2:
            break
        names.append(Path(parts[1]).name)
        pid = int(parts[0])
        if pid <= 1:
            break
    return names


def _windows_chain(limit: int) -> list[str]:  # pragma: no cover - Windows only
    platform: str = sys.platform
    if platform != "win32":
        return []
    from ctypes import wintypes

    class Entry(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", ctypes.c_wchar * 260),
        ]

    kernel32 = _dll("kernel32")
    snapshot = kernel32.CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
    parents: dict[int, tuple[int, str]] = {}
    entry = Entry()
    entry.dwSize = ctypes.sizeof(Entry)
    try:
        ok = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while ok:
            parents[entry.th32ProcessID] = (entry.th32ParentProcessID, entry.szExeFile)
            ok = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    names, pid = [], os.getpid()
    for _ in range(limit):
        if pid not in parents:
            break
        parent, name = parents[pid]
        names.append(name)
        pid = parent
    return names


# --- grants -----------------------------------------------------------------------------


@dataclass(frozen=True)
class Grant:
    change: str
    method: str
    issued: float


@dataclass
class Broker:
    """Asks for approvals and keeps the grants they produce, in memory only."""

    presence: Presence | None = None
    clock: Callable[[], float] = time.monotonic
    wall: Callable[[], float] = time.time
    _live: dict[int, Grant] = field(default_factory=dict, repr=False)

    def request(self, summary: str, change: str) -> Grant | None:
        """Ask the person to approve one exact change. None if refused or ignored."""
        if self.presence is None:
            raise NoGrant("no approval method on this machine, so Curb stays read-only")
        paused = self._paused_until()
        if paused:
            raise Paused(
                "three approvals were refused or ignored in ten minutes; "
                f"Curb asks again after {time.strftime('%H:%M', time.localtime(paused))}"
            )
        chain = " → ".join(process_chain())
        reason = f"flanner curb: {summary}" + (f" (asked by {chain})" if chain else "")
        try:
            yes = self.presence.confirm(reason)
        except (OSError, subprocess.SubprocessError):
            yes = False
        if not yes:
            self._record_denial()
            curb_log.record_approval(summary, "refused")
            return None
        grant = Grant(change, self.presence.name, self.clock())
        self._live[id(grant)] = grant
        curb_log.record_approval(summary, "granted")
        return grant

    def redeem(self, grant: Grant | None, change: str) -> None:
        """Spend a grant on the change it was issued for, or raise NoGrant."""
        if grant is None or self._live.pop(id(grant), None) is not grant:
            raise NoGrant("no grant for this change: it was refused, or already used")
        if grant.change != change:
            raise NoGrant("the grant was for a different change")
        if self.clock() - grant.issued > GRANT_SECONDS:
            raise NoGrant("the grant expired after two minutes")

    # The abuse limit outlives one process, so it is kept on disk.
    def _path(self) -> Path:
        return curb_store.curb_dir() / "approvals.json"

    def _denials(self) -> list[float]:
        try:
            data = json.loads(self._path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        return [float(t) for t in data.get("denied", []) if isinstance(t, int | float)]

    def _paused_until(self) -> float | None:
        recent = [t for t in self._denials() if self.wall() - t < DENIAL_WINDOW + PAUSE]
        for index in range(len(recent) - DENIALS + 1):
            window = recent[index : index + DENIALS]
            if window[-1] - window[0] <= DENIAL_WINDOW and self.wall() < window[-1] + PAUSE:
                return window[-1] + PAUSE
        return None

    def _record_denial(self) -> None:
        kept = [t for t in self._denials() if self.wall() - t < DENIAL_WINDOW + PAUSE]
        kept.append(self.wall())
        path = self._path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"denied": kept}), encoding="utf-8")
        if self._paused_until():
            notify.desktop(
                "flanner curb paused approvals",
                "Three approvals were refused or ignored in ten minutes. "
                "If you did not start them, something on this machine is asking.",
            )


def paused_until() -> float | None:
    """When Curb asks again after too many refusals, or None while it still asks."""
    return Broker()._paused_until()


# --- the Windows password prompt -----------------------------------------------------------


def _windows_password(reason: str) -> bool:  # pragma: no cover - draws a real prompt
    """CredUIPromptForWindowsCredentials for this user, verified by LogonUser."""
    platform: str = sys.platform
    if platform != "win32":
        return False
    from ctypes import wintypes

    class Info(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("hwndParent", wintypes.HWND),
            ("pszMessageText", wintypes.LPCWSTR),
            ("pszCaptionText", wintypes.LPCWSTR),
            ("hbmBanner", wintypes.HBITMAP),
        ]

    credui, advapi = _dll("credui"), _dll("advapi32")
    kernel32, ole32 = _dll("kernel32"), _dll("ole32")
    info = Info(ctypes.sizeof(Info), None, reason, "flanner curb", None)
    package = wintypes.ULONG(0)
    out_buffer = ctypes.c_void_p()
    out_size = wintypes.ULONG(0)
    save = wintypes.BOOL(False)
    status = credui.CredUIPromptForWindowsCredentialsW(
        ctypes.byref(info),
        0,
        ctypes.byref(package),
        None,
        0,
        ctypes.byref(out_buffer),
        ctypes.byref(out_size),
        ctypes.byref(save),
        0x200,  # CREDUIWIN_ENUMERATE_CURRENT_USER
    )
    if status != 0:
        return False
    user = ctypes.create_unicode_buffer(514)
    domain = ctypes.create_unicode_buffer(514)
    password = ctypes.create_unicode_buffer(514)
    sizes = [wintypes.DWORD(514) for _ in range(3)]
    try:
        unpacked = credui.CredUnPackAuthenticationBufferW(
            0,
            out_buffer,
            out_size,
            user,
            ctypes.byref(sizes[0]),
            domain,
            ctypes.byref(sizes[1]),
            password,
            ctypes.byref(sizes[2]),
        )
        if not unpacked:
            return False
        name, realm = user.value, domain.value or None
        if "\\" in name and not realm:
            realm, name = name.split("\\", 1)
        token = wintypes.HANDLE()
        ok = advapi.LogonUserW(name, realm, password, 3, 0, ctypes.byref(token))  # network logon
        if ok:
            kernel32.CloseHandle(token)
        return bool(ok)
    finally:
        ctypes.memset(password, 0, ctypes.sizeof(password))
        ctypes.memset(out_buffer, 0, out_size.value)
        ole32.CoTaskMemFree(out_buffer)
