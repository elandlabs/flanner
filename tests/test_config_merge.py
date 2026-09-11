"""What `flanner init` does to config files it did not write.

`.claude/settings.json` and `.mcp.json` belong to the repo, not to flanner.
Both are read, merged into, and written back — and a file that failed to
parse was read as `{}`, so the write replaced it. Every other MCP server the
repo declared, every hook, every permission setting: gone, because of a
trailing comma or because somebody was mid-edit when they ran `init`.

The cost of refusing is one integration that installs on the next run once
the file parses. The cost of the alternative is work that cannot be got
back.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from flanner.agent_hooks import (
    ensure_project_mcp_json,
    ensure_settings_hook,
    wire_agent_integration,
)
from flanner.database import create_project, get_session
from flanner.exceptions import ConfigError

# What somebody would lose. A trailing comma is the most ordinary way to get
# here, and this file names two servers flanner knows nothing about.
THEIRS = """{
  "mcpServers": {
    "postgres": {"command": "mcp-postgres", "args": ["--dsn", "$DSN"]},
    "github": {"command": "mcp-github", "args": []},
  }
}
"""


@pytest.fixture
def repo(tmp_path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    return root


def test_an_unparseable_mcp_json_is_not_replaced(repo) -> None:
    """The defect, with the file whose loss would hurt most."""
    path = repo / ".mcp.json"
    path.write_text(THEIRS, encoding="utf-8")

    with pytest.raises(ConfigError) as refused:
        ensure_project_mcp_json(str(repo))

    assert path.read_text(encoding="utf-8") == THEIRS, "their servers were overwritten"
    assert "not valid json" in str(refused.value)
    assert ".mcp.json" in str(refused.value)


def test_an_unparseable_settings_file_is_not_replaced(repo) -> None:
    path = repo / ".claude" / "settings.json"
    path.parent.mkdir(parents=True)
    original = '{"permissions": {"allow": ["Bash(git:*)"]},}'
    path.write_text(original, encoding="utf-8")

    with pytest.raises(ConfigError):
        ensure_settings_hook(str(repo))

    assert path.read_text(encoding="utf-8") == original


def test_a_json_file_that_is_not_an_object_is_not_replaced(repo) -> None:
    """Valid json, nothing to merge into. Still not ours to discard."""
    path = repo / ".mcp.json"
    path.write_text("[1, 2, 3]", encoding="utf-8")

    with pytest.raises(ConfigError) as refused:
        ensure_project_mcp_json(str(repo))

    assert path.read_text(encoding="utf-8") == "[1, 2, 3]"
    assert "json object" in str(refused.value)


def test_a_valid_file_still_keeps_everything_it_had(repo) -> None:
    """Refusing must not have come at the cost of merging properly."""
    path = repo / ".mcp.json"
    path.write_text(
        json.dumps({"mcpServers": {"postgres": {"command": "mcp-postgres", "args": []}}}),
        encoding="utf-8",
    )

    assert ensure_project_mcp_json(str(repo))

    after = json.loads(path.read_text(encoding="utf-8"))
    assert set(after["mcpServers"]) == {"postgres", "flanner"}


def test_installing_twice_changes_nothing_the_second_time(repo) -> None:
    assert ensure_project_mcp_json(str(repo))
    assert not ensure_project_mcp_json(str(repo))


def test_one_bad_file_does_not_cost_the_repo_the_rest(repo, db, monkeypatch) -> None:
    """The reason this is a skip rather than a raised error.

    Somebody with a broken `.mcp.json` should still get their CLAUDE.md
    block and their skill, and should be told which one file was left alone
    and why — not have the whole integration abandoned.
    """
    monkeypatch.setenv("FLANNER_HOME", str(repo.parent / "home"))
    (repo / ".mcp.json").write_text(THEIRS, encoding="utf-8")
    session = get_session()
    project = create_project(session, name="p", project_root=str(repo), auto_gitignore=False)

    wiring = wire_agent_integration(str(repo), project)

    assert any("CLAUDE.md" in item for item in wiring.installed), "the rest was abandoned too"
    assert any("flanner skills" in item for item in wiring.installed)
    assert len(wiring.skipped) == 1
    assert ".mcp.json" in wiring.skipped[0]
    assert (repo / ".mcp.json").read_text(encoding="utf-8") == THEIRS
