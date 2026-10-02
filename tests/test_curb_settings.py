"""Curb's settings resolution: which files a launch loads and what they merge to.

Every case plants files under a temp directory and points the agents'
directories and the system root at it. Nothing reads the machine's own
configuration.
"""

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from flanner import curb_settings
from flanner.curb_context import default, parse

needs_toml = pytest.mark.skipif(sys.version_info < (3, 11), reason="Python 3.10 has no tomllib")


@pytest.fixture
def dirs(tmp_path, monkeypatch):
    found = SimpleNamespace(
        claude=tmp_path / "claude-config",
        codex=tmp_path / "codex-home",
        root=tmp_path / "system-root",
        project=tmp_path / "project",
    )
    found.project.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(found.claude))
    monkeypatch.setenv("CODEX_HOME", str(found.codex))
    monkeypatch.setenv("FLANNER_CURB_SYSTEM_ROOT", str(found.root))
    return found


def write(path: Path, data) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")
    return path


def claude(dirs, *argv: str, platform: str = "linux"):
    context = parse(["claude", *argv], dirs.project) if argv else default("claude", dirs.project)
    return curb_settings.resolve_claude(context, platform=platform)


def managed(dirs, data) -> None:
    write(dirs.root / "etc" / "claude-code" / "managed-settings.json", data)


# --- Claude Code: permissions ------------------------------------------------------


def test_deny_rules_from_every_source_apply(dirs):
    write(dirs.claude / "settings.json", {"permissions": {"deny": ["Read(~/.aws/**)"]}})
    write(dirs.project / ".claude" / "settings.json", {"permissions": {"deny": ["Read(.env)"]}})
    found = claude(dirs, "--disallowedTools", "Bash")
    assert {r.text for r in found.deny} == {"Read(~/.aws/**)", "Read(.env)", "Bash"}


def test_a_user_rule_anchors_at_the_config_directory_and_a_project_rule_at_the_project(dirs):
    write(dirs.claude / "settings.json", {"permissions": {"deny": ["Read(/secrets/**)"]}})
    write(dirs.project / ".claude" / "settings.json", {"permissions": {"deny": ["Read(/x)"]}})
    anchors = {r.source: r.anchor for r in claude(dirs).deny}
    assert anchors == {"user": dirs.claude, "project": dirs.project}


def test_the_mode_comes_from_the_highest_scope_that_sets_it(dirs):
    write(dirs.claude / "settings.json", {"permissions": {"defaultMode": "acceptEdits"}})
    write(dirs.project / ".claude" / "settings.json", {"permissions": {"defaultMode": "plan"}})
    assert claude(dirs).mode == "plan"
    assert claude(dirs, "--permission-mode", "dontAsk").mode == "dontAsk"


def test_a_project_cannot_turn_on_bypass_mode(dirs):
    write(
        dirs.project / ".claude" / "settings.json",
        {"permissions": {"defaultMode": "bypassPermissions"}},
    )
    assert claude(dirs).mode == "default"


def test_disabling_bypass_mode_holds_against_the_launch_flag(dirs):
    write(
        dirs.claude / "settings.json", {"permissions": {"disableBypassPermissionsMode": "disable"}}
    )
    assert claude(dirs, "--dangerously-skip-permissions").mode == "default"


def test_a_settings_file_given_at_launch_is_a_layer_anchored_at_its_folder(dirs):
    path = write(dirs.project / "ci" / "settings.json", {"permissions": {"deny": ["Read(/k)"]}})
    found = claude(dirs, "--settings", str(path))
    cli = [layer for layer in found.layers if layer.name == "cli"]
    assert cli and cli[0].usable and cli[0].anchor == path.parent
    assert [r.anchor for r in found.deny if r.source == "cli"] == [path.parent]


def test_inline_json_settings_are_read(dirs):
    found = claude(dirs, "--settings", '{"sandbox": {"enabled": true}}')
    assert found.sandbox_enabled


def test_setting_sources_leaves_out_what_it_does_not_name(dirs):
    write(dirs.project / ".claude" / "settings.json", {"permissions": {"deny": ["Read(.env)"]}})
    found = claude(dirs, "--setting-sources", "user")
    assert not any(r.source == "project" for r in found.deny)
    assert any("not loaded" in layer.where for layer in found.layers if layer.name == "project")


def test_managed_rules_only_drops_every_other_source(dirs):
    managed(
        dirs,
        {"permissions": {"allowManagedPermissionRulesOnly": True, "deny": ["Read(~/.ssh/**)"]}},
    )
    write(dirs.claude / "settings.json", {"permissions": {"deny": ["Read(~/.aws/**)"]}})
    assert [r.text for r in claude(dirs).deny] == ["Read(~/.ssh/**)"]


def test_an_unreadable_settings_file_is_assumed_and_counts_for_nothing(dirs):
    write(dirs.claude / "settings.json", "{ not json")
    found = claude(dirs)
    user = next(layer for layer in found.layers if layer.name == "user")
    assert user.present and user.error
    assert any("counts as unknown" in note for note in found.assumed)


# --- Claude Code: sandbox -------------------------------------------------------


def test_strict_sandbox_in_user_settings_holds_over_a_project_turning_it_back_on(dirs):
    write(dirs.claude / "settings.json", {"sandbox": {"allowUnsandboxedCommands": False}})
    write(
        dirs.project / ".claude" / "settings.json",
        {"sandbox": {"enabled": True, "allowUnsandboxedCommands": True}},
    )
    found = claude(dirs)
    assert found.sandbox_enabled and not found.allow_unsandboxed and not found.admin_required


def test_an_admin_required_sandbox_ignores_the_project_s_excluded_commands(dirs):
    managed(dirs, {"sandbox": {"enabled": True, "allowUnsandboxedCommands": False}})
    write(
        dirs.project / ".claude" / "settings.json", {"sandbox": {"excludedCommands": ["docker *"]}}
    )
    write(dirs.claude / "settings.json", {"sandbox": {"excludedCommands": ["make *"]}})
    found = claude(dirs)
    assert found.admin_required
    assert found.excluded_commands == ["make *"]


def test_a_project_cannot_switch_off_filesystem_isolation(dirs):
    write(
        dirs.project / ".claude" / "settings.json",
        {"sandbox": {"enabled": True, "filesystem": {"disabled": True}}},
    )
    assert not claude(dirs).filesystem_disabled
    write(dirs.claude / "settings.json", {"sandbox": {"filesystem": {"disabled": True}}})
    assert claude(dirs).filesystem_disabled


def test_managed_domains_only_keeps_only_managed_webfetch_domains(dirs):
    managed(
        dirs,
        {
            "sandbox": {"network": {"allowManagedDomainsOnly": True}},
            "permissions": {"allow": ["WebFetch(domain:pypi.org)"]},
        },
    )
    write(dirs.claude / "settings.json", {"permissions": {"allow": ["WebFetch(domain:*)"]}})
    found = claude(dirs)
    assert found.managed_domains_only
    assert found.allowed_domains == ["pypi.org"]


def test_strict_allowlist_is_honoured_only_from_trusted_scopes(dirs):
    write(
        dirs.project / ".claude" / "settings.json",
        {"sandbox": {"network": {"strictAllowlist": True}}},
    )
    assert not claude(dirs).strict_allowlist
    write(dirs.claude / "settings.json", {"sandbox": {"network": {"strictAllowlist": True}}})
    assert claude(dirs).strict_allowlist


def test_credential_masks_count_only_from_trusted_scopes(dirs):
    write(
        dirs.project / ".claude" / "settings.json",
        {
            "sandbox": {
                "credentials": {
                    "envVars": [
                        {"name": "A_TOKEN", "mode": "mask"},
                        {"name": "B_TOKEN", "mode": "deny"},
                    ]
                }
            }
        },
    )
    assert claude(dirs).credential_env == ["B_TOKEN"]


# --- Claude Code: MCP servers -----------------------------------------------------


def test_servers_come_from_every_scope_once_each(dirs):
    write(
        dirs.claude / ".claude.json",
        {
            "mcpServers": {"pencil": {"command": "pencil"}},
            "projects": {
                str(dirs.project): {
                    "mcpServers": {"local-one": {"command": "x"}},
                    "disabledMcpjsonServers": ["off"],
                }
            },
        },
    )
    write(dirs.claude / "settings.json", {"mcpServers": {"pencil": {"command": "pencil"}}})
    write(
        dirs.project / ".mcp.json",
        {
            "mcpServers": {
                "shared": {"type": "http", "url": "https://x.dev/mcp"},
                "off": {"command": "y"},
            }
        },
    )
    names = sorted(s.name for s in claude(dirs).mcp)
    assert names == ["local-one", "pencil", "shared"]


def test_a_deployed_managed_mcp_file_takes_exclusive_control(dirs):
    write(
        dirs.root / "etc" / "claude-code" / "managed-mcp.json",
        {"mcpServers": {"company": {"command": "/usr/bin/company"}}},
    )
    write(dirs.claude / ".claude.json", {"mcpServers": {"pencil": {"command": "pencil"}}})
    assert [s.name for s in claude(dirs).mcp] == ["company"]


def test_strict_mcp_config_loads_only_the_given_servers(dirs):
    write(dirs.claude / ".claude.json", {"mcpServers": {"pencil": {"command": "pencil"}}})
    path = write(dirs.project / "mcp.json", {"mcpServers": {"only": {"command": "only"}}})
    found = claude(dirs, "--mcp-config", str(path), "--strict-mcp-config")
    assert [s.name for s in found.mcp] == ["only"]


def test_safe_mode_loads_no_servers_but_the_admin_s(dirs):
    write(dirs.claude / ".claude.json", {"mcpServers": {"pencil": {"command": "pencil"}}})
    assert claude(dirs, "--safe-mode").mcp == []


def test_an_allowlist_name_entry_matches_stdio_only_without_command_entries(dirs):
    """The mixed name-and-command trap (E14), as the tighten-only test will need."""
    write(
        dirs.claude / ".claude.json",
        {"mcpServers": {"github": {"command": "node", "args": ["server.js"]}}},
    )
    mixed = [{"serverName": "github"}, {"serverCommand": ["npx", "-y", "approved"]}]
    managed(dirs, {"allowedMcpServers": mixed})
    assert claude(dirs).mcp == []
    managed(dirs, {"allowedMcpServers": [{"serverName": "github"}]})
    assert [s.name for s in claude(dirs).mcp] == ["github"]


def test_a_denylist_entry_removes_a_server(dirs):
    write(dirs.claude / ".claude.json", {"mcpServers": {"bad": {"command": "bad"}}})
    managed(dirs, {"deniedMcpServers": [{"serverName": "bad"}]})
    assert claude(dirs).mcp == []


def test_env_values_never_leave_the_resolver_only_names(dirs):
    write(
        dirs.claude / ".claude.json",
        {"mcpServers": {"s": {"command": "s", "env": {"API_TOKEN": "sk-live-123"}}}},
    )
    server = claude(dirs).mcp[0]
    assert server.env_names == ("API_TOKEN",)
    assert "sk-live-123" not in repr(server)


def test_hooks_are_counted_and_bare_mode_skips_them(dirs):
    write(
        dirs.claude / "settings.json",
        {"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command"}] * 2}]}},
    )
    assert [(h.event, h.count) for h in claude(dirs).hooks] == [("PreToolUse", 2)]
    assert claude(dirs, "--bare").hooks == []


def test_managed_settings_live_where_each_platform_keeps_them(dirs):
    root = dirs.root
    assert curb_settings.managed_dir("linux", root) == root / "etc" / "claude-code"
    assert curb_settings.managed_dir("darwin", root) == (
        root / "Library" / "Application Support" / "ClaudeCode"
    )


# --- Codex -------------------------------------------------------------------------


def codex(dirs, *argv: str):
    context = parse(["codex", *argv], dirs.project) if argv else default("codex", dirs.project)
    return curb_settings.resolve_codex(context, platform="linux")


@needs_toml
def test_codex_layers_merge_in_documented_order(dirs):
    write(dirs.root / "etc" / "codex" / "config.toml", 'sandbox_mode = "read-only"\n')
    assert codex(dirs).sandbox == "read-only"
    write(dirs.codex / "config.toml", 'sandbox_mode = "workspace-write"\n')
    assert codex(dirs).sandbox == "workspace-write"
    write(dirs.codex / "ci.config.toml", 'sandbox_mode = "danger-full-access"\n')
    assert codex(dirs, "-p", "ci").sandbox == "danger-full-access"
    assert codex(dirs, "-p", "ci", "-s", "read-only").sandbox == "read-only"


@needs_toml
def test_a_project_config_loads_only_when_the_project_is_trusted(dirs):
    write(dirs.project / ".codex" / "config.toml", 'sandbox_mode = "danger-full-access"\n')
    write(dirs.codex / "config.toml", 'sandbox_mode = "read-only"\n')
    assert codex(dirs).sandbox == "read-only"
    key = str(dirs.project).replace("\\", "\\\\")
    write(
        dirs.codex / "config.toml",
        f'sandbox_mode = "read-only"\n[projects."{key}"]\ntrust_level = "trusted"\n',
    )
    found = codex(dirs)
    assert found.trusted and found.sandbox == "danger-full-access"


@needs_toml
def test_codex_defaults_are_recorded_as_assumed(dirs):
    found = codex(dirs)
    assert found.sandbox_assumed and found.approval_assumed and found.web_search_assumed
    assert found.apps is None
    assert len(found.defaults) == 4


@needs_toml
@pytest.mark.parametrize(
    ("config", "apps"),
    [
        ("[features]\napps = false\n", False),
        ("[apps._default]\nenabled = false\n", False),
        ("[apps._default]\nenabled = false\n[apps.github]\nenabled = true\n", True),
        ("[apps.github]\nenabled = false\n", None),
    ],
)
def test_codex_apps_are_on_unless_turned_off(dirs, config, apps):
    write(dirs.codex / "config.toml", config)
    assert codex(dirs).apps is apps


@needs_toml
def test_codex_apps_can_be_turned_off_at_launch(dirs):
    assert codex(dirs, "--disable", "apps").apps is False


@needs_toml
def test_codex_system_config_is_a_unix_location(dirs, tmp_path):
    root = tmp_path / "root"
    write(root / "etc" / "codex" / "config.toml", 'sandbox_mode = "read-only"\n')
    context = default("codex", dirs.project)
    assert curb_settings.resolve_codex(context, platform="linux", root=root).sandbox == "read-only"
    windows = curb_settings.resolve_codex(context, platform="win32", root=root)
    assert windows.sandbox_assumed


@needs_toml
def test_the_bypass_flag_removes_the_sandbox(dirs):
    found = codex(dirs, "--dangerously-bypass-approvals-and-sandbox")
    assert (found.sandbox, found.approval) == ("danger-full-access", "never")


@needs_toml
def test_granular_approvals_without_sandbox_escalation_count_as_never(dirs):
    write(
        dirs.codex / "config.toml",
        "[approval_policy.granular]\nsandbox_approval = false\nrules = true\n",
    )
    assert codex(dirs).approval == "never"


@needs_toml
def test_a_permissions_profile_replaces_the_sandbox_and_brings_its_rules(dirs):
    write(
        dirs.codex / "config.toml",
        "\n".join(
            [
                'default_permissions = "locked"',
                "[permissions.base]",
                'extends = ":workspace"',
                "[permissions.base.filesystem]",
                '"~/src" = "write"',
                "[permissions.locked]",
                'extends = "base"',
                "[permissions.locked.filesystem]",
                '"~/.aws" = "deny"',
                '[permissions.locked.filesystem.":workspace_roots"]',
                '"**/.env" = "deny"',
                "[permissions.locked.network]",
                "enabled = true",
                "[permissions.locked.network.domains]",
                '"pypi.org" = "allow"',
            ]
        ),
    )
    found = codex(dirs)
    assert (found.sandbox, found.profile_base) == ("profile", ":workspace")
    assert sorted(found.profile_entries) == [
        ("**/.env", "deny", True),
        ("~/.aws", "deny", False),
        ("~/src", "write", False),
    ]
    assert found.network_access and found.network_domains == {"pypi.org": "allow"}
    assert found.deny_read == [] and found.workspace_roots == [dirs.project]


@needs_toml
def test_a_profile_and_sandbox_mode_together_are_a_documented_misconfiguration(dirs):
    write(
        dirs.codex / "config.toml",
        'sandbox_mode = "workspace-write"\ndefault_permissions = ":workspace"\n',
    )
    assert any("do not combine" in note for note in codex(dirs).assumed)


@needs_toml
def test_a_builtin_full_access_profile_is_no_sandbox(dirs):
    write(dirs.codex / "config.toml", 'default_permissions = ":danger-full-access"\n')
    assert codex(dirs).sandbox == "danger-full-access"


@needs_toml
def test_web_search_follows_the_flag_then_the_key_then_the_legacy_tool(dirs):
    assert codex(dirs, "--search").web_search == "live"
    write(dirs.codex / "config.toml", "[tools]\nweb_search = true\n")
    assert codex(dirs).web_search == "live"
    write(dirs.codex / "config.toml", 'web_search = "disabled"\n')
    assert codex(dirs).web_search == "disabled"


@needs_toml
def test_disabled_codex_servers_are_left_out_and_env_values_never_kept(dirs):
    write(
        dirs.codex / "config.toml",
        "\n".join(
            [
                "[mcp_servers.on]",
                'command = "on"',
                "[mcp_servers.on.env]",
                'GH_TOKEN = "ghp_secret"',
                "[mcp_servers.off]",
                'command = "off"',
                "enabled = false",
            ]
        ),
    )
    servers = codex(dirs).mcp
    assert [s.name for s in servers] == ["on"]
    assert servers[0].env_names == ("GH_TOKEN",) and "ghp_secret" not in repr(servers)


@needs_toml
def test_only_real_hook_events_count_as_hooks(dirs):
    write(
        dirs.codex / "config.toml", "[hooks.state]\nfoo = 1\n[[hooks.PreToolUse]]\ncommand = 'x'\n"
    )
    assert [(h.event, h.count) for h in codex(dirs).hooks] == [("PreToolUse", 1)]


@needs_toml
def test_the_shell_environment_policy_is_read(dirs):
    write(
        dirs.codex / "config.toml",
        "\n".join(
            [
                "[shell_environment_policy]",
                'inherit = "all"',
                "ignore_default_excludes = false",
                "[shell_environment_policy.filters]",
                '"AWS_*" = "exclude"',
            ]
        ),
    )
    found = codex(dirs)
    assert found.env_inherit == "all"
    assert not found.env_keep_secret_names
    assert found.env_filters == {"AWS_*": "exclude"}


@needs_toml
def test_a_legacy_profile_table_is_still_read(dirs):
    write(dirs.codex / "config.toml", '[profiles.old]\nsandbox_mode = "read-only"\n')
    assert codex(dirs, "-p", "old").sandbox == "read-only"
