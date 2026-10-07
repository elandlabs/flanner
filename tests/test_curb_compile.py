"""One org policy, compiled into each agent's settings (Curb PRD §10.8).

The R5 criterion: each supported agent's compiled output matches its saved
expected files in tests/data/curb_policy/. Plus the rules' validation, and
the user-settings merges a device without admin rights writes.
"""

import json
import sys
from pathlib import Path

import pytest

from flanner import curb_compile

DATA = Path(__file__).resolve().parent / "data" / "curb_policy"
needs_toml = pytest.mark.skipif(sys.version_info < (3, 11), reason="Python 3.10 has no tomllib")


def rules(**raw):
    return curb_compile.parse(raw)


def sample():
    return curb_compile.parse(json.loads((DATA / "rules.json").read_text(encoding="utf-8")))


def expected(name):
    return (DATA / name).read_text(encoding="utf-8").replace("\r\n", "\n")


# --- the saved expected files ---------------------------------------------------------------


def test_claude_code_managed_settings_match_the_saved_file():
    data, notes = curb_compile.claude_managed(sample())
    assert json.dumps(data, indent=2) + "\n" == expected("managed-settings.json")
    assert notes == [
        "Claude Code's sandbox does not run on native Windows, so there only the Read and web "
        "rules apply; use WSL2"
    ]


@needs_toml
def test_codex_requirements_match_the_saved_file_and_parse():
    import tomllib

    text, notes = curb_compile.codex_requirements(sample())
    assert text == expected("requirements.toml")
    parsed = tomllib.loads(text)
    assert parsed["allowed_sandbox_modes"] == ["read-only", "workspace-write"]
    assert parsed["mcp_servers"]["search"]["identity"] == {"url": "https://mcp.example.com/search"}
    assert notes == ["Codex matches MCP server docs by its program, not its arguments"]


def test_openshell_policy_matches_the_saved_file():
    import yaml

    text, notes = curb_compile.openshell(sample())
    assert text == expected("openshell-policy.yaml")
    data = yaml.safe_load(text)
    assert data["version"] == 1 and data["landlock"]["compatibility"] == "hard_requirement"
    assert "OpenShell cannot deny a path inside a folder its policy allows" in notes
    assert any("docs runs in the sandbox" in n for n in notes)


def test_an_empty_policy_compiles_to_nothing_but_openshells_baseline():
    empty = rules()
    assert curb_compile.claude_managed(empty) == ({}, [])
    assert curb_compile.codex_requirements(empty) == ("\n", [])
    assert "network_policies" not in curb_compile.openshell(empty)[0]


def test_network_turns_the_sandbox_on_and_no_domains_means_none():
    data, _ = curb_compile.claude_managed(rules(network={"allowed_domains": []}))
    assert data["sandbox"]["enabled"] is True
    assert data["sandbox"]["network"]["allowedDomains"] == []
    assert "allowed_sandbox_modes" in curb_compile.codex_requirements(rules(network={}))[0]


def test_a_server_allowed_by_name_alone_is_claude_only_and_noted():
    by_name = rules(mcp={"allowed": [{"name": "docs"}]})
    data, _ = curb_compile.claude_managed(by_name)
    assert data["allowedMcpServers"] == [{"serverName": "docs"}]
    text, notes = curb_compile.codex_requirements(by_name)
    assert "mcp_servers" not in text
    assert notes == ["Codex cannot allow MCP server docs by name alone"]


def test_codex_cannot_say_no_mcp_server_and_says_so():
    _, notes = curb_compile.codex_requirements(rules(mcp={"allowed": []}))
    assert notes == ["Codex requirements cannot say that no MCP server may load"]
    data, _ = curb_compile.claude_managed(rules(mcp={"allowed": []}))
    assert data["allowedMcpServers"] == [] and data["allowManagedMcpServersOnly"] is True


# --- reading the rules ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        {"deny_read": ["~/.aws/*"]},
        {"deny_read": ["secrets"]},
        {"deny_read": ["~/../etc"]},
        {"deny_read": "~/.aws"},
        {"sandbox": "on"},
        {"web": "limited"},
        {"network": ["github.com"]},
        {"mcp": {"allowed": [{"name": "x", "command": ["a"], "url": "https://b"}]}},
        {"mcp": {"allowed": [{"command": ["a"]}]}},
        {"mcp": {"allowed": [{"name": "x", "url": "ftp://b"}]}},
        {"mcp": {}},
    ],
)
def test_a_malformed_rule_is_refused(raw):
    with pytest.raises(ValueError):
        curb_compile.parse(raw)


def test_windows_paths_and_unknown_rules_are_kept():
    parsed = rules(deny_read=["C:\\Users\\a\\.aws", "/etc/x/"], future_rule=True)
    assert parsed.deny_read == ("C:\\Users\\a\\.aws", "/etc/x")
    assert parsed.unknown == ("future_rule",)
    assert curb_compile._claude_reads("C:\\Users\\a\\.aws") == [
        "Read(//c/users/a/.aws)",
        "Read(//c/users/a/.aws/**)",
    ]


# --- user settings ----------------------------------------------------------------------------


def test_claude_user_settings_keep_what_is_there_and_only_narrow():
    before = {
        "model": "x",
        "permissions": {"deny": ["Bash(rm:*)"], "allow": ["Read(./**)"]},
        "sandbox": {"network": {"allowedDomains": ["github.com", "example.com"]}},
        "allowedMcpServers": [{"serverCommand": ["docs-mcp", "--stdio"]}, {"serverName": "x"}],
    }
    after, actions, guided = curb_compile.claude_user(sample(), before, platform="linux")
    assert after["model"] == "x" and after["permissions"]["allow"] == ["Read(./**)"]
    assert after["permissions"]["deny"][0] == "Bash(rm:*)"
    assert "Read(~/.aws/**)" in after["permissions"]["deny"]
    assert after["sandbox"]["network"]["allowedDomains"] == ["github.com"]
    assert after["allowedMcpServers"] == [{"serverCommand": ["docs-mcp", "--stdio"]}]
    assert after["sandbox"]["enabled"] is True and guided == []
    assert before["permissions"]["deny"] == ["Bash(rm:*)"]  # not mutated
    again, more, _ = curb_compile.claude_user(sample(), after, platform="linux")
    assert again == after and more == []
    assert "turn off web fetch and web search" in actions


def test_on_native_windows_claude_gets_the_read_rules_and_a_guided_step():
    after, _, guided = curb_compile.claude_user(sample(), {}, platform="win32")
    assert "sandbox" not in after
    assert "Read(~/.ssh)" in after["permissions"]["deny"]
    assert any("WSL2" in g for g in guided)


@needs_toml
def test_codex_user_config_is_edited_line_by_line_with_comments_kept():
    original = (
        "# my config\n"
        'sandbox_mode = "danger-full-access"\n'
        'web_search = "live"\n'
        "\n"
        "[sandbox_workspace_write]\n"
        "network_access = true  # needed for npm\n"
        "\n"
        "[mcp_servers.docs]\n"
        'command = "docs-mcp"\n'
        "\n"
        "[mcp_servers.stranger]\n"
        'command = "other-mcp"\n'
    )
    import tomllib

    before = tomllib.loads(original)
    after, text, actions, guided = curb_compile.codex_user(sample(), before, original)
    assert text is not None and text.startswith("# my config\n")
    assert tomllib.loads(text) == after
    assert after["sandbox_mode"] == "workspace-write"
    assert after["web_search"] == "disabled"
    assert after["sandbox_workspace_write"]["network_access"] is False
    assert after["mcp_servers"]["stranger"]["enabled"] is False
    assert "enabled" not in after["mcp_servers"]["docs"]
    assert any("permissions profile" in g for g in guided)  # deny_read needs one
    assert "turn off an MCP server" in actions


@needs_toml
def test_codex_with_a_profile_denies_the_paths_in_it():
    original = (
        'default_permissions = "dev"\n\n[permissions.dev.filesystem]\n'
        '":workspace_roots" = "write"\n'
    )
    import tomllib

    after, text, _, guided = curb_compile.codex_user(
        rules(deny_read=["~/.aws"]), tomllib.loads(original), original
    )
    assert text is not None and guided == []
    assert after["permissions"]["dev"]["filesystem"]["~/.aws"] == "deny"


@needs_toml
def test_codex_with_nothing_to_change_writes_nothing():
    original = 'web_search = "disabled"\n'
    import tomllib

    _, text, actions, _ = curb_compile.codex_user(
        rules(web="off"), tomllib.loads(original), original
    )
    assert text is None and actions == []
