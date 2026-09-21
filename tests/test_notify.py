"""Desktop notifications: off in tests, and text that cannot become a command."""

from __future__ import annotations

import subprocess

from flanner import notify


def test_off_means_nothing_is_run(monkeypatch):
    ran = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: ran.append(a))
    monkeypatch.setenv(notify.ENV, "off")

    assert notify.desktop("flanner", "@ben sent a message") is False
    assert ran == []


def test_the_text_travels_in_the_environment_not_the_command(monkeypatch):
    seen = {}

    def run(command, env, **kwargs):
        seen["command"], seen["env"] = command, env
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setenv(notify.ENV, "on")
    monkeypatch.setattr(notify.shutil, "which", lambda _: "/usr/bin/notify-send")
    monkeypatch.setattr(subprocess, "run", run)

    hostile = '"; rm -rf ~; echo "'
    notify.desktop("flanner", hostile)

    assert seen["env"]["FLANNER_NOTE_TEXT"] == hostile
    if notify.sys.platform in ("win32", "darwin"):
        assert hostile not in " ".join(seen["command"])


def test_a_missing_tool_is_not_an_error(monkeypatch):
    monkeypatch.setenv(notify.ENV, "on")

    def missing(*a, **k):
        raise FileNotFoundError("no such tool")

    monkeypatch.setattr(subprocess, "run", missing)
    assert notify.desktop("flanner", "hi") is False
