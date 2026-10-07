"""`flanner curb`: inventory, the redacted map, and the window that holds the detail.

No test runs an agent or a scheduler: `shutil.which` finds nothing, the
scheduler listings come from a fake runner, and the window is never drawn.
One test runs flanner itself, behind a pseudo-terminal, with an empty PATH.
"""

import json
import os
import plistlib
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from flanner import actions, curb_inventory, curb_window
from flanner.cli import cli


@pytest.fixture
def machine(tmp_path, monkeypatch):
    home = Path.home()
    found = SimpleNamespace(
        home=home,
        claude=tmp_path / "claude-config",
        codex=tmp_path / "codex-home",
        project=tmp_path / "project",
    )
    for folder in (found.claude, found.codex, found.project):
        folder.mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(found.claude))
    monkeypatch.setenv("CODEX_HOME", str(found.codex))
    monkeypatch.setattr(curb_inventory.shutil, "which", lambda name: None)
    monkeypatch.setattr(curb_inventory, "run", lambda argv: None)
    monkeypatch.chdir(found.project)
    # A credential with names that must never reach the terminal.
    aws = home / ".aws" / "credentials"
    aws.parent.mkdir(parents=True)
    aws.write_text("[quokka-prod]\naws_access_key_id = x\n", encoding="utf-8")
    (found.project / ".env").write_text("QUOKKA_API_TOKEN=abc\n", encoding="utf-8")
    (found.claude / ".claude.json").write_text(
        json.dumps(
            {"mcpServers": {"pencil": {"command": "pencil", "env": {"QUOKKA_SECRET": "v"}}}}
        ),
        encoding="utf-8",
    )
    return found


def invoke(*args: str):
    return CliRunner().invoke(cli, ["curb", *args], catch_exceptions=False)


# --- the map ------------------------------------------------------------------------


def test_map_json_reports_each_agent_and_names_nothing(machine):
    result = invoke("map", "--json")
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["function"] == "severity-r1 v1"
    assert [r["agent"] for r in data["reports"]] == ["claude", "codex"]
    assert all(not r["supported"] for r in data["reports"])  # no version found
    assert "quokka" not in result.output.lower()


def test_map_table_names_nothing_and_points_to_the_window(machine):
    result = invoke("map", "--agent", "claude")
    assert result.exit_code == 0, result.output
    assert "High" in result.output
    assert "flanner curb show" in result.output
    assert "quokka" not in result.output.lower()


def test_map_assesses_a_given_launch_command(machine):
    settings = machine.project / "ci.json"
    settings.write_text('{"permissions": {"deny": ["WebFetch", "WebSearch"]}}', encoding="utf-8")
    result = invoke("map", "--json", "--", "claude", "--settings", str(settings))
    data = json.loads(result.output)
    assert len(data["reports"]) == 1
    report = data["reports"][0]
    assert "--settings" in report["launch"] and str(settings) not in report["launch"]
    web = next(c for c in report["channels"] if c["channel"] == "Web fetch and web search")
    assert web["state"] == "controlled"


def test_map_refuses_a_command_that_is_not_an_agent(machine):
    result = CliRunner().invoke(cli, ["curb", "map", "--", "vim", "x"])
    assert result.exit_code == 2
    assert "Claude Code or Codex" in result.output


def test_an_agent_that_is_nowhere_on_the_machine_is_left_out(machine, tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "never-created"))
    data = json.loads(invoke("map", "--json").output)
    assert [r["agent"] for r in data["reports"]] == ["claude"]
    assert data["skipped"] == ["Codex was not found on this machine."]


# --- the window ---------------------------------------------------------------------


def test_show_without_a_desktop_says_why_and_fails(machine, monkeypatch):
    monkeypatch.setattr(curb_window, "unavailable", lambda: "there is no desktop session here")
    result = CliRunner().invoke(cli, ["curb", "show"])
    assert result.exit_code == 1
    assert "no desktop session" in result.output


def test_show_starts_a_window_and_prints_nothing_from_it(machine, monkeypatch):
    started = []
    monkeypatch.setattr(curb_window, "unavailable", lambda: None)
    monkeypatch.setattr(curb_window, "launch", lambda args: started.append(list(args)))
    result = invoke("show", "--agent", "claude")
    assert result.exit_code == 0, result.output
    assert started and started[0][:2] == ["--dir", str(machine.project)]
    assert "--agent" in started[0]
    assert "quokka" not in result.output.lower()


def test_the_window_holds_the_names_and_locations(machine, monkeypatch):
    drawn = []
    monkeypatch.setattr(curb_window, "show", lambda title, text: drawn.append(text))
    result = invoke("show", "--in-window", "--agent", "claude")
    assert result.exit_code == 0, result.output
    assert "quokka" not in result.output.lower()
    text = drawn[0]
    assert "quokka-prod" in text and "QUOKKA_API_TOKEN" in text
    assert str(machine.home / ".aws" / "credentials") in text


def test_the_window_runs_in_a_detached_process_of_its_own():
    calls = []
    curb_window.launch(["--dir", "x"], popen=lambda cmd, **kw: calls.append((cmd, kw)))
    command, options = calls[0]
    assert command[-4:] == ["show", "--in-window", "--dir", "x"]
    assert options["stdout"] is not None and options["stdin"] is not None


# --- the inventory ------------------------------------------------------------------


@pytest.mark.skipif(sys.version_info < (3, 11), reason="Python 3.10 has no tomllib")
def test_inventory_lists_what_was_planted_and_marks_an_untested_version(machine, monkeypatch):
    # PRD §10.1: each planted agent, MCP server, hook, skill and scheduled job
    # (the jobs have their own tests below, one per scheduler).
    versions = {"claude": "2.1.290 (Claude Code)\n", "codex": "codex-cli 0.154.0\n"}
    monkeypatch.setattr(curb_inventory.shutil, "which", lambda name: f"/opt/bin/{name}")
    monkeypatch.setattr(
        curb_inventory,
        "run",
        lambda argv: versions.get(Path(argv[0]).name) if argv[-1] == "--version" else None,
    )
    hook = {"matcher": "Bash", "hooks": [{"type": "command", "command": "check"}]}
    (machine.claude / "settings.json").write_text(
        json.dumps({"hooks": {"PreToolUse": [hook]}}), encoding="utf-8"
    )
    (machine.codex / "config.toml").write_text(
        '[mcp_servers.docs]\ncommand = "docs-server"\n', encoding="utf-8"
    )
    for folder in ("project/.claude/skills/review", "project/.agents/skills/tidy"):
        skill = machine.project.parent / folder
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("---\nname: x\n---\n", encoding="utf-8")

    claude, codex = json.loads(invoke("inventory", "--json").output)["agents"]
    assert (claude["version"], claude["supported"]) == ("2.1.290", False)
    assert (codex["version"], codex["supported"]) == ("0.154.0", True)
    assert [(h["event"], h["count"]) for h in claude["hooks"]] == [("PreToolUse", 1)]
    assert claude["skills"] == {"project": 1} and codex["skills"] == {"project": 1}
    assert [s["name"] for s in codex["mcp_servers"]] == ["docs"]


def test_inventory_lists_servers_and_counts_their_secrets(machine):
    result = invoke("inventory", "--json")
    assert result.exit_code == 0, result.output
    claude = json.loads(result.output)["agents"][0]
    assert claude["label"] == "Claude Code"
    assert claude["mcp_servers"] == [
        {
            "name": "pencil",
            "transport": "stdio",
            "configured_in": "user (~/.claude.json)",
            "controlled_by": "user",
            "env_vars": 1,
        }
    ]
    assert "QUOKKA_SECRET" not in result.output


def test_the_version_comes_from_the_agent_itself(monkeypatch):
    monkeypatch.setattr(curb_inventory.shutil, "which", lambda name: f"/usr/bin/{name}")
    binary, version = curb_inventory.version_of("claude", lambda argv: "2.1.287 (Claude Code)\n")
    assert (binary, version) == ("/usr/bin/claude", "2.1.287")


def test_a_cron_job_running_an_agent_is_found_with_its_launch_flags(tmp_path):
    crontab = "\n".join(
        [
            "MAILTO=me@example.com",
            "# nightly",
            "0 3 * * * cd /srv/repo && claude -p --dangerously-skip-permissions 'review'",
            "*/5 * * * * /usr/bin/backup",
        ]
    )
    jobs = curb_inventory.scheduled_jobs(tmp_path, platform="linux", runner=lambda argv: crontab)
    assert [j.name for j in jobs] == ["crontab line 3"]
    assert jobs[0].context.permission_mode == "bypassPermissions"
    assert jobs[0].context.source == "scheduled job: crontab line 3"


def test_a_systemd_timer_running_codex_is_found(tmp_path):
    folder = tmp_path / ".config" / "systemd" / "user"
    folder.mkdir(parents=True)
    (folder / "review.service").write_text(
        "[Service]\nWorkingDirectory=%h/repo\n"
        'ExecStart=/usr/bin/codex exec -s danger-full-access "fix it"\n',
        encoding="utf-8",
    )
    (folder / "review.timer").write_text("[Timer]\nOnCalendar=daily\n", encoding="utf-8")
    jobs = curb_inventory.scheduled_jobs(tmp_path, platform="linux", runner=lambda argv: None)
    assert [(j.name, j.context.sandbox) for j in jobs] == [("review", "danger-full-access")]
    assert jobs[0].context.cwd == tmp_path / "repo"


def test_a_launch_agent_running_claude_is_found(tmp_path):
    folder = tmp_path / "Library" / "LaunchAgents"
    folder.mkdir(parents=True)
    (folder / "com.me.review.plist").write_bytes(
        plistlib.dumps(
            {"Label": "com.me.review", "ProgramArguments": ["/bin/zsh", "-c", "claude -p hello"]}
        )
    )
    jobs = curb_inventory.scheduled_jobs(tmp_path, platform="darwin", runner=lambda argv: None)
    assert [j.name for j in jobs] == ["com.me.review"] and jobs[0].context.headless


def test_a_windows_task_running_codex_is_found(tmp_path):
    def task(name: str, command: str, arguments: str, folder: str = "") -> str:
        return (
            f"<!-- \\{name} -->\n"
            '<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">'
            f"<RegistrationInfo><URI>\\{name}</URI></RegistrationInfo>"
            f"<Actions><Exec><Command>{command}</Command><Arguments>{arguments}</Arguments>"
            f"<WorkingDirectory>{folder}</WorkingDirectory></Exec></Actions></Task>\n"
        )

    listing = (
        "\n<Tasks>\n"
        + task("Nightly Review", '"C:\\Program Files\\nodejs\\codex.cmd"', 'exec --search "tidy"')
        + task("Other", "notepad.exe", "", "C:\\repo")
        + "</Tasks>\n"
    )
    seen = []
    jobs = curb_inventory.scheduled_jobs(
        tmp_path, platform="win32", runner=lambda argv: seen.append(argv) or listing
    )
    assert seen == [["schtasks", "/query", "/xml", "ONE"]]
    assert [j.name for j in jobs] == ["Nightly Review"]
    assert jobs[0].context.search and jobs[0].context.cwd == tmp_path


def test_an_unreadable_windows_task_listing_finds_nothing(tmp_path):
    jobs = curb_inventory.scheduled_jobs(tmp_path, platform="win32", runner=lambda argv: "<Tasks")
    assert jobs == []


def test_the_window_text_lists_channels_and_credentials():
    text = curb_window.render(
        [
            {
                "label": "Claude Code",
                "severity": {
                    "level": "High",
                    "rule": "H1",
                    "evidence": "configured",
                    "reason": "A wide credential is readable",
                },
                "launch": "Claude Code launched as `claude, no flags`",
                "directory": "/repo",
                "version": "2.1.287",
                "supported": True,
                "baseline": "2.1.287",
                "channels": [
                    {
                        "channel": "MCP servers",
                        "state": "absent",
                        "evidence": "configured",
                        "disposition": "informational",
                        "why": "none",
                        "fix": None,
                    }
                ],
                "credentials": {
                    "items": [
                        {
                            "label": "AWS credentials file",
                            "category": "cloud",
                            "paths": ["/home/a/.aws/credentials"],
                            "names": ["prod"],
                            "identity": None,
                            "expires": None,
                            "readable_through": [],
                            "blocked_by": ["sandbox denyRead ~/.aws"],
                        }
                    ]
                },
                "assumption": "Assumes no other flags.",
            }
        ],
        [],
    )
    assert "Claude Code: High (H1, configured)" in text
    assert "/home/a/.aws/credentials" in text and "Blocked by: sandbox denyRead ~/.aws" in text


# --- R1 exit criteria (Curb PRD §15) ------------------------------------------------


def _web_state(output: str, index: int = 0) -> str:
    report = json.loads(output)["reports"][index]
    return next(
        c["state"] for c in report["channels"] if c["channel"] == "Web fetch and web search"
    )


def test_another_working_directory_changes_the_result(machine, tmp_path):
    other = tmp_path / "locked-down"
    (other / ".claude").mkdir(parents=True)
    (other / ".claude" / "settings.json").write_text(
        '{"permissions": {"deny": ["WebFetch", "WebSearch"]}}', encoding="utf-8"
    )
    here = invoke("map", "--json", "--agent", "claude").output
    there = invoke("map", "--json", "--agent", "claude", "--dir", str(other)).output
    assert (_web_state(here), _web_state(there)) == ("uncontrolled", "controlled")


@pytest.mark.parametrize(
    "env",
    [
        {"CLAUDECODE": "1"},
        {"CLAUDECODE": "", "CODEX_SANDBOX": "", "TERM": "xterm-256color", "FORCE_COLOR": "1"},
        {"CI": "true", "NO_COLOR": "1"},
    ],
)
def test_no_flag_environment_or_terminal_setting_prints_detail(machine, monkeypatch, env):
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(curb_window, "unavailable", lambda: "there is no desktop session here")
    every_flag = [
        ["map"],
        ["map", "--json"],
        ["map", "--agent", "claude", "--dir", str(machine.project)],
        ["map", "--agent", "codex", "--profile", "ci", "--json"],
        ["map", "--json", "--", "claude", "--settings", "ci.json", "-p", "hi"],
        ["inventory"],
        ["inventory", "--json", "--agent", "claude"],
        ["show"],
        ["show", "--agent", "claude"],
    ]
    for args in every_flag:
        result = CliRunner().invoke(cli, ["curb", *args])
        assert result.exit_code in (0, 1), (args, result.output)
        assert "quokka" not in result.output.lower(), args


@pytest.mark.skipif(sys.platform == "win32", reason="pseudo-terminals are POSIX")
def test_a_pseudo_terminal_with_no_agent_markers_gets_no_detail(machine):
    env = {k: v for k, v in os.environ.items() if k not in actions.AGENT_SHELL_MARKERS}
    env.update(PATH="", TERM="xterm-256color")  # PATH: no agent binary, no crontab
    leader, follower = os.openpty()
    child = subprocess.Popen(
        [sys.executable, "-m", "flanner", "curb", "map"],
        stdin=follower,
        stdout=follower,
        stderr=follower,
        env=env,
        cwd=machine.project,
    )
    os.close(follower)
    output = b""
    while True:
        try:
            chunk = os.read(leader, 4096)
        except OSError:  # Linux reports the closed terminal as EIO
            break
        if not chunk:
            break
        output += chunk
    child.wait(timeout=60)
    os.close(leader)
    text = output.decode(errors="replace")
    assert "High" in text, text
    assert "quokka" not in text.lower()


def test_a_large_repository_is_mapped_well_inside_the_time_budget(machine):
    # PRD §16: inventory plus reach now, p95 under 10 seconds. The .env walk
    # is the only part that grows with the repository.
    for package in range(40):
        folder = machine.project / "src" / f"pkg{package}"
        folder.mkdir(parents=True)
        for module in range(10):
            (folder / f"m{module}.py").write_text("x = 1\n", encoding="utf-8")
        (machine.project / "node_modules" / f"dep{package}" / "lib").mkdir(parents=True)
    started = time.perf_counter()
    assert invoke("map", "--json").exit_code == 0
    assert time.perf_counter() - started < 10


def test_curb_writes_nothing_to_disk(machine, tmp_path, monkeypatch):
    monkeypatch.setattr(curb_window, "show", lambda title, text: None)

    def files() -> set[Path]:
        return {p for p in tmp_path.rglob("*") if p.is_file()}

    before = files()
    for args in (["map"], ["map", "--json"], ["inventory"], ["show", "--in-window"]):
        assert invoke(*args).exit_code == 0
    assert files() == before


def test_a_read_command_never_opens_the_action_store(machine, monkeypatch):
    def refuse():
        raise AssertionError("a read command opened the action store")

    monkeypatch.setattr(actions, "watching", refuse)
    assert invoke("map", "--json").exit_code == 0
