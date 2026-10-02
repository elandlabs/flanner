"""Fixes: planned, checked tighten-only, written with a grant, backed up and undone.

Covers the R3 fix criteria (Curb PRD §15): each fix kind applies, backs up,
restores byte for byte, leaves the file parseable and closes its channel;
a write without a grant fails; every agent is denied the backup folder.
The operating system's prompt is a stand-in throughout.
"""

import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from flanner import curb_approval, curb_fix, curb_reach, curb_settings
from flanner.cli import cli
from flanner.curb_approval import Broker, NoGrant
from flanner.curb_context import BASELINE, default
from flanner.curb_credentials import Credential

needs_toml = pytest.mark.skipif(sys.version_info < (3, 11), reason="Python 3.10 has no tomllib")


class Yes:
    name, weak = "test prompt", False

    def available(self):
        return True

    def confirm(self, reason):
        return True


@pytest.fixture
def box(tmp_path, monkeypatch):
    found = SimpleNamespace(
        home=Path.home(),
        claude=tmp_path / "claude-config",
        codex=tmp_path / "codex-home",
        project=tmp_path / "project",
    )
    for folder in (found.home, found.claude, found.codex, found.project):
        folder.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(found.claude))
    monkeypatch.setenv("CODEX_HOME", str(found.codex))
    monkeypatch.setattr(curb_approval, "process_chain", lambda: [])
    aws = found.home / ".aws" / "credentials"
    aws.parent.mkdir(parents=True, exist_ok=True)
    aws.write_text("[default]\n", encoding="utf-8")
    found.creds = [
        Credential("aws", "cloud", "AWS credentials file", (aws,)),
        Credential("env", "environment", "Secrets", via_shell=True, names=("OPENAI_API_KEY",)),
    ]
    return found


def report(box, agent, platform="linux"):
    context = default(agent, box.project)
    settings = curb_settings.resolve(context, platform=platform)
    return curb_reach.assess(
        context,
        settings,
        box.creds,
        platform=platform,
        home=box.home,
        env={},
        version=BASELINE[agent],
    )


def plan(box, *agents, platform="linux"):
    reports = [report(box, agent, platform) for agent in agents]
    return curb_fix.plan(reports, home=box.home, platform=platform, env={})


def granted(planned):
    broker = Broker(Yes())
    grant = broker.request(planned.summary(), curb_approval.change_hash(planned.change()))
    return broker, grant


def state(rep, key):
    return next(c.state for c in rep.channels if c.key == key)


# --- Claude Code ------------------------------------------------------------------------


def test_a_claude_fix_closes_the_file_channels_and_backs_up_first(box):
    settings = box.claude / "settings.json"
    settings.write_text(json.dumps({"model": "opus"}, indent=4) + "\n", encoding="utf-8")
    original = settings.read_bytes()
    planned = plan(box, "claude")
    (edit,) = planned.edits
    assert "turn on the sandbox" in "; ".join(edit.actions)
    assert not any("aws" in action.lower() for action in edit.actions)  # counts, not places
    folder = curb_fix.apply(planned, *granted(planned))
    data = json.loads(settings.read_text(encoding="utf-8"))
    assert data["model"] == "opus" and data["sandbox"]["allowUnsandboxedCommands"] is False
    after = report(box, "claude")
    assert state(after, curb_reach.FILE_TOOLS) == curb_reach.CONTROLLED
    assert state(after, curb_reach.SHELL_FILES) == curb_reach.CONTROLLED
    assert state(after, curb_reach.SHELL_NETWORK) == curb_reach.CONTROLLED
    assert (folder / "0-settings.json").read_bytes() == original
    if sys.platform != "win32":
        assert (folder / "0-settings.json").stat().st_mode & 0o077 == 0


def test_undo_puts_the_file_back_byte_for_byte(box):
    settings = box.claude / "settings.json"
    settings.write_text('{\n    "model": "opus"\n}\n', encoding="utf-8")
    original = settings.read_bytes()
    planned = plan(box, "claude")
    folder = curb_fix.apply(planned, *granted(planned))
    broker = Broker(Yes())
    grant = broker.request("undo", curb_approval.change_hash(curb_fix.undo_change(folder)))
    assert curb_fix.undo(broker, grant, folder) == (["settings.json"], [])
    assert settings.read_bytes() == original


def test_undo_leaves_a_file_edited_since(box):
    planned = plan(box, "claude")
    folder = curb_fix.apply(planned, *granted(planned))
    settings = box.claude / "settings.json"
    settings.write_text('{"edited": true}\n', encoding="utf-8")
    assert curb_fix.restore(folder) == ([], ["settings.json"])
    assert json.loads(settings.read_text(encoding="utf-8")) == {"edited": True}


def test_a_fix_where_no_file_existed_is_undone_by_removing_it(box):
    planned = plan(box, "claude")
    folder = curb_fix.apply(planned, *granted(planned))
    assert (box.claude / "settings.json").exists()
    curb_fix.restore(folder)
    assert not (box.claude / "settings.json").exists()


def test_a_write_without_a_grant_fails_and_changes_nothing(box):
    planned = plan(box, "claude")
    with pytest.raises(NoGrant):
        curb_fix.apply(planned, Broker(Yes()), None)
    assert not (box.claude / "settings.json").exists()
    broker = Broker(Yes())
    other = broker.request("something else", "not-this-change")
    with pytest.raises(NoGrant):
        curb_fix.apply(planned, broker, other)


def test_a_file_that_reads_back_wrong_puts_everything_back(box, monkeypatch):
    settings = box.claude / "settings.json"
    settings.write_text('{"model": "opus"}\n', encoding="utf-8")
    original = settings.read_bytes()
    planned = plan(box, "claude")
    monkeypatch.setattr(curb_fix.curb_tighten, "load", lambda path: {"not": "it"})
    with pytest.raises(curb_fix.FixFailed):
        curb_fix.apply(planned, *granted(planned))
    assert settings.read_bytes() == original


def test_on_native_windows_the_sandbox_fix_is_guided(box):
    planned = plan(box, "claude", platform="win32")
    (edit,) = planned.edits
    assert not any("sandbox" in action for action in edit.actions)
    assert any("WSL2" in step for step in planned.guided)


def test_every_fix_denies_claude_its_backup_folder(box):
    planned = plan(box, "claude")
    folder = curb_fix.apply(planned, *granted(planned))
    probe = Credential("probe", "backup", "Backup", (folder / "0-settings.json",))
    context = default("claude", box.project)
    after = curb_reach.assess(
        context,
        curb_settings.resolve(context, platform="linux"),
        [probe],
        platform="linux",
        home=box.home,
        env={},
        version=BASELINE["claude"],
    )
    assert after.reach[0].via == ()


def test_nothing_is_planned_when_nothing_is_open(box):
    box.creds = []
    strict = {
        "permissions": {"deny": ["WebFetch", "WebSearch"]},
        "sandbox": {
            "enabled": True,
            "allowUnsandboxedCommands": False,
            "network": {"strictAllowlist": True},
        },
    }
    (box.claude / "settings.json").write_text(json.dumps(strict), encoding="utf-8")
    assert plan(box, "claude").edits == []


# --- Codex ---------------------------------------------------------------------------------


@needs_toml
def test_a_codex_fix_edits_lines_and_keeps_comments(box):
    config = box.codex / "config.toml"
    config.write_text(
        "# my settings\n"
        'sandbox_mode = "danger-full-access"  # for now\n'
        'approval_policy = "on-request"\n'
        "\n[features]\napps = false\n",
        encoding="utf-8",
    )
    planned = plan(box, "codex")
    (edit,) = planned.edits
    curb_fix.apply(planned, *granted(planned))
    text = config.read_text(encoding="utf-8")
    assert text.startswith("# my settings\n") and "[features]\napps = false" in text
    assert 'sandbox_mode = "workspace-write"' in text and 'approval_policy = "never"' in text
    assert 'exclude = ["OPENAI_API_KEY"]' in text
    assert any("permissions profile" in step for step in planned.guided)


@needs_toml
def test_a_codex_profile_gets_denials_including_the_backup_folder(box):
    config = box.codex / "config.toml"
    config.write_text(
        'approval_policy = "never"\ndefault_permissions = "mine"\n'
        '[permissions.mine]\nextends = ":workspace"\n'
        '[permissions.mine.filesystem]\n"~" = "read"\n',
        encoding="utf-8",
    )
    before = report(box, "codex")
    assert any(curb_reach.SHELL_FILES in r.via for r in before.reach)
    planned = plan(box, "codex")
    curb_fix.apply(planned, *granted(planned))
    entries = curb_fix._toml_parse(config.read_text(encoding="utf-8"))["permissions"]["mine"]
    denied = [key for key, value in entries["filesystem"].items() if value == "deny"]
    assert "~/.aws" in denied and any("backups" in key for key in denied)
    after = report(box, "codex")
    aws = next(r for r in after.reach if r.credential.kind == "aws")
    assert aws.via == ()


@needs_toml
def test_a_claude_fix_also_denies_codex_the_backup_folder(box):
    (box.codex / "config.toml").write_text(
        'approval_policy = "never"\ndefault_permissions = "mine"\n'
        '[features]\napps = false\n[permissions.mine]\nextends = ":workspace"\n',
        encoding="utf-8",
    )
    box.creds = box.creds[:1]
    planned = plan(box, "claude", "codex")
    codex_edit = next(e for e in planned.edits if e.agent == "codex")
    assert codex_edit.actions == ("deny Codex Curb's backup folder",)


@needs_toml
def test_a_codex_config_curb_cannot_edit_safely_becomes_guided_steps(box):
    (box.codex / "config.toml").write_text(
        'approval_policy = "on-request"\n[shell_environment_policy]\nexclude = [\n  "AWS_*",\n]\n',
        encoding="utf-8",
    )
    planned = plan(box, "codex")
    assert planned.edits == []
    assert any("could not edit Codex's config.toml safely" in step for step in planned.guided)


def test_backups_older_than_seven_days_are_dropped(box):
    planned = plan(box, "claude")
    folder = curb_fix.backup(planned.edits, now=time.time() - 8 * 86400)
    curb_fix.prune()
    assert not folder.exists()


# --- the command ---------------------------------------------------------------------------


@pytest.fixture
def cli_box(box, monkeypatch):
    monkeypatch.setattr("flanner.curb_inventory.shutil.which", lambda name: None)
    monkeypatch.setattr("flanner.curb_inventory.run", lambda argv: None)
    monkeypatch.setattr("flanner.curb_credentials.find", lambda *a: box.creds)
    monkeypatch.chdir(box.project)
    return box


def run(*args, input=None):
    return CliRunner().invoke(cli, ["curb", *args], input=input)


def test_fix_dry_run_shows_counts_and_changes_nothing(cli_box):
    result = run("fix", "--dry-run", "--agent", "claude")
    assert result.exit_code == 0, result.output
    assert "Read deny rule" in result.output and "Dry run" in result.output
    assert ".aws" not in result.output and "OPENAI_API_KEY" not in result.output
    assert not (cli_box.claude / "settings.json").exists()


def test_fix_without_an_approval_method_stays_read_only(cli_box, monkeypatch):
    monkeypatch.setattr(curb_approval, "method", lambda: None)
    result = run("fix", "--agent", "claude")
    assert result.exit_code == 1 and "read-only" in result.output
    assert not (cli_box.claude / "settings.json").exists()


def test_fix_applies_after_a_yes_and_undo_puts_it_back(cli_box, monkeypatch):
    monkeypatch.setattr(curb_approval, "method", lambda: Yes())
    result = run("fix", "--agent", "claude")
    assert result.exit_code == 0, result.output
    assert (cli_box.claude / "settings.json").exists()
    undone = run("fix", "--undo")
    assert undone.exit_code == 0, undone.output
    assert not (cli_box.claude / "settings.json").exists()


def test_a_refused_approval_changes_nothing(cli_box, monkeypatch):
    class No(Yes):
        def confirm(self, reason):
            return False

    monkeypatch.setattr(curb_approval, "method", lambda: No())
    result = run("fix", "--agent", "claude")
    assert result.exit_code == 1 and "Not approved" in result.output
    assert not (cli_box.claude / "settings.json").exists()


def test_forget_asks_about_backups_separately(cli_box, monkeypatch):
    monkeypatch.setattr(curb_approval, "method", lambda: Yes())
    run("fix", "--agent", "claude")
    assert curb_fix.count() == 1
    run("forget", input="y\nn\n")
    assert curb_fix.count() == 1
    run("forget", "--yes", "--backups")
    assert curb_fix.count() == 0


def test_fix_registry_and_examples_name_the_command():
    from flanner import operations

    assert any("curb fix" in op.cli for op in operations.OPERATIONS)
    assert os.path.basename(curb_fix.__file__) == "curb_fix.py"
