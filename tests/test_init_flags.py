"""What `flanner init` can be told to do, and to leave alone.

Two questions this answers. Which agents get registered, now that `init`
does the registration `flanner setup` used to; and whether the plan files
already sitting in a repository get imported when it is adopted.

The second is the case somebody cloning a colleague's repository hits. The
plans are committed and right there on disk, and until `--sync` existed the
catalog listed none of them.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from uuid import uuid4

import pytest
from click.testing import CliRunner

import flanner.claude_integration as ci
from flanner.cli import cli


@pytest.fixture
def runner(tmp_path, monkeypatch):
    home = Path(tmp_path) / "flanner-home"
    home.mkdir(parents=True, exist_ok=True)
    # Registering with Claude Code means running somebody else's binary. A
    # test must not, so the path where it is absent is the one exercised.
    real = shutil.which
    monkeypatch.setattr(
        shutil, "which", lambda name, *a, **k: None if name == "claude" else real(name, *a, **k)
    )
    return CliRunner(env={"FLANNER_HOME": str(home), "FLANNER_DB_PATH": None})


@pytest.fixture
def desktop_config(tmp_path, monkeypatch):
    path = tmp_path / "claude_desktop_config.json"
    monkeypatch.setattr(ci, "get_claude_config_path", lambda: path)
    return path


@pytest.fixture
def repo_with_plans(tmp_path):
    """A repository holding plan files no catalog on this machine has seen.

    What a clone of a colleague's project looks like. The headers carry the
    ids from the machine that wrote them, including a project id belonging to
    a project that does not exist here — which is correct, and is why the
    import keys on the plan's own id and attaches it to the local project.
    """
    repo = tmp_path / "repo"
    plans = repo / ".plans"
    plans.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)  # noqa: S603,S607

    theirs = uuid4()
    for name in ("architecture", "migration"):
        (plans / f"{name}.md").write_text(
            "---\n"
            "mcp_plan_file: true\n"
            f"project_id: {theirs}\n"
            f"plan_file_id: {uuid4()}\n"
            f"plan_name: {name}\n"
            "version: 1\n"
            "created_by: someone-else\n"
            "---\n"
            f"\n# {name}\n\nBody.\n",
            encoding="utf-8",
        )
    return repo


# --- --sync -----------------------------------------------------------------


def test_adopting_a_repository_leaves_its_plans_alone_by_default(runner, repo_with_plans):
    result = runner.invoke(
        cli, ["init", "--skip-claude", "--project-root", str(repo_with_plans)], input="proj\n"
    )
    assert result.exit_code == 0, result.output

    listed = runner.invoke(cli, ["list", "--project", "proj"]).output
    assert "architecture" not in listed


def test_sync_imports_what_the_repository_already_holds(runner, repo_with_plans):
    """The whole point: adopt and import in one command."""
    result = runner.invoke(
        cli,
        ["init", "--skip-claude", "--sync", "--project-root", str(repo_with_plans)],
        input="proj\n",
    )

    assert result.exit_code == 0, result.output
    assert "Imported 2 of 2" in result.output, result.output
    listed = runner.invoke(cli, ["list", "--project", "proj"]).output
    assert "architecture" in listed and "migration" in listed


def test_sync_on_a_repository_with_nothing_to_import_is_not_an_error(runner, tmp_path):
    repo = tmp_path / "empty"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)  # noqa: S603,S607

    result = runner.invoke(
        cli, ["init", "--skip-claude", "--sync", "--project-root", str(repo)], input="empty\n"
    )

    assert result.exit_code == 0, result.output


def test_sync_can_be_run_twice_without_importing_twice(runner, repo_with_plans):
    """`init` is advertised as safe to re-run, and the flag must not break that."""
    args = ["init", "--skip-claude", "--sync", "--project-root", str(repo_with_plans)]
    runner.invoke(cli, args, input="proj\n")

    again = runner.invoke(cli, args)

    assert again.exit_code == 0, again.output
    assert "Imported 0 of 2" in again.output, again.output


# --- --setup ----------------------------------------------------------------


def _nudge_written() -> bool:
    return (Path.home() / ".claude" / "CLAUDE.md").exists()


def test_by_default_every_agent_is_registered(runner, repo_with_plans, desktop_config):
    result = runner.invoke(cli, ["init", "--project-root", str(repo_with_plans)], input="proj\n")

    assert result.exit_code == 0, result.output
    assert "flanner" in json.loads(desktop_config.read_text())["mcpServers"]
    assert "Codex" in result.output
    assert _nudge_written()


def test_naming_one_agent_means_that_one_only(runner, repo_with_plans, desktop_config):
    """The ambiguity a flag per agent could not resolve: `--setup codex` is
    Codex instead of the default, not Codex as well as it."""
    result = runner.invoke(
        cli,
        ["init", "--setup", "codex", "--project-root", str(repo_with_plans)],
        input="proj\n",
    )

    assert result.exit_code == 0, result.output
    assert "Codex" in result.output
    assert not desktop_config.exists(), "registered Claude Desktop when only Codex was asked for"
    assert not _nudge_written(), "wrote Claude's instruction file for a Codex-only setup"


def test_agents_can_be_named_more_than_once(runner, repo_with_plans, desktop_config):
    result = runner.invoke(
        cli,
        [
            "init",
            "--setup",
            "codex",
            "--setup",
            "claude-desktop",
            "--project-root",
            str(repo_with_plans),
        ],
        input="proj\n",
    )

    assert result.exit_code == 0, result.output
    assert "flanner" in json.loads(desktop_config.read_text())["mcpServers"]
    assert "Codex" in result.output


def test_none_registers_nothing_and_so_does_the_old_flag(runner, repo_with_plans, desktop_config):
    """`--skip-claude` predates this and is in the README, so it keeps working."""
    for args in (["--setup", "none"], ["--skip-claude"]):
        result = runner.invoke(
            cli, ["init", *args, "--project-root", str(repo_with_plans)], input="proj\n"
        )

        assert result.exit_code == 0, result.output
        assert not desktop_config.exists(), args
        assert "[Agents]" not in result.output, args
        assert not _nudge_written(), args


def test_an_unknown_agent_is_refused_rather_than_ignored(runner, repo_with_plans):
    """Silently registering nothing for a typo would be the worst outcome:
    the machine looks set up and no agent can reach it."""
    result = runner.invoke(
        cli, ["init", "--setup", "emacs", "--project-root", str(repo_with_plans)]
    )

    assert result.exit_code != 0
    assert "emacs" in result.output


def test_the_choices_are_the_agents_status_reports(runner):
    """One list. A flag naming an agent the status table does not would be a
    setup nobody could then check."""
    from flanner.cli import AGENTS

    offered = runner.invoke(cli, ["init", "--help"]).output
    for agent in AGENTS:
        assert agent in offered, agent
