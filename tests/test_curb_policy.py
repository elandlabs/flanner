"""Org policy on a device (Curb PRD §10.8): trust, check-in outcomes, the apply step, drift.

Covers the R5 client criteria that need no second machine: every row of
the check-in table, authority lists and key rotation, revocation, expiry,
the delegation, the mixed-allowlist trap, a change Curb cannot establish,
drift after a local edit, and two devices meeting one policy sharing a hash.
"""

import base64
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from flanner import (
    curb_approval,
    curb_fix,
    curb_policy,
    curb_reach,
    curb_settings,
    curb_store,
    curb_wire,
    identity,
)
from flanner.curb_approval import Broker
from flanner.curb_context import BASELINE, default

NOW = datetime(2026, 10, 2, 12, tzinfo=timezone.utc)
ISSUER = Ed25519PrivateKey.from_private_bytes(bytes([11]) * 32)
CURRENT = Ed25519PrivateKey.from_private_bytes(bytes([12]) * 32)
NEXT = Ed25519PrivateKey.from_private_bytes(bytes([13]) * 32)
ORG = "org_test"
TIGHTEN = {"deny_read": ["~/.aws"], "sandbox": "required", "web": "off"}


def pub(key):
    return identity.public_key_b64(key.public_key())


ISSUER_RING = {"iss": pub(ISSUER)}


def stamp(moment):
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def signer(key):
    return lambda payload: base64.b64encode(key.sign(payload)).decode("ascii")


def entry(
    key_id, key, status="active", start=NOW - timedelta(days=30), end=NOW + timedelta(days=365)
):
    return {
        "key_id": key_id,
        "public_key": pub(key),
        "not_before": stamp(start),
        "not_after": stamp(end),
        "status": status,
    }


def authority(version=1, keys=None, expires=NOW + timedelta(days=365)):
    fields = {
        "kind": curb_wire.AUTHORITY,
        "key_id": "iss",
        "version": version,
        "issued_at": stamp(NOW - timedelta(days=1)),
        "expires_at": stamp(expires),
        "keys": keys if keys is not None else [entry("pol_cur", CURRENT)],
    }
    return curb_wire.encode(fields, signer(ISSUER))


def policy(
    version,
    rules=None,
    *,
    key=("pol_cur", CURRENT),
    org=ORG,
    previous="",
    expires=NOW + timedelta(days=30),
    export=None,
):
    fields = {
        "kind": curb_wire.POLICY,
        "key_id": key[0],
        "organization_id": org,
        "version": version,
        "issued_at": stamp(NOW - timedelta(hours=1)),
        "expires_at": stamp(expires),
        "previous_hash": previous,
        "rules": TIGHTEN if rules is None else rules,
    }
    if export:
        fields["audit_export"] = export
    return curb_wire.encode(fields, signer(key[1]))


def hash_of(token):
    return curb_wire.doc_hash(curb_wire.fields_of(token))


def receive(token, now=NOW):
    return curb_policy.receive(
        {"status": "policy", "policy": token},
        issuer_keyring=ISSUER_RING,
        organization_id=ORG,
        now=now,
    )


@pytest.fixture
def box(tmp_path, monkeypatch):
    found = SimpleNamespace(
        home=Path.home(),
        claude=tmp_path / "claude-config",
        codex=tmp_path / "codex-home",
        project=tmp_path / "project",
    )
    for folder in (found.claude, found.codex, found.project):
        folder.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(found.claude))
    monkeypatch.setenv("CODEX_HOME", str(found.codex))
    monkeypatch.setattr(curb_approval, "process_chain", lambda: [])
    return found


def reports(box, *agents, platform="linux", version=None):
    out = []
    for agent in agents or ("claude",):
        context = default(agent, box.project)
        out.append(
            curb_reach.assess(
                context,
                curb_settings.resolve(context, platform=platform),
                [],
                platform=platform,
                home=box.home,
                env={},
                version=version or BASELINE[agent],
            )
        )
    return out


def claude_settings(box):
    path = box.claude / "settings.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}


def accept_authority(token=None):
    return curb_policy.accept_authority(token or authority(), ISSUER_RING)


# --- the policy authority list --------------------------------------------------------------


def test_an_authority_list_is_cached_and_an_older_one_refused():
    listing, problem = accept_authority(authority(version=2))
    assert problem == "" and listing["version"] == 2
    held, problem = accept_authority(authority(version=1))
    assert held["version"] == 2 and "older" in problem
    assert curb_policy.authority(ISSUER_RING)["version"] == 2


def test_an_authority_list_changed_without_a_new_version_is_refused():
    accept_authority(authority(version=2))
    _, problem = accept_authority(authority(version=2, keys=[entry("pol_cur", NEXT)]))
    assert "without a new version" in problem


def test_an_authority_list_not_signed_by_the_issuer_is_refused():
    fields = curb_wire.fields_of(authority())
    forged = curb_wire.encode(fields, signer(CURRENT))
    held, problem = curb_policy.accept_authority(forged, ISSUER_RING)
    assert held is None and "does not verify" in problem


# --- check-in outcomes (PRD §10.8) ---------------------------------------------------------


def judge(token, received=None, now=NOW, listing=None):
    return curb_policy.judge(
        token,
        listing=listing if listing is not None else curb_wire.fields_of(authority()),
        organization_id=ORG,
        received=received,
        now=now,
    )


def held(token):
    return curb_policy.Policy(token, curb_wire.fields_of(token))


def test_a_higher_valid_version_goes_to_the_apply_step():
    first = policy(1)
    assert judge(first)[0] == curb_policy.APPLY
    assert judge(policy(2, previous=hash_of(first)), received=held(first))[0] == curb_policy.APPLY
    assert (
        judge(policy(5, previous="sha256:skipped"), received=held(first))[0] == curb_policy.APPLY
    )


def test_the_same_version_and_hash_is_a_normal_check_in():
    first = policy(1)
    assert judge(first, received=held(first))[0] == curb_policy.UNCHANGED


def test_the_same_version_with_other_contents_is_an_integrity_error():
    first = policy(1)
    outcome, kept, reason = judge(policy(1, {"web": "off"}), received=held(first))
    assert outcome == curb_policy.INTEGRITY and kept is None and "different contents" in reason


def test_a_successor_that_does_not_chain_is_an_integrity_error():
    first = policy(1)
    outcome, _, reason = judge(policy(2, previous="sha256:other"), received=held(first))
    assert outcome == curb_policy.INTEGRITY and "does not follow" in reason


def test_a_lower_version_is_a_rollback():
    newer = policy(3)
    outcome, _, reason = judge(policy(2), received=held(newer))
    assert outcome == curb_policy.ROLLBACK and "older" in reason


@pytest.mark.parametrize(
    ("token", "why"),
    [
        (policy(1, key=("pol_cur", NEXT)), "does not verify"),
        (policy(1, key=("pol_unknown", NEXT)), "does not verify"),
        (policy(1, org="org_other"), "another organization"),
        (policy(1, expires=NOW - timedelta(minutes=1)), "expired"),
    ],
)
def test_bad_signature_unknown_key_other_org_or_expired_is_refused(token, why):
    outcome, kept, reason = judge(token)
    assert outcome == curb_policy.REJECTED and kept is None and why in reason


def test_a_policy_signed_by_a_revoked_key_is_refused():
    listing = curb_wire.fields_of(authority(keys=[entry("pol_cur", CURRENT, "revoked")]))
    outcome, _, reason = judge(policy(1), listing=listing)
    assert outcome == curb_policy.REJECTED and "revoked" in reason


def test_rotation_accepts_the_retiring_key_until_its_window_ends():
    keys = [
        entry("pol_new", NEXT),
        entry("pol_cur", CURRENT, "retiring", end=NOW + timedelta(days=10)),
    ]
    listing = curb_wire.fields_of(authority(version=2, keys=keys))
    assert judge(policy(1), listing=listing)[0] == curb_policy.APPLY
    assert judge(policy(1, key=("pol_new", NEXT)), listing=listing)[0] == curb_policy.APPLY
    later = NOW + timedelta(days=11)
    assert judge(policy(1), listing=listing, now=later)[0] == curb_policy.REJECTED


def test_no_new_policy_is_accepted_under_an_expired_authority_list():
    listing = curb_wire.fields_of(authority(expires=NOW - timedelta(days=1)))
    outcome, _, reason = judge(policy(1), listing=listing)
    assert outcome == curb_policy.REJECTED and "authority list has expired" in reason


def test_a_refused_policy_leaves_the_one_in_force():
    accept_authority()
    first = policy(1, export={"url": "https://collector.example.com/ocsf", "format": "ocsf"})
    outcome, _ = curb_policy.receive(
        {"status": "policy", "policy": first, "audit_export_token": "tok"},
        issuer_keyring=ISSUER_RING,
        organization_id=ORG,
        now=NOW,
    )
    assert outcome == curb_policy.APPLY
    assert curb_store.read_secret("export") == "tok"
    assert receive(policy(1, {"web": "off"}))[0] == curb_policy.INTEGRITY
    state = curb_policy.load()
    assert state.received.token == first
    assert state.rejected["outcome"] == curb_policy.INTEGRITY
    assert receive(first)[0] == curb_policy.UNCHANGED
    assert (
        curb_policy.receive(
            {"status": "unchanged"}, issuer_keyring=ISSUER_RING, organization_id=ORG, now=NOW
        )[0]
        == "unchanged"
    )


def test_expiry_and_a_later_revocation_keep_the_policy_in_force_flagged():
    accept_authority()
    receive(policy(1))
    state = curb_policy.load()
    listing = curb_policy.authority(ISSUER_RING)
    assert curb_policy.flags(state, listing, NOW) == []
    assert curb_policy.flags(state, listing, NOW + timedelta(days=31)) == ["expired"]
    accept_authority(authority(version=2, keys=[entry("pol_cur", CURRENT, "revoked")]))
    listing = curb_policy.authority(ISSUER_RING)
    assert curb_policy.flags(curb_policy.load(), listing, NOW) == ["revoked_key"]
    assert curb_policy.load().received is not None  # still in force


def test_a_tampered_stored_policy_is_flagged():
    accept_authority()
    receive(policy(1))
    state = curb_policy.load()
    fields = dict(state.received.fields, rules={})
    state.received = curb_policy.Policy(curb_wire.encode(fields, signer(NEXT)), fields)
    curb_policy.save(state)
    flagged = curb_policy.flags(curb_policy.load(), curb_policy.authority(ISSUER_RING), NOW)
    assert "unverified" in flagged


# --- the apply step --------------------------------------------------------------------------


def arrive(token):
    accept_authority()
    assert receive(token)[0] == curb_policy.APPLY


def test_a_tighten_only_policy_applies_under_the_delegation(box):
    curb_policy.delegate(True)
    arrive(policy(1))
    change = curb_policy.apply_received(reports(box), home=box.home, platform="linux", env={})
    assert change.written and not change.held
    data = claude_settings(box)
    assert "Read(~/.aws/**)" in data["permissions"]["deny"]
    assert data["sandbox"]["enabled"] is True and "WebFetch" in data["permissions"]["deny"]
    state = curb_policy.load()
    assert state.applied["by"] == "delegation" and state.pending is None
    assert curb_fix.latest() is not None  # backed up first
    assert (
        curb_policy.apply_received(reports(box), home=box.home, platform="linux", env={}) is None
    )


def test_without_the_delegation_every_change_waits(box):
    arrive(policy(1))
    change = curb_policy.apply_received(reports(box), home=box.home, platform="linux", env={})
    assert change.ready and not change.written
    assert claude_settings(box) == {}
    pending = curb_policy.load().pending
    assert pending["version"] == 1 and "delegation is off" in pending["reasons"][0]


def test_a_withdrawn_delegation_leaves_every_change_pending(box):
    curb_policy.delegate(True)
    curb_policy.delegate(False)
    arrive(policy(1))
    curb_policy.apply_received(reports(box), home=box.home, platform="linux", env={})
    assert curb_policy.load().pending is not None and claude_settings(box) == {}


def test_removing_the_last_server_command_from_a_mixed_allowlist_waits(box):
    (box.claude / "settings.json").write_text(
        json.dumps(
            {"allowedMcpServers": [{"serverName": "docs"}, {"serverCommand": ["other-mcp"]}]}
        ),
        encoding="utf-8",
    )
    curb_policy.delegate(True)
    arrive(policy(1, {"mcp": {"allowed": [{"name": "docs"}]}}))
    change = curb_policy.apply_received(reports(box), home=box.home, platform="linux", env={})
    assert change.held and not change.written
    assert any("MCP server could load" in r for r in change.reasons)
    assert claude_settings(box)["allowedMcpServers"][1] == {"serverCommand": ["other-mcp"]}


def test_a_change_curb_cannot_establish_waits(box):
    curb_policy.delegate(True)
    arrive(policy(1))
    untested = reports(box, version="9.9.9")
    change = curb_policy.apply_received(untested, home=box.home, platform="linux", env={})
    assert change.held and not change.written
    assert "cannot establish" in change.reasons[0]


def test_the_person_approves_the_held_change_with_a_grant(box):
    arrive(policy(1))
    curb_policy.apply_received(reports(box), home=box.home, platform="linux", env={})
    state = curb_policy.load()
    change = curb_policy.plan(
        state.received, reports(box), home=box.home, platform="linux", env={}
    )
    planned = curb_fix.Plan(change.edits)

    class Yes:
        name, weak = "test prompt", False

        def available(self):
            return True

        def confirm(self, reason):
            return True

    broker = Broker(Yes())
    grant = broker.request(planned.summary(), curb_approval.change_hash(planned.change()))
    curb_fix.apply(planned, broker, grant)
    curb_policy.approved()
    state = curb_policy.load()
    assert state.applied["by"] == "person" and state.pending is None
    assert "Read(~/.aws)" in claude_settings(box)["permissions"]["deny"]


# --- drift ------------------------------------------------------------------------------------


def test_a_local_edit_shows_as_drift_by_the_next_reconciliation(box):
    curb_policy.delegate(True)
    arrive(policy(1))
    curb_policy.apply_received(reports(box), home=box.home, platform="linux", env={})
    met = curb_policy.reconcile(reports(box), home=box.home, env={}, platform="linux")
    assert met == {"claude": []}
    matching = curb_policy.summary(curb_policy.load())
    assert matching["drift"] is False
    (box.claude / "settings.json").write_text(
        json.dumps({"sandbox": {"enabled": False}}), encoding="utf-8"
    )
    missing = curb_policy.reconcile(reports(box), home=box.home, env={}, platform="linux")
    assert "the sandbox is off or can be bypassed" in missing["claude"]
    assert any(m.startswith("denied path 1 is readable through") for m in missing["claude"])
    assert "web fetch or web search is on" in missing["claude"]
    drifted = curb_policy.summary(curb_policy.load())
    assert drifted["drift"] is True and drifted["compliance_hash"] != matching["compliance_hash"]


def test_two_devices_meeting_one_policy_share_a_compliance_hash(box):
    token = policy(1)
    accept_authority()
    receive(token)
    meets = curb_policy.compliance_hash(held(token), {"claude": [], "codex": []})
    assert meets == curb_policy.compliance_hash(held(token), {"claude": []})
    assert meets != curb_policy.compliance_hash(
        held(token), {"claude": ["web fetch or web search is on"]}
    )


def test_an_unknown_rule_is_unmet_never_ignored(box):
    arrive(policy(1, {"web": "off", "future_rule": {"x": 1}}))
    missing = curb_policy.reconcile(reports(box), home=box.home, env={}, platform="linux")
    assert any("does not know rule(s) future_rule" in m for m in missing["claude"])


def test_the_summary_names_no_path(box):
    curb_policy.delegate(True)
    arrive(policy(1))
    curb_policy.apply_received(reports(box), home=box.home, platform="linux", env={})
    curb_policy.reconcile(reports(box), home=box.home, env={}, platform="linux")
    text = json.dumps(curb_policy.summary(curb_policy.load(), ["expired"]))
    assert ".aws" not in text and str(box.home) not in text and "~" not in text
