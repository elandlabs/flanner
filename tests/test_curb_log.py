"""The action log and observed use (Curb PRD §10.6, §10.7, R4 exit criteria)."""

import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from flanner import curb_approval, curb_fix, curb_log, curb_observe, curb_reach
from flanner.cli import cli

KEY = b"k" * 32


def claude(event, tool, tool_input, session="s1", response=None):
    payload = {
        "hook_event_name": event,
        "session_id": session,
        "tool_name": tool,
        "tool_input": tool_input,
    }
    if response is not None:
        payload["tool_response"] = response
    return json.dumps(payload)


@pytest.mark.parametrize(
    ("tool", "given", "channel", "program"),
    [
        ("Read", {"file_path": "/home/a/.aws/credentials"}, "file_tools", None),
        ("Bash", {"command": "curl -s https://example.com"}, "shell_network", "curl"),
        ("Bash", {"command": "cat notes.txt"}, "shell_files", "cat"),
        ("Bash", {"command": "git push origin main"}, "shell_network", "git"),
        ("Bash", {"command": "git status"}, "shell_files", "git"),
        ("WebFetch", {"url": "https://example.com"}, "web", None),
        ("mcp__github__search", {"query": "x"}, "mcp", None),
    ],
)
def test_claude_hook_payloads_become_one_record_shape(tool, given, channel, program):
    record = curb_log.from_hook("claude", claude("PreToolUse", tool, given), KEY)
    assert (record["channel"], record["program"], record["decision"]) == (
        channel,
        program,
        "requested",
    )
    assert record["target_digest"] and not any(
        str(value) in json.dumps(record) for value in given.values()
    )


def test_codex_payloads_share_the_format_and_unwrap_bash_c():
    raw = json.dumps(
        {
            "hook_event_name": "PreToolUse",
            "session_id": "c1",
            "tool_name": "shell",
            "tool_input": {"command": ["bash", "-lc", "wget http://x.example/a"]},
        }
    )
    record = curb_log.from_hook("codex", raw, KEY)
    assert (record["agent"], record["channel"], record["program"]) == (
        "codex",
        "shell_network",
        "wget",
    )
    assert set(record) == set(
        curb_log.from_hook("claude", claude("PreToolUse", "Read", {"file_path": "a"}), KEY)
    )


def test_denials_and_failures_are_their_own_decisions():
    denied = curb_log.from_hook(
        "claude", claude("PermissionDenied", "Read", {"file_path": "x"}), KEY
    )
    failed = curb_log.from_hook(
        "claude",
        claude("PostToolUse", "Bash", {"command": "ls"}, response={"is_error": True}),
        KEY,
    )
    assert (denied["decision"], failed["decision"]) == ("denied", "failed")


def test_the_hook_fails_open_on_anything():
    curb_log.record_hook("claude", "{not json")
    curb_log.record_hook("claude", "")
    assert curb_log.records() == []


def test_the_hook_command_appends_a_record():
    raw = claude("PreToolUse", "Read", {"file_path": "/x/y"})
    result = CliRunner().invoke(cli, ["hook", "curb-record", "--agent", "claude"], input=raw)
    assert result.exit_code == 0
    assert [r["tool"] for r in curb_log.records()] == ["Read"]


# --- tamper evidence ----------------------------------------------------------------------


def three():
    for n in range(3):
        curb_log.append({"kind": "tool", "agent": "claude", "tool": f"T{n}"})
    return curb_log.log_path().read_text(encoding="utf-8").splitlines()


def test_an_untouched_log_verifies():
    three()
    assert curb_log.verify() == (True, "3 record(s) intact and signed")


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        (lambda lines: [lines[0], lines[1].replace('"T1"', '"Tx"'), lines[2]], "was changed"),
        (lambda lines: [lines[0], lines[2]], "does not follow"),
        (lambda lines: [lines[1], lines[0], lines[2]], "removed"),
        (lambda lines: lines[1:], "removed"),
    ],
)
def test_an_edited_removed_or_reordered_record_fails(change, reason):
    lines = three()
    curb_log.log_path().write_text("\n".join(change(lines)) + "\n", encoding="utf-8")
    ok, said = curb_log.verify()
    assert not ok and reason in said


def test_a_record_signed_by_another_key_fails():
    lines = three()
    forged = json.loads(lines[2])
    forged["sig"] = forged["sig"][::-1]
    curb_log.log_path().write_text(
        "\n".join([*lines[:2], json.dumps(forged)]) + "\n", encoding="utf-8"
    )
    assert curb_log.verify() == (False, "record 3 is not signed by this device")


def test_pruning_keeps_a_verifiable_chain():
    old = time.time() - 40 * 86400
    curb_log.append({"kind": "tool", "tool": "old"}, now=old)
    curb_log.append({"kind": "tool", "tool": "new"})
    assert curb_log.prune() == 1
    assert [r.get("tool") for r in curb_log.records()] == [None, "new"]
    assert curb_log.verify()[0]


def test_approval_outcomes_are_logged(monkeypatch):
    monkeypatch.setattr(curb_approval, "process_chain", lambda: [])

    class Answer:
        name, weak = "test", False

        def __init__(self, yes):
            self.yes = yes

        def available(self):
            return True

        def confirm(self, reason):
            return self.yes

    curb_approval.Broker(Answer(True)).request("Add 2 deny rules", "h")
    curb_approval.Broker(Answer(False)).request("Add 2 deny rules", "h")
    outcomes = [r["decision"] for r in curb_log.records() if r["kind"] == "approval"]
    assert outcomes == ["granted", "refused"]


# --- observed use -----------------------------------------------------------------------------


@pytest.fixture
def agents(tmp_path, monkeypatch):
    found = SimpleNamespace(claude=tmp_path / "claude", codex=tmp_path / "codex")
    found.claude.mkdir()
    found.codex.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(found.claude))
    monkeypatch.setenv("CODEX_HOME", str(found.codex))
    return found


def hooks_on(agent):
    plan = curb_observe.hook_plan([agent], enable=True)
    for edit in plan.edits:
        edit.path.parent.mkdir(parents=True, exist_ok=True)
        edit.path.write_text(edit.text, encoding="utf-8")


def history(
    agent, agents, *, days=15, sessions=25, channels=("file_tools", "shell_files"), unlogged=0
):
    start = time.time() - days * 86400
    tools = {
        "file_tools": ("Read", {"file_path": "a"}),
        "shell_files": ("Bash", {"command": "ls"}),
        "web": ("WebFetch", {"url": "https://e.x"}),
    }
    for n in range(sessions):
        session = f"session-{n}"
        for channel in channels:
            tool, given = tools[channel]
            entry = curb_log.from_hook(agent, claude("PreToolUse", tool, given, session), KEY)
            curb_log.append(entry, now=start + n * 3600)
        (agents.claude / "projects" / "p").mkdir(parents=True, exist_ok=True)
        (agents.claude / "projects" / "p" / f"{session}.jsonl").write_text("{}", encoding="utf-8")
    for n in range(unlogged):
        (agents.claude / "projects" / "p" / f"quiet-{n}.jsonl").write_text("{}", encoding="utf-8")


def test_with_the_log_off_there_is_no_evidence(agents):
    observation = curb_observe.observe("claude")
    assert observation.state == curb_observe.NO_EVIDENCE
    assert "the action log is off" in observation.gaps


def test_a_short_window_is_no_evidence(agents):
    hooks_on("claude")
    history("claude", agents, days=3, sessions=5)
    observation = curb_observe.observe("claude")
    assert (
        observation.state == curb_observe.NO_EVIDENCE
        and "the window is 14 days" in observation.gaps[0]
    )


def test_sessions_without_the_hooks_make_evidence_partial(agents):
    hooks_on("claude")
    history("claude", agents, unlogged=2)
    observation = curb_observe.observe("claude")
    assert observation.state == curb_observe.PARTIAL
    assert "2 session(s) ran without the hooks" in observation.gaps


def test_a_full_window_is_observed_use_and_says_what_was_seen(agents):
    hooks_on("claude")
    history("claude", agents)
    observation = curb_observe.observe("claude")
    assert observation.state == curb_observe.OBSERVED
    assert observation.seen["file_tools"] == 25 and observation.seen["web"] == 0
    assert "child processes of shell commands" in observation.gaps


def test_ideas_cover_only_open_channels_the_hooks_can_see(agents):
    hooks_on("claude")
    history("claude", agents)

    class Report:
        channels = [
            curb_reach.Channel(k, curb_reach.UNCONTROLLED, "configured", "x", "y")
            for k in ("web", "mcp", "file_tools")
        ]

    ideas = curb_observe.observe("claude", Report()).ideas
    assert len(ideas) == 2 and ideas[0].startswith("Web fetch and web search: not seen in use")
    assert "not seen is not the same as not needed" in ideas[0]
    assert not any(i.startswith("Built-in file tools") for i in ideas)  # it was seen


def test_no_idea_for_a_channel_the_hooks_cannot_see(agents):
    observation = curb_observe.Observation(
        "codex",
        curb_observe.OBSERVED,
        20,
        30,
        seen={
            "web": 0,
            "mcp": 0,
            "apps": 0,
            "file_tools": 2,
            "shell_files": 4,
            "shell_network": 1,
        },
        covered=dict(curb_observe.COVERAGE["codex"]),
        gaps=[],
    )
    assert curb_observe._ideas(observation, None) == []


def test_the_log_command_turns_hooks_on_and_off_behind_a_yes(agents, monkeypatch):
    class Yes:
        name, weak = "test", False

        def available(self):
            return True

        def confirm(self, reason):
            return True

    monkeypatch.setattr(curb_approval, "method", lambda: Yes())
    monkeypatch.setattr(curb_approval, "process_chain", lambda: [])
    on = CliRunner().invoke(cli, ["curb", "log", "--enable"])
    assert on.exit_code == 0, on.output
    assert curb_observe.hooks_on("claude") and curb_observe.hooks_on("codex")
    assert curb_fix.count() == 1  # backed up, so it can be undone
    off = CliRunner().invoke(cli, ["curb", "log", "--disable", "--agent", "codex"])
    assert off.exit_code == 0 and not curb_observe.hooks_on("codex")
    assert curb_observe.hooks_on("claude")
    verified = CliRunner().invoke(cli, ["curb", "log", "--verify"])
    assert verified.exit_code == 0 and "intact and signed" in verified.output


def test_the_hook_command_names_this_interpreter():
    command = curb_observe.hook_command("claude")
    assert command.endswith(" hook curb-record --agent claude")
    assert Path(command.split('"')[1]).name.lower().startswith("python")
