"""The leak sweep, its local state and its commands (Curb PRD §10.3, §7).

A fake detector stands in for Kingfisher: it finds `CURBTEST_` tokens, so
every test plants exactly what it expects found. No test needs the `sweep`
extra, and the adapter is checked against a stand-in for Kingfisher's SDK.
"""

import json
import os
import re
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from flanner import curb_kingfisher, curb_settings, curb_store, curb_sweep, curb_window
from flanner.cli import cli
from flanner.curb_context import BASELINE, default
from flanner.curb_kingfisher import Match

TOKEN = re.compile(r"CURBTEST_[A-Z0-9]{12}")


def detect(path: Path) -> list[Match]:
    found = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        found += [Match("test.token", "Test token", number, m) for m in TOKEN.findall(line)]
    return found


def token(n: int) -> str:
    return f"CURBTEST_{n:012d}"


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
    monkeypatch.chdir(found.project)
    return found


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def launch(box, agent: str = "claude", settings: dict | None = None, platform: str = "linux"):
    if settings is not None:
        write(box.claude / "settings.json", json.dumps(settings))
    context = default(agent, box.project)
    return context, curb_settings.resolve(context, platform=platform), BASELINE[agent]


def sweep(box, launches=(), platform="linux", detector=detect):
    return curb_sweep.run(
        box.project,
        detector,
        list(launches),
        home=box.home,
        env={},
        platform=platform,
        key=b"k" * 32,
    )


# --- where it looks -----------------------------------------------------------------


def test_every_kind_of_artifact_is_read_once(box):
    planted = {
        box.claude / "projects" / "repo" / "s1.jsonl": "Claude Code transcript",
        box.codex / "sessions" / "2026" / "10" / "02" / "rollout-1.jsonl": "Codex session",
        box.codex / "history.jsonl": "Codex prompt history",
        box.claude / "CLAUDE.md": "CLAUDE.md",
        box.project / "CLAUDE.md": "CLAUDE.md",
        box.project / "AGENTS.md": "AGENTS.md",
        box.project / ".claude" / "skills" / "deploy" / "SKILL.md": "skill",
        box.project / ".agents" / "skills" / "tidy" / "scripts" / "run.sh": "skill",
        box.claude / ".claude.json": "MCP config",
        box.project / ".mcp.json": "MCP config",
        box.project / ".claude" / "settings.local.json": "Claude Code settings",
        box.codex / "config.toml": "Codex config",
        box.home / ".bash_history": "shell history",
        box.project / "services" / "api" / ".env.production": "project .env",
        box.project / ".plans" / "migration.md": "flanner plan",
        box.project / ".flanner" / "memory" / "deploys.md": "flanner memory",
    }
    for path in planted:
        write(path, "x\n")
    write(box.project / ".agents" / "skills" / "tidy" / "SKILL.md", "---\nname: tidy\n---\n")
    write(box.project / ".env.example", "SAMPLE=1\n")  # a template, never read
    found = curb_sweep.artifacts(box.project, home=box.home, env={}, platform="linux")
    by_path = {a.path: a.category for a in found}
    for path, category in planted.items():
        assert by_path.get(path) == category, path
    assert box.project / ".env.example" not in by_path
    assert len(by_path) == len(found)  # each file once
    sent = {"Claude Code transcript", "Codex session", "Codex prompt history"}
    assert all(a.transcript == (a.category in sent) for a in found)


def test_powershell_history_is_read_where_windows_keeps_it(box):
    roaming = box.home / "AppData" / "Roaming"
    history = roaming / "Microsoft" / "Windows" / "PowerShell" / "PSReadLine"
    path = write(history / "ConsoleHost_history.txt", "x\n")
    found = curb_sweep.artifacts(
        box.project, home=box.home, env={"APPDATA": str(roaming)}, platform="win32"
    )
    assert [a.category for a in found if a.path == path] == ["shell history"]


# --- exposure classes ----------------------------------------------------------------


def test_a_secret_in_a_transcript_was_sent_to_the_model_provider(box):
    write(box.claude / "projects" / "repo" / "s.jsonl", f'{{"text": "{token(1)}"}}\n')
    report = sweep(box, [launch(box)])
    assert [f.exposure for f in report.findings] == [curb_sweep.SENT]


def test_a_secret_an_agent_can_read_is_class_b(box):
    write(box.project / ".env", f"API_TOKEN={token(2)}\n")
    report = sweep(box, [launch(box)])
    assert [(f.exposure, f.readable_by) for f in report.findings] == [("B", ("Claude Code",))]


def test_a_secret_no_agent_can_read_is_class_c(box):
    locked = {
        "permissions": {"deny": ["Read(//**)"]},
        "sandbox": {
            "enabled": True,
            "allowUnsandboxedCommands": False,
            "filesystem": {"denyRead": ["/"]},
        },
    }
    write(box.project / ".env", f"API_TOKEN={token(3)}\n")
    report = sweep(box, [launch(box, settings=locked)])
    assert [f.exposure for f in report.findings] == [curb_sweep.BLOCKED]
    # The same rules on native Windows, where the sandbox does not run.
    windows = sweep(box, [launch(box, settings=locked, platform="win32")], platform="win32")
    assert [f.exposure for f in windows.findings] == [curb_sweep.READABLE]


def test_with_no_agent_nothing_counts_as_readable(box):
    write(box.project / ".env", f"API_TOKEN={token(4)}\n")
    report = sweep(box, [])
    assert [f.exposure for f in report.findings] == [curb_sweep.BLOCKED]
    assert report.launches == []


def test_a_secret_counts_once_in_its_worst_class(box):
    write(box.project / ".env", f"API_TOKEN={token(5)}\n")
    write(
        box.claude / "projects" / "repo" / "s.jsonl",
        f'{{"text": "{token(5)}"}}\n{{"text": "{token(5)} again"}}\n',
    )
    report = sweep(box, [launch(box)])
    assert len(report.findings) == 2  # once per file, however often it repeats
    assert report.secrets() == {report.findings[0].secret_digest: curb_sweep.SENT}
    assert curb_sweep.redacted(report)["by_class"] == {"A": 1, "B": 0, "C": 0}


def test_files_too_big_or_unreadable_are_listed_as_not_checked(box, monkeypatch):
    write(box.project / ".env", "x\n")
    monkeypatch.setattr(curb_sweep, "MAX_BYTES", 1)
    assert sweep(box).not_checked == ["1 file(s) over 0 MB"]
    monkeypatch.setattr(curb_sweep, "MAX_BYTES", 10**9)

    def broken(path):
        raise RuntimeError("cannot decode")

    assert sweep(box, detector=broken).not_checked == ["1 file(s) that could not be read"]


# --- what each view may hold ----------------------------------------------------------


def strings(value) -> list[str]:
    """Every string anywhere in a view, so a check cannot miss a nested one."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for k, v in value.items() for s in strings(k) + strings(v)]
    if isinstance(value, list | tuple):
        return [s for item in value for s in strings(item)]
    return []


def test_no_view_holds_a_value_and_only_the_window_holds_a_location(box):
    secret = token(6)
    env_file = write(box.project / ".env", f"API_TOKEN={secret}\n")
    report = sweep(box, [launch(box)])
    redacted = strings(curb_sweep.redacted(report))
    stored = strings(curb_sweep.stored(report))
    full = strings(curb_sweep.full(report))
    for view in (redacted, stored, full):
        assert not any(secret in s for s in view)
    for view in (redacted, stored):
        assert not any(str(env_file) in s or "Test token" in s for s in view)
    assert str(env_file) in full and "Test token" in full
    assert report.findings[0].secret_digest in stored


def test_validation_records_each_issuers_answer(box):
    write(box.project / ".env", f"A_TOKEN={token(7)}\nB_TOKEN={token(8)}\n")
    report = sweep(box, [launch(box)])
    curb_sweep.validate(report, lambda matches: ["verified_active", "verified_inactive"])
    assert curb_sweep.redacted(report)["validation"] == {
        "verified_active": 1,
        "verified_inactive": 1,
    }


def test_the_sweep_asks_for_low_cpu_priority(monkeypatch):
    asked = []
    monkeypatch.setattr(curb_sweep.sys, "platform", "linux")
    monkeypatch.setattr(curb_sweep.os, "nice", asked.append, raising=False)
    curb_sweep.lower_priority()
    assert asked == [10]


# --- the digest key and stored reports -------------------------------------------------


def test_a_digest_is_stable_under_one_key_and_useless_without_it():
    assert curb_store.digest("s3cret", b"a" * 32) == curb_store.digest("s3cret", b"a" * 32)
    assert curb_store.digest("s3cret", b"a" * 32) != curb_store.digest("s3cret", b"b" * 32)
    assert len(curb_store.digest("s3cret", b"a" * 32)) == 32  # 16 bytes, hex


def test_without_a_keychain_the_digest_key_is_a_user_only_file(monkeypatch):
    monkeypatch.setenv("FLANNER_NO_KEYCHAIN", "1")
    key = curb_store.digest_key()
    assert curb_store.digest_key() == key
    path = curb_store.curb_dir() / "digest.key"
    assert path.is_file()
    if sys.platform != "win32":
        assert path.stat().st_mode & 0o077 == 0


def test_the_digest_key_lives_in_the_keychain_when_there_is_one():
    # conftest gives every test an in-memory keychain.
    key = curb_store.digest_key()
    assert not (curb_store.curb_dir() / "digest.key").exists()
    assert curb_store.digest_key() == key


def test_reports_older_than_30_days_are_dropped():
    old = curb_store.save_report("sweep", {"secrets": 1}, now=time.time() - 40 * 86400)
    os.utime(old, (time.time() - 40 * 86400,) * 2)
    fresh = curb_store.save_report("sweep", {"secrets": 2})
    assert not old.exists() and fresh.exists()


def test_forget_removes_reports_and_the_key():
    curb_store.save_report("sweep", {"secrets": 1})
    curb_store.digest_key()
    removed = curb_store.forget()
    assert removed == [
        "1 stored report(s)",
        "the digest key, so older fingerprints can no longer be matched",
    ]
    assert curb_store.forget() == []


# --- the Kingfisher adapter -------------------------------------------------------------


class FakeFinding:
    def __init__(self, rule_id, secret, line, visible=True, name="GitHub Token"):
        self.rule_id, self.secret, self.visible = rule_id, secret, visible
        self._line, self._name = line, name

    def to_dict(self, *, redact=True):
        secret = "ghp_****" if redact else self.secret
        return {
            "rule_id": self.rule_id,
            "rule_name": self._name,
            "secret": secret,
            "location": {"line": self._line, "start_offset": 0},
        }


@pytest.fixture
def fake_sdk(monkeypatch):
    calls = SimpleNamespace(validated=[])

    class Scanner:
        def scan_file(self, path):
            return [
                FakeFinding("kingfisher.github.1", "ghp_secret", 3),
                FakeFinding("kingfisher.aws.helper", "AKIA", 3, visible=False),
            ]

    class Validator:
        def validate(self, findings):
            calls.validated.append(len(findings))
            return [SimpleNamespace(outcome=f"outcome-{f.rule_id}") for f in findings]

    monkeypatch.setitem(
        sys.modules, "kingfisher_sdk", SimpleNamespace(Scanner=Scanner, Validator=Validator)
    )
    return calls


def test_the_adapter_reports_visible_findings_with_rule_and_line(fake_sdk, tmp_path):
    matches = curb_kingfisher.Detector()(tmp_path / "x")
    assert [(m.rule_id, m.rule_name, m.line, m.secret) for m in matches] == [
        ("kingfisher.github.1", "GitHub Token", 3, "ghp_secret")
    ]
    assert "ghp_secret" not in repr(matches[0])


def test_the_adapter_validates_a_files_findings_together(fake_sdk, tmp_path):
    matches = curb_kingfisher.Detector()(tmp_path / "x")
    assert curb_kingfisher.validate(matches) == ["outcome-kingfisher.github.1"]
    assert fake_sdk.validated == [2]  # the helper went along with it


def test_without_kingfisher_the_sweep_says_how_to_add_it(monkeypatch):
    monkeypatch.setitem(sys.modules, "kingfisher_sdk", None)
    assert "pip install 'flanner[sweep]'" in curb_kingfisher.unavailable()


# --- the commands ---------------------------------------------------------------------


@pytest.fixture
def swept(box, monkeypatch):
    monkeypatch.setattr(curb_kingfisher, "unavailable", lambda: None)
    monkeypatch.setattr(curb_kingfisher, "Detector", lambda: detect)
    monkeypatch.setattr(curb_kingfisher, "validate", lambda m: ["verified_active"] * len(m))
    monkeypatch.setattr(curb_sweep, "lower_priority", lambda: None)
    monkeypatch.setattr("flanner.curb_inventory.shutil.which", lambda name: None)
    monkeypatch.setattr("flanner.curb_inventory.run", lambda argv: None)
    box.secret = token(42)
    box.env_file = write(box.project / ".env", f"STRIPE_SECRET_KEY={box.secret}\n")
    write(box.claude / "projects" / "repo" / "s.jsonl", f'{{"text": "{box.secret}"}}\n')
    return box


def run_cli(*args, input=None):
    return CliRunner().invoke(cli, ["curb", *args], input=input)


def test_sweep_prints_counts_and_nothing_else(swept):
    result = run_cli("sweep")
    assert result.exit_code == 0, result.output
    assert "A, sent to a model provider" in result.output
    assert swept.secret not in result.output
    assert str(swept.env_file) not in result.output and "Test token" not in result.output
    assert "flanner curb show --sweep" in result.output


def test_sweep_json_is_counts_and_keeps_a_redacted_report(swept):
    result = run_cli("sweep", "--json")
    data = json.loads(result.output)
    assert (data["secrets"], data["by_class"]["A"]) == (1, 1)
    reports = list((curb_store.curb_dir() / "reports").glob("sweep-*.json"))
    assert len(reports) == 1
    kept = reports[0].read_text(encoding="utf-8")
    assert swept.secret not in kept and str(swept.env_file) not in kept


def test_sweep_validates_only_after_a_yes(swept):
    assert "Issuers said" not in run_cli("sweep", "--validate", input="n\n").output
    assert "Issuers said" not in run_cli("sweep", "--validate").output  # nobody to ask
    assert "verified_active 2" in run_cli("sweep", "--validate", input="y\n").output


def test_sweep_without_kingfisher_fails_and_says_why(box, monkeypatch):
    monkeypatch.setitem(sys.modules, "kingfisher_sdk", None)
    result = run_cli("sweep")
    assert result.exit_code == 1
    assert "flanner[sweep]" in result.output


def test_show_sweep_opens_a_window_with_locations_but_no_values(swept, monkeypatch):
    started, drawn = [], []
    monkeypatch.setattr(curb_window, "unavailable", lambda: None)
    monkeypatch.setattr(curb_window, "launch", lambda args: started.append(list(args)))
    monkeypatch.setattr(curb_window, "show", lambda title, text: drawn.append(text))
    result = run_cli("show", "--sweep", "--validate", input="y\n")
    assert result.exit_code == 0, result.output
    assert started[0][:4] == ["--dir", str(swept.project), "--sweep", "--validate"]
    assert swept.secret not in result.output
    run_cli("show", "--sweep", "--in-window", "--dir", str(swept.project))
    assert str(swept.env_file) in drawn[0] and swept.secret not in drawn[0]


def test_validate_needs_sweep(box):
    result = run_cli("show", "--validate")
    assert result.exit_code == 2 and "--validate goes with --sweep" in result.output


def test_forget_asks_first_unless_told_not_to(box):
    curb_store.save_report("sweep", {"secrets": 1})
    assert "Nothing deleted" in run_cli("forget").output
    assert (curb_store.curb_dir() / "reports").is_dir()
    result = run_cli("forget", "--yes")
    assert "1 stored report(s)" in result.output
    assert not (curb_store.curb_dir() / "reports").exists()
