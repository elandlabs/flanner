"""The team pass against a fake control plane (Curb PRD §10.8-10.11, §15 R5).

The fake keeps what the real one must: the newest policy, "unchanged" for
the current one, report sequence numbers per device, and alert ids it has
delivered. Each test is one of the R5 exit scenarios that one device can
show; the two-device ones run in meshlab.
"""

import json
from types import SimpleNamespace

import pytest

from flanner import (
    curb_alerts,
    curb_attribution,
    curb_log,
    curb_policy,
    curb_settings,
    curb_store,
    curb_team,
    curb_wire,
    identity,
)
from flanner.entitlements import CURB_ALERTS, CURB_ATTRIBUTION, CURB_FLEET, CURB_POLICY, Claims
from tests.test_curb_attribution import A, entry, registry
from tests.test_curb_policy import (  # noqa: F401 - box is a fixture
    ISSUER_RING,
    NOW,
    ORG,
    authority,
    box,
    hash_of,
    policy,
    reports,
)


class Refused(Exception):
    def __init__(self, code, message="refused"):
        super().__init__(message)
        self.code = code


class Plane:
    """A control plane that keeps the contract's promises, and nothing else."""

    def __init__(self):
        self.authority = authority()
        self.policies = []
        self.always_send = False
        self.refuse = {}
        self.calls = []
        self.states = []
        self.last_sequence = {}
        self.reports = []
        self.delivered = []
        self.collected = []
        self.registered = []
        self.registry = registry(1, [])

    def call(self, name, body):
        self.calls.append(name)
        if name in self.refuse:
            raise self.refuse.pop(name)
        if name == "authority":
            return {"authority": self.authority}
        if name == "policy":
            if not self.policies:
                return {"status": "none"}
            newest = self.policies[-1]
            current = body["current_version"] == curb_wire.fields_of(newest)["version"] and body[
                "current_hash"
            ] == hash_of(newest)
            if current and not self.always_send:
                return {"status": "unchanged"}
            return {"status": "policy", "policy": newest, "audit_export_token": "tok"}
        if name == "state":
            self.states.append(body)
            return {}
        if name == "reports":
            fields = curb_wire.read(
                body["report"],
                {identity.device_id(): identity.device_public_key_b64()},
                curb_wire.REPORT,
            )
            assert fields is not None
            if fields["sequence"] <= self.last_sequence.get(fields["device_id"], 0):
                raise Refused("stale_sequence")
            self.last_sequence[fields["device_id"]] = fields["sequence"]
            self.reports.append(fields)
            return {}
        if name == "attribution-keys":
            self.registered.append(body)
            return {"status": "active"}
        if name == "attribution-registry":
            return {"registry": self.registry}
        if name == "alerts":
            ids = [a["event_id"] for a in body["alerts"]]
            self.delivered += [i for i in ids if i not in self.delivered]
            return {"accepted": ids}
        raise AssertionError(name)

    def collect(self, url, token, records):
        self.collected.append(SimpleNamespace(url=url, token=token, records=records))


def device(offered=curb_wire.CAPABILITIES):
    claims = Claims(
        ORG,
        "user",
        identity.device_id(),
        "iss",
        "2026-01-01T00:00:00Z",
        "2027-01-01T00:00:00Z",
        features=(CURB_POLICY, CURB_FLEET, CURB_ALERTS, CURB_ATTRIBUTION),
    )
    return curb_team.Device(
        device_id=identity.device_id(),
        organization_id=ORG,
        issuer_keyring=ISSUER_RING,
        claims=claims,
        offered=tuple(offered),
        sign=identity.sign,
        version="0.16.0",
    )


@pytest.fixture
def plane():
    return Plane()


def run(plane, box, offered=curb_wire.CAPABILITIES):  # noqa: F811 - box is the fixture's value
    return curb_team.cycle(
        plane,
        device(offered),
        lambda: reports(box),
        home=box.home,
        env={},
        platform="linux",
        now=NOW,
    )


def test_delivery_applies_under_the_delegation_and_reports(plane, box):  # noqa: F811
    curb_policy.delegate(True)
    plane.policies = [policy(1)]
    outcome = run(plane, box)
    assert outcome.problems == []
    assert "a new org policy arrived" in outcome.said
    settings = json.loads((box.claude / "settings.json").read_text(encoding="utf-8"))
    assert settings["sandbox"]["enabled"] is True
    state = plane.states[-1]
    assert (state["received"], state["applied"], state["delegation"], state["drift"]) == (
        1,
        1,
        True,
        False,
    )
    assert plane.reports[-1]["sequence"] == 1
    assert plane.reports[-1]["policy"]["compliance_hash"] == state["compliance_hash"]


def test_a_redelivered_current_policy_is_a_no_op_with_no_alert(plane, box):  # noqa: F811
    curb_policy.delegate(True)
    plane.policies = [policy(1)]
    run(plane, box)
    plane.always_send = True
    outcome = run(plane, box)
    assert "the org policy is unchanged" in outcome.said
    assert outcome.alerts == 0 and plane.delivered == []
    assert [r["sequence"] for r in plane.reports] == [1, 2]


def test_the_same_version_with_another_hash_is_an_integrity_alert(plane, box):  # noqa: F811
    plane.policies = [policy(1)]
    run(plane, box)
    plane.policies = [policy(1, {"web": "off"})]
    outcome = run(plane, box)
    assert any("different contents" in p for p in outcome.problems)
    assert curb_policy.load().received.token == policy(1)
    assert plane.states[-1]["rejected_outcome"] == curb_policy.INTEGRITY
    assert len(plane.delivered) == 1


def test_an_older_policy_is_refused_and_alerted(plane, box):  # noqa: F811
    first = policy(1)
    plane.policies = [first, policy(2, previous=hash_of(first))]
    run(plane, box)
    plane.policies = [first]
    run(plane, box)
    assert plane.states[-1]["rejected_outcome"] == curb_policy.ROLLBACK
    assert plane.states[-1]["received"] == 2


def test_an_older_authority_list_is_refused_and_alerted(plane, box):  # noqa: F811
    plane.authority = authority(version=2)
    run(plane, box)
    plane.authority = authority(version=1)
    outcome = run(plane, box)
    assert "an older policy authority list was refused" in outcome.problems
    assert curb_policy.authority(ISSUER_RING)["version"] == 2
    assert len(plane.delivered) == 1


def test_a_replayed_report_is_refused_and_dropped(plane, box):  # noqa: F811
    run(plane, box)
    plane.last_sequence[identity.device_id()] = 99  # as if a later report had been accepted
    outcome = run(plane, box)
    assert outcome.problems == []
    assert curb_store.read_state("fleet-outbox")["reports"] == []


def test_an_alert_retried_after_a_failure_is_delivered_once(plane, box):  # noqa: F811
    run(plane, box)
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
        device_id=identity.device_id(),
        now=0,
    )
    plane.refuse["alerts"] = Refused("throttled")
    outcome = run(plane, box)
    assert any("alerts are queued" in p for p in outcome.problems)
    waiting = curb_store.read_state("alerts-outbox")["alerts"]
    curb_alerts.settle([], [], now=0)
    for alert in waiting:
        alert["next"] = 0
    curb_store.write_state("alerts-outbox", {"alerts": waiting})
    run(plane, box)
    plane.call("alerts", {"alerts": waiting})  # the same ids again, as after a lost answer
    assert plane.delivered == [waiting[0]["event_id"]]


def test_without_the_servers_capabilities_nothing_is_sent(plane, box):  # noqa: F811
    plane.policies = [policy(1)]
    run(plane, box, offered=())
    assert plane.calls == []
    assert curb_store.read_state("alerts")  # the local half still ran


def test_a_client_below_the_minimum_says_so_and_keeps_its_policy(plane, box):  # noqa: F811
    plane.policies = [policy(1)]
    run(plane, box)
    plane.refuse["policy"] = Refused(
        "client_too_old", "too old (update flanner to 0.17.0 or later)"
    )
    outcome = run(plane, box)
    assert any("update flanner to 0.17.0" in p for p in outcome.problems)
    assert curb_policy.load().received is not None


def test_managed_settings_delivered_later_show_as_drift_and_alert(plane, box):  # noqa: F811
    curb_policy.delegate(True)
    plane.policies = [policy(1)]
    run(plane, box)
    managed = curb_settings.managed_dir("linux", curb_settings.system_root())
    managed.mkdir(parents=True, exist_ok=True)
    (managed / "managed-settings.json").write_text(
        json.dumps({"sandbox": {"enabled": False}}), encoding="utf-8"
    )
    outcome = run(plane, box)
    assert plane.states[-1]["drift"] is True
    assert outcome.alerts == 1 and len(plane.delivered) == 1


def test_audit_records_go_straight_to_the_collector(plane, box):  # noqa: F811
    plane.policies = [
        policy(1, export={"url": "https://collector.example.com/ocsf", "format": "ocsf"})
    ]
    curb_log.append(
        {
            "kind": "tool",
            "agent": "claude",
            "session": "s",
            "event": "PostToolUse",
            "tool": "Read",
            "channel": "file_tools",
            "target": "file",
            "target_digest": "d" * 32,
            "program": None,
            "decision": "ran",
        }
    )
    run(plane, box)
    sent = plane.collected[-1]
    assert (sent.url, sent.token) == ("https://collector.example.com/ocsf", "tok")
    assert sent.records[0]["class_uid"] == 6003
    run(plane, box)
    assert len(plane.collected) == 1  # nothing new to send


def test_a_pass_is_due_after_six_hours():
    assert curb_team.due(now=1000)
    curb_store.write_state("team", {"last": 1000})
    assert not curb_team.due(now=1000 + 3600)
    assert curb_team.due(now=1000 + 6 * 3600)


def test_the_pass_registers_new_attribution_keys_and_refreshes_the_registry(plane, box):  # noqa: F811
    curb_attribution.create("claude")
    run(plane, box)
    (body,) = plane.registered
    assert body["agent"] == "claude" and body["proof"] and body["replaces"] == ""
    assert curb_attribution.keys()["keys"]["claude"]["registered"] is True
    assert curb_attribution.registry(ISSUER_RING)["version"] == 1
    run(plane, box)
    assert len(plane.registered) == 1  # registered once


def test_a_registry_that_drops_a_revocation_is_refused_and_alerted(plane, box):  # noqa: F811
    plane.registry = registry(1, [entry(A, "revoked")])
    run(plane, box)
    plane.registry = registry(2, [entry(A)])
    outcome = run(plane, box)
    assert any("drops a known revocation" in p for p in outcome.problems)
    assert curb_attribution.registry(ISSUER_RING)["version"] == 1
    assert len(plane.delivered) == 1
