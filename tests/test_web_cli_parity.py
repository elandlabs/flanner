"""The web UI does what the CLI does, through the same code, and says why when it cannot.

Each surface used to answer setup questions its own way. Settings read
Claude Desktop's file alone and disagreed with `flanner status`; capture
mode, registration and push acceptance could only be changed in a terminal
or an environment variable; and actions the page could not take were simply
missing from it. These tests hold the two surfaces to one answer.
"""

from __future__ import annotations

import html
import re
import subprocess
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import unquote

import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient

import flanner.claude_integration as ci
from flanner import peer, setup_check
from flanner.cli import _register_codex, cli
from flanner.database import get_project_by_root, get_session
from flanner.operations import OPERATIONS
from flanner.web import _cli_only, app

LOCAL_URL = "http://127.0.0.1:8080"
TEMPLATES = Path(__file__).resolve().parent.parent / "flanner" / "web" / "templates"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """An adopted repository, the web UI running inside it, and the CLI beside it."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("FLANNER_HOME", str(home))
    monkeypatch.delenv("FLANNER_ACCEPT_PUSHES", raising=False)
    where = tmp_path / "repo"
    where.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=where, check=True)  # noqa: S603,S607
    monkeypatch.chdir(where)
    runner = CliRunner()
    started = runner.invoke(
        cli, ["init", "--skip-claude", "--project-root", str(where)], input="demo\n"
    )
    assert started.exit_code == 0, started.output
    client = TestClient(app, base_url=LOCAL_URL, follow_redirects=False)
    return SimpleNamespace(runner=runner, where=where, client=client)


def text(response) -> str:
    return html.unescape(response.text)


def said(response) -> str:
    return unquote(response.headers["location"])


def project(repo):
    return get_project_by_root(get_session(), str(repo.where))


# --- 1. settings and status read one check ---------------------------------------


def test_settings_and_status_agree_about_claude_code(repo, tmp_path):
    """Registered at user scope only: the case Settings used to call unregistered."""
    (tmp_path / "claude.json").write_text('{"mcpServers": {"flanner": {}}}', encoding="utf-8")

    status = repo.runner.invoke(cli, ["status"])
    page = text(repo.client.get("/settings"))

    assert "user scope" in status.output
    assert "Claude Code" in page and "user scope" in page
    assert "Codex" in page and "Not registered" in page


# --- 2. the setup check page -------------------------------------------------------


def test_the_setup_page_shows_what_the_setup_check_returns(repo):
    check = setup_check.check(get_session())

    page = text(repo.client.get("/setup"))

    assert check["project"]["name"] in page
    assert f'value="{check["capture_mode"]}" selected' in page
    assert f">{check['tools']['count']}<" in page
    assert "flanner login <code>" in page


# --- 3. capture mode ---------------------------------------------------------------


def test_capture_mode_from_the_page_writes_what_the_cli_writes(repo):
    policy = repo.where / ".flanner" / "memory-policy.yml"
    assert repo.runner.invoke(cli, ["mem", "mode", "off"]).exit_code == 0
    by_cli = policy.read_bytes()
    assert repo.runner.invoke(cli, ["mem", "mode", "suggest"]).exit_code == 0

    sent = repo.client.post("/memory/mode", data={"capture_mode": "off"})

    assert sent.status_code == 303
    assert "Capture mode is now off" in said(sent)
    assert policy.read_bytes() == by_cli


def test_an_unknown_capture_mode_changes_nothing(repo):
    assert repo.runner.invoke(cli, ["mem", "mode", "explicit"]).exit_code == 0
    policy = repo.where / ".flanner" / "memory-policy.yml"
    before = policy.read_bytes()

    sent = repo.client.post("/memory/mode", data={"capture_mode": "everything"})

    assert "capture mode must be one of" in said(sent)
    assert policy.read_bytes() == before


# --- 4. registering an agent -------------------------------------------------------


def test_registering_claude_desktop_from_the_page_leaves_the_bytes_register_leaves(
    repo, tmp_path, monkeypatch
):
    existing = '{"mcpServers": {"other": {"command": "x"}}, "theme": "dark"}'
    by_cli = tmp_path / "cli" / "claude_desktop_config.json"
    by_web = tmp_path / "web" / "claude_desktop_config.json"
    for path in (by_cli, by_web):
        path.parent.mkdir()
        path.write_text(existing, encoding="utf-8")

    monkeypatch.setattr(ci, "get_claude_config_path", lambda: by_cli)
    assert repo.runner.invoke(cli, ["register"]).exit_code == 0

    monkeypatch.setattr(ci, "get_claude_config_path", lambda: by_web)
    preview = repo.client.get("/setup/register/claude-desktop")
    assert '+    "flanner": {' in text(preview)
    assert by_web.read_text(encoding="utf-8") == existing, "a preview must not write"

    seen = ci.registration_preview("claude-desktop")["fingerprint"]
    confirmed = repo.client.post("/setup/register/claude-desktop", data={"seen": seen})

    assert confirmed.status_code == 303
    assert by_web.read_bytes() == by_cli.read_bytes()


def test_registering_codex_from_the_page_writes_what_setup_writes(repo, tmp_path, monkeypatch):
    existing = 'model = "o3"\n'
    by_cli, by_web = tmp_path / "cli.toml", tmp_path / "web.toml"
    by_cli.write_text(existing, encoding="utf-8")
    by_web.write_text(existing, encoding="utf-8")
    monkeypatch.setattr(ci, "codex_installed", lambda: True)

    monkeypatch.setattr(ci, "codex_config_path", lambda: by_cli)
    _register_codex()

    monkeypatch.setattr(ci, "codex_config_path", lambda: by_web)
    assert "[mcp_servers.flanner]" in text(repo.client.get("/setup/register/codex"))
    seen = ci.registration_preview("codex")["fingerprint"]
    repo.client.post("/setup/register/codex", data={"seen": seen})

    assert by_web.read_bytes() == by_cli.read_bytes()


def test_a_file_that_changed_after_the_preview_is_not_overwritten(repo, tmp_path, monkeypatch):
    path = tmp_path / "desktop" / "claude_desktop_config.json"
    path.parent.mkdir()
    path.write_text('{"mcpServers": {}}', encoding="utf-8")
    monkeypatch.setattr(ci, "get_claude_config_path", lambda: path)
    seen = ci.registration_preview("claude-desktop")["fingerprint"]
    changed = '{"mcpServers": {}, "edited": "by hand"}'
    path.write_text(changed, encoding="utf-8")

    sent = repo.client.post("/setup/register/claude-desktop", data={"seen": seen})

    assert said(sent).startswith("/setup/register/claude-desktop?said=The file changed")
    assert path.read_text(encoding="utf-8") == changed


def test_claude_code_is_not_registered_from_the_page_and_says_why(repo):
    # `init` registers Claude Code for the repository it adopts.
    (repo.where / ".mcp.json").unlink()

    page = text(repo.client.get("/settings"))

    assert repo.client.get("/setup/register/claude-code").status_code == 404
    assert "Claude Code · not registered" in page
    assert "Claude Code writes its own config through its CLI" in page


# --- 5. terminal-only actions ------------------------------------------------------


def test_terminal_only_actions_are_shown_disabled_with_the_registry_reason(repo):
    reasons = {op.action: op.why_not_web for op in OPERATIONS}

    mesh = text(repo.client.get("/mesh"))
    detail = text(repo.client.get(f"/projects/{project(repo).id}"))

    assert reasons["Enrol this machine"] in mesh
    assert "flanner login <code>" in mesh and "Terminal only" in mesh
    assert reasons["Bind a repository to a workspace"] in detail
    assert 'flanner join <workspace-id> --project "demo"' in detail


def test_every_terminal_only_row_on_any_page_has_a_reason():
    named = {
        match
        for page in TEMPLATES.glob("*.html")
        for match in re.findall(r'cli_only\("([^"]+)"\)', page.read_text(encoding="utf-8"))
    }

    assert named, "no page names a terminal-only action; the scan is broken"
    for action in named:
        assert _cli_only(action)["why"]


# --- 6. the team card --------------------------------------------------------------


def test_signed_out_the_team_card_explains_instead_of_disappearing(repo):
    page = text(repo.client.get("/settings"))

    assert "not signed in, so it has no team" in page
    assert "flanner login <code>" in page


def test_signed_in_the_team_card_links_to_the_console_it_signed_in_to(repo, monkeypatch):
    import flanner.session as cache

    held = SimpleNamespace(
        endpoint="https://console.example.test/", organization_id="org_1", device_keys={}
    )
    monkeypatch.setattr(cache, "load", lambda: held)

    page = repo.client.get("/settings").text

    assert 'href="https://console.example.test/members"' in page
    assert 'href="https://console.example.test/billing"' in page

    held.endpoint = "javascript:alert(1)"
    assert 'href="javascript' not in repo.client.get("/settings").text


# --- 7. sharing and push acceptance ------------------------------------------------


def test_push_acceptance_is_one_setting_for_the_page_the_cli_and_the_peer(repo):
    detail = f"/projects/{project(repo).id}"

    sent = repo.client.post("/mesh/pushes", data={"accept": "off", "back": detail})

    assert said(sent).startswith(f"{detail}?said=This device now refuses pushes")
    assert not peer.accepting_pushes()
    assert "Accept pushes · off" in text(repo.client.get(detail))

    assert repo.runner.invoke(cli, ["peer", "pushes", "on"]).exit_code == 0
    assert peer.accepting_pushes()


def test_the_environment_variable_still_decides_and_the_page_says_so(repo, monkeypatch):
    monkeypatch.setenv("FLANNER_ACCEPT_PUSHES", "0")
    detail = f"/projects/{project(repo).id}"

    sent = repo.client.post("/mesh/pushes", data={"accept": "on", "back": detail})

    assert "Nothing was changed" in said(sent)
    assert not peer.accepting_pushes()
    assert 'disabled title="Set by FLANNER_ACCEPT_PUSHES"' in text(repo.client.get(detail))


def test_the_pushes_form_will_not_send_anybody_off_the_site(repo):
    for back in ("//evil.test", "https://evil.test", "/" + chr(92) + "evil.test"):
        sent = repo.client.post("/mesh/pushes", data={"accept": "on", "back": back})
        assert said(sent).startswith("/mesh?said="), back


def test_a_bound_project_shows_its_workspace_and_why_there_is_no_role(repo):
    session = get_session()
    bound = get_project_by_root(session, str(repo.where))
    bound.workspace_id = "ws_parity"
    session.commit()

    page = text(repo.client.get(f"/projects/{bound.id}"))

    assert "ws_parity" in page
    assert "no access yet" in page and "not logged in" in page
