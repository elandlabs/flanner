"""Guard-write hook decisions and init-time file wiring."""

import json

import pytest

from flanner.agent_hooks import (
    AGENT_MD_START,
    BLOCK_VERSION,
    agent_md_block,
    decide_write,
    ensure_project_mcp_json,
    ensure_settings_hook,
    install_skill,
    installed_block_version,
    run_guard_write,
    upsert_agent_md,
)
from flanner.database import create_project, get_session


def _project(session, root):
    return create_project(
        session,
        name="guarded",
        project_root=str(root),
        plan_directory=".plans",
        auto_gitignore=False,
    )


def _payload(file_path, cwd):
    return {"cwd": str(cwd), "tool_input": {"file_path": str(file_path)}}


# --- decide_write gates ------------------------------------------------------


def test_allows_write_in_unregistered_repo(db, git_repo):
    # no project created for this repo
    assert decide_write(_payload(git_repo / ".plans" / "x.md", git_repo), get_session()) is None


def test_allows_non_plan_markdown(db, git_repo):
    _project(get_session(), git_repo)
    # a README at the repo root is not in the plan dir
    assert decide_write(_payload(git_repo / "README.md", git_repo), get_session()) is None


def test_allows_non_markdown_in_plan_dir(db, git_repo):
    _project(get_session(), git_repo)
    payload = _payload(git_repo / ".plans" / "notes.txt", git_repo)
    assert decide_write(payload, get_session()) is None


def test_denies_plan_md_written_directly(db, git_repo):
    project = _project(get_session(), git_repo)
    decision = decide_write(_payload(git_repo / ".plans" / "auth.md", git_repo), get_session())
    assert decision is not None
    out = decision["hookSpecificOutput"]
    assert out["permissionDecision"] == "deny"
    assert "create_plan_file_tool" in out["permissionDecisionReason"]
    assert str(project.id) in out["permissionDecisionReason"]


def test_denies_relative_path_resolved_against_cwd(db, git_repo):
    _project(get_session(), git_repo)
    payload = {"cwd": str(git_repo), "tool_input": {"file_path": ".plans/design.md"}}
    assert decide_write(payload, get_session()) is not None


# --- run_guard_write: fail open ----------------------------------------------


def test_run_guard_write_allows_on_garbage_input(db):
    assert run_guard_write("not json", get_session()) == ""
    assert run_guard_write("", get_session()) == ""


def test_run_guard_write_emits_deny_json(db, git_repo):
    _project(get_session(), git_repo)
    raw = json.dumps(_payload(git_repo / ".plans" / "p.md", git_repo))
    out = run_guard_write(raw, get_session())
    assert json.loads(out)["hookSpecificOutput"]["permissionDecision"] == "deny"


# --- CLAUDE.md block ---------------------------------------------------------


def test_claude_md_created_and_idempotent(db, git_repo):
    project = _project(get_session(), git_repo)
    block = agent_md_block(project)

    assert upsert_agent_md(str(git_repo), "CLAUDE.md", block) is True
    text = (git_repo / "CLAUDE.md").read_text(encoding="utf-8")
    assert "managed by flanner" in text
    # second run is a no-op
    assert upsert_agent_md(str(git_repo), "CLAUDE.md", block) is False


def test_claude_md_preserves_existing_content(db, git_repo):
    (git_repo / "CLAUDE.md").write_text("# My project\n\nExisting notes.\n", encoding="utf-8")
    project = _project(get_session(), git_repo)
    upsert_agent_md(str(git_repo), "CLAUDE.md", agent_md_block(project))
    text = (git_repo / "CLAUDE.md").read_text(encoding="utf-8")
    assert "Existing notes." in text
    assert text.count(AGENT_MD_START) == 1


def test_claude_md_replaces_stale_block(db, git_repo):
    (git_repo / "CLAUDE.md").write_text(
        "<!-- flanner:managed -->\nold content\n<!-- /flanner:managed -->\n", encoding="utf-8"
    )
    project = _project(get_session(), git_repo)
    upsert_agent_md(str(git_repo), "CLAUDE.md", agent_md_block(project))
    text = (git_repo / "CLAUDE.md").read_text(encoding="utf-8")
    assert "old content" not in text
    assert text.count(AGENT_MD_START) == 1


def test_the_block_carries_the_version_that_wrote_it(db, git_repo):
    """`doctor` reads this stamp to spot instructions an upgrade left behind."""
    project = _project(get_session(), git_repo)
    upsert_agent_md(str(git_repo), "CLAUDE.md", agent_md_block(project))
    assert installed_block_version(str(git_repo), "CLAUDE.md") == BLOCK_VERSION


def test_a_block_written_before_stamps_existed_reads_as_zero(db, git_repo):
    """It has to be found and replaced, not duplicated beside the new one."""
    (git_repo / "CLAUDE.md").write_text(
        "<!-- flanner:managed -->"
        + chr(10)
        + "old"
        + chr(10)
        + "<!-- /flanner:managed -->"
        + chr(10),
        encoding="utf-8",
    )
    assert installed_block_version(str(git_repo), "CLAUDE.md") == 0

    project = _project(get_session(), git_repo)
    upsert_agent_md(str(git_repo), "CLAUDE.md", agent_md_block(project))
    text = (git_repo / "CLAUDE.md").read_text(encoding="utf-8")
    assert text.count(AGENT_MD_START) == 1
    assert installed_block_version(str(git_repo), "CLAUDE.md") == BLOCK_VERSION


def test_no_block_at_all_is_not_a_finding(db, git_repo):
    assert installed_block_version(str(git_repo), "CLAUDE.md") is None


# --- settings.json hook ------------------------------------------------------


def test_settings_hook_added_and_idempotent(db, git_repo):
    assert ensure_settings_hook(str(git_repo)) is True
    settings = json.loads((git_repo / ".claude" / "settings.json").read_text(encoding="utf-8"))
    cmds = [h["command"] for entry in settings["hooks"]["PreToolUse"] for h in entry["hooks"]]
    assert "flanner hook guard-write" in cmds
    assert ensure_settings_hook(str(git_repo)) is False  # no duplicate


def test_settings_hook_merges_with_existing(db, git_repo):
    claude_dir = git_repo / ".claude"
    claude_dir.mkdir()
    (claude_dir / "settings.json").write_text(
        json.dumps({"permissions": {"allow": ["Bash(ls:*)"]}}), encoding="utf-8"
    )
    ensure_settings_hook(str(git_repo))
    settings = json.loads((claude_dir / "settings.json").read_text(encoding="utf-8"))
    assert settings["permissions"]["allow"] == ["Bash(ls:*)"]  # preserved
    assert settings["hooks"]["PreToolUse"]


# --- .mcp.json (Claude Code CLI) ---------------------------------------------


def test_project_mcp_json_added_and_idempotent(git_repo):
    assert ensure_project_mcp_json(str(git_repo)) is True
    config = json.loads((git_repo / ".mcp.json").read_text(encoding="utf-8"))
    assert config["mcpServers"]["flanner"] == {"command": "flanner-mcp", "args": []}
    assert ensure_project_mcp_json(str(git_repo)) is False  # idempotent


def test_project_mcp_json_merges_with_existing(git_repo):
    (git_repo / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"other": {"command": "x"}}}), encoding="utf-8"
    )
    assert ensure_project_mcp_json(str(git_repo)) is True
    config = json.loads((git_repo / ".mcp.json").read_text(encoding="utf-8"))
    assert config["mcpServers"]["other"] == {"command": "x"}  # preserved
    assert config["mcpServers"]["flanner"]["command"] == "flanner-mcp"


def test_project_mcp_json_refuses_to_replace_a_malformed_file(git_repo):
    """This test used to assert the opposite, and called it recovery.

    "Recovering" meant reading an unparseable file as `{}` and writing over
    it, which discards every other MCP server the repo declared. What the
    file cannot be is silently replaced; see `test_config_merge.py`.
    """
    from flanner.exceptions import ConfigError

    (git_repo / ".mcp.json").write_text("{not json", encoding="utf-8")

    with pytest.raises(ConfigError):
        ensure_project_mcp_json(str(git_repo))

    assert (git_repo / ".mcp.json").read_text(encoding="utf-8") == "{not json"


# --- skill -------------------------------------------------------------------


def test_skill_installed_and_idempotent(db, git_repo):
    assert install_skill(str(git_repo)) is True
    skill = git_repo / ".claude" / "skills" / "flanner-plan" / "SKILL.md"
    assert skill.exists()
    assert "create_plan_file_tool" in skill.read_text(encoding="utf-8")
    assert install_skill(str(git_repo)) is False


# --- end-to-end: the CLI hook command initializes its own DB -----------------


def test_cli_guard_write_denies_over_stdio(tmp_path):
    """`flanner hook guard-write` must init the DB itself and deny a plan write."""
    import os
    import subprocess
    import sys

    home = tmp_path / "home"
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)

    env = dict(os.environ, FLANNER_HOME=str(home))
    subprocess.run(
        [
            sys.executable,
            "-m",
            "flanner.cli",
            "init",
            "--skip-claude",
            "--project-root",
            str(repo),
        ],
        input="repo\n",
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )

    payload = json.dumps(
        {"cwd": str(repo), "tool_input": {"file_path": str(repo / ".plans" / "x.md")}}
    )
    result = subprocess.run(
        [sys.executable, "-m", "flanner.cli", "hook", "guard-write"],
        input=payload,
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    assert result.returncode == 0
    decision = json.loads(result.stdout)
    assert decision["hookSpecificOutput"]["permissionDecision"] == "deny"


# --- AGENTS.md (Codex) gets the same block -----------------------------------


def test_agents_md_written_for_codex(db, git_repo):
    project = _project(get_session(), git_repo)
    block = agent_md_block(project)
    assert upsert_agent_md(str(git_repo), "AGENTS.md", block) is True
    text = (git_repo / "AGENTS.md").read_text(encoding="utf-8")
    assert "managed by flanner" in text
    assert "create_plan_file_tool" in text
    assert upsert_agent_md(str(git_repo), "AGENTS.md", block) is False  # idempotent


def test_init_writes_both_agent_files(tmp_path):
    """`flanner init` drops the block into CLAUDE.md and AGENTS.md."""
    import os
    import subprocess
    import sys

    home = tmp_path / "home"
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    env = dict(os.environ, FLANNER_HOME=str(home))
    subprocess.run(
        [
            sys.executable,
            "-m",
            "flanner.cli",
            "init",
            "--skip-claude",
            "--project-root",
            str(repo),
        ],
        input="repo\n",
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    for name in ("CLAUDE.md", "AGENTS.md"):
        assert (repo / name).exists(), name
        assert "managed by flanner" in (repo / name).read_text(encoding="utf-8")
    assert (repo / ".claude" / "settings.json").exists()
    assert (repo / ".claude" / "skills" / "flanner-plan" / "SKILL.md").exists()


# --- shared wiring + global nudge --------------------------------------------


def test_wire_agent_integration(db, git_repo):
    from flanner.agent_hooks import wire_agent_integration

    project = _project(get_session(), git_repo)
    done = wire_agent_integration(str(git_repo), project).installed
    assert any("CLAUDE.md" in d for d in done)
    assert any(".mcp.json" in d for d in done)
    assert any("skill" in d for d in done)
    assert (git_repo / ".mcp.json").exists()
    assert (git_repo / ".claude" / "settings.json").exists()

    again = wire_agent_integration(str(git_repo), project)  # idempotent
    assert again.installed == []
    assert again.skipped == []


def test_upsert_global_nudge(monkeypatch, tmp_path):
    from pathlib import Path

    from flanner.agent_hooks import upsert_global_nudge

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert upsert_global_nudge() is True
    text = (tmp_path / ".claude" / "CLAUDE.md").read_text(encoding="utf-8")
    assert "initialize_project_tool" in text
    assert upsert_global_nudge() is False  # idempotent


# --- guidance reaches every domain, and both agents ---------------------------


def test_the_block_points_at_context_memory_and_skills(db, git_repo):
    block = agent_md_block(_project(get_session(), git_repo))

    for tool in (
        "project_context",
        "memory_recall",
        "memory_consider",
        "memory_decide",
        "skills_report",
        "skills_usage",
    ):
        assert tool in block, tool
    assert "Do not install, approve, share or roll back a skill yourself" in block


def test_both_skills_are_installed_where_each_agent_reads(db, git_repo):
    from flanner.agent_hooks import SKILL_DIRS, SKILLS

    assert install_skill(str(git_repo)) is True
    for folder in SKILL_DIRS:
        for name in SKILLS:
            assert (git_repo / folder / name / "SKILL.md").exists(), f"{folder}/{name}"

    memory = git_repo / ".agents" / "skills" / "flanner-memory" / "SKILL.md"
    text = memory.read_text(encoding="utf-8")
    assert "memory_recall" in text and "memory_consider" in text
    assert install_skill(str(git_repo)) is False
