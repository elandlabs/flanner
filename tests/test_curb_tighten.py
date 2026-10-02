"""The tighten-only test, including every known trap in Curb PRD §10.8."""

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from flanner import curb_settings, curb_tighten
from flanner.curb_context import default
from flanner.curb_credentials import Credential

needs_toml = pytest.mark.skipif(sys.version_info < (3, 11), reason="Python 3.10 has no tomllib")


@pytest.fixture
def dirs(tmp_path, monkeypatch):
    found = SimpleNamespace(
        claude=tmp_path / "claude-config",
        codex=tmp_path / "codex-home",
        project=tmp_path / "project",
        root=tmp_path / "system",
        home=Path.home(),
    )
    for folder in (found.claude, found.codex, found.project):
        folder.mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(found.claude))
    monkeypatch.setenv("CODEX_HOME", str(found.codex))
    return found


def probes(dirs):
    return [
        Credential("aws", "cloud", "AWS", (dirs.home / ".aws" / "credentials",)),
        Credential("probe", "probe", "Probe", (dirs.project / ".env",)),
    ]


def judge(dirs, agent, path, before, after):
    if before is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            before if isinstance(before, str) else json.dumps(before), encoding="utf-8"
        )
    return curb_tighten.check(
        default(agent, dirs.project),
        path,
        after,
        probes=probes(dirs),
        platform="linux",
        home=dirs.home,
        env={},
        root=dirs.root,
    )


def claude(dirs, before, after, path=None):
    return judge(dirs, "claude", path or dirs.claude / "settings.json", before, after)


STRICT = {
    "sandbox": {
        "enabled": True,
        "allowUnsandboxedCommands": False,
        "filesystem": {"denyRead": ["~/.aws"]},
    }
}


# --- Claude Code ------------------------------------------------------------------------


def test_adding_a_deny_rule_is_tighten_only(dirs):
    verdict = claude(
        dirs,
        {"permissions": {"deny": ["WebFetch"]}},
        {"permissions": {"deny": ["WebFetch", "Read(~/.aws/**)"]}},
    )
    assert verdict.tighten_only, verdict


def test_removing_a_deny_rule_is_broader(dirs):
    verdict = claude(dirs, {"permissions": {"deny": ["Read(~/.aws/**)"]}}, {})
    assert "permissions.deny moves the looser way" in verdict.broader
    assert any("readable" in reason for reason in verdict.broader)


def test_adding_an_allow_rule_is_broader(dirs):
    verdict = claude(dirs, {}, {"permissions": {"allow": ["Bash(curl *)"]}})
    assert verdict.broader == ("permissions.allow moves the looser way",)


def test_turning_the_sandbox_on_is_tighten_only(dirs):
    assert claude(dirs, {}, STRICT).tighten_only


def test_turning_the_sandbox_off_is_broader(dirs):
    verdict = claude(dirs, STRICT, {"sandbox": {"enabled": False}})
    assert "sandbox.enabled moves the looser way" in verdict.broader


def test_opening_the_escape_hatch_is_broader(dirs):
    loose = {"sandbox": {**STRICT["sandbox"], "allowUnsandboxedCommands": True}}
    assert not claude(dirs, STRICT, loose).tighten_only


def test_a_key_curb_cannot_judge_needs_a_person(dirs):
    verdict = claude(dirs, {}, {"env": {"HTTPS_PROXY": "http://proxy.local:3128"}})
    assert not verdict.tighten_only
    assert verdict.unproven == ("env.HTTPS_PROXY changes, and Curb cannot establish its effect",)


def test_an_unreadable_file_cannot_be_judged(dirs):
    verdict = claude(dirs, '{"permissions": ', {"permissions": {"deny": ["WebFetch"]}})
    assert not verdict.tighten_only


def managed(dirs, data):
    path = curb_settings.managed_dir("linux", dirs.root) / "managed-settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_trap_removing_the_last_command_entry_lets_any_program_use_an_allowed_name(dirs):
    before = {
        "allowedMcpServers": [{"serverName": "github"}, {"serverCommand": ["npx", "gh-mcp"]}]
    }
    after = {"allowedMcpServers": [{"serverName": "github"}]}
    verdict = claude(dirs, None, after, path=managed(dirs, before))
    assert "an MCP server could load that could not before" in verdict.broader


def test_trap_removing_an_empty_allowlist_allows_every_server(dirs):
    verdict = claude(dirs, None, {}, path=managed(dirs, {"allowedMcpServers": []}))
    assert "an MCP server could load that could not before" in verdict.broader


def test_trap_dropping_managed_only_lets_other_allowlists_count(dirs):
    (dirs.claude / "settings.json").write_text(
        json.dumps({"allowedMcpServers": [{"serverName": "personal"}]}), encoding="utf-8"
    )
    before = {"allowManagedMcpServersOnly": True, "allowedMcpServers": [{"serverName": "work"}]}
    after = {"allowedMcpServers": [{"serverName": "work"}]}
    verdict = claude(dirs, None, after, path=managed(dirs, before))
    assert not verdict.tighten_only
    assert "an MCP server could load that could not before" in verdict.broader


def test_adding_any_allowlist_entry_admits_more(dirs):
    before = {"allowedMcpServers": [{"serverName": "github"}]}
    after = {"allowedMcpServers": [{"serverName": "github"}, {"serverCommand": ["npx", "gh"]}]}
    verdict = claude(dirs, None, after, path=managed(dirs, before))
    assert "an MCP server could load that could not before" in verdict.broader


def test_denying_an_mcp_server_is_tighten_only(dirs):
    verdict = claude(
        dirs, None, {"deniedMcpServers": [{"serverName": "x"}]}, path=managed(dirs, {})
    )
    assert verdict.tighten_only, verdict


# --- Codex --------------------------------------------------------------------------------


def codex(dirs, before, after):
    return judge(dirs, "codex", dirs.codex / "config.toml", before, after)


@needs_toml
@pytest.mark.parametrize(
    ("before", "after"),
    [
        ('sandbox_mode = "danger-full-access"\n', {"sandbox_mode": "workspace-write"}),
        ('approval_policy = "on-request"\n', {"approval_policy": "never"}),
        (
            "[sandbox_workspace_write]\nnetwork_access = true\n",
            {"sandbox_workspace_write": {"network_access": False}},
        ),
        ('web_search = "live"\n', {"web_search": "cached"}),
        ('[mcp_servers.docs]\ncommand = "docs"\n', {}),
        (
            'default_permissions = "mine"\n[permissions.mine.filesystem]\n"~/.ssh" = "deny"\n',
            {
                "default_permissions": "mine",
                "permissions": {"mine": {"filesystem": {"~/.ssh": "deny", "~/.aws": "deny"}}},
            },
        ),
    ],
)
def test_codex_changes_that_tighten(dirs, before, after):
    verdict = codex(dirs, before, after)
    assert verdict.tighten_only, verdict


@needs_toml
@pytest.mark.parametrize(
    ("before", "after", "reason"),
    [
        ('web_search = "cached"\n', {"web_search": "live"}, "web_search moves the looser way"),
        (
            'approval_policy = "never"\n',
            {"approval_policy": "on-request"},
            "approval_policy moves the looser way",
        ),
        (
            "",
            {"mcp_servers": {"docs": {"command": "docs"}}},
            "mcp_servers.docs.command changes, and Curb cannot establish its effect",
        ),
        (
            'default_permissions = "a"\n',
            {"default_permissions": "b"},
            "default_permissions changes, and Curb cannot establish its effect",
        ),
    ],
)
def test_codex_changes_that_do_not(dirs, before, after, reason):
    verdict = codex(dirs, before, after)
    assert not verdict.tighten_only
    assert reason in verdict.broader + verdict.unproven
