"""Messages between team members: two devices, two people, one process.

Each person is a real device (`tests.test_peer.Device`): its own home, key
and catalog. Messages travel over the real peer HTTP transport to a real
server, so what is tested is what ships, short of iroh.
"""

from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta, timezone

import pytest

from flanner import artifacts, mesh_delivery, mesh_messages, peer, push, refusals, sync
from flanner import session as cache
from flanner.artifacts import canonical_bytes
from flanner.database import MeshDeliveryModel, MeshMessageModel
from flanner.entitlements import MESH_MESSAGES, ROSTER, TEAM_SYNC, verify_roster
from flanner.identity import sign
from flanner.mesh_messages import MessageError
from flanner.workflow import EDITOR, READER
from tests.test_peer import WORKSPACE, Device, an_entitlement, keyring_of, serve  # noqa: F401

MESSAGING = (TEAM_SYNC, MESH_MESSAGES)


def a_roster(issuer_key, people, *, retention=None, expires=timedelta(hours=1)):
    """The signed team list, naming each person, their handle and devices."""
    now = datetime.now(timezone.utc)
    fields = {
        "kind": ROSTER,
        "key_id": "sk_1",
        "organization_id": "org_1",
        "issued_at": now.isoformat().replace("+00:00", "Z"),
        "expires_at": (now + expires).isoformat().replace("+00:00", "Z"),
        "workspaces": {
            WORKSPACE: [
                {
                    "user_id": p.user,
                    "role": p.role,
                    "devices": [p.device_id],
                    "handle": p.user,
                    "name": p.user.title(),
                }
                for p in people
            ]
        },
    }
    if retention is not None:
        fields["message_retention_days"] = retention
    data = canonical_bytes(fields)
    return base64.urlsafe_b64encode(data).decode().rstrip("=") + "." + sign(data, issuer_key)


class Person(Device):
    """A device signed in as its own user, unlike the one-user peer tests."""

    def __init__(self, root, issuer_key, user, role=EDITOR):
        super().__init__(root, issuer_key, [])
        self.user = user
        self.role = role

    def join(self, people, *, features=MESSAGING, roster=None):
        keys = {p.device_id: p.public_key for p in people}
        with self.active():
            cache.save(
                cache.Session(
                    endpoint="https://api.example.test",
                    device_id=self.device_id,
                    organization_id="org_1",
                    user_id=self.user,
                    entitlement=an_entitlement(
                        self.issuer_key,
                        device_id=self.device_id,
                        role=self.role,
                        user=self.user,
                        features=features,
                    ),
                    keyring=keyring_of(self.issuer_key),
                    device_keys=keys,
                    roster=roster or a_roster(self.issuer_key, people),
                )
            )

    def send(self, **kw):
        kw.setdefault("confirm", True)
        with self.active():
            return mesh_delivery.send(self.session, **kw)


@pytest.fixture
def issuer_key():
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    return Ed25519PrivateKey.generate()


@pytest.fixture
def alice(tmp_path, issuer_key):
    return Person(tmp_path / "alice", issuer_key, "alice")


@pytest.fixture
def bob(tmp_path, issuer_key):
    return Person(tmp_path / "bob", issuer_key, "bob")


@pytest.fixture
def team(alice, bob):
    alice.join([alice, bob])
    bob.join([alice, bob])
    return alice, bob


@pytest.fixture
def online(serve):  # noqa: F811 - the fixture imported above
    """Serve people's peer apps, and a dialler that reaches them by device id."""
    addresses: dict[str, str] = {}

    def up(*people):
        for p in people:
            addresses[p.device_id] = serve(peer.create_peer_app(p.sessions, p.held))

    def dial(device_id, workspace_id, held):
        if device_id not in addresses:
            raise peer.PeerError(f"could not reach {device_id}")
        return peer.RemotePeer(addresses[device_id], workspace_id, held)

    up.dial = dial
    return up


# --- sending and receiving -----------------------------------------------------


def test_a_preview_sends_nothing(team, online):
    alice, bob = team
    online(bob)

    preview = alice.send(to=["@bob"], body="drop the old column now?", confirm=False)

    assert preview["preview"] is True
    assert preview["to"] == [{"user_id": "bob", "handle": "bob", "name": "Bob"}]
    assert alice.session.query(MeshMessageModel).count() == 0
    assert bob.session.query(MeshMessageModel).count() == 0


def test_a_message_arrives_and_is_acknowledged(team, online):
    alice, bob = team
    online(bob)

    sent = alice.send(to=["bob"], body="drop the old column now?", dial=online.dial)

    assert [d["state"] for d in sent["delivery"]] == ["delivered"]
    with bob.active():
        inbox = mesh_messages.inbox(bob.session, me="bob", person=lambda u: u)
    assert inbox["unread"] == 1
    (thread,) = inbox["threads"]
    assert thread["last"]["preview"] == "drop the old column now?"
    assert thread["last"]["from"] == "alice"


def test_reading_a_thread_marks_it_read_and_a_reply_goes_back(team, online):
    alice, bob = team
    online(alice, bob)
    sent = alice.send(to=["bob"], body="now or next release?", dial=online.dial)

    view = mesh_messages.thread(
        bob.session, sent["thread_id"], me="bob", person=lambda u: u, retention_days=90
    )
    assert view["thread"]["messages"][0]["body"] == "now or next release?"
    assert mesh_messages.unread_count(bob.session) == 0

    reply = bob.send(thread=view["thread"]["short"], body="next release", dial=online.dial)

    assert reply["thread_id"] == sent["thread_id"]
    (message,) = alice.session.query(MeshMessageModel).filter_by(outgoing=False).all()
    assert (message.body, message.thread_id) == ("next release", sent["thread_id"])


def test_an_offline_device_queues_then_delivers_on_retry(team, online):
    alice, bob = team

    sent = alice.send(to=["bob"], body="are you there?", dial=online.dial)
    assert [d["state"] for d in sent["delivery"]] == ["queued"]

    online(bob)
    row = alice.session.query(MeshDeliveryModel).one()
    row.next_attempt_at = mesh_messages.now_utc() - timedelta(seconds=1)
    alice.session.commit()
    with alice.active():
        assert mesh_delivery.retry_due(alice.session, dial=online.dial) == 1

    (after,) = mesh_messages.delivery_by_person(alice.session, sent["message_id"])
    assert after["state"] == "delivered"
    assert bob.session.query(MeshMessageModel).count() == 1


def test_a_retry_of_a_message_already_held_counts_as_delivered(team, online):
    """What makes a slow first attempt safe to try again."""
    alice, bob = team
    online(bob)
    sent = alice.send(to=["bob"], body="hello", dial=online.dial)
    row = alice.session.query(MeshDeliveryModel).one()
    row.state = mesh_messages.QUEUED
    row.next_attempt_at = mesh_messages.now_utc() - timedelta(seconds=1)
    alice.session.commit()

    with alice.active():
        mesh_delivery.retry_due(alice.session, dial=online.dial)

    assert mesh_messages.delivery_by_person(alice.session, sent["message_id"])[0]["state"] == (
        "delivered"
    )
    assert bob.session.query(MeshMessageModel).count() == 1


def test_a_device_whose_organization_switched_messaging_off_refuses(team, online):
    alice, bob = team
    bob.join([alice, bob], features=(TEAM_SYNC,))
    online(bob)

    sent = alice.send(to=["bob"], body="hello", dial=online.dial)

    (delivery,) = sent["delivery"]
    assert (delivery["state"], delivery["code"]) == ("failed", refusals.MESSAGING_OFF)


def test_sending_needs_messaging_on_for_this_device(team):
    alice, bob = team
    alice.join([alice, bob], features=(TEAM_SYNC,))

    with pytest.raises(MessageError) as refused:
        alice.send(to=["bob"], body="hello")
    assert refused.value.code == refusals.MESSAGING_OFF


def test_sending_needs_a_current_roster(team, issuer_key):
    alice, bob = team
    stale = a_roster(issuer_key, [alice, bob], expires=-timedelta(minutes=5))
    alice.join([alice, bob], roster=stale)

    with pytest.raises(MessageError) as refused:
        alice.send(to=["bob"], body="hello")
    assert refused.value.code == refusals.ROSTER_STALE


def test_an_unknown_handle_is_refused_with_close_matches(team):
    alice, _ = team
    with pytest.raises(MessageError) as refused:
        alice.send(to=["bobb"], body="hello", confirm=False)
    assert refused.value.code == refusals.MEMBER_UNKNOWN
    assert refused.value.extra["matches"] == ["@bob"]


def test_a_reader_cannot_send(tmp_path, issuer_key):
    alice = Person(tmp_path / "a", issuer_key, "alice", role=READER)
    bob = Person(tmp_path / "b", issuer_key, "bob")
    alice.join([alice, bob])

    with pytest.raises(MessageError) as refused:
        alice.send(to=["bob"], body="hello", confirm=False)
    assert refused.value.code == refusals.NO_GRANT


def test_a_reader_is_refused_on_receipt_too(team):
    """The sender is another machine; its checks are its own business."""
    alice, bob = team
    envelope, payload = mesh_messages.compose(
        workspace_id=WORKSPACE,
        to=["bob"],
        body="hi",
        refs=[],
        thread_id=None,
        user_id="alice",
        organization_id="org_1",
        signing_key=alice.signing_key(),
    )
    with bob.active():
        current = cache.load()
    with pytest.raises(MessageError) as refused:
        mesh_messages.receive(
            bob.session,
            envelope=envelope.to_dict(),
            payload=payload,
            caller=mesh_messages.Caller(alice.device_id, "alice", READER),
            workspace_id=WORKSPACE,
            me="bob",
            roster=verify_roster(current.roster, current.keyring),
            public_key=alice.public_key,
        )
    assert refused.value.code == refusals.NO_GRANT


def test_a_workspace_message_reaches_everyone_else_in_it(tmp_path, issuer_key, online):
    people = [Person(tmp_path / n, issuer_key, n) for n in ("alice", "bob", "carol")]
    for p in people:
        p.join(people)
    alice, bob, carol = people
    online(bob, carol)

    sent = alice.send(workspace=WORKSPACE, body="deploying in ten", dial=online.dial)

    assert sorted(d["user_id"] for d in sent["delivery"]) == ["bob", "carol"]
    assert all(d["state"] == "delivered" for d in sent["delivery"])
    assert carol.session.query(MeshMessageModel).one().body == "deploying in ten"


# --- the receiving device's own checks -----------------------------------------


def received(bob, alice, *, to, body="hi", sent_at=None, roster=None):
    """Push one message signed by alice straight into bob's `receive`."""
    envelope, payload = mesh_messages.compose(
        workspace_id=WORKSPACE,
        to=to,
        body=body,
        refs=[],
        thread_id=None,
        user_id="alice",
        organization_id="org_1",
        sent_at=sent_at,
        signing_key=alice.signing_key(),
    )
    with bob.active():
        current = cache.load()
    return mesh_messages.receive(
        bob.session,
        envelope=envelope.to_dict(),
        payload=payload,
        caller=mesh_messages.Caller(alice.device_id, "alice", EDITOR),
        workspace_id=WORKSPACE,
        me="bob",
        roster=roster or verify_roster(current.roster, current.keyring),
        public_key=alice.public_key,
    )


def test_a_message_for_someone_else_is_refused(team):
    alice, bob = team
    with pytest.raises(MessageError) as refused:
        received(bob, alice, to=["carol"])
    assert refused.value.code == refusals.NOT_A_RECIPIENT


def test_a_message_older_than_retention_is_refused(team, issuer_key):
    alice, bob = team
    roster = verify_roster(
        a_roster(issuer_key, [alice, bob], retention=30), keyring_of(issuer_key)
    )
    with pytest.raises(MessageError) as refused:
        received(
            bob,
            alice,
            to=["bob"],
            sent_at=mesh_messages.now_utc() - timedelta(days=31),
            roster=roster,
        )
    assert refused.value.code == refusals.MESSAGE_EXPIRED


def test_twenty_one_messages_in_a_minute_is_one_too_many(team):
    alice, bob = team
    for n in range(mesh_messages.PER_PERSON_PER_MINUTE):
        received(bob, alice, to=["bob"], body=f"message {n}")
    with pytest.raises(MessageError) as refused:
        received(bob, alice, to=["bob"], body="one more")
    assert refused.value.code == refusals.THROTTLED


def test_the_same_message_twice_is_held_once(team):
    alice, bob = team
    envelope, payload = mesh_messages.compose(
        workspace_id=WORKSPACE,
        to=["bob"],
        body="hi",
        refs=[],
        thread_id=None,
        user_id="alice",
        organization_id="org_1",
        signing_key=alice.signing_key(),
    )
    with bob.active():
        current = cache.load()
    roster = verify_roster(current.roster, current.keyring)
    args = dict(
        envelope=envelope.to_dict(),
        payload=payload,
        caller=mesh_messages.Caller(alice.device_id, "alice", EDITOR),
        workspace_id=WORKSPACE,
        me="bob",
        roster=roster,
        public_key=alice.public_key,
    )
    assert mesh_messages.receive(bob.session, **args) == "accepted"
    assert mesh_messages.receive(bob.session, **args) == "already_held"


def test_a_message_never_travels_by_manifest_sync(team):
    """Stored in `artifacts` it would reach every teammate, not only bob."""
    alice, bob = team
    envelope, payload = mesh_messages.compose(
        workspace_id=WORKSPACE,
        to=["bob"],
        body="hi",
        refs=[],
        thread_id=None,
        user_id="alice",
        organization_id="org_1",
        signing_key=alice.signing_key(),
    )
    report = push.accept(
        bob.session,
        [{"envelope": envelope.to_dict(), "payload": payload.decode()}],
        workspace_id=WORKSPACE,
        role=EDITOR,
        resolve_key={alice.device_id: alice.public_key}.get,
    )
    assert report.rejected and not report.accepted
    verdict = sync.ingest_artifact(
        bob.session, envelope.to_dict(), payload, {alice.device_id: alice.public_key}.get
    )
    assert not verdict


# --- limits --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("body", "code"),
    [
        ("x" * 4097, refusals.BODY_TOO_LARGE),
        ("look \x1b[2J here", refusals.BODY_INVALID),
        ("invoice‮gpj.exe", refusals.BODY_INVALID),
        ("   ", refusals.MALFORMED),
    ],
)
def test_a_body_that_breaks_the_rules_is_refused(body, code):
    with pytest.raises(MessageError) as refused:
        mesh_messages.check_body(body)
    assert refused.value.code == code


def test_newlines_and_tabs_are_fine():
    assert mesh_messages.check_body("one\n\ttwo") == "one\n\ttwo"


def test_twenty_one_named_recipients_is_too_many():
    with pytest.raises(MessageError) as refused:
        mesh_messages.check_recipients([f"u{n}" for n in range(21)])
    assert refused.value.code == refusals.TOO_MANY_RECIPIENTS


def test_six_plan_references_is_too_many():
    with pytest.raises(MessageError) as refused:
        mesh_messages.check_refs([{"kind": "plan", "id": str(n)} for n in range(6)])
    assert refused.value.code == refusals.TOO_MANY_REFS


# --- delivery bookkeeping -----------------------------------------------------


def test_a_refusal_that_retrying_cannot_fix_fails_at_once(team):
    alice, _ = team
    row = MeshDeliveryModel(message_id="m", user_id="bob", device_id="d", state="queued")
    alice.session.add(row)
    alice.session.commit()

    mesh_messages.not_delivered(alice.session, row, code=refusals.NOT_A_RECIPIENT, detail="no")

    assert row.state == "failed"


def test_an_unreachable_device_waits_one_then_five_minutes(team):
    alice, _ = team
    row = MeshDeliveryModel(message_id="m", user_id="bob", device_id="d", state="queued")
    alice.session.add(row)
    alice.session.commit()
    now = mesh_messages.now_utc()

    mesh_messages.not_delivered(alice.session, row, code=refusals.UNKNOWN, detail="", now=now)
    assert row.next_attempt_at == now + timedelta(minutes=1)
    mesh_messages.not_delivered(alice.session, row, code=refusals.UNKNOWN, detail="", now=now)
    assert row.next_attempt_at == now + timedelta(minutes=5)


def test_a_queued_message_fails_after_a_day(team):
    alice, _ = team
    old = mesh_messages.now_utc() - timedelta(hours=25)
    alice.session.add(
        MeshDeliveryModel(
            message_id="m",
            user_id="bob",
            device_id="d",
            state="queued",
            queued_at=old,
            next_attempt_at=old,
        )
    )
    alice.session.commit()

    assert mesh_messages.due(alice.session) == []
    assert alice.session.query(MeshDeliveryModel).one().state == "failed"


# --- retention, ids, mutes and quiet hours -------------------------------------


def test_expiry_deletes_old_messages_and_their_rows(team, online):
    alice, bob = team
    online(bob)
    sent = alice.send(to=["bob"], body="old news", dial=online.dial)
    row = alice.session.get(MeshMessageModel, sent["message_id"])
    row.sent_at = mesh_messages.now_utc() - timedelta(days=31)
    alice.session.commit()

    assert mesh_messages.expire(alice.session, 30) == 1
    assert alice.session.query(MeshDeliveryModel).count() == 0


def test_short_ids_grow_until_they_are_unique():
    ids = ["sha256:abcd1111", "sha256:abcd2222", "sha256:ffff0000"]
    assert mesh_messages.short_ids(ids) == {
        "sha256:abcd1111": "abcd1",
        "sha256:abcd2222": "abcd2",
        "sha256:ffff0000": "ffff",
    }


def test_an_ambiguous_prefix_is_refused(team):
    alice, _ = team
    for suffix in ("1111", "2222"):
        alice.session.add(
            MeshMessageModel(
                message_id=f"sha256:abcd{suffix}",
                thread_id=f"sha256:abcd{suffix}",
                workspace_id=WORKSPACE,
                author_user_id="bob",
                author_device_id="d",
                recipients="[]",
                body="x",
                sent_at=mesh_messages.now_utc(),
                envelope="{}",
                payload="{}",
            )
        )
    alice.session.commit()

    with pytest.raises(MessageError) as refused:
        mesh_messages.find_thread(alice.session, "abcd")
    assert refused.value.code == refusals.AMBIGUOUS_ID
    assert mesh_messages.find_thread(alice.session, "abcd1") == "sha256:abcd1111"


def test_a_muted_sender_is_listed_until_the_mute_ends(team):
    alice, _ = team
    soon = mesh_messages.now_utc() + timedelta(hours=1)
    mesh_messages.mute(alice.session, "bob", until=soon)
    assert "bob" in mesh_messages.muted(alice.session)
    assert "bob" not in mesh_messages.muted(alice.session, now=soon + timedelta(seconds=1))
    mesh_messages.mute(alice.session, "bob", off=True)
    assert mesh_messages.muted(alice.session) == {}


def test_quiet_hours_cross_midnight(team):
    alice, _ = team
    with alice.active():
        mesh_messages.set_quiet_hours("22:00-07:00")
        late = datetime(2026, 9, 21, 23, 30).astimezone()
        noon = datetime(2026, 9, 21, 12, 0).astimezone()
        assert mesh_messages.quiet_hours(now=late)["active"] is True
        assert mesh_messages.quiet_hours(now=noon)["active"] is False
        assert mesh_messages.set_quiet_hours("off")["enabled"] is False


@pytest.mark.parametrize("spec", ["22-07", "22:00-22:00", "25:00-07:00", "later"])
def test_quiet_hours_that_make_no_sense_are_refused(team, spec):
    alice, _ = team
    with alice.active(), pytest.raises(MessageError) as refused:
        mesh_messages.set_quiet_hours(spec)
    assert refused.value.code == refusals.MALFORMED


def test_the_message_type_has_a_push_rule():
    assert push.may_send(artifacts.MESH_MESSAGE, EDITOR)
    assert not push.may_send(artifacts.MESH_MESSAGE, READER)
    assert json.loads(canonical_bytes({"a": 1})) == {"a": 1}
