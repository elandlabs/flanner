"""Watching skill use, and the managed packages that go with it.

The M1 half. Written against a real repository and a real hook payload
rather than mocks, because the two things most likely to break are the
hook actually reaching `.claude/settings.json` and an install actually
being reversible, and neither of those is visible to a mocked test.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from flanner import skills_manage, skills_observe
from flanner.cli import cli


def write_skill(root: Path, name: str, description: str = "Does a thing") -> Path:
    package = root / name
    package.mkdir(parents=True, exist_ok=True)
    (package / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\nBody.\n", encoding="utf-8"
    )
    return package


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """An adopted repository holding one project skill, as a person has it."""
    home = tmp_path / "flanner-home"
    monkeypatch.setenv("FLANNER_HOME", str(home))
    monkeypatch.setenv("FLANNER_SKILLS_HOME", str(tmp_path / "user-home"))

    where = tmp_path / "repo"
    where.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=where, check=True)  # noqa: S603,S607
    monkeypatch.chdir(where)

    runner = CliRunner()
    started = runner.invoke(
        cli, ["init", "--skip-claude", "--project-root", str(where)], input="demo\n"
    )
    assert started.exit_code == 0, started.output

    write_skill(where / ".claude" / "skills", "demo-skill")
    assert runner.invoke(cli, ["skills", "scan"]).exit_code == 0
    return runner, where


def event(repo_path: Path, *, skill: str = "demo-skill", at: str = "2026-01-01T00:00:00Z") -> str:
    """A PostToolUse payload shaped the way the harness sends one."""
    return json.dumps(
        {
            "session_id": "sess-1",
            "cwd": str(repo_path),
            "tool_name": "Skill",
            "tool_input": {"skill": skill, "args": ""},
            "timestamp": at,
        }
    )


# --- consent ------------------------------------------------------------------


def test_nothing_is_recorded_until_it_is_turned_on(repo):
    runner, where = repo
    result = runner.invoke(cli, ["hook", "skill-use"], input=event(where))
    assert result.exit_code == 0

    report = json.loads(runner.invoke(cli, ["skills", "report", "--json"]).output)
    assert report["rows"] == []
    assert report["coverage"]["watching"] is False


def test_the_hook_is_installed_only_when_asked(repo):
    runner, where = repo
    settings = where / ".claude" / "settings.json"

    before = json.loads(settings.read_text(encoding="utf-8")) if settings.exists() else {}
    assert "PostToolUse" not in (before.get("hooks") or {})

    assert runner.invoke(cli, ["skills", "observe", "enable"]).exit_code == 0
    after = json.loads(settings.read_text(encoding="utf-8"))
    assert any(
        h["command"] == "flanner hook skill-use"
        for entry in after["hooks"]["PostToolUse"]
        for h in entry["hooks"]
    )


def test_turning_it_off_takes_the_hook_back_out(repo):
    """A disabled hook that stays installed still starts a process per use."""
    runner, where = repo
    settings = where / ".claude" / "settings.json"

    runner.invoke(cli, ["skills", "observe", "enable"])
    runner.invoke(cli, ["skills", "observe", "disable"])

    after = json.loads(settings.read_text(encoding="utf-8"))
    assert "PostToolUse" not in (after.get("hooks") or {})
    # The guard-write hook is a different decision and must survive.
    assert after["hooks"]["PreToolUse"]


def test_turning_it_off_keeps_what_was_recorded(repo):
    runner, where = repo
    runner.invoke(cli, ["skills", "observe", "enable"])
    runner.invoke(cli, ["hook", "skill-use"], input=event(where))

    stopped = runner.invoke(cli, ["skills", "observe", "disable"])
    assert "1 recorded use" in stopped.output


# --- recording ----------------------------------------------------------------


def test_an_invocation_is_recorded_and_attributed(repo):
    runner, where = repo
    runner.invoke(cli, ["skills", "observe", "enable"])
    assert runner.invoke(cli, ["hook", "skill-use"], input=event(where)).exit_code == 0

    report = json.loads(runner.invoke(cli, ["skills", "report", "--json"]).output)
    assert len(report["rows"]) == 1
    row = report["rows"][0]
    assert row["skill"] == "demo-skill"
    assert row["invocations"] == 1
    assert row["attributed"] == 1
    assert row["certainty"] == "observed"


def test_the_same_event_twice_is_one_use(repo):
    """A hook can fire again after a retry, and a doubled count reads as real."""
    runner, where = repo
    runner.invoke(cli, ["skills", "observe", "enable"])
    payload = event(where)
    runner.invoke(cli, ["hook", "skill-use"], input=payload)
    runner.invoke(cli, ["hook", "skill-use"], input=payload)

    report = json.loads(runner.invoke(cli, ["skills", "report", "--json"]).output)
    assert report["rows"][0]["invocations"] == 1


def test_two_genuine_uses_are_two(repo):
    runner, where = repo
    runner.invoke(cli, ["skills", "observe", "enable"])
    runner.invoke(cli, ["hook", "skill-use"], input=event(where, at="2026-01-01T00:00:00Z"))
    runner.invoke(cli, ["hook", "skill-use"], input=event(where, at="2026-01-01T00:05:00Z"))

    report = json.loads(runner.invoke(cli, ["skills", "report", "--json"]).output)
    assert report["rows"][0]["invocations"] == 2


def test_a_use_of_something_not_installed_is_unattributed(repo):
    """Recorded, but tied to no version. Guessing the newest would corrupt
    every comparison drawn from it afterwards."""
    runner, where = repo
    runner.invoke(cli, ["skills", "observe", "enable"])
    runner.invoke(cli, ["hook", "skill-use"], input=event(where, skill="never-installed"))

    report = json.loads(runner.invoke(cli, ["skills", "report", "--json"]).output)
    row = next(r for r in report["rows"] if r["skill"] == "never-installed")
    assert row["invocations"] == 1
    assert row["attributed"] == 0


def test_a_payload_for_another_tool_is_ignored(repo):
    runner, where = repo
    runner.invoke(cli, ["skills", "observe", "enable"])
    other = json.dumps({"cwd": str(where), "tool_name": "Bash", "tool_input": {"command": "ls"}})
    runner.invoke(cli, ["hook", "skill-use"], input=other)

    report = json.loads(runner.invoke(cli, ["skills", "report", "--json"]).output)
    assert report["rows"] == []


def test_nonsense_never_breaks_the_hook(repo):
    """A hook that throws interrupts somebody's work; no statistic is worth that."""
    runner, where = repo
    runner.invoke(cli, ["skills", "observe", "enable"])
    for junk in ("", "not json", "{}", '{"tool_name": "Skill"}'):
        assert runner.invoke(cli, ["hook", "skill-use"], input=junk).exit_code == 0


# --- coverage -----------------------------------------------------------------


def test_a_report_says_when_nothing_was_watching(repo):
    """Zero uses and zero coverage are different facts and must read differently."""
    runner, _ = repo
    result = runner.invoke(cli, ["skills", "report"])
    assert "Nothing was watching" in result.output


def test_a_report_stops_saying_it_once_something_is(repo):
    runner, where = repo
    runner.invoke(cli, ["skills", "observe", "enable"])
    runner.invoke(cli, ["hook", "skill-use"], input=event(where))
    result = runner.invoke(cli, ["skills", "report"])
    assert "Nothing was watching" not in result.output


def test_status_shows_where_it_is_on(repo):
    runner, _ = repo
    assert "off everywhere" in runner.invoke(cli, ["skills", "observe", "status"]).output

    runner.invoke(cli, ["skills", "observe", "enable"])
    state = json.loads(runner.invoke(cli, ["skills", "observe", "status", "--json"]).output)
    assert state["scopes"][0]["observing"] is True
    assert state["scopes"][0]["project"] == "demo"


def test_csv_carries_the_window_with_the_numbers(repo):
    """A table of counts detached from its window is how a false claim gets made."""
    runner, where = repo
    runner.invoke(cli, ["skills", "observe", "enable"])
    runner.invoke(cli, ["hook", "skill-use"], input=event(where))

    out = runner.invoke(cli, ["skills", "report", "--csv"]).output
    assert out.startswith("# window: last 30 days")
    assert "watching=True" in out
    assert "demo-skill,1,1," in out


# --- retention ----------------------------------------------------------------


def test_purge_deletes_what_was_recorded(repo):
    runner, where = repo
    runner.invoke(cli, ["skills", "observe", "enable"])
    runner.invoke(cli, ["hook", "skill-use"], input=event(where))

    gone = runner.invoke(cli, ["skills", "data", "purge", "--yes"])
    assert "Deleted 1" in gone.output
    report = json.loads(runner.invoke(cli, ["skills", "report", "--json"]).output)
    assert report["rows"] == []


def test_purge_asks_first(repo):
    runner, where = repo
    runner.invoke(cli, ["skills", "observe", "enable"])
    runner.invoke(cli, ["hook", "skill-use"], input=event(where))

    refused = runner.invoke(cli, ["skills", "data", "purge"], input="n\n")
    assert "Nothing deleted" in refused.output
    report = json.loads(runner.invoke(cli, ["skills", "report", "--json"]).output)
    assert report["rows"][0]["invocations"] == 1


# --- managed packages ---------------------------------------------------------


def test_adopting_copies_rather_than_moves(repo):
    runner, where = repo
    package = where / ".claude" / "skills" / "demo-skill"
    result = runner.invoke(cli, ["skills", "adopt", "demo-skill"])

    assert result.exit_code == 0, result.output
    assert (package / "SKILL.md").is_file(), "adopting must not move the original"
    assert len(skills_manage.stored()) == 1
    assert skills_manage.stored()[0]["verified"]


def test_adopting_the_same_bytes_twice_stores_one_copy(repo):
    runner, _ = repo
    runner.invoke(cli, ["skills", "adopt", "demo-skill"])
    second = runner.invoke(cli, ["skills", "adopt", "demo-skill"])
    assert "Already stored" in second.output
    assert len(skills_manage.stored()) == 1


def test_installing_over_a_directory_flanner_does_not_own_is_refused(repo):
    runner, _ = repo
    runner.invoke(cli, ["skills", "adopt", "demo-skill"])
    digest = skills_manage.stored()[0]["manifest_hash"]

    refused = runner.invoke(cli, ["skills", "install", digest])
    assert refused.exit_code == 1
    assert "not installed by flanner" in refused.output


def test_an_install_can_be_rolled_back(repo):
    """PKG-01: what an install replaced has to still be recoverable."""
    runner, where = repo
    manifest = where / ".claude" / "skills" / "demo-skill" / "SKILL.md"

    runner.invoke(cli, ["skills", "adopt", "demo-skill"])
    first = skills_manage.stored()[0]["manifest_hash"]

    manifest.write_text(
        "---\nname: demo-skill\ndescription: Second version\n---\n\nTwo.\n", encoding="utf-8"
    )
    runner.invoke(cli, ["skills", "scan"])
    runner.invoke(cli, ["skills", "adopt", "demo-skill"])

    installed = runner.invoke(cli, ["skills", "install", first, "--force"])
    assert installed.exit_code == 0, installed.output
    assert "Second version" not in manifest.read_text(encoding="utf-8")

    rows = json.loads(runner.invoke(cli, ["skills", "installs", "--json"]).output)
    assert rows[0]["intact"] is True

    back = runner.invoke(cli, ["skills", "rollback", rows[0]["id"]])
    assert back.exit_code == 0, back.output
    assert "Second version" in manifest.read_text(encoding="utf-8")


def test_an_install_of_what_is_already_there_changes_nothing(repo):
    runner, where = repo
    runner.invoke(cli, ["skills", "adopt", "demo-skill"])
    digest = skills_manage.stored()[0]["manifest_hash"]

    runner.invoke(cli, ["skills", "install", digest, "--force"])
    again = runner.invoke(cli, ["skills", "install", digest])
    assert "already holds exactly these bytes" in again.output


def test_a_hand_edit_makes_the_directory_somebody_elses_again(repo):
    """Overwriting an edit somebody made by hand is the failure worth refusing."""
    runner, where = repo
    manifest = where / ".claude" / "skills" / "demo-skill" / "SKILL.md"

    runner.invoke(cli, ["skills", "adopt", "demo-skill"])
    digest = skills_manage.stored()[0]["manifest_hash"]
    runner.invoke(cli, ["skills", "install", digest, "--force"])

    manifest.write_text(manifest.read_text(encoding="utf-8") + "\nby hand\n", encoding="utf-8")
    refused = runner.invoke(cli, ["skills", "install", digest])
    assert refused.exit_code == 1
    assert "by hand" in manifest.read_text(encoding="utf-8")

    rows = json.loads(runner.invoke(cli, ["skills", "installs", "--json"]).output)
    assert rows[0]["intact"] is False


def test_installing_an_unknown_hash_is_refused(repo):
    runner, _ = repo
    result = runner.invoke(cli, ["skills", "install", "sha256:" + "0" * 64, "--name", "x"])
    assert result.exit_code == 1
    assert "no verified snapshot" in result.output


def test_a_snapshot_that_was_tampered_with_stops_verifying(repo):
    runner, _ = repo
    runner.invoke(cli, ["skills", "adopt", "demo-skill"])
    stored = skills_manage.stored()[0]
    (Path(stored["path"]) / "SKILL.md").write_text("tampered\n", encoding="utf-8")

    assert skills_manage.stored()[0]["verified"] is False
    assert skills_manage.verify(stored["manifest_hash"]) is False


# --- the local web page -------------------------------------------------------


def test_the_page_carries_every_workflow_the_cli_has(repo):
    """UI-01: parity is an acceptance condition, not a nicety."""
    from starlette.testclient import TestClient

    from flanner.web import app

    runner, where = repo
    client = TestClient(app, base_url="http://127.0.0.1:8000")

    page = client.get("/skills")
    assert page.status_code == 200
    assert "Watching skill use" in page.text
    assert "Start watching this project" in page.text

    turned_on = client.post(
        "/skills/observe", data={"action": "enable", "retention_days": 30}, follow_redirects=False
    )
    assert turned_on.status_code == 303

    runner.invoke(cli, ["hook", "skill-use"], input=event(where))
    used = client.get("/skills")
    assert "Stop watching" in used.text
    assert "demo-skill" in used.text

    emptied = client.post("/skills/purge", data={"scope": "project"}, follow_redirects=False)
    assert "deleted+1" in (emptied.headers.get("location") or "")

    turned_off = client.post("/skills/observe", data={"action": "disable"}, follow_redirects=False)
    assert turned_off.status_code == 303
    assert "Start watching this project" in client.get("/skills").text


def test_the_page_can_roll_an_install_back(repo):
    from starlette.testclient import TestClient

    from flanner.web import app

    runner, where = repo
    manifest = where / ".claude" / "skills" / "demo-skill" / "SKILL.md"

    runner.invoke(cli, ["skills", "adopt", "demo-skill"])
    first = skills_manage.stored()[0]["manifest_hash"]
    manifest.write_text(
        "---\nname: demo-skill\ndescription: Second version\n---\n\nTwo.\n", encoding="utf-8"
    )
    runner.invoke(cli, ["skills", "adopt", "demo-skill"])
    runner.invoke(cli, ["skills", "install", first, "--force"])

    client = TestClient(app, base_url="http://127.0.0.1:8000")
    rows = json.loads(runner.invoke(cli, ["skills", "installs", "--json"]).output)
    done = client.post(
        "/skills/rollback", data={"installation_id": rows[0]["id"]}, follow_redirects=False
    )
    assert done.status_code == 303
    assert "Second version" in manifest.read_text(encoding="utf-8")


# --- the domain, directly -----------------------------------------------------


def test_a_dedupe_key_separates_two_uses_a_moment_apart():
    one = skills_observe.dedupe_key("s", "skill", "2026-01-01T00:00:00Z")
    same = skills_observe.dedupe_key("s", "skill", "2026-01-01T00:00:00Z")
    other = skills_observe.dedupe_key("s", "skill", "2026-01-01T00:00:01Z")
    assert one == same
    assert one != other


def test_the_snapshot_store_is_named_by_content(repo, tmp_path):
    package = write_skill(tmp_path / "elsewhere", "thing")
    kept = skills_manage.snapshot(package)
    assert kept.path.name == kept.manifest_hash.replace("sha256:", "")
    assert skills_manage.verify(kept.manifest_hash)
