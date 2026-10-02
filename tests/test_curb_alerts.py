"""Alerts (Curb PRD §10.11): one logical alert per change that grows reach.

Covers each source of growth (a new MCP server, a removed deny rule, the
sandbox turned off, a new class A secret), one alert per secret per
device, stable event ids through retries, the 7-day outbox, the
developer's notices, and payloads free of names, paths and values.
"""

import json

from flanner import curb_alerts, curb_store
from tests.test_curb_policy import box, reports  # noqa: F401 - box is a fixture

KEY = b"k" * 32


def write_claude(box, data):  # noqa: F811 - box is the fixture's value
    (box.claude / "settings.json").write_text(json.dumps(data), encoding="utf-8")


def seen(box, sweep=None, extra=()):  # noqa: F811
    return curb_alerts.observe(reports(box, "claude"), KEY, sweep=sweep, extra=extra)


def test_the_first_pass_only_records_what_is_there(box):  # noqa: F811
    write_claude(box, {"mcpServers": {"docs": {"command": "docs-mcp"}}})
    assert seen(box) == []
    assert curb_store.read_state("alerts")["agents"]["claude"]["mcp"]


def test_a_new_mcp_server_raises_one_alert_without_its_name(box):  # noqa: F811
    write_claude(box, {"mcpServers": {"docs": {"command": "docs-mcp"}}})
    seen(box)
    write_claude(
        box,
        {"mcpServers": {"docs": {"command": "docs-mcp"}, "secret-server": {"command": "x"}}},
    )
    found = seen(box)
    assert [f["type"] for f in found] == [curb_alerts.MCP_ADDED]
    assert found[0]["location"] == "Claude Code MCP settings"
    assert "secret-server" not in json.dumps(found)
    assert seen(box) == []  # the same change alerts once


def test_a_removed_deny_rule_and_a_disabled_sandbox_each_alert(box):  # noqa: F811
    write_claude(box, {"permissions": {"deny": ["Read(~/.aws/**)"]}, "sandbox": {"enabled": True}})
    seen(box)
    write_claude(box, {"sandbox": {"enabled": False}})
    kinds = sorted(f["type"] for f in seen(box))
    assert kinds == [curb_alerts.DENY_REMOVED, curb_alerts.SANDBOX_OFF]


def test_a_new_class_a_secret_alerts_once_per_device(box):  # noqa: F811
    sweep = {
        "findings": [
            {"class": "A", "category": "Claude Code transcript", "secret": "d1"},
            {"class": "A", "category": "shell history", "secret": "d1"},
            {"class": "B", "category": "project .env", "secret": "d2"},
        ]
    }
    seen(box)
    found = seen(box, sweep=sweep)
    assert [(f["type"], f["digest"]) for f in found] == [(curb_alerts.SECRET_SENT, "d1")]
    assert seen(box, sweep=sweep) == []


def test_policy_alerts_pass_through_even_on_the_first_pass(box):  # noqa: F811
    refused = curb_alerts.policy(curb_alerts.POLICY_ROLLBACK, 3, "older")
    assert seen(box, extra=[refused]) == [refused]


def finding(kind=curb_alerts.SANDBOX_OFF):
    return {"type": kind, "agent": "claude", "digest": "", "severity": "high", "location": "x"}


def test_event_ids_survive_retries_and_delivery_ends_them():
    alerts = curb_alerts.raise_alerts([finding()], device_id="dev_a", now=1000)
    event = alerts[0]["event_id"]
    assert [a["event_id"] for a in curb_alerts.due(now=1000)] == [event]
    curb_alerts.settle([], [event], now=1000)
    assert curb_alerts.due(now=1001) == []  # backing off
    assert [a["event_id"] for a in curb_alerts.due(now=1000 + 61)] == [event]
    curb_alerts.settle([event], [], now=1100)
    assert curb_alerts.due(now=5000) == []


def test_each_pass_numbers_its_changes_so_a_repeat_gets_a_new_id():
    first = curb_alerts.raise_alerts([finding()], device_id="dev_a", now=1000)
    second = curb_alerts.raise_alerts([finding()], device_id="dev_a", now=2000)
    assert first[0]["event_id"] != second[0]["event_id"]


def test_alerts_older_than_seven_days_are_dropped():
    curb_alerts.raise_alerts([finding()], device_id="dev_a", now=1000)
    assert curb_alerts.due(now=1000 + 8 * 86400) == []


def test_the_developer_is_told_once():
    curb_alerts.raise_alerts(
        [finding(), finding(curb_alerts.MCP_ADDED)], device_id="dev_a", now=1000
    )
    notices = curb_alerts.take_notices()
    assert len(notices) == 1
    assert "an MCP server was added" in notices[0] and "the sandbox was turned off" in notices[0]
    assert curb_alerts.take_notices() == []


def test_an_alert_carries_only_the_contract_fields():
    alert = curb_alerts.raise_alerts([finding()], device_id="dev_a", now=1000)[0]
    assert set(alert) == {
        "event_id",
        "type",
        "agent",
        "severity",
        "digest",
        "location",
        "created_at",
    }
    assert alert["event_id"].startswith("evt_") and len(alert["event_id"]) == 36


def test_forget_drops_alerts_and_export_but_keeps_policy_and_sequence():
    curb_alerts.raise_alerts([finding()], device_id="dev_a", now=1000)
    curb_store.write_state("export", {"cursor": "x"})
    curb_store.write_state("fleet", {"sequence": 4})
    curb_store.write_state("policy", {"delegated_at": 1.0})
    assert "queued alerts and the audit export position" in curb_store.forget()
    assert curb_store.read_state("alerts-outbox") == {} and curb_store.read_state("export") == {}
    assert curb_store.read_state("fleet")["sequence"] == 4  # a reset would read as a replay
    assert curb_store.read_state("policy")["delegated_at"] == 1.0
