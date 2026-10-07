"""Curb's launch parser: tables of launch commands, no files."""

from pathlib import Path

import pytest

from flanner import curb_context
from flanner.curb_context import LaunchError, parse

CWD = Path("/work/repo")


# --- launch commands -------------------------------------------------------------


def test_a_bare_launch_is_the_default_context():
    context = parse(["claude"], CWD)
    assert context.agent == "claude"
    assert context.unknown_flags == ()
    assert context.permission_mode is None


def test_claude_flags_that_change_reach_are_read():
    context = parse(
        [
            "claude",
            "-p",
            "fix the build",
            "--settings",
            "ci.json",
            "--permission-mode",
            "acceptEdits",
            "--mcp-config",
            "a.json",
            "b.json",
            "--strict-mcp-config",
            "--setting-sources",
            "user,project",
            "--permission-prompts",
            "none",
        ],
        CWD,
    )
    assert context.headless
    assert context.settings == ("ci.json",)
    assert context.permission_mode == "acceptEdits"
    assert context.mcp_configs == ("a.json", "b.json")
    assert context.strict_mcp
    assert context.setting_sources == ("user", "project")
    assert context.prompts_denied
    assert context.unknown_flags == ()


def test_skipping_permissions_is_bypass_mode_and_manual_is_default():
    assert parse(["claude", "--dangerously-skip-permissions"], CWD).permission_mode == (
        "bypassPermissions"
    )
    assert parse(["claude", "--permission-mode", "manual"], CWD).permission_mode == "default"


def test_disallowed_tools_split_on_commas_outside_parentheses():
    context = parse(["claude", "--disallowedTools", "Bash(git push *),Edit", "WebFetch"], CWD)
    assert context.disallowed == ("Bash(git push *)", "Edit", "WebFetch")


def test_tools_flag_limits_the_built_in_tools():
    assert parse(["claude", "--tools", "Read,Edit"], CWD).tools == ("Read", "Edit")
    assert parse(["claude", "--tools", "default"], CWD).tools == ("default",)


def test_an_unknown_flag_is_recorded_not_guessed():
    context = parse(["claude", "--not-a-real-flag", "--model", "opus"], CWD)
    assert context.unknown_flags == ("--not-a-real-flag",)


def test_a_flag_value_attached_with_equals_is_read():
    context = parse(["claude", "--settings=ci.json", "--permission-mode=plan"], CWD)
    assert context.settings == ("ci.json",)
    assert context.permission_mode == "plan"


def test_codex_flags_that_change_reach_are_read():
    context = parse(
        [
            "codex",
            "exec",
            "-s",
            "workspace-write",
            "-a",
            "never",
            "-p",
            "ci",
            "-c",
            "sandbox_workspace_write.network_access=true",
            "--enable",
            "network_proxy",
            "--search",
            "-C",
            "sub",
            "do the thing",
        ],
        CWD,
    )
    assert context.headless
    assert context.sandbox == "workspace-write"
    assert context.approval == "never"
    assert context.profile == "ci"
    assert ("sandbox_workspace_write.network_access", "true") in context.overrides
    assert ("features.network_proxy", "true") in context.overrides
    assert context.search
    assert context.cwd == (CWD / "sub").resolve()
    assert context.unknown_flags == ()


def test_codex_bypass_flag_is_read():
    assert parse(["codex", "--dangerously-bypass-approvals-and-sandbox"], CWD).bypass


def test_a_codex_command_that_is_not_a_session_is_refused():
    with pytest.raises(LaunchError):
        parse(["codex", "login"], CWD)


def test_only_claude_and_codex_are_read():
    with pytest.raises(LaunchError):
        parse(["vim", "notes.txt"], CWD)
    with pytest.raises(LaunchError):
        parse([], CWD)


@pytest.mark.parametrize(
    ("word", "agent"),
    [
        ("claude", "claude"),
        ("/usr/local/bin/claude", "claude"),
        ("C:\\Users\\a\\AppData\\Roaming\\npm\\codex.cmd", "codex"),
        ("codex.exe", "codex"),
        ("cursor", None),
    ],
)
def test_the_agent_is_read_from_a_program_name_or_path(word, agent):
    assert curb_context.agent_of(word) == agent


def test_a_description_carries_flags_but_never_values_or_the_prompt():
    context = parse(["claude", "-p", "deploy with key sk-123", "--settings", "secret.json"], CWD)
    text = context.describe()
    assert "--settings" in text
    assert "sk-123" not in text
    assert "secret.json" not in text


def test_a_one_string_command_is_split_like_a_shell_would():
    context = parse(['claude --settings "my settings.json"'], CWD)
    assert context.settings == ("my settings.json",)
