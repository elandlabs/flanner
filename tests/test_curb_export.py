"""Audit export (Curb PRD §10.10): OCSF records that validate and hold no secret or path.

`required` below is OCSF 1.9.0's API Activity class (6003): the base event's
required attributes, the class's own, and the required parts of each object,
including the "at least one of" constraints, read from schema.ocsf.io.
"""

import json

import pytest

from flanner import curb_export, curb_log, curb_store

SECRET_PATH = "/home/dev/.aws/credentials-with-AKIAEXAMPLE"


def required(event):
    """The OCSF 1.9.0 API Activity requirements this exporter must meet."""
    for name in (
        "class_uid",
        "category_uid",
        "type_uid",
        "activity_id",
        "severity_id",
        "time",
        "metadata",
        "actor",
        "api",
        "src_endpoint",
        "cloud",
    ):
        assert name in event, name
    assert event["class_uid"] == 6003 and event["category_uid"] == 6
    assert event["type_uid"] == 6003 * 100 + event["activity_id"]
    assert event["activity_id"] in (0, 1, 2, 3, 4, 99)
    if event["activity_id"] == 99:
        assert event["activity_name"]
    assert event["metadata"]["product"] and event["metadata"]["version"] == "1.9.0"
    assert any(event["actor"].get(k) for k in ("process", "user", "session", "application"))
    assert event["api"]["operation"]
    assert any(event["src_endpoint"].get(k) for k in ("ip", "uid", "name", "hostname"))
    assert event["cloud"]["provider"]
    assert isinstance(event["time"], int)
    if "ai_agent" in event:
        assert event["ai_agent"].get("name") or event["ai_agent"].get("uid")


def record(tool="Read", decision="ran", target="file", value=SECRET_PATH):
    return {
        "kind": "tool",
        "agent": "claude",
        "session": "s-1",
        "event": "PostToolUse",
        "tool": tool,
        "channel": "file_tools",
        "target": target,
        "target_digest": curb_store.digest(value, b"k" * 32),
        "program": None,
        "decision": decision,
        "time": 1759395600.25,
        "hash": "h" * 64,
    }


@pytest.mark.parametrize(
    ("tool", "decision", "activity", "disposition", "status"),
    [
        ("Read", "ran", 2, 1, 1),
        ("Edit", "failed", 3, 1, 2),
        ("Bash", "denied", 99, 2, 0),
        ("mcp__docs__search", "requested", 99, 0, 0),
    ],
)
def test_each_tool_call_is_a_valid_api_activity(tool, decision, activity, disposition, status):
    event = curb_export.ocsf(record(tool, decision), device_id="dev_a", version="0.16.0")
    required(event)
    assert (event["activity_id"], event["disposition_id"], event["status_id"]) == (
        activity,
        disposition,
        status,
    )
    assert event["time"] == 1759395600250
    assert event["api"]["service"]["name"] == "Claude Code"
    assert event["actor"]["session"]["uid"] == "s-1"


def test_a_record_carries_no_path_and_no_secret():
    event = curb_export.ocsf(record(), device_id="dev_a", version="0.16.0")
    text = json.dumps(event)
    assert ".aws" not in text and "AKIA" not in text and "/home" not in text
    assert event["resources"] == [{"type": "file", "uid": record()["target_digest"]}]


def test_the_cursor_moves_only_past_what_the_collector_took():
    for n in range(3):
        curb_log.append({k: v for k, v in record(tool=f"Tool{n}").items() if k != "hash"})
    first, dropped = curb_export.pending(limit=2)
    assert [r["tool"] for r in first] == ["Tool0", "Tool1"] and dropped == 0
    assert curb_export.pending(limit=2)[0] == first  # not yet sent: offered again
    curb_export.sent(first)
    assert [r["tool"] for r in curb_export.pending()[0]] == ["Tool2"]


def test_a_backlog_past_the_limit_is_skipped_and_counted(monkeypatch):
    monkeypatch.setattr(curb_export, "MAX_BACKLOG", 2)
    for n in range(5):
        curb_log.append({k: v for k, v in record(tool=f"Tool{n}").items() if k != "hash"})
    records, dropped = curb_export.pending()
    assert dropped == 3 and [r["tool"] for r in records] == ["Tool3", "Tool4"]
    assert curb_store.read_state("export")["dropped"] == 3


def test_openshell_records_pass_through_redacted(tmp_path):
    folder = tmp_path / "openshell"
    folder.mkdir()
    lines = [
        {"class_uid": 1007, "process": {"cmd_line": "curl -H 'Authorization: Bearer s3cret'"}},
        {"class_uid": 4002, "http_request": {"url": {"path": "/private", "hostname": "api.x"}}},
        "not json",
    ]
    (folder / "openshell-ocsf.2026-10-02.log").write_text(
        "\n".join(json.dumps(x) if isinstance(x, dict) else x for x in lines), encoding="utf-8"
    )
    passed, offsets = curb_export.openshell(folder, b"k" * 32)
    text = json.dumps(passed)
    assert len(passed) == 2 and "s3cret" not in text and "/private" not in text
    assert passed[1]["http_request"]["url"]["hostname"] == "api.x"
    curb_export.openshell_sent(offsets)
    assert curb_export.openshell(folder, b"k" * 32)[0] == []
