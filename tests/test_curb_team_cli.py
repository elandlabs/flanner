"""`flanner curb policy`, `flanner curb fleet` and the session hook (Curb PRD §10.8-10.11).

The OS prompt and the control plane are stand-ins. Nothing printed names a
denied path or a credential, for any caller.
"""

import json
from datetime import datetime, timezone

import pytest
from click.testing import CliRunner

from flanner import cli as cli_module
from flanner import (
    curb_alerts,
    curb_approval,
    curb_policy,
    curb_store,
    curb_wire,
    identity,
    release,
)
from flanner.cli import cli
from tests.test_curb_policy import (  # noqa: F401 - box is a fixture
    ORG,
    accept_authority,
    box,
    policy,
    receive,
    reports,
)

NOW = datetime.now(timezone.utc)


class Yes:
    name, weak = "test prompt", False

    def available(self):
        return True

    def confirm(self, reason):
        return True


def run(*args, input=None):
    return CliRunner().invoke(cli, ["curb", *args], input=input)


@pytest.fixture
def approves(monkeypatch):
    monkeypatch.setattr(curb_approval, "method", lambda: Yes())


def arrived(token):
    accept_authority()
    receive(token, now=NOW)


def test_with_no_policy_it_says_how_to_get_one(box):  # noqa: F811
    result = run("policy")
    assert result.exit_code == 0
    assert "No org policy has arrived" in result.output and "flanner login" in result.output


def test_checking_in_needs_a_signed_in_device(box):  # noqa: F811
    result = run("policy", "--check-in")
    assert result.exit_code == 1 and "not signed in" in result.output


def test_enrolling_asks_once_installs_the_session_hooks_and_delegates(box, approves):  # noqa: F811
    result = run("policy", "--enrol")
    assert result.exit_code == 0, result.output
    hooks = json.loads((box.claude / "settings.json").read_text(encoding="utf-8"))["hooks"]
    assert set(hooks) == {"SessionStart", "ConfigChange"}
    assert " hook curb-session --agent claude" in hooks["SessionStart"][0]["hooks"][0]["command"]
    assert curb_policy.load().delegated_at is not None
    assert run("policy", "--withdraw").exit_code == 0
    assert curb_policy.load().delegated_at is None


def test_enrolling_without_an_approval_method_changes_nothing(box, monkeypatch):  # noqa: F811
    monkeypatch.setattr(curb_approval, "method", lambda: None)
    result = run("policy", "--enrol")
    assert result.exit_code == 1 and "No approval method" in result.output
    assert not (box.claude / "settings.json").exists()
    assert curb_policy.load().delegated_at is None


def test_the_status_shows_what_waits_and_names_no_path(box, monkeypatch):  # noqa: F811
    arrived(policy(1))
    curb_policy.apply_received(reports(box), home=box.home, platform="linux", env={})
    result = run("policy")
    assert "Org policy version 1" in result.output and "waits for your approval" in result.output
    assert ".aws" not in result.output and str(box.home) not in result.output
    data = json.loads(run("policy", "--json").output)
    assert data["pending"] == 1 and data["delegation"] is False
    assert ".aws" not in json.dumps(data)


def test_approving_applies_the_waiting_change(box, approves, monkeypatch):  # noqa: F811
    monkeypatch.setattr(cli_module, "_curb_home_reports", lambda: reports(box))
    arrived(policy(1))
    curb_policy.apply_received(reports(box), home=box.home, platform="linux", env={})
    result = run("policy", "--approve")
    assert result.exit_code == 0, result.output
    assert "Applied policy version 1" in result.output
    assert curb_policy.load().applied["by"] == "person"
    assert (
        "Read(~/.aws)"
        in json.loads((box.claude / "settings.json").read_text(encoding="utf-8"))["permissions"][
            "deny"
        ]
    )
    assert run("policy", "--approve").output.strip().endswith("Nothing waits for your approval.")


def test_export_writes_each_admin_owned_file(box, tmp_path):  # noqa: F811
    arrived(policy(1, {"web": "off", "sandbox": "required"}))
    target = tmp_path / "mdm"
    result = run("policy", "--export", str(target))
    assert result.exit_code == 0, result.output
    assert sorted(p.name for p in target.iterdir()) == [
        "managed-settings.json",
        "openshell-policy.yaml",
        "requirements.toml",
    ]
    assert (
        json.loads((target / "managed-settings.json").read_text(encoding="utf-8"))["sandbox"][
            "enabled"
        ]
        is True
    )


def test_the_fleet_needs_a_signed_in_device(box):  # noqa: F811
    result = run("fleet")
    assert result.exit_code == 1 and "not signed in" in result.output


def test_the_fleet_shows_only_what_verifies(box, monkeypatch):  # noqa: F811
    from flanner import account, curb_fleet

    body = {
        "kind": curb_wire.REPORT,
        "key_id": identity.device_id(),
        "device_id": identity.device_id(),
        "organization_id": ORG,
        "client_version": "0.16.0",
        "agents": [],
        "policy": {"applied": 1, "drift": True, "compliance_hash": "sha256:x"},
        "severity": {"high": 2, "medium": 0, "low": 1},
        "exposure": None,
        "checked_at": "2026-10-02T09:00:00Z",
    }
    token = curb_fleet.issue(body, identity.sign)
    monkeypatch.setattr(cli_module, "_curb_device", lambda: object())
    monkeypatch.setattr(
        account,
        "fetch_device_keys",
        lambda: {identity.device_id(): identity.device_public_key_b64()},
    )
    devices = [
        {"device_id": identity.device_id(), "label": "laptop", "reports": [token]},
        {"device_id": "dev_nokey", "label": "stranger", "reports": [token]},
    ]
    monkeypatch.setattr(cli_module._CurbClient, "call", lambda self, name, b: {"devices": devices})
    result = run("fleet")
    assert result.exit_code == 0, result.output
    assert "laptop: verified" in result.output and "policy 1 drift" in result.output
    assert "stranger: NOT verified" in result.output
    rows = json.loads(run("fleet", "--json").output)
    assert [r["verified"] for r in rows] == [True, False]
    # Kept for the web UI's Devices page, which never fetches them itself.
    kept = curb_store.read_state("fleet-view")
    assert kept["rows"] == rows and kept["fetched_at"]


def test_the_session_hook_starts_a_pass_in_the_background_and_shows_notices(box, monkeypatch):  # noqa: F811
    started = []
    monkeypatch.setattr(release, "spawn_detached", lambda code: started.append(code) or True)
    curb_alerts.raise_alerts(
        [
            {
                "type": "sandbox_off",
                "agent": "claude",
                "digest": "",
                "severity": "high",
                "location": "x",
            }
        ],
        device_id="dev_a",
    )
    payload = json.dumps({"hook_event_name": "SessionStart", "session_id": "s"})
    result = CliRunner().invoke(cli, ["hook", "curb-session", "--agent", "claude"], input=payload)
    assert result.exit_code == 0
    assert started and "curb_background" in started[0]
    assert "the sandbox was turned off" in json.loads(result.output)["systemMessage"]
    again = CliRunner().invoke(cli, ["hook", "curb-session", "--agent", "claude"], input=payload)
    assert len(started) == 1 and again.output == ""  # quiet for five minutes, notices shown once


def test_the_session_hook_never_fails(box):  # noqa: F811
    result = CliRunner().invoke(
        cli, ["hook", "curb-session", "--agent", "codex"], input="not json"
    )
    assert result.exit_code == 0 and result.output == ""
    assert curb_store.read_state("team") == {}
