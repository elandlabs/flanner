"""The memory commands, driven the way a person drives them.

Written because `flanner mem show` crashed on a name that was imported for
type annotations only, and nothing noticed. The domain had tests, the web
had tests, and the command line in between had none, so every command here
now gets exercised at least once.
"""

from __future__ import annotations

import json
import subprocess
from uuid import UUID

import pytest
from click.testing import CliRunner

from flanner.cli import cli

PNG = b"\x89PNG\r\n\x1a\n" + b"pixels" * 40


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """An adopted repository, as the command line sees one."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("FLANNER_HOME", str(home))
    where = tmp_path / "repo"
    where.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=where, check=True)  # noqa: S603,S607
    monkeypatch.chdir(where)

    runner = CliRunner()
    started = runner.invoke(
        cli, ["init", "--skip-claude", "--project-root", str(where)], input="demo\n"
    )
    assert started.exit_code == 0, started.output
    return runner, where


def _run(runner, *args, **kwargs):
    result = runner.invoke(cli, list(args), **kwargs)
    assert result.exit_code == 0, result.output
    return result


def _first_id(runner) -> str:
    listed = _run(runner, "mem", "list", "--output", "json")
    return str(json.loads(listed.output)[0]["id"])


# --- the ordinary path --------------------------------------------------------


def test_remember_then_recall(repo):
    runner, where = repo

    _run(runner, "mem", "remember", "Use advisory locks.", "--category", "decision")
    found = _run(runner, "mem", "recall", "advisory locks", "--output", "json")

    assert json.loads(found.output)["memories"][0]["title"] == "Use advisory locks."


def test_remember_reads_a_body_from_stdin(repo):
    """So a long memory does not have to survive shell quoting."""
    runner, where = repo

    _run(
        runner,
        "mem",
        "remember",
        "-",
        "--category",
        "lesson",
        input="The integration test fails behind a firewall that blocks UDP.\n",
    )

    listed = json.loads(_run(runner, "mem", "list", "--output", "json").output)
    assert "blocks UDP" in listed[0]["title"]


def test_show_prints_a_memory(repo):
    """The command that was crashing."""
    runner, where = repo
    _run(runner, "mem", "remember", "Use advisory locks.", "--category", "decision")

    shown = _run(runner, "mem", "show", _first_id(runner))

    assert "Use advisory locks." in shown.output
    assert "decision" in shown.output


def test_show_as_json(repo):
    runner, where = repo
    _run(runner, "mem", "remember", "Use advisory locks.", "--category", "decision")

    shown = _run(runner, "mem", "show", _first_id(runner), "--output", "json")

    assert json.loads(shown.output)["category"] == "decision"


def test_list_says_when_there_is_nothing(repo):
    runner, where = repo

    listed = _run(runner, "mem", "list")

    assert "Nothing remembered here yet" in listed.output


def test_supersede_then_forget_then_restore(repo):
    runner, where = repo
    _run(runner, "mem", "remember", "Use Redis for the queue.", "--category", "decision")
    original = _first_id(runner)

    _run(runner, "mem", "supersede", original, "--with", "Use advisory locks instead.")
    replacement = _first_id(runner)
    _run(runner, "mem", "forget", replacement)
    assert json.loads(_run(runner, "mem", "list", "--output", "json").output) == []

    _run(runner, "mem", "restore", replacement)
    assert len(json.loads(_run(runner, "mem", "list", "--output", "json").output)) == 1


def test_a_credential_is_refused_with_a_readable_reason(repo):
    runner, where = repo

    refused = runner.invoke(
        cli, ["mem", "remember", "key is AKIAIOSFODNN7EXAMPLE", "--category", "fact"]
    )

    assert refused.exit_code == 1
    assert "credential" in refused.output
    assert "Nothing was written" in refused.output


def test_an_unknown_id_is_a_readable_refusal(repo):
    runner, where = repo

    missing = runner.invoke(cli, ["mem", "show", "00000000-0000-0000-0000-000000000000"])

    assert missing.exit_code == 1
    assert "No memory with id" in missing.output


def test_rebuild_says_what_it_cannot_restore(repo):
    runner, where = repo
    _run(runner, "mem", "remember", "Use advisory locks.", "--category", "decision")

    rebuilt = _run(runner, "mem", "rebuild")

    assert "adopted" in rebuilt.output
    assert "Event history is not restored" in rebuilt.output


# --- capture ------------------------------------------------------------------


def test_mode_shows_and_sets(repo):
    runner, where = repo

    assert "suggest" in _run(runner, "mem", "mode").output
    _run(runner, "mem", "mode", "explicit")

    assert "explicit" in _run(runner, "mem", "mode").output


def test_policy_show_names_where_each_value_came_from(repo):
    runner, where = repo

    shown = _run(runner, "mem", "policy", "show")

    assert "capture_mode" in shown.output
    assert "default" in shown.output


def test_policy_init_then_validate(repo):
    runner, where = repo

    written = _run(runner, "mem", "policy", "init")
    checked = _run(runner, "mem", "policy", "validate")

    assert (where / ".flanner" / "memory-policy.yml").is_file()
    assert "Wrote" in written.output
    assert "valid" in checked.output


def test_policy_validate_reports_a_broken_file(repo):
    runner, where = repo
    (where / ".flanner").mkdir(exist_ok=True)
    (where / ".flanner" / "memory-policy.yml").write_text(
        "version: 1\ncapture_mode: always\n", encoding="utf-8"
    )

    broken = runner.invoke(cli, ["mem", "policy", "validate"])

    assert broken.exit_code == 1
    assert "capture_mode must be one of" in broken.output


def test_pending_says_when_nothing_is_waiting(repo):
    runner, where = repo

    assert "Nothing waiting" in _run(runner, "mem", "pending").output


# --- attachments ---------------------------------------------------------------


def test_attach_then_show_lists_it(repo, tmp_path):
    runner, where = repo
    _run(runner, "mem", "remember", "The limiter is per tenant.", "--category", "fact")
    memory_id = _first_id(runner)
    shot = tmp_path / "throttling.png"
    shot.write_bytes(PNG)

    attached = _run(runner, "mem", "attach", memory_id, str(shot), "--description", "the stack")
    shown = _run(runner, "mem", "show", memory_id)

    assert "Attached throttling.png" in attached.output
    assert "throttling.png" in shown.output
    assert "image/png" in shown.output


def test_attaching_a_file_that_is_not_there_is_refused_before_anything_runs(repo):
    """Click checks the path first, which is the cheapest place to find
    out and the only one that costs no database work."""
    runner, where = repo
    _run(runner, "mem", "remember", "A fact.", "--category", "fact")

    missing = runner.invoke(cli, ["mem", "attach", _first_id(runner), "nowhere.png"])

    assert missing.exit_code != 0
    assert "does not exist" in missing.output


def test_gc_says_when_there_is_nothing_to_collect(repo):
    runner, where = repo

    assert "Nothing to collect" in _run(runner, "mem", "gc").output


def test_detach_then_gc_removes_the_file(repo, tmp_path):
    runner, where = repo
    _run(runner, "mem", "remember", "A fact.", "--category", "fact")
    memory_id = _first_id(runner)
    shot = tmp_path / "a.png"
    shot.write_bytes(PNG)
    _run(runner, "mem", "attach", memory_id, str(shot))

    from flanner import memory_ops
    from flanner.database import get_session

    attachment = memory_ops.attachments_of(get_session(), UUID(memory_id))[0]
    _run(runner, "mem", "detach", attachment["id"])
    collected = _run(runner, "mem", "gc", "--yes")

    assert "1 file(s) removed" in collected.output
