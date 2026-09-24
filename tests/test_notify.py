"""Desktop notifications: off in tests, and text that cannot become a command."""

from __future__ import annotations

import subprocess

from flanner import notify


def test_the_setting_turns_them_off_where_the_variable_cannot_reach(monkeypatch):
    """A receiver started at login never sees a shell's variables; it reads this."""
    from flanner import mesh_messages

    monkeypatch.delenv(notify.ENV, raising=False)
    assert notify.enabled()

    mesh_messages.set_notifications("off")
    assert not notify.enabled()

    mesh_messages.set_notifications("on")
    assert notify.enabled()


def test_the_variable_still_turns_them_off(monkeypatch):
    monkeypatch.setenv(notify.ENV, "off")
    assert not notify.enabled()


def test_one_messaging_setting_keeps_the_other(monkeypatch):
    from flanner import mesh_messages

    mesh_messages.set_notifications("off")
    mesh_messages.set_interrupt("prompt")

    assert mesh_messages.settings() == {"interrupt": "prompt", "notifications": "off"}


def test_the_command_shows_and_sets_notifications(monkeypatch):
    from click.testing import CliRunner

    from flanner.cli import cli

    monkeypatch.delenv(notify.ENV, raising=False)
    shown = CliRunner().invoke(cli, ["messages", "notifications"])
    assert shown.exit_code == 0, shown.output
    assert "Desktop notifications are on" in shown.output

    turned = CliRunner().invoke(cli, ["messages", "notifications", "off"])
    assert turned.exit_code == 0, turned.output
    assert "are off" in turned.output
    assert not notify.enabled()


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


def test_a_click_opens_the_thread_when_the_web_ui_is_running(monkeypatch):
    from flanner import ipc, peer

    envelope = {"artifact_id": "sha256:abcd1234ef"}
    monkeypatch.setattr(ipc, "read_daemon_info", lambda: None)
    assert peer._thread_link(envelope, "{}") == ""

    monkeypatch.setattr(ipc, "read_daemon_info", lambda: {"port": 8080, "token": "t"})
    assert peer._thread_link(envelope, "{}") == "http://127.0.0.1:8080/mesh/messages/abcd"
    reply = '{"thread_id": "sha256:ffff0000aa"}'
    assert peer._thread_link(envelope, reply).endswith("/mesh/messages/ffff")


def test_the_link_travels_in_the_environment_too(monkeypatch):
    seen = {}

    def run(command, env, **kwargs):
        seen["env"] = env
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setenv(notify.ENV, "on")
    monkeypatch.setattr(notify.shutil, "which", lambda _: "/usr/bin/notify-send")
    monkeypatch.setattr(subprocess, "run", run)
    notify.desktop("flanner", "hi", "http://127.0.0.1:8080/mesh/messages/abcd")
    assert seen["env"]["FLANNER_NOTE_URL"].endswith("/abcd")


def _record(monkeypatch, platform, found, stdout="", fail=()):
    """Run desktop() as if on `platform`, with `found` tools, recording commands."""
    ran = []

    def run(command, env, **kwargs):
        ran.append(command)
        code = 1 if any(flag in command for flag in fail) else 0
        return subprocess.CompletedProcess(command, code, stdout=stdout)

    monkeypatch.setenv(notify.ENV, "on")
    monkeypatch.setattr(notify.sys, "platform", platform)
    monkeypatch.setattr(notify.shutil, "which", lambda name, path=None: found.get(name))
    monkeypatch.setattr(subprocess, "run", run)
    opened = []
    import webbrowser

    monkeypatch.setattr(webbrowser, "open", opened.append)
    return ran, opened


LINK = "http://127.0.0.1:8080/mesh/messages/abcd"


def test_macos_opens_the_thread_through_terminal_notifier(monkeypatch):
    ran, _ = _record(monkeypatch, "darwin", {"terminal-notifier": "/opt/homebrew/bin/t-n"})
    assert notify.desktop("flanner", "-@ben sent a message", LINK)
    assert ran == [
        [
            "/opt/homebrew/bin/t-n",
            "-title",
            "flanner",
            "-message",
            " -@ben sent a message",
            "-open",
            LINK,
        ]
    ]


def test_macos_without_terminal_notifier_still_notifies(monkeypatch):
    ran, _ = _record(monkeypatch, "darwin", {})
    assert notify.desktop("flanner", "@ben sent a message", LINK)
    assert ran[0][0] == "osascript"


def test_linux_opens_the_thread_on_a_click(monkeypatch):
    ran, opened = _record(
        monkeypatch, "linux", {"notify-send": "/usr/bin/notify-send"}, stdout="default\n"
    )
    assert notify.desktop("flanner", "@ben sent a message", LINK)
    assert "--action=default=Open" in ran[0]
    assert opened == [LINK]


def test_linux_dismissed_opens_nothing(monkeypatch):
    _, opened = _record(monkeypatch, "linux", {"notify-send": "/usr/bin/notify-send"})
    assert notify.desktop("flanner", "@ben sent a message", LINK)
    assert opened == []


def test_an_older_notify_send_falls_back_to_plain(monkeypatch):
    ran, _ = _record(
        monkeypatch,
        "linux",
        {"notify-send": "/usr/bin/notify-send"},
        fail=("--action=default=Open",),
    )
    assert notify.desktop("flanner", "@ben sent a message", LINK)
    assert ran[1] == ["notify-send", "--", "flanner", "@ben sent a message"]
