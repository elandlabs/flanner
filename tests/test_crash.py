"""Crash reports: what they may carry, when they are kept, and how they leave.

The promise is that a report is the error type and where it happened, and
nothing a person wrote. Most of this file is that promise, tested by trying
hard to break it.
"""

from __future__ import annotations

import json
import os
import socket
import time
from pathlib import Path

import pytest
from click.testing import CliRunner

from flanner import crash, release
from flanner.exceptions import NotFoundError, ValidationError

PLAN_BODY = "## Migration plan: move billing to the new ledger by Q3"
TOKEN = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("FLANNER_HOME", str(tmp_path / "home"))
    for name in ("DO_NOT_TRACK", crash.SWITCH_ENV, crash.DSN_ENV, "CI"):
        monkeypatch.delenv(name, raising=False)
    return tmp_path / "home"


def _crash_with_secrets() -> BaseException:
    """An error whose message, locals and cause all carry things a report must not."""
    home_path = str(Path.home())
    host = socket.gethostname()

    def inner(plan_text: str, token: str) -> None:
        where = f"{home_path}/secret-project/plan.md"  # noqa: F841 - read by the frame
        raise KeyError(f"{plan_text} {token} {host}")

    try:
        try:
            inner(PLAN_BODY, TOKEN)
        except KeyError as cause:
            raise RuntimeError(f"failed on {home_path} for {PLAN_BODY}") from cause
    except RuntimeError as error:
        return error
    raise AssertionError("unreachable")


def _on() -> None:
    crash.set_consent(True)


# --- what a report may carry -------------------------------------------------


def test_a_report_carries_no_message_locals_paths_or_names():
    report = crash.build_report(_crash_with_secrets(), surface="cli", command="mem tag")
    sent = json.dumps(report)

    forbidden = (
        PLAN_BODY,
        TOKEN,
        str(Path.home()),
        Path.home().name,
        socket.gethostname(),
        "secret-project",
    )
    for text in forbidden:
        # Both as written and as JSON writes it: a Windows path is escaped.
        assert text not in sent, text
        assert json.dumps(text)[1:-1] not in sent, text
    assert all(value["value"] == "" for value in report["exception"]["values"])


def test_a_report_holds_exactly_the_allowlisted_fields():
    report = crash.build_report(_crash_with_secrets(), surface="mcp", command="memory_tag")

    assert set(report) == {
        "event_id",
        "timestamp",
        "platform",
        "level",
        "release",
        "exception",
        "tags",
    }
    assert set(report["tags"]) == {"surface", "command", "os", "python", "install"}
    assert report["tags"]["surface"] == "mcp"
    assert report["tags"]["install"] in crash.INSTALL_KINDS
    for value in report["exception"]["values"]:
        assert set(value) == {"type", "value", "stacktrace"}
        for frame in value["stacktrace"]["frames"]:
            assert set(frame) == {"module", "function", "filename", "lineno", "in_app"}


def test_the_cause_comes_first_and_the_chain_is_kept():
    report = crash.build_report(_crash_with_secrets(), surface="cli")

    assert [v["type"] for v in report["exception"]["values"]] == ["KeyError", "RuntimeError"]


def test_an_unknown_surface_is_not_passed_through():
    report = crash.build_report(RuntimeError("x"), surface="anything a caller typed")

    assert report["tags"]["surface"] == "cli"


@pytest.mark.parametrize(
    ("raw", "shown"),
    [
        (
            "C:\\Users\\alice\\venv\\Lib\\site-packages\\sqlalchemy\\orm\\session.py",
            "site-packages/sqlalchemy/orm/session.py",
        ),
        (
            "/home/alice/.local/lib/python3.12/site-packages/click/core.py",
            "site-packages/click/core.py",
        ),
        ("/home/alice/work/script.py", "other/script.py"),
        ("C:\\Users\\alice\\script.py", "other/script.py"),
    ],
)
def test_a_frame_path_never_says_whose_machine_it_is(raw, shown):
    assert crash._relative_path(raw) == shown


def test_flanner_frames_are_relative_to_the_package():
    assert crash._relative_path(crash.__file__) == "flanner/crash.py"


def test_the_same_crash_has_the_same_fingerprint():
    first = crash.build_report(_crash_with_secrets(), surface="cli")
    second = crash.build_report(_crash_with_secrets(), surface="cli")

    assert crash.fingerprint(first) == crash.fingerprint(second)
    assert first["event_id"] != second["event_id"]


# --- what counts as a crash --------------------------------------------------


@pytest.mark.parametrize(
    "error",
    [
        ValidationError("a refusal"),
        NotFoundError("no such thing"),
        SystemExit(1),
        KeyboardInterrupt(),
    ],
)
def test_refusals_and_exits_are_not_crashes(error):
    assert crash.is_crash(error) is False


def test_click_usage_errors_are_not_crashes():
    import click

    assert crash.is_crash(click.UsageError("bad option")) is False
    assert crash.is_crash(click.Abort()) is False


def test_a_bug_is_a_crash():
    assert crash.is_crash(ZeroDivisionError()) is True


# --- consent -----------------------------------------------------------------


def test_off_until_somebody_says_yes():
    assert crash.consent() == (False, "not asked")
    assert crash.asked() is False
    assert crash.capture(RuntimeError("x"), surface="cli") is None


def test_do_not_track_beats_a_yes(monkeypatch):
    _on()
    monkeypatch.setenv("DO_NOT_TRACK", "1")

    assert crash.consent() == (False, "DO_NOT_TRACK")
    assert crash.capture(RuntimeError("x"), surface="cli") is None


def test_the_switch_decides_without_asking(monkeypatch):
    monkeypatch.setenv(crash.SWITCH_ENV, "1")
    assert crash.consent() == (True, crash.SWITCH_ENV)

    crash.set_consent(True)
    monkeypatch.setenv(crash.SWITCH_ENV, "0")
    assert crash.consent() == (False, crash.SWITCH_ENV)


def test_saying_no_drops_what_was_waiting():
    _on()
    crash.capture(RuntimeError("x"), surface="cli")
    assert crash.waiting()

    crash.set_consent(False)

    assert crash.waiting() == []


# --- keeping -----------------------------------------------------------------


def test_a_crash_is_kept_on_disk_and_nothing_is_sent(monkeypatch):
    import urllib.request

    _on()
    monkeypatch.setattr(
        urllib.request, "urlopen", lambda *a, **k: pytest.fail("the crashing process sent")
    )

    path = crash.capture(RuntimeError("x"), surface="cli", command="list")

    assert path is not None and path.exists()
    assert json.loads(path.read_text(encoding="utf-8"))["tags"]["command"] == "list"


def test_a_refusal_is_never_kept():
    _on()

    assert crash.capture(ValidationError("no"), surface="cli") is None
    assert crash.waiting() == []


def test_only_so_many_reports_wait(monkeypatch):
    _on()
    monkeypatch.setattr(crash, "MAX_WAITING", 3)

    for _ in range(5):
        crash.capture(RuntimeError("x"), surface="cli")

    assert len(crash.waiting()) == 3


def test_capture_never_raises(monkeypatch):
    _on()
    monkeypatch.setattr(crash, "build_report", lambda *a, **k: 1 / 0)

    assert crash.capture(RuntimeError("x"), surface="cli") is None


# --- sending -----------------------------------------------------------------


@pytest.fixture
def sender(monkeypatch):
    """Reports on, an address to send to, and a record of what was posted."""
    _on()
    monkeypatch.setenv(crash.DSN_ENV, "https://publickey@o1.ingest.example.test/42")
    posted: list[dict] = []
    outcome = {"ok": True}

    def post(report):
        posted.append(report)
        return outcome["ok"]

    monkeypatch.setattr(crash, "_post", post)
    return posted, outcome


def _kept(message: str = "x") -> Path:
    try:
        raise RuntimeError(message)
    except RuntimeError as error:
        path = crash.capture(error, surface="cli")
    assert path is not None
    return path


def test_sending_removes_what_was_sent_and_remembers_the_last(sender):
    posted, _ = sender
    _kept()

    assert crash.send_waiting() == 1
    assert crash.waiting() == []
    assert crash.last_sent() == posted[0]


def test_a_failed_send_keeps_the_report(sender):
    _, outcome = sender
    outcome["ok"] = False
    _kept()

    assert crash.send_waiting() == 0
    assert len(crash.waiting()) == 1


def test_the_same_crash_is_sent_once_a_day(sender):
    posted, _ = sender
    _kept()
    _kept()

    assert crash.send_waiting() == 1
    assert len(posted) == 1
    assert crash.waiting() == []


def test_no_more_than_the_daily_limit_is_sent(sender, monkeypatch):
    posted, _ = sender
    monkeypatch.setattr(crash, "MAX_PER_DAY", 2)
    for kind in (ValueError, TypeError, KeyError):
        try:
            raise kind()
        except Exception as error:  # noqa: BLE001
            crash.capture(error, surface="cli")

    assert crash.send_waiting() == 2
    assert len(crash.waiting()) == 1


def test_an_old_report_is_dropped_unsent(sender):
    posted, _ = sender
    path = _kept()
    old = time.time() - crash.KEEP_FOR.total_seconds() - 60
    os.utime(path, (old, old))

    assert crash.send_waiting() == 0
    assert posted == []
    assert crash.waiting() == []


def test_nothing_is_sent_without_an_address(monkeypatch):
    _on()
    monkeypatch.setattr(crash, "_post", lambda report: pytest.fail("sent with no address"))
    _kept()

    assert crash.dsn() == ""
    assert crash.send_waiting() == 0
    assert len(crash.waiting()) == 1


def test_the_envelope_goes_to_the_address_in_the_dsn(monkeypatch):
    import urllib.request

    _on()
    monkeypatch.setenv(crash.DSN_ENV, "https://publickey@o1.ingest.example.test/42")
    seen = {}

    class Reply:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def urlopen(request, timeout):
        seen["url"] = request.full_url
        seen["auth"] = request.headers["X-sentry-auth"]
        seen["lines"] = request.data.decode("utf-8").split("\n")
        seen["timeout"] = timeout
        return Reply()

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    report = crash.build_report(RuntimeError("x"), surface="cli")

    assert crash._post(report) is True
    assert seen["url"] == "https://o1.ingest.example.test/api/42/envelope/"
    assert "sentry_key=publickey" in seen["auth"]
    assert json.loads(seen["lines"][1]) == {"type": "event"}
    assert json.loads(seen["lines"][2]) == report
    assert seen["timeout"] == crash.TIMEOUT_SECONDS


@pytest.mark.parametrize(
    "value", ["", "http://key@host/1", "https://host/1", "https://key@host/", "not a url"]
)
def test_only_an_https_dsn_with_a_key_and_project_is_used(value):
    assert crash._parse_dsn(value) is None


def test_a_sender_is_started_only_when_there_is_something_to_send(monkeypatch):
    started: list[str] = []
    monkeypatch.setattr(release, "spawn_detached", lambda code: started.append(code) or True)

    assert crash.send_in_background() is False  # off
    _on()
    _kept()
    assert crash.send_in_background() is False  # no address
    monkeypatch.setenv(crash.DSN_ENV, "https://k@h.example.test/1")
    assert crash.send_in_background() is True
    assert started == ["from flanner import crash; crash.send_waiting()"]


# --- the surfaces ------------------------------------------------------------


def test_a_command_that_crashes_leaves_one_report_and_still_fails(monkeypatch):
    import click

    from flanner import cli as cli_module

    _on()

    @click.command("explode-for-test")
    @click.argument("secret")
    def explode(secret):
        raise ZeroDivisionError(secret)

    monkeypatch.setitem(cli_module.cli.commands, "explode-for-test", explode)
    monkeypatch.setattr(cli_module, "_send_waiting_crash_reports", lambda: None)
    monkeypatch.setattr("sys.argv", ["flanner", "explode-for-test", PLAN_BODY])

    with pytest.raises(ZeroDivisionError):
        cli_module.main()

    [path] = crash.waiting()
    report = json.loads(path.read_text(encoding="utf-8"))
    assert report["tags"]["command"] == "explode-for-test"
    assert PLAN_BODY not in path.read_text(encoding="utf-8")


def test_a_command_name_never_includes_what_was_typed_after_it():
    from flanner.cli import _command_names

    assert _command_names(["mem", "tag", "some-id", "auth"]) == "mem tag"
    assert _command_names(["--verbose", "list"]) == ""
    assert _command_names(["not-a-command"]) == ""


def test_a_tool_that_crashes_leaves_one_report():
    from mcp.server.fastmcp import FastMCP

    from flanner import server

    _on()
    observed = server._Observed(FastMCP("crash-test"))

    @observed.tool()
    def broken_tool(content: str) -> dict:
        raise ZeroDivisionError(content)

    with pytest.raises(ZeroDivisionError):
        broken_tool(content=PLAN_BODY)

    [path] = crash.waiting()
    report = json.loads(path.read_text(encoding="utf-8"))
    assert (report["tags"]["surface"], report["tags"]["command"]) == ("mcp", "broken_tool")
    assert PLAN_BODY not in path.read_text(encoding="utf-8")


def test_a_page_that_crashes_leaves_a_report_naming_the_route_not_the_path(tmp_path):
    from fastapi.testclient import TestClient

    from flanner.database import init_database
    from flanner.web import app

    init_database(str(tmp_path / "data.db"))
    _on()

    def explode(name: str):
        raise ZeroDivisionError(name)

    app.add_api_route("/crash-for-test/{name}", explode, methods=["GET"])
    try:
        client = TestClient(app, base_url="http://127.0.0.1:8080", raise_server_exceptions=False)
        reply = client.get("/crash-for-test/secret-plan-name")
    finally:
        app.router.routes[:] = [
            r for r in app.router.routes if getattr(r, "path", "") != "/crash-for-test/{name}"
        ]

    assert reply.status_code == 500
    [path] = crash.waiting()
    text = path.read_text(encoding="utf-8")
    assert json.loads(text)["tags"]["command"] == "GET /crash-for-test/{name}"
    assert "secret-plan-name" not in text


# --- the command -------------------------------------------------------------


def test_the_command_switches_shows_and_explains():
    from flanner.cli import cli

    runner = CliRunner()

    assert "Not decided yet" in runner.invoke(cli, ["crash-reports"]).output
    on = runner.invoke(cli, ["crash-reports", "on"])
    assert on.exit_code == 0 and crash.consent() == (True, "setting")
    assert "no address to send to" in on.output

    shown = runner.invoke(cli, ["crash-reports", "show"])
    assert shown.exit_code == 0
    example = json.loads(shown.output[shown.output.index("{") :])
    assert example["exception"]["values"][0]["value"] == ""
    assert "this message is never sent" not in shown.output.split("{", 1)[1]

    _kept()
    off = runner.invoke(cli, ["crash-reports", "off"])
    assert off.exit_code == 0 and crash.consent() == (False, "setting")
    assert crash.waiting() == []


def test_the_command_says_when_the_environment_decides(monkeypatch):
    from flanner.cli import cli

    monkeypatch.setenv("DO_NOT_TRACK", "1")

    said = CliRunner().invoke(cli, ["crash-reports", "on"]).output

    assert "off, because DO_NOT_TRACK is set" in said


def test_init_asks_about_updates_and_crash_reports_separately(tmp_path, monkeypatch):
    import subprocess

    from flanner.cli import cli

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)  # noqa: S603,S607
    monkeypatch.chdir(repo)

    result = CliRunner().invoke(
        cli,
        ["init", "--skip-claude", "--no-watch-skills", "--project-root", str(repo)],
        input="demo\nn\ny\n",
    )

    assert result.exit_code == 0, result.output
    assert "Check for new versions?" in result.output
    assert "Send crash reports?" in result.output
    assert release.update_check_consent() is False
    assert crash.consent() == (True, "setting")


def test_init_does_not_ask_in_ci(tmp_path, monkeypatch):
    import subprocess

    from flanner.cli import cli

    monkeypatch.setenv("CI", "true")
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)  # noqa: S603,S607

    result = CliRunner().invoke(
        cli,
        ["init", "--skip-claude", "--no-watch-skills", "--project-root", str(repo)],
        input="demo\nn\n",
    )

    assert "Send crash reports?" not in result.output
    assert crash.asked() is False


def test_the_setup_check_says_what_this_machine_sends(monkeypatch):
    from flanner import setup_check

    assert setup_check.sending() == {
        "update_check": False,
        "crash_reports": False,
        "crash_reports_decided_by": "not asked",
    }
    _on()
    release.set_update_check_consent(True)
    assert setup_check.sending()["crash_reports"] is True
    assert setup_check.sending()["update_check"] is True


# --- the words ---------------------------------------------------------------


def test_no_absolute_nothing_is_sent_claim_is_left():
    """The promise is now "your plans never leave, and nothing else is sent
    unless you turn it on". An absolute that says otherwise is untrue for
    anybody who said yes."""
    root = Path(__file__).resolve().parent.parent
    banned = (
        "nothing leaves your disk",
        "runs entirely on this machine",
        "no telemetry",
        "nothing is ever sent anywhere",
        "100% local",
        "nothing here reaches the network unasked",
    )
    files = [
        root / "README.md",
        *(root / "flanner").rglob("*.py"),
        *(root / "flanner").rglob("*.html"),
    ]
    found = [
        f"{path.relative_to(root)}: {phrase}"
        for path in files
        for phrase in banned
        if phrase in path.read_text(encoding="utf-8").lower()
    ]
    assert not found, found
