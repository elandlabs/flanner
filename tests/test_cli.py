"""CLI tests via click.testing.CliRunner.

All invocations run with FLANNER_HOME pointed at a per-test temp directory so
nothing touches the real ~/.flanner. Claude-config-writing commands monkeypatch
get_claude_config_path so the real Claude config is never written.
"""

import json
import os
import re
from pathlib import Path
from uuid import uuid4

import pytest
from click.testing import CliRunner

import flanner.claude_integration as ci
import flanner.cli as cli_module
from flanner.cli import cli
from flanner.database import (
    create_jira_link,
    create_plan_file,
    create_project,
    create_version,
    get_session,
    init_database,
)

JIRA_URL = "https://x.atlassian.net"


@pytest.fixture(autouse=True)
def _integrations_on(monkeypatch):
    """Linear and Jira ship switched off; this file still drives them.

    Hiding a feature is only defensible while its tests keep running, or it
    rots behind the flag and the flag becomes a delete with extra steps.
    `tests/test_features.py` owns the other half: that nothing offers them
    while the switch is off.
    """
    monkeypatch.setenv("FLANNER_INTEGRATIONS", "1")


@pytest.fixture
def home(tmp_path):
    """The one flanner home for this test.

    Reuses the isolated FLANNER_HOME conftest already set, so the CLI and
    any fixture that calls into flanner directly share a device identity.
    Pointing the runner somewhere else would quietly simulate two devices,
    and signature checks would then fail for the wrong reason.
    """
    h = Path(os.environ["FLANNER_HOME"])
    h.mkdir(parents=True, exist_ok=True)
    return h


@pytest.fixture
def runner(home):
    return CliRunner(env={"FLANNER_HOME": str(home), "FLANNER_DB_PATH": None})


@pytest.fixture
def initialized(home):
    """Database file at FLANNER_HOME/data.db, singleton pointed at it."""
    init_database(str(home / "data.db"))


@pytest.fixture
def project(initialized, git_repo):
    """Project named 'proj'; returns its UUID (plain value, no ORM detachment)."""
    return create_project(get_session(), name="proj", project_root=str(git_repo)).id


@pytest.fixture
def plan(project, git_repo):
    """Plan file 'myplan' with one version; returns its UUID."""
    session = get_session()
    pf = create_plan_file(session, project, "myplan")
    create_version(session, pf.id, 1, str(git_repo / ".plans" / "myplan_v1.md"), "hash1")
    return pf.id


@pytest.fixture
def claude_config(tmp_path, monkeypatch):
    """Redirect all Claude config reads/writes to a temp file."""
    path = tmp_path / "claude_desktop_config.json"
    monkeypatch.setattr(ci, "get_claude_config_path", lambda: path)
    return path


@pytest.fixture
def no_claude_cli(monkeypatch):
    """Pretend the Claude Code binary is absent.

    Registering there means running somebody else's executable, which a test
    must not do: it is slow, it is not installed everywhere, and on a machine
    where it is, it would write real configuration. The path where it is
    missing is the one worth asserting anyway, since it is what a new user
    without Claude Code hits.
    """
    import shutil

    real = shutil.which
    monkeypatch.setattr(
        shutil, "which", lambda name, *a, **k: None if name == "claude" else real(name, *a, **k)
    )


# --- guard: commands that require an initialized database exit 1 ---


@pytest.mark.parametrize(
    "args",
    [
        ["list"],
        ["config", "p"],
        ["setup-gitignore", "p"],
        ["delete", "p"],
        ["sync"],
        ["web"],
        ["jira", "config", "p", "--url", JIRA_URL],
        ["jira", "link", "p", "--issue", "A-1"],
        ["jira", "unlink", "p"],
        ["jira", "links"],
        ["jira", "show", "p"],
        ["linear", "config", "p", "--workspace", "acme"],
        ["linear", "link", "p", "--issue", "ENG-1"],
        ["linear", "unlink", "p"],
        ["linear", "links"],
        ["linear", "show", "p"],
    ],
)
def test_commands_require_db(runner, args):
    result = runner.invoke(cli, args)
    assert result.exit_code == 1
    # Asserts the refusal explains itself, not its exact prose. The previous
    # wording named `init` and nothing else, which is how somebody following
    # our own instructions ended up stuck at it.
    assert "no flanner store yet" in result.output
    assert "adopts the repository" in result.output


# --- init ---


def test_init_creates_project(runner, home, git_repo):
    result = runner.invoke(
        cli,
        ["init", "--skip-claude", "--project-root", str(git_repo)],
        input="myproj\n",
    )
    assert result.exit_code == 0, result.output
    assert "Initialized Flanner" in result.output
    assert "Created project: myproj" in result.output
    assert (home / "data.db").exists()
    assert (git_repo / ".plans").is_dir()
    assert ".plans/" in (git_repo / ".gitignore").read_text()
    # Claude Code CLI registration (.mcp.json) is written even with --skip-claude,
    # which only skips the Claude Desktop config.
    mcp_json = json.loads((git_repo / ".mcp.json").read_text())
    assert mcp_json["mcpServers"]["flanner"]["command"] == "flanner-mcp"

def test_init_ends_with_everyday_commands_and_how_to_join_a_team(runner, home, git_repo):
    result = runner.invoke(
        cli, ["init", "--skip-claude", "--project-root", str(git_repo)], input="myproj\n"
    )
    said = " ".join(result.output.split())

    for command in ("flanner freshness", "flanner mem remember", "flanner web"):
        assert command in said, f"{command} was not offered"
    assert "flanner accept" in said and "flanner login" in said


def test_init_with_a_relative_root_leaves_a_project_later_commands_find(
    runner, home, git_repo, monkeypatch
):
    """`--project-root .` was stored as ".", which no lookup ever matched."""
    import os

    from flanner.database import get_project_by_root, get_session

    monkeypatch.chdir(git_repo)
    runner.invoke(cli, ["init", "--skip-claude", "--project-root", "."], input="myproj\n")

    project = get_project_by_root(get_session(), str(git_repo))
    assert project is not None
    assert os.path.isabs(project.project_root)


def test_init_existing_project(runner, git_repo):
    runner.invoke(
        cli, ["init", "--skip-claude", "--project-root", str(git_repo)], input="myproj\n"
    )
    result = runner.invoke(cli, ["init", "--skip-claude", "--project-root", str(git_repo)])
    assert result.exit_code == 0
    assert "myproj is already adopted here" in result.output


def test_init_force_new_project(runner, git_repo):
    runner.invoke(
        cli, ["init", "--skip-claude", "--project-root", str(git_repo)], input="myproj\n"
    )
    result = runner.invoke(
        cli,
        ["init", "--skip-claude", "--project-root", str(git_repo), "--force-new-project"],
        input="second\n",
    )
    assert result.exit_code == 0
    assert "already exists here" in result.output
    assert "Created project: second" in result.output


def test_init_force_new_duplicate_name_errors(runner, git_repo):
    runner.invoke(
        cli, ["init", "--skip-claude", "--project-root", str(git_repo)], input="myproj\n"
    )
    result = runner.invoke(
        cli,
        ["init", "--skip-claude", "--project-root", str(git_repo), "--force-new-project"],
        input="myproj\n",
    )
    assert result.exit_code == 0
    assert "already exists" in result.output


def test_init_no_git_repo_skips_project(runner, outside_any_repo, monkeypatch):
    monkeypatch.chdir(outside_any_repo)
    result = runner.invoke(cli, ["--verbose", "init", "--skip-claude"])
    assert result.exit_code == 0
    assert "Initialized Flanner" in result.output
    assert "Detected git repository" not in result.output


def test_init_registers_with_claude(runner, git_repo, claude_config, no_claude_cli):
    """`init` registers the agents itself; there is no second command to run.

    This used to be `flanner setup`, and a machine that never ran it had a
    store no agent could reach. The local MCP server is how an agent talks to
    flanner at all, so the command that creates the store now wires it up.
    """
    result = runner.invoke(cli, ["init", "--project-root", str(git_repo)], input="regproj\n")

    assert result.exit_code == 0, result.output
    assert "flanner" in json.loads(claude_config.read_text())["mcpServers"]
    assert "Global nudge" in result.output, "the global adoption nudge was not written"
    assert (Path.home() / ".claude" / "CLAUDE.md").exists()


def test_init_can_be_told_to_leave_every_agent_alone(runner, git_repo, claude_config):
    """`--skip-claude` covers all of it, not only Claude Desktop."""
    result = runner.invoke(
        cli, ["init", "--skip-claude", "--project-root", str(git_repo)], input="quietproj\n"
    )

    assert result.exit_code == 0, result.output
    assert not claude_config.exists()
    assert "Global nudge" not in result.output
    assert not (Path.home() / ".claude" / "CLAUDE.md").exists()


def test_setup_repairs_the_registration_without_touching_the_store(runner, claude_config):
    """What `setup` is for now: running the same wiring again on its own."""
    result = runner.invoke(cli, ["setup"])

    assert result.exit_code == 0, result.output
    assert "flanner" in json.loads(claude_config.read_text())["mcpServers"]


# --- list ---


def test_list_empty(runner, initialized):
    result = runner.invoke(cli, ["list"])
    assert result.exit_code == 0
    assert "No projects yet" in result.output


def test_list_table(runner, project):
    result = runner.invoke(cli, ["list"])
    assert result.exit_code == 0
    assert "proj" in result.output


def test_list_json(runner, project):
    result = runner.invoke(cli, ["--quiet", "list", "--output", "json"])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data[0]["name"] == "proj"
    assert data[0]["plan_files"] == 0


def test_list_project_plans(runner, plan):
    result = runner.invoke(cli, ["list", "--project", "proj"])
    assert result.exit_code == 0
    assert "myplan" in result.output


def test_list_project_no_plans(runner, project):
    result = runner.invoke(cli, ["list", "--project", "proj"])
    assert result.exit_code == 0
    assert "No plans in" in result.output


def test_list_project_missing(runner, initialized):
    result = runner.invoke(cli, ["list", "--project", "nope"])
    assert result.exit_code == 1
    assert "not found" in result.output


# --- config ---


def test_config_updates_project(runner, project, git_repo):
    result = runner.invoke(
        cli, ["config", "proj", "--plan-dir", "docs/plans", "--auto-gitignore", "true"]
    )
    assert result.exit_code == 0, result.output
    assert "updated" in result.output
    assert "docs/plans" in result.output
    assert "docs/plans/" in (git_repo / ".gitignore").read_text()


def test_config_project_missing(runner, initialized):
    result = runner.invoke(cli, ["config", "nope"])
    assert result.exit_code == 1
    assert "not found" in result.output


# --- setup-gitignore ---


def test_setup_gitignore(runner, project, git_repo):
    result = runner.invoke(cli, ["setup-gitignore", "proj"])
    assert result.exit_code == 0
    assert "Added" in result.output
    assert ".plans/" in (git_repo / ".gitignore").read_text()

    again = runner.invoke(cli, ["setup-gitignore", "proj"])
    assert "already in .gitignore" in again.output


def test_setup_gitignore_missing_project(runner, initialized):
    result = runner.invoke(cli, ["setup-gitignore", "nope"])
    assert result.exit_code == 1


def test_setup_gitignore_no_root(runner, initialized):
    create_project(get_session(), name="rootless")
    result = runner.invoke(cli, ["setup-gitignore", "rootless"])
    assert result.exit_code == 1
    assert "no project_root" in result.output


# --- delete ---


def test_delete_force(runner, plan):
    result = runner.invoke(cli, ["delete", "proj", "--force"])
    assert result.exit_code == 0
    assert "Deleted proj" in result.output

    gone = runner.invoke(cli, ["list", "--project", "proj"])
    assert gone.exit_code == 1


def test_delete_confirm_no(runner, project):
    result = runner.invoke(cli, ["delete", "proj"], input="n\n")
    assert result.exit_code == 0
    assert "Cancelled" in result.output


def test_delete_needs_the_name_typed_not_a_letter(runner, project):
    """A [y/N] is answered by reflex; the name is not."""
    result = runner.invoke(cli, ["delete", "proj"], input="y\n")
    assert result.exit_code == 0
    assert "Cancelled" in result.output
    assert runner.invoke(cli, ["list", "--project", "proj"]).exit_code == 0


def test_delete_confirm_yes(runner, project):
    result = runner.invoke(cli, ["delete", "proj"], input="proj\n")
    assert result.exit_code == 0
    assert "Deleted proj" in result.output


def test_quiet_keeps_the_data_and_drops_the_chrome(runner, plan):
    """--quiet promised "only errors" and used to change nothing visible."""
    loud = runner.invoke(cli, ["list", "--project", "proj"])
    quiet = runner.invoke(cli, ["--quiet", "list", "--project", "proj"])
    assert quiet.exit_code == 0
    assert "PLAN" in quiet.output  # the table is the answer, not chrome
    assert len(quiet.output) <= len(loud.output)
    args = ["mem", "remember", "Deploy on Tuesdays", "--category", "decision"]
    saved = runner.invoke(cli, ["--quiet", *args, "--scope", "personal"])
    assert saved.exit_code == 0, saved.output
    assert "Remembered" not in saved.output


def test_delete_missing_project(runner, initialized):
    result = runner.invoke(cli, ["delete", "nope", "--force"])
    assert result.exit_code == 1


# --- sync ---


def _plan_file_content(project_id, plan_file_id, version):
    return f"""---
mcp_plan_file: true
plan_manager_version: '1.0'
project_id: {project_id}
project_name: proj
plan_file_id: {plan_file_id}
plan_name: synced
version: {version}
created_at: '2024-01-01T00:00:00Z'
created_by: user
---

# Synced Plan v{version}
"""


def test_sync_no_projects(runner, initialized):
    result = runner.invoke(cli, ["sync"])
    assert result.exit_code == 0
    assert "No projects found" in result.output


def test_sync_missing_project(runner, initialized):
    result = runner.invoke(cli, ["sync", "--project", "nope"])
    assert result.exit_code == 1


def test_sync_no_plan_directory(runner, project):
    result = runner.invoke(cli, ["sync"])
    assert "Plan directory doesn't exist yet" in result.output


def test_sync_no_project_root(runner, initialized):
    create_project(get_session(), name="rootless")
    result = runner.invoke(cli, ["sync"])
    assert "no project_root configured" in result.output


def test_sync_empty_plan_directory(runner, project, git_repo):
    (git_repo / ".plans").mkdir()
    result = runner.invoke(cli, ["sync"])
    assert "No plan files found" in result.output


def test_sync_dry_run_and_import(runner, project, git_repo):
    plans = git_repo / ".plans"
    plans.mkdir()
    plan_file_id = uuid4()
    (plans / "synced_v1.md").write_text(_plan_file_content(project, plan_file_id, 1))
    (plans / "notes.md").write_text("# just notes, no frontmatter\n")
    (plans / "invalid.md").write_text("---\nmcp_plan_file: true\n---\nmissing fields\n")

    dry = runner.invoke(cli, ["sync", "--dry-run"])
    assert dry.exit_code == 0, dry.output
    assert "Dry run" in dry.output
    assert "WOULD IMPORT" in dry.output
    assert "Not an MCP plan file" in dry.output
    assert "Invalid frontmatter" in dry.output
    assert "Run flanner sync to apply" in dry.output

    real = runner.invoke(cli, ["sync", "--project", "proj"])
    assert real.exit_code == 0, real.output
    assert "imported synced_v1.md" in real.output
    assert "1 imported" in real.output
    assert "1 skipped" in real.output
    assert "1 error" in real.output

    # Re-sync: version already in database
    again = runner.invoke(cli, ["sync"])
    assert "already in database" in again.output

    # A newer version of the same plan gets picked up as an update
    (plans / "synced_v2.md").write_text(_plan_file_content(project, plan_file_id, 2))
    dry2 = runner.invoke(cli, ["sync", "--dry-run"])
    assert "WOULD UPDATE" in dry2.output
    updated = runner.invoke(cli, ["sync"])
    assert "updated synced_v2.md" in updated.output
    assert "v1 -> v2" in updated.output


def test_sync_unparseable_plan_id_is_error(runner, project, git_repo):
    plans = git_repo / ".plans"
    plans.mkdir()
    content = _plan_file_content(project, "not-a-uuid", 1)
    (plans / "broken.md").write_text(content)
    result = runner.invoke(cli, ["sync"])
    assert result.exit_code == 0
    assert "broken.md - " in result.output
    assert "1 error" in result.output


# --- start / stop / status ---


# `start` and `stop` now spawn and signal a real process, so what they do is
# tested against real processes in `test_server_lifecycle.py` rather than by
# patching `os.kill` to pretend. What is left here is the status line, which
# is CLI wiring rather than process handling.


def test_status_no_db(runner, claude_config):
    result = runner.invoke(cli, ["status"])
    assert result.exit_code == 0
    assert "stopped" in result.output
    assert "not initialized" in result.output
    assert "not registered" in result.output


def test_status_with_db_and_registration(runner, home, project, claude_config, monkeypatch):
    ci.register_mcp_server()
    (home / "server.pid").write_text("12345")
    monkeypatch.setattr(cli_module, "_process_alive", lambda pid: True)
    result = runner.invoke(cli, ["status"])
    assert "running" in result.output
    assert "1 project" in result.output
    assert "registered" in result.output


def test_status_stale_pid(runner, home, initialized, claude_config, monkeypatch):
    (home / "server.pid").write_text("12345")
    monkeypatch.setattr(cli_module, "_process_alive", lambda pid: False)
    result = runner.invoke(cli, ["status"])
    assert "stopped" in result.output
    assert not (home / "server.pid").exists()


# --- register / unregister / claude-info ---


def test_register_local(runner, claude_config):
    result = runner.invoke(cli, ["register"])
    assert result.exit_code == 0, result.output
    assert "Next steps" in result.output
    assert "flanner" in json.loads(claude_config.read_text())["mcpServers"]


def test_register_no_config_path(runner, monkeypatch):
    monkeypatch.setattr(ci, "get_claude_config_path", lambda: None)
    result = runner.invoke(cli, ["register"])
    assert result.exit_code == 0
    assert "Could not find Claude Code configuration path" in result.output


def test_register_cloud_requires_url(runner, claude_config):
    result = runner.invoke(cli, ["register", "--type", "cloud"])
    assert result.exit_code == 1


def test_register_cloud(runner, claude_config):
    result = runner.invoke(
        cli, ["register", "--type", "cloud", "--url", "https://mcp.example.com"]
    )
    assert result.exit_code == 0
    config = json.loads(claude_config.read_text())
    assert config["mcpServers"]["flanner"]["url"] == "https://mcp.example.com"


def test_unregister_cancelled(runner, claude_config):
    result = runner.invoke(cli, ["unregister"], input="n\n")
    assert "Cancelled" in result.output


def test_unregister_not_registered(runner, claude_config):
    result = runner.invoke(cli, ["unregister"], input="y\n")
    assert "is not registered" in result.output


def test_unregister_success(runner, claude_config):
    ci.register_mcp_server()
    result = runner.invoke(cli, ["unregister"], input="y\n")
    assert "Unregistered" in result.output


def test_claude_info_not_registered(runner, claude_config):
    result = runner.invoke(cli, ["claude-info"])
    assert result.exit_code == 0
    assert "Not registered" in result.output


def test_claude_info_registered(runner, claude_config):
    ci.register_mcp_server()
    result = runner.invoke(cli, ["claude-info"])
    assert "Current configuration" in result.output


# --- jira ---


def test_jira_config_valid(runner, project):
    result = runner.invoke(
        cli, ["jira", "config", "proj", "--url", JIRA_URL + "/", "--project-key", "PROJ"]
    )
    assert result.exit_code == 0, result.output
    assert "JIRA configuration updated" in result.output
    assert JIRA_URL in result.output
    assert "PROJ" in result.output


def test_jira_config_invalid_url(runner, project):
    result = runner.invoke(cli, ["jira", "config", "proj", "--url", "not-a-url"])
    assert result.exit_code == 1
    assert "Invalid JIRA URL" in result.output


def test_jira_config_missing_project(runner, initialized):
    result = runner.invoke(cli, ["jira", "config", "nope", "--url", JIRA_URL])
    assert result.exit_code == 1


def test_jira_link_flow(runner, plan):
    runner.invoke(cli, ["jira", "config", "proj", "--url", JIRA_URL])
    result = runner.invoke(
        cli,
        [
            "jira",
            "link",
            "myplan",
            "--issue",
            "proj-1",
            "--project",
            "proj",
            "--type",
            "Epic",
            "--notes",
            "linked from test",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "Linked 'myplan' to PROJ-1" in result.output
    assert f"{JIRA_URL}/browse/PROJ-1" in result.output
    assert "Type: Epic" in result.output

    dup = runner.invoke(cli, ["jira", "link", "myplan", "--issue", "PROJ-1", "--project", "proj"])
    assert "already linked" in dup.output


def test_jira_link_invalid_issue_key(runner, plan):
    result = runner.invoke(cli, ["jira", "link", "myplan", "--issue", "123", "--project", "proj"])
    assert result.exit_code == 1
    assert "Invalid JIRA issue key" in result.output


def test_jira_link_missing_plan(runner, project):
    result = runner.invoke(
        cli, ["jira", "link", "ghost", "--issue", "PROJ-1", "--project", "proj"]
    )
    assert result.exit_code == 1
    # Names the plan and the project it was looked for in. Asserted by
    # content rather than by exact phrasing, because every command now
    # reports this through one helper. (No "Available plans" line here:
    # this project has none, and listing nothing helps nobody.)
    assert "ghost" in result.output
    assert "proj" in result.output


def test_jira_link_missing_project(runner, initialized):
    result = runner.invoke(
        cli, ["jira", "link", "myplan", "--issue", "PROJ-1", "--project", "nope"]
    )
    assert result.exit_code == 1


def test_jira_link_from_project_directory(runner, plan, git_repo, monkeypatch):
    monkeypatch.chdir(git_repo)
    result = runner.invoke(cli, ["jira", "link", "myplan", "--issue", "PROJ-9"])
    assert result.exit_code == 0, result.output
    assert "Linked 'myplan' to PROJ-9" in result.output


def test_jira_links_listing(runner, plan):
    create_jira_link(get_session(), plan, "PROJ-1", "Story")

    all_projects = runner.invoke(cli, ["jira", "links"])
    assert all_projects.exit_code == 0
    assert "PROJ-1" in all_projects.output

    one = runner.invoke(cli, ["jira", "links", "--project", "proj"])
    assert "myplan" in one.output
    assert "Story" in one.output


def test_jira_links_none_found(runner, project):
    result = runner.invoke(cli, ["jira", "links", "--project", "proj"])
    assert "No JIRA links found" in result.output


def test_jira_links_no_projects(runner, initialized):
    result = runner.invoke(cli, ["jira", "links"])
    assert "No projects found" in result.output


def test_jira_links_missing_project(runner, initialized):
    result = runner.invoke(cli, ["jira", "links", "--project", "nope"])
    assert result.exit_code == 1


def test_jira_show(runner, plan):
    runner.invoke(cli, ["jira", "config", "proj", "--url", JIRA_URL])
    create_jira_link(get_session(), plan, "PROJ-1", "Epic", "some notes")

    result = runner.invoke(cli, ["jira", "show", "myplan", "--project", "proj"])
    assert result.exit_code == 0
    assert "PROJ-1" in result.output
    assert f"{JIRA_URL}/browse/PROJ-1" in result.output
    assert "some notes" in result.output


def test_jira_show_no_links(runner, plan):
    result = runner.invoke(cli, ["jira", "show", "myplan", "--project", "proj"])
    assert "No JIRA links found" in result.output


def test_jira_show_missing_plan(runner, project):
    result = runner.invoke(cli, ["jira", "show", "ghost", "--project", "proj"])
    assert result.exit_code == 1


def test_jira_show_missing_project(runner, initialized):
    result = runner.invoke(cli, ["jira", "show", "myplan", "--project", "nope"])
    assert result.exit_code == 1


def test_jira_unlink_specific(runner, plan):
    create_jira_link(get_session(), plan, "PROJ-1")
    result = runner.invoke(
        cli, ["jira", "unlink", "myplan", "--issue", "PROJ-1", "--project", "proj"]
    )
    assert result.exit_code == 0
    assert "Unlinked 'myplan' from PROJ-1" in result.output

    missing = runner.invoke(
        cli, ["jira", "unlink", "myplan", "--issue", "PROJ-1", "--project", "proj"]
    )
    assert "not found" in missing.output


def test_jira_unlink_all(runner, plan):
    session = get_session()
    create_jira_link(session, plan, "PROJ-1")
    create_jira_link(session, plan, "PROJ-2")
    result = runner.invoke(cli, ["jira", "unlink", "myplan", "--all", "--project", "proj"])
    assert "Unlinked 2 JIRA issue(s)" in result.output

    none_left = runner.invoke(cli, ["jira", "unlink", "myplan", "--project", "proj"])
    assert "No JIRA links found" in none_left.output


def test_jira_unlink_missing_plan(runner, project):
    result = runner.invoke(cli, ["jira", "unlink", "ghost", "--project", "proj"])
    assert result.exit_code == 1


def test_jira_unlink_missing_project(runner, initialized):
    result = runner.invoke(cli, ["jira", "unlink", "myplan", "--project", "nope"])
    assert result.exit_code == 1


def test_list_project_plans_json(runner, plan):
    result = runner.invoke(cli, ["list", "--project", "proj", "--output", "json"])
    assert result.exit_code == 0
    rows = json.loads(result.output)
    assert rows and rows[0]["name"] == "myplan" and rows[0]["version"] == 1


def test_web_warns_on_non_local_host(runner, initialized, monkeypatch):
    import uvicorn

    from flanner import web as web_module

    # monkeypatch records the real value and puts it back afterwards. Without
    # that, this command would leave the Host check switched off for every
    # later test in the process, because `flanner web` normally never returns.
    monkeypatch.setattr(web_module, "ALLOW_ANY_HOST", False)
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: None)
    result = runner.invoke(cli, ["web", "--host", "0.0.0.0", "--port", "0"])
    assert result.exit_code == 0
    assert "exposes the web UI beyond localhost" in result.output
    # The warning and the stood-down Host check are one decision, so the test
    # that pins the warning pins the check too.
    assert web_module.ALLOW_ANY_HOST is True


def test_web_no_warning_on_localhost(runner, initialized, monkeypatch):
    import uvicorn

    from flanner import web as web_module

    monkeypatch.setattr(web_module, "ALLOW_ANY_HOST", False)
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: None)
    result = runner.invoke(cli, ["web", "--host", "127.0.0.1", "--port", "0"])
    assert "exposes the web UI" not in result.output
    assert web_module.ALLOW_ANY_HOST is False


def test_web_port_in_use_is_graceful(runner, initialized):
    import socket

    with socket.socket() as held:
        held.bind(("127.0.0.1", 0))
        held.listen()
        port = held.getsockname()[1]
        result = runner.invoke(cli, ["web", "--host", "127.0.0.1", "--port", str(port)])
    assert result.exit_code == 1
    assert "already in use" in result.output
    assert f"flanner web --port {port + 1}" in result.output  # actionable next step


def test_a_port_held_on_every_address_counts_as_in_use():
    """What Docker Desktop does with port 80: listen on 0.0.0.0.

    Windows lets a second socket bind 127.0.0.1 on that port with no
    error, so a check that only tried to bind reported the port free,
    and `flanner web` started where connections went to Docker.
    """
    import socket

    from flanner.cli import _port_in_use

    with socket.socket() as held:
        held.bind(("0.0.0.0", 0))
        held.listen()
        port = held.getsockname()[1]
        assert _port_in_use("127.0.0.1", port)


def test_a_free_port_is_free():
    import socket

    from flanner.cli import _port_in_use

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    assert not _port_in_use("127.0.0.1", port)


# --- linear ---


def test_linear_auth_no_key(runner, monkeypatch):
    monkeypatch.delenv("LINEAR_API_KEY", raising=False)
    result = runner.invoke(cli, ["linear", "auth"])
    assert result.exit_code == 1
    assert "LINEAR_API_KEY is not set" in result.output
    assert "linear.app/settings/api" in result.output


def test_linear_auth_valid(runner, monkeypatch):
    monkeypatch.setenv("LINEAR_API_KEY", "lin_api_x")
    from flanner import linear_api

    monkeypatch.setattr(
        linear_api, "fetch_viewer", lambda *a, **k: {"name": "Ada", "email": "ada@x.com"}
    )
    result = runner.invoke(cli, ["linear", "auth"])
    assert result.exit_code == 0, result.output
    assert "Authenticated with Linear as Ada" in result.output
    assert "LINEAR_API_KEY" in result.output  # prints the MCP env snippet


def test_linear_auth_rejected(runner, monkeypatch):
    monkeypatch.setenv("LINEAR_API_KEY", "bad")
    from flanner import linear_api
    from flanner.exceptions import LinearError

    def boom(*a, **k):
        raise LinearError("Authentication required")

    monkeypatch.setattr(linear_api, "fetch_viewer", boom)
    result = runner.invoke(cli, ["linear", "auth"])
    assert result.exit_code == 1
    assert "rejected the key" in result.output


def test_linear_config_valid(runner, project):
    result = runner.invoke(
        cli, ["linear", "config", "proj", "--workspace", "https://linear.app/Acme"]
    )
    assert result.exit_code == 0, result.output
    assert "acme" in result.output


def test_linear_config_invalid_workspace(runner, project):
    result = runner.invoke(cli, ["linear", "config", "proj", "--workspace", "Bad Space"])
    assert result.exit_code == 1
    assert "Invalid Linear workspace" in result.output


def test_linear_link_flow(runner, plan, monkeypatch):
    monkeypatch.delenv("LINEAR_API_KEY", raising=False)
    runner.invoke(cli, ["linear", "config", "proj", "--workspace", "acme"])
    result = runner.invoke(
        cli, ["linear", "link", "myplan", "--issue", "eng-1", "--project", "proj"]
    )
    assert result.exit_code == 0, result.output
    assert "Linked 'myplan' to ENG-1" in result.output
    assert "https://linear.app/acme/issue/ENG-1" in result.output

    dup = runner.invoke(cli, ["linear", "link", "myplan", "--issue", "ENG-1", "--project", "proj"])
    assert "already linked" in dup.output


def test_linear_link_invalid_issue(runner, plan, monkeypatch):
    monkeypatch.delenv("LINEAR_API_KEY", raising=False)
    result = runner.invoke(
        cli, ["linear", "link", "myplan", "--issue", "123", "--project", "proj"]
    )
    assert result.exit_code == 1
    assert "Invalid Linear issue id" in result.output


def test_linear_link_verify_enriches(runner, plan, monkeypatch):
    monkeypatch.setenv("LINEAR_API_KEY", "key")
    from flanner import linear_api

    monkeypatch.setattr(
        linear_api,
        "fetch_issue_by_identifier",
        lambda *a, **k: {"id": "u1", "title": "Ship it", "state": "Todo"},
    )
    result = runner.invoke(
        cli, ["linear", "link", "myplan", "--issue", "ENG-5", "--project", "proj"]
    )
    assert result.exit_code == 0, result.output
    assert "[Todo] Ship it" in result.output


def test_linear_link_verify_missing_issue(runner, plan, monkeypatch):
    monkeypatch.setenv("LINEAR_API_KEY", "key")
    from flanner import linear_api

    monkeypatch.setattr(linear_api, "fetch_issue_by_identifier", lambda *a, **k: None)
    result = runner.invoke(
        cli, ["linear", "link", "myplan", "--issue", "ENG-404", "--project", "proj"]
    )
    assert result.exit_code == 1
    assert "not found" in result.output


def test_linear_links_and_show(runner, plan, monkeypatch):
    monkeypatch.delenv("LINEAR_API_KEY", raising=False)
    runner.invoke(cli, ["linear", "config", "proj", "--workspace", "acme"])
    runner.invoke(cli, ["linear", "link", "myplan", "--issue", "ENG-1", "--project", "proj"])

    listed = runner.invoke(cli, ["linear", "links", "--project", "proj"])
    assert "ENG-1" in listed.output
    shown = runner.invoke(cli, ["linear", "show", "myplan", "--project", "proj"])
    assert "ENG-1" in shown.output
    assert "https://linear.app/acme/issue/ENG-1" in shown.output


def test_linear_unlink(runner, plan, monkeypatch):
    monkeypatch.delenv("LINEAR_API_KEY", raising=False)
    runner.invoke(cli, ["linear", "link", "myplan", "--issue", "ENG-1", "--project", "proj"])
    result = runner.invoke(
        cli, ["linear", "unlink", "myplan", "--issue", "ENG-1", "--project", "proj"]
    )
    assert result.exit_code == 0
    assert "Unlinked 'myplan' from ENG-1" in result.output


def test_linear_refresh_requires_key(runner, plan, monkeypatch):
    monkeypatch.delenv("LINEAR_API_KEY", raising=False)
    result = runner.invoke(cli, ["linear", "refresh", "myplan", "--project", "proj"])
    assert result.exit_code == 1
    assert "LINEAR_API_KEY is not set" in result.output


def test_linear_refresh_updates_cache(runner, plan, monkeypatch):
    monkeypatch.setenv("LINEAR_API_KEY", "key")
    from flanner import linear_api

    # Link first (no verify) so there is a row to refresh.
    monkeypatch.setattr(linear_api, "fetch_issue_by_identifier", lambda *a, **k: None)
    runner.invoke(
        cli, ["linear", "link", "myplan", "--issue", "ENG-1", "--no-verify", "--project", "proj"]
    )
    monkeypatch.setattr(
        linear_api,
        "fetch_issue_by_identifier",
        lambda *a, **k: {"id": "u1", "title": "Now done", "state": "Done"},
    )
    result = runner.invoke(cli, ["linear", "refresh", "myplan", "--project", "proj"])
    assert result.exit_code == 0, result.output
    assert "[Done] Now done" in result.output


def test_setup_registers_and_writes_global_nudge(runner, tmp_path, monkeypatch, claude_config):
    import shutil
    from pathlib import Path

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(shutil, "which", lambda name: None)  # pretend the claude CLI is absent
    result = runner.invoke(cli, ["setup"])
    assert result.exit_code == 0, result.output
    nudge = tmp_path / ".claude" / "CLAUDE.md"
    assert nudge.exists()
    assert "initialize_project_tool" in nudge.read_text(encoding="utf-8")
    assert "Claude Code CLI not found" in result.output  # took the no-claude branch


# --- doctor: catalog/disk reconciliation ---


@pytest.fixture
def written_plan(project, git_repo):
    """A plan created through the real write path, so its file exists on disk."""
    from flanner.database import get_project
    from flanner.plan_ops import create_plan

    session = get_session()
    proj = get_project(session, project)
    plan_file, _ = create_plan(
        session, project=proj, name="written", content="# body\n", created_by="test"
    )
    session.commit()
    return plan_file.id


def test_doctor_clean_project(runner, written_plan, git_repo):
    result = runner.invoke(cli, ["doctor", "--project", "proj"])
    assert result.exit_code == 0, result.output
    assert "agree" in result.output


def test_doctor_reports_missing_file(runner, plan):
    # The `plan` fixture records a version whose file was never written.
    result = runner.invoke(cli, ["doctor", "--project", "proj"])
    assert result.exit_code == 0, result.output
    assert "missing_file" in result.output


def test_doctor_json_output(runner, plan):
    result = runner.invoke(cli, ["doctor", "--project", "proj", "--output", "json"])
    assert result.exit_code == 0, result.output
    report = json.loads(result.output)
    assert report["project"] == "proj"
    assert report["catalog"][0]["kind"] == "missing_file"
    assert report["catalog"][0]["repairable"] is False


def test_doctor_json_carries_enrollment(runner, plan):
    """The half a setup script actually reads."""
    result = runner.invoke(cli, ["doctor", "--project", "proj", "--output", "json"])
    report = json.loads(result.output)
    codes = [c["code"] for c in report["enrollment"]]
    assert "not_enrolled" in codes
    # Every check carries the command that resolves it, or nothing to do.
    for check in report["enrollment"]:
        assert check["level"] in {"ok", "info", "action", "problem"}


def test_doctor_says_a_local_only_device_is_fine(runner, plan):
    """Not enrolled is information, not a fault: local work needs no account."""
    result = runner.invoke(cli, ["doctor", "--project", "proj", "--output", "json"])
    report = json.loads(result.output)
    not_enrolled = next(c for c in report["enrollment"] if c["code"] == "not_enrolled")
    assert not_enrolled["level"] == "info"
    assert "needs no account" in not_enrolled["detail"]


def test_doctor_flags_a_binding_with_no_account(runner, plan):
    """A repository bound to a workspace by a device that cannot enter one."""
    from flanner.database import ProjectModel, get_session

    db = get_session()
    project = db.query(ProjectModel).filter_by(name="proj").one()
    project.workspace_id = "ws_orphaned"
    db.commit()

    result = runner.invoke(cli, ["doctor", "--project", "proj", "--output", "json"])
    report = json.loads(result.output)
    flagged = next(c for c in report["enrollment"] if c["code"] == "bound_without_account")
    assert flagged["level"] == "problem"
    assert "ws_orphaned" in flagged["detail"]


def test_doctor_table_always_reports_the_team_section(runner, plan):
    """Printed on success too, so it can be used to confirm setup worked."""
    result = runner.invoke(cli, ["doctor", "--project", "proj"])
    assert result.exit_code == 0, result.output
    assert "Team" in result.output


def test_doctor_repair_adopts_orphan(runner, written_plan, git_repo):
    original = (git_repo / ".plans" / "written_v1.md").read_text(encoding="utf-8")
    orphan = git_repo / ".plans" / "written_v2.md"
    orphan.write_text(original.replace("version: 1", "version: 2"), encoding="utf-8")

    result = runner.invoke(cli, ["doctor", "--project", "proj"])
    assert "orphan_file" in result.output
    assert "--repair" in result.output  # suggests the fix

    repaired = runner.invoke(cli, ["doctor", "--project", "proj", "--repair"])
    assert repaired.exit_code == 0, repaired.output
    assert "Repaired 1" in repaired.output

    after = runner.invoke(cli, ["doctor", "--project", "proj"])
    assert "agree" in after.output


def test_doctor_unknown_project_exits_1(runner, initialized):
    """The name given back, not generic advice to do what was just done."""
    result = runner.invoke(cli, ["doctor", "--project", "nope"])
    assert result.exit_code == 1
    assert "No project named 'nope'" in result.output
    assert "pass --project" not in result.output


# --- single-writer discipline: CLI writes go through the daemon ---


def test_cli_write_routes_through_the_daemon(runner, plan, monkeypatch):
    """With a daemon up, a CLI write is forwarded rather than applied directly."""
    from flanner import services

    seen = []

    def fake_call(path, payload):
        seen.append((path, payload["op"]))
        return {"result": {"success": True, "message": "ok", "count": 2}}

    monkeypatch.setattr(services.ipc, "call_daemon", fake_call)
    result = runner.invoke(cli, ["jira", "unlink", "myplan", "--all", "--project", "proj"])
    assert result.exit_code == 0, result.output
    assert seen == [("/ipc/call", "unlink_jira_issue")]
    assert "Unlinked 2 JIRA issue(s)" in result.output


def test_cli_write_applies_locally_without_a_daemon(runner, project, git_repo):
    """No daemon: the write still happens exactly once, in-process."""
    from flanner.database import get_jira_config, get_session

    result = runner.invoke(
        cli, ["jira", "config", "proj", "--url", JIRA_URL, "--project-key", "PROJ"]
    )
    assert result.exit_code == 0, result.output
    assert get_jira_config(get_session(), project).jira_url == JIRA_URL


def test_cli_links_are_attributed_to_the_user_not_the_agent(runner, plan):
    """Routing through the shared service must not relabel human actions."""
    from flanner.database import get_jira_links, get_session

    result = runner.invoke(
        cli, ["jira", "link", "myplan", "--issue", "PROJ-7", "--project", "proj"]
    )
    assert result.exit_code == 0, result.output
    links = get_jira_links(get_session(), plan)
    assert links[0].created_by == "user"


# --- history, diff and why --------------------------------------------------


@pytest.fixture
def revised_plan(project, git_repo):
    """A plan with three versions, so history and diff have something to say."""
    from flanner.database import get_project
    from flanner.plan_ops import create_plan, record_new_version

    session = get_session()
    proj = get_project(session, project)
    plan_file, _ = create_plan(
        session,
        project=proj,
        name="revised",
        content="# Title\n\nfirst line\n",
        created_by="claude",
    )
    record_new_version(
        session,
        project=proj,
        plan_file=plan_file,
        content="# Title\n\nfirst line\nsecond line\n",
        created_by="jayson",
        notes="added a line",
    )
    record_new_version(
        session,
        project=proj,
        plan_file=plan_file,
        content="# Title\n\nreplaced line\nsecond line\n",
        created_by="claude",
        notes="reworded",
    )
    session.commit()
    return plan_file


def test_history_lists_every_version_newest_first(runner, revised_plan):
    result = runner.invoke(cli, ["history", "revised", "--project", "proj"])
    assert result.exit_code == 0, result.output
    assert result.output.index("v3") < result.output.index("v1")
    assert "reworded" in result.output
    assert "added a line" in result.output
    assert "3 versions" in result.output


def test_history_counts_what_changed(runner, revised_plan):
    """The churn column is computed from the files; it is stored nowhere."""
    result = runner.invoke(cli, ["history", "revised", "--project", "proj"])
    assert "+1" in result.output  # v2 added one line
    assert "+3" in result.output  # v1 counts as all-added


def test_history_limit(runner, revised_plan):
    result = runner.invoke(cli, ["history", "revised", "--project", "proj", "--limit", "1"])
    assert result.exit_code == 0, result.output
    assert "v3" in result.output
    assert "v1" not in result.output.split("3 versions")[0]


def test_diff_defaults_to_the_last_two_versions(runner, revised_plan):
    result = runner.invoke(cli, ["diff", "revised", "--project", "proj"])
    assert result.exit_code == 0, result.output
    assert "- first line" in result.output
    assert "+ replaced line" in result.output


def test_diff_takes_a_version_with_or_without_the_v(runner, revised_plan):
    bare = runner.invoke(cli, ["diff", "revised", "1", "3", "--project", "proj"])
    prefixed = runner.invoke(cli, ["diff", "revised", "v1", "v3", "--project", "proj"])
    assert bare.exit_code == 0, bare.output
    assert prefixed.exit_code == 0, prefixed.output
    assert "+ second line" in bare.output
    assert bare.output == prefixed.output


def test_diff_labels_hunks_with_the_heading_above_them(runner, revised_plan):
    """Line numbers are true and useless; the section is what orients you."""
    result = runner.invoke(cli, ["diff", "revised", "v1", "v3", "--project", "proj"])
    assert "@@ # Title @@" in result.output


def test_diff_rejects_a_version_that_does_not_exist(runner, revised_plan):
    result = runner.invoke(cli, ["diff", "revised", "v1", "v99", "--project", "proj"])
    assert result.exit_code == 1
    assert "v99" in result.output


def test_diff_says_so_when_nothing_differs(runner, written_plan):
    result = runner.invoke(cli, ["diff", "written", "v1", "v1", "--project", "proj"])
    assert result.exit_code == 0, result.output
    assert "identical" in result.output


def test_why_reports_the_same_evidence_as_freshness(runner, written_plan, git_repo):
    why = runner.invoke(cli, ["why", "written", "--project", "proj"])
    freshness = runner.invoke(cli, ["freshness", "written", "--project", "proj"])
    assert why.exit_code == 0, why.output
    assert why.output == freshness.output


# --- pack and import ----------------------------------------------------------


def test_pack_writes_one_self_contained_file(runner, written_plan, git_repo, tmp_path):
    out = tmp_path / "packet.html"
    result = runner.invoke(
        cli, ["review", "pack", "written", "--project", "proj", "--output", str(out)]
    )
    assert result.exit_code == 0, result.output
    assert out.exists()
    html = out.read_text(encoding="utf-8")
    assert "@font-face" in html
    assert 'class="toc"' in html
    assert 'id="notes"' in html
    assert "no team review" in result.output


def test_pack_can_leave_the_fonts_out(runner, written_plan, git_repo, tmp_path):
    fat = tmp_path / "fat.html"
    thin = tmp_path / "thin.html"
    runner.invoke(cli, ["review", "pack", "written", "--project", "proj", "--output", str(fat)])
    runner.invoke(
        cli,
        ["review", "pack", "written", "--project", "proj", "--output", str(thin), "--no-fonts"],
    )
    assert thin.stat().st_size < fat.stat().st_size


def test_pack_refuses_a_version_that_does_not_exist(runner, written_plan, git_repo):
    result = runner.invoke(
        cli, ["review", "pack", "written", "--project", "proj", "--version", "99"]
    )
    assert result.exit_code == 1
    assert "does not exist" in result.output


def test_deciding_in_a_solo_project_says_the_decision_binds_nobody(runner, written_plan):
    """`review status` said this already; the moment of deciding did not.

    Somebody can approve a plan without ever running status, and "Recorded
    approve" on its own reads exactly like an authorization.
    """
    runner.invoke(cli, ["review", "propose", "written", "--project", "proj"])

    result = runner.invoke(cli, ["review", "decide", "written", "approve", "--project", "proj"])

    assert result.exit_code == 0, result.output
    assert "Recorded approve" in result.output
    assert "advisory" in result.output
    # The why, not just the word. Taken from the resolution, so this also
    # fails if the two call sites ever start wording it differently.
    assert "has not joined a workspace" in result.output


def test_whoami_says_what_this_device_holds_and_that_it_keeps_it(runner, written_plan):
    """The Settings page has said this since retirement landed; the CLI had not.

    A CLI-only user is the common case, so "keep everything" was a decision
    they lived with and could not see.
    """
    result = runner.invoke(cli, ["whoami"])

    assert result.exit_code == 0, result.output
    assert "Holds" in result.output
    assert "artifacts" in result.output
    assert "never pruned" in result.output


def test_retiring_with_yes_still_says_nothing_was_erased(runner, written_plan):
    """--yes skips the prompt, which was the only place that said this.

    A script is exactly where somebody would assume the command deleted
    something, and it is the one context that never sees the warning.
    """
    result = runner.invoke(cli, ["retire", "written", "--project", "proj", "--yes"])

    assert result.exit_code == 0, result.output
    assert "retired" in result.output
    assert "Nothing was erased" in result.output


def _review_file(tmp_path, plan="written", version=1, notes=None):
    body = {
        "packet": {"plan": plan, "version": version, "project": "proj"},
        "reviewer": "Dana at Acme",
        "notes": notes
        if notes is not None
        else [{"quote": "body", "section": "", "occurrence": 0, "body": "Why this?"}],
    }
    path = tmp_path / "review.json"
    path.write_text(json.dumps(body), encoding="utf-8")
    return path


def test_import_records_the_notes(runner, written_plan, git_repo, tmp_path):
    result = runner.invoke(
        cli, ["review", "import", str(_review_file(tmp_path)), "--project", "proj"]
    )
    assert result.exit_code == 0, result.output
    assert "Recorded 1 note from Dana at Acme" in result.output
    assert "Why this?" in result.output


def test_import_says_the_notes_are_unverified(runner, written_plan, git_repo, tmp_path):
    """The reviewer has no device key. Presenting their notes as though they
    were a teammate's signed comment would be the one unforgivable bug."""
    result = runner.invoke(
        cli, ["review", "import", str(_review_file(tmp_path)), "--project", "proj"]
    )
    assert "Unverified" in result.output
    assert "no device key" in result.output


def test_imported_review_is_stored_as_its_own_artifact_type(
    runner, written_plan, git_repo, tmp_path
):
    """A distinct type is what stops anything downstream mistaking outside
    review for a signed teammate comment."""
    from flanner.artifacts import REVIEW_EXTERNAL
    from flanner.assurance import load_external_reviews

    runner.invoke(cli, ["review", "import", str(_review_file(tmp_path)), "--project", "proj"])
    external = load_external_reviews(get_session(), str(written_plan))
    assert all(e.artifact.artifact_type == REVIEW_EXTERNAL for e in external)
    assert len(external) == 1
    assert external[0].payload["verified"] is False
    assert external[0].payload["reviewer"] == "Dana at Acme"
    assert external[0].payload["notes"][0]["body"] == "Why this?"


def test_import_works_on_a_plan_that_was_never_signed(runner, written_plan, git_repo, tmp_path):
    """A local plan that has never joined a workspace has no signed
    artifact. Refusing there would fail exactly the people most likely to
    be sending plans to outsiders."""
    result = runner.invoke(
        cli, ["review", "import", str(_review_file(tmp_path)), "--project", "proj"]
    )
    assert result.exit_code == 0, result.output


def test_import_warns_when_the_plan_has_moved_on(runner, written_plan, git_repo, tmp_path):
    path = _review_file(tmp_path, version=1)
    from flanner.database import get_project_by_name
    from flanner.plan_ops import record_new_version

    session = get_session()
    proj = get_project_by_name(session, "proj")
    assert proj is not None
    plan = next(p for p in proj.plan_files if p.name == "written")
    record_new_version(
        session,
        project=proj,
        plan_file=plan,
        content="# body\n\nnow with more\n",
        created_by="test",
        notes="a later revision",
    )
    session.commit()

    result = runner.invoke(cli, ["review", "import", str(path), "--project", "proj"])
    assert result.exit_code == 0, result.output
    assert "written against v1" in result.output.lower()


def test_import_rejects_a_file_that_names_no_plan(runner, initialized, tmp_path):
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({"notes": [{"body": "x"}]}), encoding="utf-8")
    result = runner.invoke(cli, ["review", "import", str(path)])
    assert result.exit_code == 1
    assert "which plan" in result.output


def test_import_of_an_empty_review_changes_nothing(runner, written_plan, git_repo, tmp_path):
    path = _review_file(tmp_path, notes=[])
    result = runner.invoke(cli, ["review", "import", str(path), "--project", "proj"])
    assert result.exit_code == 0, result.output
    assert "no notes" in result.output


def test_join_without_an_id_says_where_to_find_one(runner):
    """The refusal a person actually hits. A workspace id is not guessable
    and nothing prints it by accident, so a message that only says one is
    missing leaves them searching a console they may not have access to."""
    result = runner.invoke(cli, ["join", "--project", "anything"])

    assert result.exit_code == 1
    # Names the surface that has the answer, not merely the omission.
    assert "not signed in" in result.output or "workspace access" in result.output
    assert "flanner login" in result.output or "flanner whoami" in result.output


def test_accept_creates_the_store_so_the_next_step_works(runner, monkeypatch):
    """The failure everybody hit: `accept` printed "run flanner join" and
    `join` refused because nothing had made a database. Accepting is the
    moment a machine commits to being used with a team, and the store is
    machine-wide, so it is made here."""
    import flanner.account as account
    from flanner import session as cache
    from flanner.cli import get_mcp_dir

    fake = cache.Session(
        endpoint="https://cp.test",
        device_id="dev_x",
        organization_id="org_1",
        user_id="sam",
        entitlement="",
        keyring={},
    )
    monkeypatch.setattr(account, "accept_invitation", lambda *a, **kw: fake)

    result = runner.invoke(cli, ["accept", "tok", "--as", "sam"])

    assert result.exit_code == 0, result.output
    assert (get_mcp_dir() / "data.db").exists(), "accept left no store behind"


def test_accept_ends_with_the_same_next_steps_as_login(runner, monkeypatch):
    """`accept` had its own older block. Both now end in `_what_next`, so an
    invitation that carried no grant yet is not sent at a `join` that refuses;
    the grant case is covered in test_login_next_steps.py."""
    import flanner.account as account
    from flanner import session as cache

    fake = cache.Session(
        endpoint="https://cp.test",
        device_id="dev_x",
        organization_id="org_1",
        user_id="sam",
        entitlement="",
        keyring={},
    )
    monkeypatch.setattr(account, "accept_invitation", lambda *a, **kw: fake)

    result = runner.invoke(cli, ["accept", "tok", "--as", "sam"])

    said = " ".join(result.output.split())
    assert "No workspace access yet" in said
    assert "whoami --refresh" in said
    assert "flanner join" not in said


def test_the_store_refusal_says_what_init_does(runner):
    """Naming a command is not explaining it, and people reach this by
    following our own instructions."""
    result = runner.invoke(cli, ["join", "ws_core"])

    assert result.exit_code == 1
    assert "no flanner store yet" in result.output
    assert "adopts the repository" in result.output


# --- the help screen --------------------------------------------------------


def test_every_command_appears_in_a_section():
    """Thirty-four commands in one alphabetical list said nothing about
    which of them a reader needs. Grouping only helps if it is complete —
    a command missing from the table would vanish from the help entirely,
    so `Sectioned` falls back to "Other" and this proves it."""
    from click.testing import CliRunner

    from flanner.cli import cli

    output = CliRunner().invoke(cli, ["--help"]).output
    declared = {name for name, c in cli.commands.items() if not c.hidden}
    shown = set(re.findall(r"^  ([a-z][a-z-]+)\s", output, re.M))

    assert declared <= shown, f"missing from help: {sorted(declared - shown)}"


def test_the_first_split_is_local_versus_networked():
    """The one somebody deciding whether to run this on a private
    repository needs: everything above the line works with no account and
    no network."""
    from flanner.cli import Sectioned

    titles = [title for title, _ in Sectioned.SECTIONS]
    local = next(i for i, t in enumerate(titles) if "no network" in t)
    team = next(i for i, t in enumerate(titles) if "control plane" in t)
    assert local < team


@pytest.mark.parametrize("path", sorted(__import__("flanner.cli", fromlist=["EXAMPLES"]).EXAMPLES))
def test_every_example_names_a_real_command(path):
    """An example for a command that no longer exists is worse than none:
    it is confidently wrong, and nothing else would catch it."""
    from flanner.cli import cli

    node = cli
    for part in path.split(" "):
        assert part in getattr(node, "commands", {}), f"{path}: no command {part!r}"
        node = node.commands[part]


@pytest.mark.parametrize("path", sorted(__import__("flanner.cli", fromlist=["EXAMPLES"]).EXAMPLES))
def test_every_example_starts_with_the_command_it_illustrates(path):
    """Guards against an example drifting onto the wrong entry — a copied
    line under the wrong key reads as correct until somebody runs it."""
    from flanner.cli import EXAMPLES

    first = EXAMPLES[path][0]
    assert first.startswith(f"flanner {path}"), f"{path}: example begins {first!r}"


def test_examples_reach_the_help_screen():
    """They are attached to the built commands, and that pass has to run
    after every command is registered."""
    from click.testing import CliRunner

    from flanner.cli import cli

    for args in (["join"], ["review", "comment"], ["peer", "pull"]):
        output = CliRunner().invoke(cli, [*args, "--help"]).output
        assert "Examples:" in output, f"{' '.join(args)} has no examples"


# --- peer serve opens the store it hands to other threads ---


def test_peer_serve_initialises_the_database(runner, project, monkeypatch):
    """`peer serve` gives `get_session` away instead of calling it.

    It passes the factory to a catch-up thread and to the request handler, so
    unlike every other command it never opens a session on the way in. It also
    never initialised one, and the thread died on its first query while the
    command printed that it was serving. Reproduced the way it happened: a
    process where nothing else has touched the database yet.
    """
    import flanner.database as database
    from flanner.cli import cli

    monkeypatch.setattr(database, "_SessionLocal", None)

    started: dict[str, object] = {}

    def fake_serve(session_factory, load, refresher):
        # What the real server does on its first request, and what the
        # background catch-up thread does immediately.
        with session_factory() as session:
            started["projects"] = len(database.list_projects(session))

    fake_endpoint = type(
        "E",
        (),
        {
            "ready": staticmethod(lambda: None),
            "close": staticmethod(lambda: None),
            "serve": staticmethod(fake_serve),
        },
    )()
    monkeypatch.setattr("flanner.peer_iroh.shared_endpoint", lambda: fake_endpoint)
    monkeypatch.setattr("flanner.cli._catch_up_in_background", lambda dial: None)
    monkeypatch.setattr("flanner.session.load", lambda: object())
    # Serving now renews the entitlement first; stand in for a signed-in device.
    monkeypatch.setattr("flanner.cli._renew_on_use", lambda: object())

    result = runner.invoke(cli, ["peer", "serve"])

    assert "Database not initialized" not in result.output, result.output
    assert started.get("projects") == 1, result.output


# --- the message you meet when join arrives before init ---


def test_join_in_an_unadopted_repo_names_init(runner, initialized, git_repo, monkeypatch):
    """The common case: a store exists, this repository was never adopted.

    It used to say "run this from inside a project", which sends somebody
    somewhere else when the answer is to adopt where they already are.
    """
    monkeypatch.chdir(git_repo)
    result = runner.invoke(cli, ["join", "ws_abc"])

    assert result.exit_code == 1
    assert "has not been adopted" in result.output
    assert "flanner init" in result.output


def test_no_project_outside_a_repo_says_so(runner, initialized, outside_any_repo, monkeypatch):
    """A different situation, and it used to share the same message."""
    monkeypatch.chdir(outside_any_repo)
    result = runner.invoke(cli, ["join", "ws_abc"])

    assert result.exit_code == 1
    assert "Not inside a git repository" in result.output


def test_join_help_states_the_prerequisite(runner):
    """Documented on the website, and absent where somebody actually fails."""
    result = runner.invoke(cli, ["join", "--help"])
    assert "flanner init" in result.output


# --- clock drift, caught during setup rather than mid-sync ---


def _served_at(offset_seconds: int):
    """A fake /health whose Date header is `offset` from now."""
    import email.utils
    from datetime import datetime, timedelta, timezone

    moment = datetime.now(timezone.utc) + timedelta(seconds=offset_seconds)

    class _Response:
        headers = {"Date": email.utils.format_datetime(moment)}

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    return lambda request, timeout=None: _Response()


def test_a_clock_in_step_says_nothing(monkeypatch):
    """A diagnostic that fires when nothing is wrong trains people to ignore it."""
    import urllib.request

    from flanner.cli import _clock_check

    monkeypatch.setattr(urllib.request, "urlopen", _served_at(5))
    assert _clock_check("https://api.flanner.io") == []


def test_a_drifted_clock_is_reported_before_it_bites(monkeypatch):
    import urllib.request

    from flanner.cli import _clock_check

    monkeypatch.setattr(urllib.request, "urlopen", _served_at(600))
    checks = _clock_check("https://api.flanner.io")

    assert [c.code for c in checks] == ["clock_drift"]
    assert checks[0].level == "problem"
    assert "syncing will fail" in checks[0].detail


def test_drift_approaching_the_limit_is_a_warning(monkeypatch):
    """Past half the window, before requests actually start failing."""
    import urllib.request

    from flanner.cli import _clock_check

    monkeypatch.setattr(urllib.request, "urlopen", _served_at(200))
    checks = _clock_check("https://api.flanner.io")

    assert [c.level for c in checks] == ["action"]


def test_an_unreachable_server_is_not_an_alarm(monkeypatch):
    """Being offline is not a clock problem, and must not be reported as one."""
    import urllib.error
    import urllib.request

    from flanner.cli import _clock_check

    def refuse(request, timeout=None):
        raise urllib.error.URLError("offline")

    monkeypatch.setattr(urllib.request, "urlopen", refuse)
    assert _clock_check("https://api.flanner.io") == []


# --- machine-readable output where a script would actually read it ---------


def test_whoami_json_says_signed_out_explicitly(runner, initialized):
    """`signed_in: false` rather than a missing key.

    A caller should not have to decide whether an absent account means "local
    only" or "something went wrong reading it".
    """
    result = runner.invoke(cli, ["whoami", "--output", "json"])
    assert result.exit_code == 0, result.output
    report = json.loads(result.output)
    assert report["signed_in"] is False
    assert report["device_id"].startswith("dev_")


def test_whoami_json_is_parseable_not_decorated(runner, initialized):
    """Nothing but JSON on stdout, or `jq` chokes on the banner."""
    result = runner.invoke(cli, ["whoami", "--output", "json"])
    json.loads(result.output)  # raises if anything else was printed


def test_whoami_table_still_shows_the_device(runner, initialized):
    """The human default is unchanged by adding a machine one."""
    result = runner.invoke(cli, ["whoami"])
    assert "Device" in result.output


# --- listing caps -------------------------------------------------------------


def test_list_limit_cuts_a_projects_plans_and_says_so(runner, project, git_repo):
    import json

    session = get_session()
    for name in ("alpha", "beta", "gamma"):
        pf = create_plan_file(session, project, name)
        create_version(session, pf.id, 1, str(git_repo / ".plans" / f"{name}_v1.md"), "h")

    cut = runner.invoke(cli, ["list", "--project", "proj", "--limit", "2", "--output", "json"])
    assert cut.exit_code == 0, cut.output
    assert len(json.loads(cut.output)) == 2

    shown = runner.invoke(cli, ["list", "--project", "proj", "--limit", "2"])
    assert shown.exit_code == 0, shown.output
    assert "2 of 3 plans shown" in shown.output

    everything = runner.invoke(
        cli, ["list", "--project", "proj", "--limit", "0", "--output", "json"]
    )
    assert len(json.loads(everything.output)) == 3


# --- recording skill use is a question, asked once ----------------------------


def test_init_asks_and_a_yes_starts_recording(runner, git_repo):
    result = runner.invoke(
        cli,
        ["init", "--skip-claude", "--project-root", str(git_repo)],
        input="myproj\ny\n",
    )
    assert result.exit_code == 0, result.output
    assert "Record skill use in this repository?" in result.output
    assert "Recording which skills are used here" in result.output
    assert "observe disable" in result.output  # and how to stop

    # The hook is what does the recording, so that is what is checked.
    settings = json.loads((git_repo / ".claude" / "settings.json").read_text(encoding="utf-8"))
    commands = [
        hook["command"] for entry in settings["hooks"]["PostToolUse"] for hook in entry["hooks"]
    ]
    assert "flanner hook skill-use" in commands


def test_init_takes_no_for_an_answer(runner, git_repo):
    result = runner.invoke(
        cli,
        ["init", "--skip-claude", "--project-root", str(git_repo)],
        input="myproj\nn\n",
    )
    assert result.exit_code == 0, result.output
    assert "Not recording" in result.output
    assert "Recording which skills are used here" not in result.output


def test_nobody_to_ask_records_nothing(runner, git_repo):
    """Enter means yes; an empty pipe is not a person pressing Enter.

    The other hook init installs protects your files. This one records what
    you do, so an unattended install must not switch it on.
    """
    result = runner.invoke(cli, ["init", "--skip-claude", "--project-root", str(git_repo)])
    assert result.exit_code == 0, result.output
    assert "No terminal to ask, so: no." in result.output
    assert "Recording which skills are used here" not in result.output


def test_a_flag_answers_it_without_a_prompt(runner, git_repo):
    result = runner.invoke(
        cli,
        ["init", "--skip-claude", "--project-root", str(git_repo), "--watch-skills"],
        input="myproj\n",
    )
    assert result.exit_code == 0, result.output
    assert "Record skill use in this repository?" not in result.output
    assert "Recording which skills are used here" in result.output

    quiet = runner.invoke(
        cli,
        ["init", "--skip-claude", "--project-root", str(git_repo), "--no-watch-skills"],
        input="myproj\n",
    )
    assert "Record skill use in this repository?" not in quiet.output
