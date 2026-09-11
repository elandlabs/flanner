"""What the first few commands tell a new person, and whether it is true.

`flanner status` read Claude Desktop's config file and reported the result
as "Claude Code". They are different programs with different files, and
nothing `init` writes for Claude Code lives in the one being checked — so a
correct setup read as "not registered", on the command somebody runs to
find out whether it worked. Codex was not mentioned at all, though `init`
hands it an AGENTS.md block that talks about MCP tools it has no way to
reach until a file it never heard of is edited. `setup` now makes that
edit where it provably cannot clobber anything, and says so where not.

And `init` died with "Aborted!" when nothing was attached to stdin, which
made the very first command unusable from a script.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

import flanner.claude_integration as ci
from flanner import cli as cli_module
from flanner.cli import cli


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def desktop(tmp_path, monkeypatch) -> Path:
    path = tmp_path / "claude_desktop_config.json"
    monkeypatch.setattr(ci, "get_claude_config_path", lambda: path)
    return path


# --- init without a terminal -----------------------------------------------


def test_init_with_nothing_on_stdin_takes_the_offered_name(runner, git_repo) -> None:
    """A script, CI, `< /dev/null`: the default is the answer, not "Aborted!"."""
    result = runner.invoke(cli, ["init", "--skip-claude", "--project-root", str(git_repo)])

    assert result.exit_code == 0, result.output
    assert "No terminal to ask" in result.output
    listed = runner.invoke(cli, ["list", "--output", "json"])
    assert [p["name"] for p in json.loads(listed.output)] == [git_repo.name]


def test_init_still_takes_a_typed_name(runner, git_repo) -> None:
    result = runner.invoke(
        cli, ["init", "--skip-claude", "--project-root", str(git_repo)], input="typed\n"
    )

    assert result.exit_code == 0, result.output
    listed = runner.invoke(cli, ["list", "--output", "json"])
    assert [p["name"] for p in json.loads(listed.output)] == ["typed"]


# --- status, one row per agent ----------------------------------------------


def _rows(output: str) -> dict[str, str]:
    """The status table as {label: rest of line}, whitespace collapsed."""
    rows: dict[str, str] = {}
    for line in output.splitlines():
        parts = line.split("  ", 1)
        if len(parts) == 2 and parts[0].strip():
            rows[parts[0].strip()] = " ".join(parts[1].split())
    return rows


def test_status_names_each_agent_and_does_not_call_desktop_claude_code(runner, desktop) -> None:
    result = runner.invoke(cli, ["status"])

    rows = _rows(result.output)
    assert "Claude Desktop" in rows
    assert "Claude Code" in rows
    assert "Codex" in rows
    assert "not registered" in rows["Claude Code"]


def test_status_sees_a_project_mcp_json_where_claude_code_would(
    runner, desktop, git_repo, monkeypatch
) -> None:
    """The file `init` writes, found by walking up as Claude Code does."""
    (git_repo / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"flanner": {"command": "flanner-mcp", "args": []}}}),
        encoding="utf-8",
    )
    nested = git_repo / "src" / "deep"
    nested.mkdir(parents=True)
    monkeypatch.chdir(nested)

    rows = _rows(runner.invoke(cli, ["status"]).output)

    assert "registered" in rows["Claude Code"] and "not registered" not in rows["Claude Code"]
    assert ".mcp.json" in rows["Claude Code"]


def test_status_sees_a_user_scope_registration(runner, desktop) -> None:
    """What `claude mcp add -s user` leaves behind."""
    ci.claude_code_user_config_path().write_text(
        json.dumps({"mcpServers": {"flanner": {"command": "flanner-mcp"}}}), encoding="utf-8"
    )

    rows = _rows(runner.invoke(cli, ["status"]).output)

    assert "user scope" in rows["Claude Code"]


def test_status_does_not_mistake_desktop_registration_for_claude_code(runner, desktop) -> None:
    """The exact confusion this replaces, kept as a test so it stays gone."""
    ci.register_mcp_server()

    rows = _rows(runner.invoke(cli, ["status"]).output)

    assert (
        "registered" in rows["Claude Desktop"] and "not registered" not in rows["Claude Desktop"]
    )
    assert "not registered" in rows["Claude Code"]


def test_status_reads_codex_config(runner, desktop) -> None:
    path = ci.codex_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('[mcp_servers.other]\ncommand = "x"\n', encoding="utf-8")
    assert "not registered" in _rows(runner.invoke(cli, ["status"]).output)["Codex"]

    path.write_text('[mcp_servers.flanner]\ncommand = "flanner-mcp"\n', encoding="utf-8")
    assert "not registered" not in _rows(runner.invoke(cli, ["status"]).output)["Codex"]


# --- setup says the Codex step out loud ---------------------------------------


@pytest.fixture
def codex(tmp_path, monkeypatch):
    """A Codex config path under tmp, and no `claude` or `codex` binary."""
    import shutil

    path = tmp_path / ".codex" / "config.toml"
    monkeypatch.setattr(ci, "codex_config_path", lambda: path)
    monkeypatch.setattr(shutil, "which", lambda name: None)
    return path


def _collapsed(output: str) -> str:
    # Rich wraps a long temp path, so compare with whitespace removed.
    return "".join(output.split())


def test_setup_prints_the_codex_lines_when_codex_is_not_installed(runner, desktop, codex) -> None:
    """Nothing is created for somebody who does not use Codex. `init` sets up
    every agent by default, and a config directory appearing for a tool that
    is not installed would be litter. The lines are still shown."""
    result = runner.invoke(cli, ["setup"])

    assert not codex.exists(), "created a Codex config on a machine without Codex"
    assert "Codex: not found" in result.output
    assert "[mcp_servers.flanner]" in result.output
    assert _collapsed(str(codex)) in _collapsed(result.output)


def test_setup_registers_codex_by_appending_its_own_table(runner, desktop, codex) -> None:
    """The one edit that cannot clobber anything: append, parse, then replace."""
    import tomllib

    codex.parent.mkdir(parents=True)
    original = '# mine\nmodel = "gpt-5"\n\n[mcp_servers.other]\ncommand = "x"\n'
    codex.write_text(original, encoding="utf-8")

    result = runner.invoke(cli, ["setup"])

    written = codex.read_text(encoding="utf-8")
    assert "Codex: registered" in result.output
    assert written.startswith(original), "rewrote what was already there"
    parsed = tomllib.loads(written)
    assert parsed["model"] == "gpt-5"
    assert parsed["mcp_servers"]["other"] == {"command": "x"}
    assert parsed["mcp_servers"]["flanner"] == {"command": "flanner-mcp"}


def test_registering_codex_twice_changes_nothing(runner, desktop, codex) -> None:
    codex.parent.mkdir(parents=True)
    runner.invoke(cli, ["setup"])
    once = codex.read_text(encoding="utf-8")

    result = runner.invoke(cli, ["setup"])

    assert codex.read_text(encoding="utf-8") == once
    assert "Codex: already registered" in result.output


def test_a_codex_config_that_does_not_parse_is_left_exactly_as_it_was(
    runner, desktop, codex
) -> None:
    """Somebody mid-edit, or a file this package cannot read: not ours to fix."""
    codex.parent.mkdir(parents=True)
    broken = "[mcp_servers\ncommand = \n"
    codex.write_text(broken, encoding="utf-8")

    result = runner.invoke(cli, ["setup"])

    assert codex.read_text(encoding="utf-8") == broken
    assert "does not parse" in result.output
    assert "[mcp_servers.flanner]" in result.output, "left the user without the lines to add"


def test_a_flanner_entry_written_another_way_counts_as_registered(runner, desktop, codex) -> None:
    """An inline table is as much a registration as a header."""
    codex.parent.mkdir(parents=True)
    inline = 'mcp_servers = { flanner = { command = "flanner-mcp" } }\n'
    codex.write_text(inline, encoding="utf-8")

    result = runner.invoke(cli, ["setup"])

    assert codex.read_text(encoding="utf-8") == inline
    assert "Codex: already registered" in result.output
    assert ci.codex_registration() is True


def test_an_empty_codex_config_is_filled_not_refused(runner, desktop, codex) -> None:
    import tomllib

    codex.parent.mkdir(parents=True)
    codex.write_text("", encoding="utf-8")

    runner.invoke(cli, ["setup"])

    assert tomllib.loads(codex.read_text(encoding="utf-8"))["mcp_servers"]["flanner"] == {
        "command": "flanner-mcp"
    }


def test_a_registration_file_that_is_not_json_reads_as_not_registered(desktop, tmp_path) -> None:
    """Never a traceback from a status command."""
    ci.claude_code_user_config_path().write_text("{not json", encoding="utf-8")
    (tmp_path / ".mcp.json").write_text("{not json either", encoding="utf-8")

    assert ci.claude_code_registration(tmp_path) == ""


# --- being asked for a project name, with nobody there to answer -------------
#
# `init` asks for a name and takes the directory when there is no one to ask.
# Getting that wrong is not cosmetic: it is the difference between a CI step
# adopting a repository and a CI step dying with "Aborted!".


def _init_unattended(tmp_path, name: str, **popen):
    """Run `flanner init` in its own process, with the given stdin."""
    home = tmp_path / f"home-{name}"
    home.mkdir()
    repo = tmp_path / name
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)  # noqa: S603,S607
    env = {**os.environ, "FLANNER_HOME": str(home)}
    env.pop("FLANNER_DB_PATH", None)
    return subprocess.run(  # noqa: S603
        [sys.executable, "-m", "flanner", "init", "--skip-claude", "--project-root", str(repo)],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
        **popen,
    )


def test_the_null_device_is_not_a_person_cancelling(tmp_path):
    """The case this used to get wrong.

    `flanner init < NUL` is a script or a CI step, and it used to die with
    "Aborted!" because the check for "is anybody there" was `isatty()`, and
    on Windows the null device says yes.
    """
    result = _init_unattended(tmp_path, "nulrepo", stdin=subprocess.DEVNULL)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "Created project: nulrepo" in result.stdout
    assert "Aborted" not in result.stderr


def test_a_closed_pipe_takes_the_directory_name(tmp_path):
    result = _init_unattended(tmp_path, "piperepo", input="")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "Created project: piperepo" in result.stdout


def test_a_typed_name_is_used(tmp_path):
    """The fallback must not have swallowed the ordinary case."""
    result = _init_unattended(tmp_path, "typedrepo", input="chosen\n")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "Created project: chosen" in result.stdout


def test_pressing_enter_accepts_the_offer(tmp_path):
    result = _init_unattended(tmp_path, "enterrepo", input="\n")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "Created project: enterrepo" in result.stdout


def test_nothing_decides_this_by_asking_whether_stdin_is_a_terminal():
    """The guess that caused it. Reading the line tells end-of-input and
    Ctrl-C apart directly, so there is nothing left to guess with.

    Checked against the parsed code rather than the text, because the
    docstring explaining why `isatty` is not used contains the word.
    """
    import ast

    tree = ast.parse(Path(cli_module.__file__).read_text(encoding="utf-8"))
    called = [
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    ]

    assert "isatty" not in called
