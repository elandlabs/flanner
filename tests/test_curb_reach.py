"""Curb's channel model and severity, including the PRD's critical invariants (§16)."""

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from flanner import curb_credentials, curb_reach, curb_settings, curb_severity
from flanner.curb_context import default, parse
from flanner.curb_credentials import Credential

needs_toml = pytest.mark.skipif(sys.version_info < (3, 11), reason="Python 3.10 has no tomllib")

RANK = {"Low": 0, "Medium": 1, "High": 2}

#: Every channel closed: strict sandbox, strict allowlist, web denied.
STRICT = {
    "sandbox": {
        "enabled": True,
        "allowUnsandboxedCommands": False,
        "filesystem": {"denyRead": ["~/.aws"]},
        "network": {"strictAllowlist": True, "allowedDomains": ["pypi.org"]},
    },
    "permissions": {"deny": ["WebFetch", "WebSearch", "Read(~/.aws/**)"]},
}


@pytest.fixture
def dirs(tmp_path, monkeypatch):
    found = SimpleNamespace(
        claude=tmp_path / "claude-config",
        codex=tmp_path / "codex-home",
        project=tmp_path / "project",
        home=Path.home(),
    )
    found.project.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(found.claude))
    monkeypatch.setenv("CODEX_HOME", str(found.codex))
    return found


def write(path: Path, data) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")
    return path


def aws(dirs, *, wide: bool = True) -> Credential:
    path = write(dirs.home / ".aws" / "credentials", "[default]\naws_access_key_id = x\n")
    return Credential(
        "aws", "cloud", "AWS credentials file", (path,), wide=wide, scope_known=not wide
    )


def claude_report(
    dirs,
    settings=None,
    *argv,
    creds=None,
    platform="linux",
    version="2.1.287",
    env=None,
    source="command",
):
    if settings is not None:
        write(dirs.claude / "settings.json", settings)
    context = (
        parse(["claude", *argv], dirs.project, source=source)
        if argv
        else default("claude", dirs.project)
    )
    resolved = curb_settings.resolve_claude(context, platform=platform)
    return curb_reach.assess(
        context,
        resolved,
        creds if creds is not None else [aws(dirs)],
        platform=platform,
        home=dirs.home,
        env=env or {},
        version=version,
    )


def channel(report, key):
    return next(c for c in report.channels if c.key == key)


# --- critical invariants ----------------------------------------------------------


def test_invariant_1_a_wide_readable_credential_with_open_egress_is_high(dirs):
    report = claude_report(dirs)
    assert (report.verdict.severity, report.verdict.rule) == ("High", "H1")


def test_invariant_2_a_read_deny_rule_alone_leaves_the_shell_open(dirs):
    report = claude_report(dirs, {"permissions": {"deny": ["Read(~/.aws/**)"]}})
    reach = report.reach[0]
    assert curb_reach.FILE_TOOLS not in reach.via
    assert curb_reach.SHELL_FILES in reach.via
    assert report.verdict.severity == "High"


def test_invariant_3_an_unreadable_setting_is_never_a_control(dirs):
    # A truncated strict config: every control in it, unread.
    write(dirs.claude / "settings.json", json.dumps(STRICT)[:-40])
    report = claude_report(dirs)
    assert {c.state for c in report.channels} == {curb_reach.UNKNOWN, curb_reach.INFO}
    assert report.verdict.evidence == curb_severity.ASSUMED


def test_invariant_5_an_untested_version_is_never_analysed_as_supported(dirs):
    report = claude_report(dirs, version="2.1.290")
    assert not report.supported
    assert all(c.evidence == curb_severity.ASSUMED for c in report.channels)
    assert all(
        c.disposition == curb_reach.UNSUPPORTED
        for c in report.channels
        if c.key != curb_reach.MODEL
    )
    assert report.verdict.evidence == curb_severity.ASSUMED


@pytest.mark.parametrize("settings", [{}, STRICT, {"permissions": {"deny": ["WebFetch"]}}])
def test_invariant_6_an_mcp_server_never_lowers_severity(dirs, settings):
    without = claude_report(dirs, settings).verdict.severity
    write(dirs.claude / ".claude.json", {"mcpServers": {"x": {"command": "x"}}})
    with_server = claude_report(dirs, settings).verdict.severity
    assert RANK[with_server] >= RANK[without]


def test_invariant_4_redacted_views_carry_no_names_or_locations(dirs):
    zebra = write(
        dirs.home / ".aws" / "credentials", "[zebra-prod-profile]\naws_access_key_id = x\n"
    )
    write(dirs.project / "zebra-service" / ".env", "ZEBRA_API_TOKEN=abc\n")
    creds = curb_credentials.find(dirs.home, dirs.project, {"ZEBRA_DB_PASSWORD": "pw"}, "linux")
    report = claude_report(dirs, creds=creds)
    hidden = json.dumps(curb_reach.redacted(report))
    assert "zebra" not in hidden.lower()
    items = curb_reach.full(report)["credentials"]["items"]
    assert any(str(zebra) in item["paths"] for item in items)
    names = {name for item in items for name in item["names"]}
    assert {"ZEBRA_API_TOKEN", "zebra-prod-profile", "ZEBRA_DB_PASSWORD"} <= names


# --- the severity table -------------------------------------------------------------


def test_m1_wide_readable_but_every_egress_channel_controlled(dirs):
    settings = json.loads(json.dumps(STRICT))
    settings["sandbox"]["filesystem"]["denyRead"] = []
    settings["permissions"]["deny"] = ["WebFetch", "WebSearch"]
    report = claude_report(dirs, settings)
    assert channel(report, curb_reach.SHELL_NETWORK).state == curb_reach.CONTROLLED
    assert (report.verdict.severity, report.verdict.rule) == ("Medium", "M1")


def test_m2_narrow_readable_with_open_egress_and_no_outside_content(dirs):
    report = claude_report(
        dirs, {"permissions": {"deny": ["WebFetch", "WebSearch"]}}, creds=[aws(dirs, wide=False)]
    )
    assert (report.verdict.severity, report.verdict.rule) == ("Medium", "M2")


def test_h2_narrow_readable_with_outside_content_and_open_egress(dirs):
    report = claude_report(dirs, {}, creds=[aws(dirs, wide=False)])
    assert report.verdict.rule == "H2"


def test_l1_when_every_channel_is_closed(dirs):
    report = claude_report(dirs, STRICT)
    assert report.readable == []
    assert (report.verdict.severity, report.verdict.rule) == ("Low", "L1")
    assert channel(report, curb_reach.SHELL_FILES).state == curb_reach.CONTROLLED


# --- Claude Code channels ---------------------------------------------------------


def test_the_sandbox_does_not_run_on_native_windows(dirs):
    report = claude_report(dirs, STRICT, platform="win32")
    assert report.verdict.severity == "High"
    assert "native Windows" in channel(report, curb_reach.SHELL_FILES).why


def test_a_sandbox_with_the_retry_escape_hatch_is_not_a_control(dirs):
    settings = {
        "sandbox": {
            "enabled": True,
            "filesystem": {"denyRead": ["~/.aws"]},
            "network": {"strictAllowlist": True},
        }
    }
    report = claude_report(dirs, settings)
    assert curb_reach.SHELL_FILES in report.reach[0].via
    net = channel(report, curb_reach.SHELL_NETWORK)
    assert net.state == curb_reach.UNCONTROLLED and "allowUnsandboxedCommands" in net.why


def test_bypass_mode_opens_the_network_unless_the_allowlist_is_strict(dirs):
    settings = json.loads(json.dumps(STRICT))
    settings["sandbox"]["network"]["strictAllowlist"] = False
    report = claude_report(dirs, settings, "--dangerously-skip-permissions")
    assert channel(report, curb_reach.SHELL_NETWORK).state == curb_reach.UNCONTROLLED
    strict = claude_report(dirs, STRICT, "--dangerously-skip-permissions")
    assert channel(strict, curb_reach.SHELL_NETWORK).state == curb_reach.CONTROLLED


def test_excluded_commands_leave_the_shell_open(dirs):
    settings = json.loads(json.dumps(STRICT))
    settings["sandbox"]["excludedCommands"] = ["docker *"]
    report = claude_report(dirs, settings)
    assert channel(report, curb_reach.SHELL_FILES).state == curb_reach.UNCONTROLLED


def test_denying_every_shell_tool_removes_the_shell_channels(dirs):
    report = claude_report(dirs, {"permissions": {"deny": ["Bash", "Monitor"]}})
    assert channel(report, curb_reach.SHELL_FILES).state == curb_reach.ABSENT
    assert channel(report, curb_reach.SHELL_NETWORK).state == curb_reach.ABSENT


def test_secrets_in_the_environment_reach_the_shell_and_proc_environ(dirs):
    env_cred = Credential(
        "env", "environment", "Secrets in the environment", via_shell=True, names=("API_TOKEN",)
    )
    report = claude_report(dirs, {}, creds=[env_cred])
    assert set(report.reach[0].via) == {curb_reach.SHELL_FILES, curb_reach.FILE_TOOLS}
    closed = json.loads(json.dumps(STRICT))
    closed["sandbox"]["credentials"] = {"envVars": [{"name": "API_TOKEN", "mode": "deny"}]}
    closed["permissions"]["deny"].append("Read(//proc/**)")
    assert claude_report(dirs, closed, creds=[env_cred]).reach[0].via == ()


def test_a_scheduled_launch_takes_in_outside_content(dirs):
    report = claude_report(dirs, STRICT, "-p", "x", creds=[], source="scheduled job: nightly")
    assert "runs unattended on a schedule" in report.external_content


def test_an_unknown_flag_makes_every_control_unknown(dirs):
    report = claude_report(dirs, STRICT, "--some-new-flag")
    assert channel(report, curb_reach.SHELL_NETWORK).state == curb_reach.UNKNOWN
    assert report.readable and report.verdict.evidence == curb_severity.ASSUMED


# --- Codex channels ---------------------------------------------------------------


def codex_report(dirs, config=None, *argv, creds=None, version="0.154.0"):
    if config is not None:
        write(dirs.codex / "config.toml", config)
    context = parse(["codex", *argv], dirs.project) if argv else default("codex", dirs.project)
    resolved = curb_settings.resolve_codex(context, platform="linux")
    return curb_reach.assess(
        context,
        resolved,
        creds if creds is not None else [aws(dirs)],
        platform="linux",
        home=dirs.home,
        env={},
        version=version,
    )


@needs_toml
def test_codex_with_approvals_off_and_deny_read_closes_the_shell(dirs):
    report = codex_report(
        dirs,
        "\n".join(
            [
                'sandbox_mode = "workspace-write"',
                'approval_policy = "never"',
                'web_search = "disabled"',
                'default_permissions = "locked"',
                "[permissions.locked.filesystem]",
                '"~/.aws" = "deny"',
            ]
        ),
    )
    assert channel(report, curb_reach.SHELL_NETWORK).state == curb_reach.CONTROLLED
    assert report.readable == []
    assert report.verdict.severity == "Low"


@needs_toml
def test_codex_on_request_approvals_are_an_escape_hatch(dirs):
    report = codex_report(
        dirs, 'sandbox_mode = "workspace-write"\napproval_policy = "on-request"\n'
    )
    assert channel(report, curb_reach.SHELL_NETWORK).state == curb_reach.UNCONTROLLED
    assert report.verdict.severity == "High"


@needs_toml
def test_codex_full_access_leaves_everything_open(dirs):
    report = codex_report(dirs, 'sandbox_mode = "danger-full-access"\napproval_policy = "never"\n')
    assert "no sandbox" in channel(report, curb_reach.SHELL_NETWORK).why


@needs_toml
def test_codex_hands_secret_named_variables_to_commands_by_default(dirs):
    env_cred = Credential("env", "environment", "Secrets", via_shell=True, names=("API_TOKEN",))
    assert codex_report(dirs, "", creds=[env_cred]).reach[0].via
    hidden = codex_report(
        dirs, "[shell_environment_policy]\nignore_default_excludes = false\n", creds=[env_cred]
    )
    assert hidden.reach[0].via == ()


def test_codex_defaults_only_hold_the_verdict_when_they_change_it(dirs):
    write(dirs.codex / "placeholder", "")
    # No config at all: the sandbox and approvals are assumed defaults, and
    # they decide whether egress is open, so the verdict rests on them.
    plain = codex_report(dirs)
    assert plain.verdict.evidence == curb_severity.ASSUMED
    assert any("default" in a for a in plain.verdict.assumptions)


@needs_toml
def test_codex_defaults_do_not_matter_once_an_mcp_server_opens_egress(dirs):
    report = codex_report(dirs, '[mcp_servers.x]\ncommand = "x"\n')
    assert report.verdict.rule == "H1"
    assert report.verdict.evidence == curb_severity.CONFIGURED


# --- launch contexts change results (Curb PRD §15, R1) ------------------------------


def test_a_settings_file_given_at_launch_changes_the_result(dirs):
    path = write(dirs.project / "ci.json", STRICT)
    assert claude_report(dirs).verdict.severity == "High"
    assert claude_report(dirs, None, "--settings", str(path)).verdict.severity == "Low"


@needs_toml
def test_a_codex_profile_changes_the_result(dirs):
    write(
        dirs.codex / "config.toml",
        "\n".join(
            [
                'sandbox_mode = "workspace-write"',
                'approval_policy = "never"',
                'web_search = "disabled"',
            ]
        ),
    )
    write(dirs.codex / "yolo.config.toml", 'sandbox_mode = "danger-full-access"\n')
    calm = codex_report(dirs, None, creds=[])
    wild = codex_report(dirs, None, "-p", "yolo", creds=[])
    assert channel(calm, curb_reach.SHELL_NETWORK).state == curb_reach.CONTROLLED
    assert channel(wild, curb_reach.SHELL_NETWORK).state == curb_reach.UNCONTROLLED


@needs_toml
def test_an_untrusted_codex_project_does_not_get_its_project_config(dirs):
    write(dirs.project / ".codex" / "config.toml", 'web_search = "live"\n')
    write(dirs.codex / "config.toml", 'web_search = "disabled"\n')
    untrusted = codex_report(dirs, None, creds=[])
    key = str(dirs.project).replace("\\", "\\\\")
    write(
        dirs.codex / "config.toml",
        f'web_search = "disabled"\n[projects."{key}"]\ntrust_level = "trusted"\n',
    )
    trusted = codex_report(dirs, None, creds=[])
    assert channel(untrusted, curb_reach.WEB).state == curb_reach.CONTROLLED
    assert channel(trusted, curb_reach.WEB).state == curb_reach.UNCONTROLLED
