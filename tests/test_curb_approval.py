"""Approvals and grants (Curb PRD §11.2). No test draws a real prompt."""

import subprocess
import sys

import pytest

from flanner import curb_approval
from flanner.curb_approval import Broker, NoGrant, Paused


class Person:
    """A stand-in for the OS prompt: answers from a list, and records what it was shown."""

    name, weak = "test prompt", False

    def __init__(self, *answers):
        self.answers, self.shown = list(answers), []

    def available(self):
        return True

    def confirm(self, reason):
        self.shown.append(reason)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


class Clock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


@pytest.fixture(autouse=True)
def quiet(monkeypatch):
    notes = []
    monkeypatch.setattr(curb_approval.notify, "desktop", lambda *a, **k: notes.append(a))
    monkeypatch.setattr(curb_approval, "process_chain", lambda: ["claude", "bash", "flanner"])
    return notes


def broker(*answers, clock=None, wall=None):
    return Broker(Person(*answers), clock=clock or Clock(), wall=wall or Clock(5000.0))


def test_a_yes_gives_a_grant_that_works_once_for_that_change():
    b = broker(True)
    change = curb_approval.change_hash({"file": "settings.json", "after": {"x": 1}})
    grant = b.request("Add 3 deny rules to Claude Code's user settings", change)
    b.redeem(grant, change)
    with pytest.raises(NoGrant, match="already used"):
        b.redeem(grant, change)


def test_the_prompt_names_the_change_and_who_asked():
    person = Person(True)
    Broker(person).request("Add 3 deny rules", "abc")
    assert person.shown == ["flanner curb: Add 3 deny rules (asked by claude → bash → flanner)"]


def test_a_grant_is_bound_to_its_change():
    b = broker(True)
    grant = b.request("one change", "hash-a")
    with pytest.raises(NoGrant, match="different change"):
        b.redeem(grant, "hash-b")


def test_a_grant_expires_after_two_minutes():
    clock = Clock()
    b = broker(True, clock=clock)
    grant = b.request("a change", "h")
    clock.now += 121
    with pytest.raises(NoGrant, match="expired"):
        b.redeem(grant, "h")


def test_no_grant_means_no_write():
    with pytest.raises(NoGrant):
        broker().redeem(None, "h")


def test_without_a_method_curb_stays_read_only():
    with pytest.raises(NoGrant, match="read-only"):
        Broker(None).request("a change", "h")


def test_a_failed_prompt_counts_as_a_refusal():
    assert broker(OSError("no prompt")).request("a change", "h") is None


def test_three_refusals_in_ten_minutes_pause_requests_for_an_hour(quiet):
    wall = Clock(5000.0)
    b = Broker(Person(False, False, False, True), wall=wall)
    for _ in range(3):
        assert b.request("a change", "h") is None
        wall.now += 60
    assert quiet  # the person was told
    with pytest.raises(Paused):
        b.request("a change", "h")
    wall.now += 3600
    assert b.request("a change", "h") is not None


def test_refusals_spread_out_do_not_pause():
    wall = Clock(5000.0)
    b = Broker(Person(False, False, False, True), wall=wall)
    for _ in range(3):
        b.request("a change", "h")
        wall.now += 400
    assert b.request("a change", "h") is not None


def test_a_change_hash_ignores_key_order():
    assert curb_approval.change_hash({"a": 1, "b": 2}) == curb_approval.change_hash(
        {"b": 2, "a": 1}
    )


@pytest.mark.parametrize(
    ("platform", "names"),
    [
        ("win32", ["Windows Hello", "the Windows account password"]),
        ("darwin", ["Touch ID or the account password"]),
        ("linux", ["polkit (security key or password)"]),
    ],
)
def test_each_os_offers_its_own_methods(monkeypatch, platform, names):
    monkeypatch.setattr(curb_approval.sys, "platform", platform)
    assert [m.name for m in curb_approval.methods()] == names


def fake_run(stdout, seen=None, code=0):
    def run(argv, env=None):
        if seen is not None:
            seen.append((argv, env))
        return subprocess.CompletedProcess(argv, code, stdout, "")

    return run


def test_windows_hello_passes_the_reason_in_the_environment(monkeypatch):
    monkeypatch.setattr(curb_approval.sys, "platform", "win32")
    seen = []
    hello = curb_approval.WindowsHello(run=fake_run("result:Verified\n", seen))
    assert hello.confirm("Add 3 deny rules")
    argv, env = seen[0]
    assert argv[0] == "powershell.exe" and env["FLANNER_CURB_REASON"] == "Add 3 deny rules"
    assert not curb_approval.WindowsHello(run=fake_run("result:Canceled\n")).confirm("x")
    assert not curb_approval.WindowsHello(
        run=fake_run("availability:DeviceNotPresent")
    ).available()


def test_touch_id_and_polkit_read_their_answers(monkeypatch):
    assert curb_approval.MacOwner(run=fake_run("verified\n")).confirm("x")
    assert not curb_approval.MacOwner(run=fake_run("denied\n")).confirm("x")
    assert curb_approval.LinuxPolkit(run=fake_run("", code=0)).confirm("x")
    assert not curb_approval.LinuxPolkit(run=fake_run("", code=3)).confirm("x")


def test_polkit_needs_a_desktop_session(monkeypatch):
    monkeypatch.setattr(curb_approval.sys, "platform", "linux")
    monkeypatch.setattr(curb_approval.shutil, "which", lambda name: "/usr/bin/pkcheck")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    assert not curb_approval.LinuxPolkit().available()
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    assert curb_approval.LinuxPolkit().available()


def test_the_chain_starts_from_this_python():
    walk = curb_approval._windows_chain if sys.platform == "win32" else curb_approval._posix_chain
    names = walk(3)
    assert names and "python" in names[0].lower()
