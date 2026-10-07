"""Fleet reports (Curb PRD §10.9): numbered, chained, signed, and checked by the admin.

Covers: a replayed or reordered report is caught by the admin's own
check, a gap or an old report shows the device stale, a report another key
signed is refused, unsent reports wait 7 days, and a report carries none
of what §7.2 keeps on the device.
"""

import base64
import json
from datetime import datetime, timedelta, timezone

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from flanner import curb_fleet, curb_wire, identity

DEVICE = Ed25519PrivateKey.from_private_bytes(bytes([21]) * 32)
OTHER = Ed25519PrivateKey.from_private_bytes(bytes([22]) * 32)
DEVICE_ID = identity.device_id_for(DEVICE.public_key())
KEYRING = {DEVICE_ID: identity.public_key_b64(DEVICE.public_key())}
START = datetime(2026, 10, 2, 9, tzinfo=timezone.utc).timestamp()


def sign(key=DEVICE):
    return lambda payload: base64.b64encode(key.sign(payload)).decode("ascii")


def body(**changes):
    return {
        "kind": curb_wire.REPORT,
        "key_id": DEVICE_ID,
        "device_id": DEVICE_ID,
        "organization_id": "org_test",
        "client_version": "0.16.0",
        "agents": [{"agent": "claude", "version": "2.1.287"}],
        "policy": {"received": 1, "compliance_hash": "sha256:" + "a" * 64, "drift": False},
        "severity": {"high": 1, "medium": 0, "low": 0},
        "exposure": {"A": 0, "B": 1, "C": 0},
        "checked_at": "2026-10-02T09:00:00Z",
        **changes,
    }


def issued(count, key=DEVICE, start=START):
    return [curb_fleet.issue(body(), sign(key), now=start + n * 3600) for n in range(count)]


def check(tokens, now_offset_hours=3):
    devices = [{"device_id": DEVICE_ID, "label": "laptop", "reports": tokens}]
    when = datetime.fromtimestamp(START, timezone.utc) + timedelta(hours=now_offset_hours)
    return curb_fleet.verify(devices, KEYRING, now=when)[0]


def test_reports_are_numbered_chained_and_queued():
    tokens = issued(3)
    fields = [curb_wire.fields_of(t) for t in tokens]
    assert [f["sequence"] for f in fields] == [1, 2, 3]
    assert fields[0]["previous_hash"] == ""
    assert fields[2]["previous_hash"] == curb_wire.doc_hash(fields[1])
    assert curb_fleet.due(now=START) == tokens


def test_an_admin_trusts_an_unbroken_signed_chain():
    device = check(issued(3))
    assert device.trusted and not device.stale
    assert device.latest["sequence"] == 3
    row = curb_fleet.view([device])[0]
    assert row["device"] == "laptop" and row["verified"] and row["severity"]["high"] == 1


def test_a_replayed_report_is_caught():
    tokens = issued(3)
    device = check([*tokens, tokens[1]])
    assert not device.trusted
    assert "report 2 is replayed or out of order" in device.problems


def test_a_report_signed_by_another_key_is_refused():
    tokens = issued(2)
    forged = curb_wire.encode(curb_wire.fields_of(tokens[1]) | {"sequence": 3}, sign(OTHER))
    device = check([*tokens, forged])
    assert "a report is not signed by this device" in device.problems


def test_a_report_that_breaks_the_chain_is_caught():
    tokens = issued(2)
    third = curb_wire.fields_of(tokens[1]) | {"sequence": 3, "previous_hash": "sha256:other"}
    device = check([*tokens, curb_wire.encode(third, sign())])
    assert "report 3 breaks the chain" in device.problems


def test_a_gap_or_an_old_report_shows_the_device_stale():
    tokens = issued(3)
    assert check([tokens[0], tokens[2]]).stale  # report 2 never arrived
    assert check(tokens, now_offset_hours=30).stale  # newest report over 24 hours old
    assert check([]).stale


def test_a_device_missing_from_the_keyring_is_not_trusted():
    devices = [{"device_id": "dev_unknown", "label": "x", "reports": issued(1)}]
    device = curb_fleet.verify(devices, KEYRING, now=datetime.now(timezone.utc))[0]
    assert not device.trusted and device.problems == ["no key for this device in the keyring"]


def test_unsent_reports_wait_seven_days_then_go():
    token = issued(1)[0]
    assert curb_fleet.due(now=START + 6 * 86400) == [token]
    assert curb_fleet.due(now=START + 8 * 86400) == []


def test_a_sent_report_leaves_the_outbox():
    first, second = issued(2)
    curb_fleet.done(first)
    assert curb_fleet.due(now=START) == [second]


def test_matching_counts_devices_per_compliance_hash():
    rows = [
        {"policy": {"compliance_hash": "sha256:a"}},
        {"policy": {"compliance_hash": "sha256:a"}},
        {"policy": {"compliance_hash": "sha256:b"}},
        {"policy": {}},
    ]
    assert curb_fleet.matching(rows) == {"sha256:a": 2, "sha256:b": 1}


def test_a_report_holds_only_the_minimised_fields():
    allowed = {
        "kind",
        "key_id",
        "device_id",
        "organization_id",
        "client_version",
        "agents",
        "policy",
        "severity",
        "exposure",
        "checked_at",
        "sequence",
        "created_at",
        "previous_hash",
    }
    fields = curb_wire.fields_of(issued(1)[0])
    assert set(fields) == allowed
    text = json.dumps(fields)
    assert "\\\\" not in text and "/home" not in text and "Users" not in text
