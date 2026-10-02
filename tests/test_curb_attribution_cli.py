"""`flanner curb attribution` and `flanner curb verify` (Curb PRD §10.15).

The OS prompt is a stand-in, the keychain the suite's in-memory one, and
gh is never run.
"""

import json
import os
import shutil
import subprocess

import pytest
from click.testing import CliRunner

from flanner import curb_approval, curb_attribution, curb_sshsig, identity
from flanner.cli import cli
from tests.test_curb_attribution import A, accept, entry, registry
from tests.test_curb_policy import ISSUER_RING


class Yes:
    name, weak = "test prompt", False

    def available(self):
        return True

    def confirm(self, reason):
        return True


@pytest.fixture
def agents(tmp_path, monkeypatch):
    claude, codex = tmp_path / "claude", tmp_path / "codex"
    claude.mkdir()
    codex.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude))
    monkeypatch.setenv("CODEX_HOME", str(codex))
    monkeypatch.setattr(curb_approval, "method", lambda: Yes())
    monkeypatch.setattr(curb_approval, "process_chain", lambda: [])
    monkeypatch.setattr(curb_attribution, "signer_path", lambda: tmp_path / "flanner-curb-sign")
    monkeypatch.setattr(shutil, "which", lambda name: None)  # never run gh
    return claude, codex


def run(*args):
    return CliRunner().invoke(cli, ["curb", *args])


def test_setup_makes_keys_configures_both_agents_and_says_how_to_add_them_to_github(agents):
    claude, codex = agents
    result = run("attribution", "--setup")
    assert result.exit_code == 0, result.output
    held = curb_attribution.keys()["keys"]
    assert set(held) == {"claude", "codex"}
    settings = json.loads((claude / "settings.json").read_text(encoding="utf-8"))
    assert settings["env"]["GIT_CONFIG_VALUE_0"] == "ssh"
    assert "shell_environment_policy" in (codex / "config.toml").read_text(encoding="utf-8")
    assert "gh ssh-key add" in result.output and "--type signing" in result.output
    status = run("attribution")
    assert held["claude"]["fingerprint"] in status.output and "not registered yet" in status.output


def test_setup_without_a_keychain_refuses_and_writes_nothing(agents, monkeypatch):
    claude, _ = agents
    monkeypatch.setattr(identity, "_keychain", lambda: None)
    result = run("attribution", "--setup")
    assert result.exit_code == 1 and "OS credential store" in result.output
    assert not (claude / "settings.json").exists()


def test_rotate_retires_each_key(agents):
    run("attribution", "--setup")
    before = curb_attribution.keys()["keys"]["claude"]["fingerprint"]
    result = run("attribution", "--rotate")
    assert result.exit_code == 0 and f"{before} is retired" in result.output
    assert curb_attribution.keys()["keys"]["claude"]["fingerprint"] != before


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_verify_reports_each_commits_state(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    git = ["git", "-C", str(repo)]
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "A",
        "GIT_AUTHOR_EMAIL": "a@x",
        "GIT_COMMITTER_NAME": "A",
        "GIT_COMMITTER_EMAIL": "a@x",
    }
    subprocess.run([*git, "init", "-q"], check=True, env=env)
    subprocess.run([*git, "commit", "-q", "--allow-empty", "-m", "first"], check=True, env=env)
    payload = curb_attribution.raw_commit("HEAD", repo)
    signed = curb_attribution.with_signature(payload, curb_sshsig.sign(A, payload))
    sha = (
        subprocess.run(
            [*git, "hash-object", "-t", "commit", "-w", "--stdin"],
            input=signed,
            capture_output=True,
            check=True,
        )
        .stdout.decode()
        .strip()
    )
    monkeypatch.chdir(repo)
    rows = json.loads(run("verify", sha, "--json").output)
    assert rows[0]["state"] == curb_attribution.UNKNOWN  # no registry on this device yet
    accept(registry(1, [entry(A)]))
    monkeypatch.setattr("flanner.cli._curb_device", lambda: _Device())
    rows = json.loads(run("verify", sha, "--json").output)
    assert rows[0]["state"] == curb_attribution.ATTRIBUTED and rows[0]["agent"] == "claude"
    unsigned = run("verify", "HEAD")
    assert "unattributed" in unsigned.output
    assert run("verify", "no-such-revision").exit_code == 1


class _Device:
    """A signed-in device whose control plane offers no attribution: the cache is used."""

    issuer_keyring = ISSUER_RING
    claims = None
    offered = ()
    organization_id = "org_test"
    device_id = "dev_a"


def test_setup_registers_each_key_once(agents, monkeypatch):
    from flanner import cli as cli_module
    from flanner import curb_wire
    from flanner.entitlements import CURB_ATTRIBUTION, Claims

    class Online(_Device):
        claims = Claims("org", "u", "dev_a", "k", "t", "t", features=(CURB_ATTRIBUTION,))
        offered = (curb_wire.ATTRIBUTION_V1,)

    calls = []
    monkeypatch.setattr(cli_module, "_curb_device", lambda: Online())
    monkeypatch.setattr(
        cli_module._CurbClient, "call", lambda self, name, body: calls.append(body["agent"]) or {}
    )
    run("attribution", "--setup")
    run("attribution", "--setup")
    assert sorted(calls) == ["claude", "codex"]
