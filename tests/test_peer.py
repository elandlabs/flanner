"""Two devices syncing over a real network.

Until now a "peer" was another session in the same process. These tests put
an HTTP server between the two, because the interesting failures are the
ones a shared object hides: what a stranger may ask for, and what happens to
an artifact whose author is a machine neither side has met.
"""

import base64
import os
import threading
from datetime import datetime, timedelta, timezone

import pytest
import uvicorn
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from flanner import artifacts, identity, peer, refusals, sync
from flanner import session as cache
from flanner.artifacts import canonical_bytes
from flanner.database import ArtifactModel, get_artifact, save_artifact
from flanner.device_auth import sign_request
from flanner.entitlements import (
    MEM_SYNC,
    ROSTER,
    SKILL_SYNC,
    TEAM_SYNC,
    Claims,
    WorkspaceCapability,
    encode_token,
)
from flanner.identity import public_key_b64, sign
from flanner.workflow import MAINTAINER, READER

WORKSPACE = "ws_core"


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("FLANNER_HOME", str(tmp_path / "home"))
    return tmp_path


@pytest.fixture
def issuer_key():
    """Stands in for the control plane's signing key."""
    return Ed25519PrivateKey.generate()


def keyring_of(issuer_key):
    return {"sk_1": public_key_b64(issuer_key.public_key())}


def an_entitlement(
    issuer_key,
    *,
    device_id,
    role=MAINTAINER,
    workspace=WORKSPACE,
    user="maria",
    features=(TEAM_SYNC,),
    expires=timedelta(hours=1),
):
    now = datetime.now(timezone.utc)
    claims = Claims(
        features=tuple(features),
        organization_id="org_1",
        user_id=user,
        device_id=device_id,
        key_id="sk_1",
        issued_at=now.isoformat().replace("+00:00", "Z"),
        expires_at=(now + expires).isoformat().replace("+00:00", "Z"),
        workspace_capabilities=(
            (WorkspaceCapability(workspace_id=workspace, role=role),) if role else ()
        ),
    )
    return encode_token(claims, sign(canonical_bytes(claims.to_dict()), issuer_key))


def a_roster(issuer_key, devices, *, user="maria", role=MAINTAINER, expires=timedelta(hours=1)):
    """The signed list of who is in the workspace, as the control plane sends it."""
    now = datetime.now(timezone.utc)
    fields = {
        "kind": ROSTER,
        "key_id": "sk_1",
        "organization_id": "org_1",
        "issued_at": now.isoformat().replace("+00:00", "Z"),
        "expires_at": (now + expires).isoformat().replace("+00:00", "Z"),
        "workspaces": {WORKSPACE: [{"user_id": user, "role": role, "devices": list(devices)}]},
    }
    data = canonical_bytes(fields)
    return base64.urlsafe_b64encode(data).decode().rstrip("=") + "." + sign(data, issuer_key)


def a_session(
    issuer_key,
    *,
    device_id,
    role=MAINTAINER,
    device_keys=None,
    features=(TEAM_SYNC,),
    roster="",
    expires=timedelta(hours=1),
):
    return cache.Session(
        endpoint="https://api.example.test",
        device_id=device_id,
        organization_id="org_1",
        user_id="maria",
        entitlement=an_entitlement(
            issuer_key, device_id=device_id, role=role, features=features, expires=expires
        ),
        keyring=keyring_of(issuer_key),
        device_keys=device_keys or {},
        roster=roster,
    )


# --- who may ask -----------------------------------------------------------


def a_peer_request(
    issuer_key, *, role=MAINTAINER, key=None, entitlement_for=None, features=(TEAM_SYNC,)
):
    signing = key or Ed25519PrivateKey.generate()
    device_id = identity.device_id_for(signing.public_key())
    return sign_request(
        {
            "workspace_id": WORKSPACE,
            "public_key": public_key_b64(signing.public_key()),
            "entitlement": an_entitlement(
                issuer_key,
                device_id=entitlement_for or device_id,
                role=role,
                features=features,
            ),
        },
        device_id=device_id,
        signing_key=signing,
    ).to_dict()


def test_a_signed_request_with_a_matching_entitlement_is_allowed(issuer_key):
    who = peer.authorize(a_peer_request(issuer_key), WORKSPACE, keyring_of(issuer_key))
    assert who.role == MAINTAINER
    assert who.user_id == "maria"


def test_a_plan_entitlement_alone_does_not_carry_memory(issuer_key):
    """Two separate purchases of trust, and the default is the narrower one.
    A device that predates memory sharing has no such flag, so it must not
    be read as holding one."""
    who = peer.authorize(a_peer_request(issuer_key), WORKSPACE, keyring_of(issuer_key))
    assert who.may_sync_memory is False


def test_memory_travels_only_when_the_entitlement_says_so(issuer_key):
    request = a_peer_request(issuer_key, features=(TEAM_SYNC, MEM_SYNC))

    who = peer.authorize(request, WORKSPACE, keyring_of(issuer_key))

    assert who.may_sync_memory is True


def test_a_plan_entitlement_alone_does_not_carry_skills(issuer_key):
    """Same default as memory, for the same reason. A device that predates
    skill sharing has no such flag and must not be read as holding one."""
    who = peer.authorize(a_peer_request(issuer_key), WORKSPACE, keyring_of(issuer_key))
    assert who.may_sync_skills is False


def test_skills_travel_only_when_the_entitlement_says_so(issuer_key):
    request = a_peer_request(issuer_key, features=(TEAM_SYNC, SKILL_SYNC))

    who = peer.authorize(request, WORKSPACE, keyring_of(issuer_key))

    assert who.may_sync_skills is True
    # Two purchases, read apart. Buying one must not imply the other.
    assert who.may_sync_memory is False


def test_the_public_key_must_hash_to_the_device_id_claimed(issuer_key):
    """This is what removes the need for a key registry to authenticate."""
    request = a_peer_request(issuer_key)
    request["body"]["public_key"] = public_key_b64(Ed25519PrivateKey.generate().public_key())
    with pytest.raises(peer.PeerError, match="does not match the device id"):
        peer.authorize(request, WORKSPACE, keyring_of(issuer_key))


def test_an_entitlement_issued_to_another_device_is_useless(issuer_key):
    """Otherwise a leaked entitlement would work as a bearer token."""
    request = a_peer_request(issuer_key, entitlement_for="dev_someone_else")
    with pytest.raises(peer.PeerError, match="issued to a different device"):
        peer.authorize(request, WORKSPACE, keyring_of(issuer_key))


def test_an_entitlement_from_an_unknown_issuer_is_refused(issuer_key):
    stranger = Ed25519PrivateKey.generate()
    with pytest.raises(peer.PeerError, match="entitlement is"):
        peer.authorize(a_peer_request(issuer_key), WORKSPACE, keyring_of(stranger))


def test_no_capability_in_this_workspace_means_no_access(issuer_key):
    with pytest.raises(peer.PeerError, match="no access to this workspace"):
        peer.authorize(a_peer_request(issuer_key, role=None), WORKSPACE, keyring_of(issuer_key))


def test_a_capability_elsewhere_does_not_open_this_workspace(issuer_key):
    with pytest.raises(peer.PeerError, match="no access"):
        peer.authorize(a_peer_request(issuer_key), "ws_other", keyring_of(issuer_key))


def test_a_reader_may_still_read(issuer_key):
    """Sync is a read. Roles gate review, not whether you receive plans."""
    who = peer.authorize(
        a_peer_request(issuer_key, role=READER), WORKSPACE, keyring_of(issuer_key)
    )
    assert who.role == READER


def test_a_garbled_request_is_refused_rather_than_crashing(issuer_key):
    with pytest.raises(peer.PeerError, match="malformed"):
        peer.authorize({"device_id": "dev_x"}, WORKSPACE, keyring_of(issuer_key))


def test_a_request_with_no_public_key_is_refused(issuer_key):
    request = a_peer_request(issuer_key)
    request["body"].pop("public_key")
    with pytest.raises(peer.PeerError, match="no public key"):
        peer.authorize(request, WORKSPACE, keyring_of(issuer_key))


def test_a_stale_request_is_refused(issuer_key):
    """The captured-request case, same rule the control plane applies."""
    key = Ed25519PrivateKey.generate()
    device_id = identity.device_id_for(key.public_key())
    request = sign_request(
        {
            "workspace_id": WORKSPACE,
            "public_key": public_key_b64(key.public_key()),
            "entitlement": an_entitlement(issuer_key, device_id=device_id),
        },
        device_id=device_id,
        signing_key=key,
        now=datetime.now(timezone.utc) - timedelta(hours=1),
    ).to_dict()

    with pytest.raises(peer.PeerError, match="out of date"):
        peer.authorize(request, WORKSPACE, keyring_of(issuer_key))


# --- two devices, one wire -------------------------------------------------


class _Home:
    """Point FLANNER_HOME at one device for the duration of a block."""

    def __init__(self, home):
        self._home = str(home)
        self._previous = None

    def __enter__(self):
        self._previous = os.environ.get("FLANNER_HOME")
        os.environ["FLANNER_HOME"] = self._home
        return self

    def __exit__(self, *exc):
        if self._previous is None:
            os.environ.pop("FLANNER_HOME", None)
        else:
            os.environ["FLANNER_HOME"] = self._previous


class Device:
    """One machine: its own home, its own catalog, its own key.

    Each device gets a private engine rather than the module-global one, so
    two catalogs can exist in one process without either standing in for the
    other. That is the whole point of these tests.

    ``team`` is every device in the test. Each one signs in holding a roster
    that names them all, because that is what a teammate's roster says.
    """

    def __init__(self, root, issuer_key, team=None):
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        from flanner.database import _apply_schema

        self.home = root
        self.home.mkdir(parents=True, exist_ok=True)
        self.issuer_key = issuer_key
        self.team = team if team is not None else []
        self.team.append(self)
        with self.active():
            self.device_id = identity.device_id()
            self.public_key = identity.device_public_key_b64()

        engine = create_engine(f"sqlite:///{self.home / 'data.db'}", future=True)
        _apply_schema(engine)
        self.sessions = sessionmaker(bind=engine, future=True)
        self.session = self.sessions()

    def active(self):
        """Make this the device that identity and session see."""
        return _Home(self.home)

    def sign_in(
        self,
        *,
        role=MAINTAINER,
        device_keys=None,
        features=(TEAM_SYNC,),
        roster=None,
        expires=timedelta(hours=1),
    ):
        if roster is None:
            roster = a_roster(self.issuer_key, [d.device_id for d in self.team])
        with self.active():
            cache.save(
                a_session(
                    self.issuer_key,
                    device_id=self.device_id,
                    role=role,
                    device_keys=device_keys or {self.device_id: self.public_key},
                    features=features,
                    roster=roster,
                    expires=expires,
                )
            )

    def signing_key(self):
        with self.active():
            return identity.load_or_create_device_key()

    def held(self):
        with self.active():
            return cache.load()

    def pull_from(self, address, workspace_id=WORKSPACE):
        """Pull as this device, with its own home and its own key."""
        with self.active():
            return peer.pull(self.session, address, workspace_id, self.held)

    def push_to(self, address, workspace_id=WORKSPACE):
        """Push as this device, with its own home and its own key."""
        with self.active():
            return peer.push(self.session, address, workspace_id, self.held)


@pytest.fixture
def serve():
    """Run a peer app on a real port, stopped when the test ends."""
    running = []

    def start(app):
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error"))
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        for _ in range(200):
            if server.started:
                break
            threading.Event().wait(0.05)
        else:
            pytest.fail("the peer server did not start")
        running.append((server, thread))
        return f"http://127.0.0.1:{server.servers[0].sockets[0].getsockname()[1]}"

    yield start

    for server, thread in running:
        server.should_exit = True
        thread.join(timeout=5)


@pytest.fixture
def team():
    return []


@pytest.fixture
def alice(tmp_path, issuer_key, team):
    return Device(tmp_path / "alice", issuer_key, team)


@pytest.fixture
def bob(tmp_path, issuer_key, team):
    return Device(tmp_path / "bob", issuer_key, team)


def link(alice, bob):
    """Both sign in already knowing each other's device key."""
    keys = {alice.device_id: alice.public_key, bob.device_id: bob.public_key}
    alice.sign_in(device_keys=keys)
    bob.sign_in(device_keys=keys)


def a_stored_artifact(session, *, key, workspace=WORKSPACE, body="# plan\n", artifact_type=None):
    """An artifact genuinely signed by a given device key."""
    artifact = artifacts.make_artifact(
        artifact_type=artifact_type or artifacts.REVIEW_PROPOSAL,
        workspace_id=workspace,
        content_hash=artifacts.hash_text(body),
        signing_key=key,
        actor_device_id=identity.device_id_for(key.public_key()),
    )
    save_artifact(
        session,
        artifact_id=artifact.artifact_id,
        artifact_type=artifact.artifact_type,
        workspace_id=artifact.workspace_id,
        content_hash=artifact.content_hash,
        actor_device_id=artifact.actor_device_id,
        created_at=artifact.created_at,
        signature=artifact.signature,
        payload=body,
    )
    session.commit()
    return artifact


def test_a_plan_crosses_between_two_devices(alice, bob, serve):
    """What this whole module exists for."""
    link(alice, bob)
    artifact = a_stored_artifact(alice.session, key=alice.signing_key())
    address = serve(peer.create_peer_app(alice.sessions, alice.held))

    report = bob.pull_from(address)

    assert report.ok, report.rejected
    assert report.accepted == [artifact.artifact_id]
    assert get_artifact(bob.session, artifact.artifact_id) is not None


def test_pulling_again_changes_nothing(alice, bob, serve):
    link(alice, bob)
    a_stored_artifact(alice.session, key=alice.signing_key())
    address = serve(peer.create_peer_app(alice.sessions, alice.held))

    bob.pull_from(address)
    again = bob.pull_from(address)

    assert again.ok and again.accepted == []


def test_an_artifact_signed_by_an_unknown_device_is_refused(alice, bob, serve):
    """A peer you talk to is not a peer you trust about what others wrote."""
    link(alice, bob)
    artifact = a_stored_artifact(alice.session, key=Ed25519PrivateKey.generate())
    address = serve(peer.create_peer_app(alice.sessions, alice.held))

    report = bob.pull_from(address)

    assert report.accepted == []
    assert any("no known key" in reason for _, reason in report.rejected)
    assert get_artifact(bob.session, artifact.artifact_id) is None


def test_a_relayed_artifact_cannot_be_rewritten(alice, bob, serve):
    """Alice may pass a plan on. She may not change what someone signed."""
    link(alice, bob)
    artifact = a_stored_artifact(alice.session, key=alice.signing_key())
    row = alice.session.query(ArtifactModel).filter_by(artifact_id=artifact.artifact_id).one()
    row.payload = "# not what was signed\n"
    alice.session.commit()
    address = serve(peer.create_peer_app(alice.sessions, alice.held))

    report = bob.pull_from(address)

    assert report.accepted == []
    assert any("content_hash" in reason for _, reason in report.rejected)


def test_a_peer_without_access_receives_nothing(alice, bob, serve):
    link(alice, bob)
    bob.sign_in(role=None, device_keys={alice.device_id: alice.public_key})
    a_stored_artifact(alice.session, key=alice.signing_key())
    address = serve(peer.create_peer_app(alice.sessions, alice.held))

    report = bob.pull_from(address)

    assert not report.ok
    assert any("no access" in reason for _, reason in report.rejected)


def test_artifacts_from_another_workspace_are_withheld(alice, bob, serve):
    """Asking by id must not reach past the workspace you proved access to."""
    link(alice, bob)
    private = a_stored_artifact(alice.session, key=alice.signing_key(), workspace="ws_private")
    address = serve(peer.create_peer_app(alice.sessions, alice.held))

    with bob.active():
        remote = peer.RemotePeer(address, WORKSPACE, bob.held)
        delivered = remote.fetch([private.artifact_id])

    assert delivered == []


def test_a_skill_package_is_withheld_from_an_entitlement_without_skill_sync(alice, bob, serve):
    """The other half of the gate. Refusing a push while still handing the
    same bytes over on a fetch would be a gate on one direction only."""
    link(alice, bob)
    package = a_stored_artifact(
        alice.session,
        key=alice.signing_key(),
        artifact_type=artifacts.SKILL_PACKAGE,
        body='{"bundle_version": 1}',
    )
    address = serve(peer.create_peer_app(alice.sessions, alice.held))

    with bob.active():
        remote = peer.RemotePeer(address, WORKSPACE, bob.held)
        delivered = remote.fetch([package.artifact_id])

    assert delivered == []


def test_a_skill_package_travels_when_the_entitlement_covers_it(alice, bob, serve):
    link(alice, bob)
    bob.sign_in(features=(TEAM_SYNC, SKILL_SYNC))
    package = a_stored_artifact(
        alice.session,
        key=alice.signing_key(),
        artifact_type=artifacts.SKILL_PACKAGE,
        body='{"bundle_version": 1}',
    )
    address = serve(peer.create_peer_app(alice.sessions, alice.held))

    with bob.active():
        remote = peer.RemotePeer(address, WORKSPACE, bob.held)
        delivered = remote.fetch([package.artifact_id])

    assert [envelope["artifact_id"] for envelope, _ in delivered] == [package.artifact_id]


def test_a_device_that_is_not_signed_in_serves_nobody(alice, bob, serve):
    """No issuer keyring means no way to check anyone's entitlement."""
    bob.sign_in()
    address = serve(peer.create_peer_app(alice.sessions, alice.held))

    report = bob.pull_from(address)
    assert not report.ok


def test_pulling_without_a_session_is_reported_not_raised(bob):
    report = peer.pull(bob.session, "http://127.0.0.1:1", WORKSPACE, lambda: None)
    assert not report.ok
    assert any("not logged in" in reason for _, reason in report.rejected)


def test_an_unreachable_peer_is_reported_not_raised(bob):
    """A sync that cannot start is a bad report, never a crash."""
    bob.sign_in()
    report = bob.pull_from("http://127.0.0.1:1")
    assert not report.ok
    assert any("could not reach" in reason for _, reason in report.rejected)


def test_a_non_http_address_is_refused(bob):
    bob.sign_in()
    report = bob.pull_from("file:///etc/passwd")
    assert not report.ok


def test_a_large_set_is_fetched_in_batches(alice, bob, serve):
    """One sync must not become one enormous request."""
    link(alice, bob)
    key = alice.signing_key()
    made = {
        a_stored_artifact(alice.session, key=key, body=f"# plan {n}\n").artifact_id
        for n in range(sync.MAX_FETCH_BATCH + 5)
    }
    address = serve(peer.create_peer_app(alice.sessions, alice.held))

    report = bob.pull_from(address)

    assert report.ok, report.rejected[:3]
    assert set(report.accepted) == made


def test_the_server_refuses_an_oversized_fetch(alice, bob, serve):
    link(alice, bob)
    address = serve(peer.create_peer_app(alice.sessions, alice.held))

    with bob.active():
        request = sign_request(
            {
                "workspace_id": WORKSPACE,
                "artifact_ids": [f"sha256:{n}" for n in range(sync.MAX_FETCH_BATCH + 1)],
                "public_key": identity.device_public_key_b64(),
                "entitlement": bob.held().entitlement,
            },
            device_id=bob.device_id,
        ).to_dict()

    import json
    import urllib.error
    import urllib.request

    post = urllib.request.Request(  # noqa: S310 - a localhost test server
        address + "/peer/fetch",
        data=json.dumps(request).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with pytest.raises(urllib.error.HTTPError) as caught:
        urllib.request.urlopen(post, timeout=10)  # noqa: S310
    assert caught.value.code == 413


def test_a_session_from_a_different_device_is_caught_locally(alice, bob, serve):
    """A cached session and a local key that disagree is a real state.

    It happens when a home directory is copied between machines. Signing
    anyway would produce a request the peer rejects as a key mismatch, which
    blames the far side for a problem that is entirely local.
    """
    alice.sign_in()
    address = serve(peer.create_peer_app(alice.sessions, alice.held))

    # Bob's home, holding Alice's session.
    with bob.active():
        cache.save(a_session(bob.issuer_key, device_id=alice.device_id))
        report = peer.pull(bob.session, address, WORKSPACE, cache.load)

    assert not report.ok
    assert any("different device" in reason for _, reason in report.rejected)


def test_an_entitlement_without_team_sync_cannot_peer(issuer_key):
    """What makes billing bite rather than decorate.

    A lapsed subscription issues the local plan, which carries a workspace
    role but no team-sync feature. Checking only the role would leave a
    cancelled team syncing exactly as before, and nobody would notice
    because everything else about the entitlement is still valid.
    """
    request = a_peer_request(issuer_key, features=())
    with pytest.raises(peer.PeerError, match="does not include team sync"):
        peer.authorize(request, WORKSPACE, keyring_of(issuer_key))


# --- push ------------------------------------------------------------------
#
# Push is the one operation where a peer hands over data nobody asked for.
# These tests are mostly about what must NOT get through, because the thing
# that gets through is the easy half.


def test_an_artifact_crosses_when_pushed(alice, bob, serve):
    """The mirror of pulling, initiated from the other end."""
    link(alice, bob)
    artifact = a_stored_artifact(alice.session, key=alice.signing_key())
    address = serve(peer.create_peer_app(bob.sessions, bob.held))

    report = alice.push_to(address)

    assert report.ok, report.rejected
    assert report.accepted == [artifact.artifact_id]
    assert get_artifact(bob.session, artifact.artifact_id) is not None


def test_pushing_again_sends_nothing(alice, bob, serve):
    """The offer round trip is what makes a repeat push nearly free."""
    link(alice, bob)
    artifact = a_stored_artifact(alice.session, key=alice.signing_key())
    address = serve(peer.create_peer_app(bob.sessions, bob.held))

    alice.push_to(address)
    again = alice.push_to(address)

    assert again.ok
    assert again.accepted == []
    assert again.already_held == [artifact.artifact_id]


def test_an_echoed_push_costs_one_offer_and_no_upload(alice, bob, serve):
    """B pushing back what A just sent must not re-upload it.

    Content addressing makes an echo harmless but not free. The offer step
    is what keeps it cheap, which is why there is no provenance field
    recording who sent what: the handshake already answers it.
    """
    link(alice, bob)
    artifact = a_stored_artifact(alice.session, key=alice.signing_key())
    alice.push_to(serve(peer.create_peer_app(bob.sessions, bob.held)))

    back = bob.push_to(serve(peer.create_peer_app(alice.sessions, alice.held)))

    assert back.ok
    assert back.accepted == []
    assert back.already_held == [artifact.artifact_id]


def test_a_pushed_artifact_signed_by_an_unknown_device_is_refused(alice, bob, serve):
    """Push does not change who is believed, only who starts the exchange."""
    link(alice, bob)
    artifact = a_stored_artifact(alice.session, key=Ed25519PrivateKey.generate())
    address = serve(peer.create_peer_app(bob.sessions, bob.held))

    report = alice.push_to(address)

    assert report.accepted == []
    assert any("no known key" in reason for _, reason in report.rejected)
    assert get_artifact(bob.session, artifact.artifact_id) is None


def test_a_pushed_artifact_cannot_be_rewritten_in_flight(alice, bob, serve):
    """Alice may relay what Bob signed. She may not edit it on the way."""
    link(alice, bob)
    artifact = a_stored_artifact(alice.session, key=alice.signing_key())
    row = alice.session.query(ArtifactModel).filter_by(artifact_id=artifact.artifact_id).one()
    row.payload = "# not what was signed\n"
    alice.session.commit()
    address = serve(peer.create_peer_app(bob.sessions, bob.held))

    report = alice.push_to(address)

    assert report.accepted == []
    assert get_artifact(bob.session, artifact.artifact_id) is None


def test_a_device_that_refuses_pushes_still_serves_reads(alice, bob, serve, monkeypatch):
    """Saying no to pushes is a posture, not a fault."""
    link(alice, bob)
    artifact = a_stored_artifact(alice.session, key=alice.signing_key())
    monkeypatch.setenv("FLANNER_ACCEPT_PUSHES", "0")
    address = serve(peer.create_peer_app(bob.sessions, bob.held))

    refused = alice.push_to(address)
    assert refused.accepted == []
    assert any("not accepting pushes" in reason for _, reason in refused.rejected)

    # The same device, the same moment: reading is untouched.
    pulled = bob.pull_from(serve(peer.create_peer_app(alice.sessions, alice.held)))
    assert pulled.accepted == [artifact.artifact_id]


def test_being_served_needs_a_current_entitlement_not_merely_a_usable_one(issuer_key):
    """A device asking a peer is online, so grace does not apply.

    This is the whole revoked-device window: with grace, a device revoked at
    the control plane could still write for the grace period plus a
    lifetime. Without it, the window is one entitlement lifetime.
    """
    expired = datetime.now(timezone.utc) - timedelta(minutes=5)
    signing = Ed25519PrivateKey.generate()
    device_id = identity.device_id_for(signing.public_key())
    claims = Claims(
        features=(TEAM_SYNC,),
        organization_id="org_1",
        user_id="maria",
        device_id=device_id,
        key_id="sk_1",
        issued_at=(expired - timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
        expires_at=expired.isoformat().replace("+00:00", "Z"),
        workspace_capabilities=(WorkspaceCapability(workspace_id=WORKSPACE, role=MAINTAINER),),
    )
    request = sign_request(
        {
            "workspace_id": WORKSPACE,
            "public_key": public_key_b64(signing.public_key()),
            "entitlement": encode_token(
                claims, sign(canonical_bytes(claims.to_dict()), issuer_key)
            ),
        },
        device_id=device_id,
        signing_key=signing,
    ).to_dict()

    # The entitlement itself still verifies; only the serving path refuses it.
    assert peer.authorize(request, WORKSPACE, keyring_of(issuer_key)).role == MAINTAINER

    with pytest.raises(peer.PeerError, match="needs a current entitlement"):
        peer.authorize(request, WORKSPACE, keyring_of(issuer_key), strict=True)


def test_a_reader_cannot_push_a_plan_version(alice, bob, serve):
    """Reading a workspace and writing to it are different permissions.

    Until push existed, everything below MAINTAINER was unused and the roles
    were decorative. This is the test that makes them mean something.
    """
    keys = {alice.device_id: alice.public_key, bob.device_id: bob.public_key}
    alice.sign_in(role=READER, device_keys=keys)
    bob.sign_in(device_keys=keys)
    artifact = a_stored_artifact(alice.session, key=alice.signing_key())
    address = serve(peer.create_peer_app(bob.sessions, bob.held))

    report = alice.push_to(address)

    assert report.accepted == []
    assert any("may not push" in reason for _, reason in report.rejected)
    assert get_artifact(bob.session, artifact.artifact_id) is None


def test_one_over_reaching_artifact_does_not_discard_the_rest(alice, bob, serve):
    """Refusals are per artifact, so a batch is not all-or-nothing."""
    from flanner.workflow import COMMENTER

    keys = {alice.device_id: alice.public_key, bob.device_id: bob.public_key}
    alice.sign_in(role=COMMENTER, device_keys=keys)
    bob.sign_in(device_keys=keys)
    allowed = a_stored_artifact(
        alice.session, key=alice.signing_key(), artifact_type=artifacts.COMMENT, body="a note"
    )
    refused = a_stored_artifact(alice.session, key=alice.signing_key(), body="# a version\n")
    address = serve(peer.create_peer_app(bob.sessions, bob.held))

    report = alice.push_to(address)

    assert report.accepted == [allowed.artifact_id]
    assert [a for a, _ in report.rejected] == [refused.artifact_id]
    assert get_artifact(bob.session, allowed.artifact_id) is not None
    assert get_artifact(bob.session, refused.artifact_id) is None


# --- catch-up --------------------------------------------------------------


def test_catching_up_pulls_from_every_known_peer(alice, bob, serve):
    """What a device that was asleep does instead of being pushed to."""
    link(alice, bob)
    artifact = a_stored_artifact(alice.session, key=alice.signing_key())
    address = serve(peer.create_peer_app(alice.sessions, alice.held))

    with bob.active():
        reports = peer.catch_up(
            bob.session,
            [WORKSPACE],
            bob.held,
            dial=lambda device_id, workspace_id: peer.RemotePeer(address, workspace_id, bob.held),
        )

    assert list(reports) == [f"{alice.device_id}/{WORKSPACE}"]
    assert reports[f"{alice.device_id}/{WORKSPACE}"].accepted == [artifact.artifact_id]
    assert get_artifact(bob.session, artifact.artifact_id) is not None


def test_catching_up_never_dials_itself(alice, bob, serve):
    link(alice, bob)
    dialled = []

    def dial(device_id, workspace_id):
        dialled.append(device_id)
        raise peer.PeerError("unreachable")

    with bob.active():
        peer.catch_up(bob.session, [WORKSPACE], bob.held, dial=dial)

    assert dialled == [alice.device_id]


def test_an_unreachable_peer_is_a_result_not_a_crash(alice, bob):
    """Most peers are asleep at any moment. A shut laptop must never be a
    reason this device cannot start."""
    link(alice, bob)

    def dial(device_id, workspace_id):
        raise OSError("no route to host")

    with bob.active():
        reports = peer.catch_up(bob.session, [WORKSPACE], bob.held, dial=dial)

    report = reports[f"{alice.device_id}/{WORKSPACE}"]
    assert not report.ok
    assert "no route to host" in report.rejected[0][1]


def test_catching_up_while_signed_out_does_nothing(bob):
    """No session means no keyring, so there is nobody to trust or to ask."""
    assert peer.catch_up(bob.session, [WORKSPACE], lambda: None) == {}


# --- a new teammate's first push -------------------------------------------


@pytest.fixture(autouse=True)
def fresh_cooldown(monkeypatch):
    """The keyring cooldown is module state shared by every peer, which is
    the point of it: one refresh per window across the whole process, so a
    caller cannot buy another by inventing a device id. That also makes it
    outlive a test, so each one starts with an unspent window."""
    from flanner import push as push_rules

    monkeypatch.setattr(peer, "_keyring_cooldown", push_rules.Cooldown())


def test_an_unknown_author_is_accepted_after_the_keyring_is_refreshed(alice, bob, serve):
    """The chicken and egg: a new teammate's key is not in your keyring
    until you sign in again, so their very first push cannot verify."""
    # Bob signs in knowing only himself, so Alice is a stranger to him.
    alice.sign_in(device_keys={alice.device_id: alice.public_key})
    bob.sign_in(device_keys={bob.device_id: bob.public_key})
    artifact = a_stored_artifact(alice.session, key=alice.signing_key())

    learned = []

    def refresh():
        """Stands in for `account.fetch_device_keys`, which the composition
        root supplies. Returns a resolver rather than nothing, because the
        one already in hand is bound to the pre-fetch session."""
        learned.append(True)
        keys = {alice.device_id: alice.public_key, bob.device_id: bob.public_key}
        return keys.get

    address = serve(peer.create_peer_app(bob.sessions, bob.held, refresh))
    report = alice.push_to(address)

    assert learned == [True], "the refresh should have been attempted exactly once"
    assert report.ok, report.rejected
    assert report.accepted == [artifact.artifact_id]
    assert get_artifact(bob.session, artifact.artifact_id) is not None


def test_without_a_refresher_the_unknown_author_is_still_refused(alice, bob, serve):
    """The refusal has to survive on its own; the refresh is an addition."""
    alice.sign_in(device_keys={alice.device_id: alice.public_key})
    bob.sign_in(device_keys={bob.device_id: bob.public_key})
    artifact = a_stored_artifact(alice.session, key=alice.signing_key())

    report = alice.push_to(serve(peer.create_peer_app(bob.sessions, bob.held)))

    assert report.accepted == []
    assert any("no known key" in reason for _, reason in report.rejected)
    assert get_artifact(bob.session, artifact.artifact_id) is None


def test_a_refresh_that_learns_nothing_leaves_the_refusal_intact(alice, bob, serve):
    """A revoked or forged device also looks like an unknown author, and
    must still be refused after the keyring has been consulted."""
    alice.sign_in(device_keys={alice.device_id: alice.public_key})
    bob.sign_in(device_keys={bob.device_id: bob.public_key})
    stranger = Ed25519PrivateKey.generate()
    artifact = a_stored_artifact(alice.session, key=stranger)

    report = alice.push_to(serve(peer.create_peer_app(bob.sessions, bob.held, lambda: {}.get)))

    assert report.accepted == []
    assert any("no known key" in reason for _, reason in report.rejected)
    assert get_artifact(bob.session, artifact.artifact_id) is None


def test_the_refresh_happens_once_per_batch_not_once_per_artifact(alice, bob, serve):
    """Otherwise fifty artifacts signed by fifty invented device ids would
    be fifty calls to the control plane, on a remote party's say-so."""
    alice.sign_in(device_keys={alice.device_id: alice.public_key})
    bob.sign_in(device_keys={bob.device_id: bob.public_key})
    for i in range(4):
        a_stored_artifact(alice.session, key=alice.signing_key(), body=f"# plan {i}\n")

    calls = []

    def refresh():
        calls.append(True)
        return {alice.device_id: alice.public_key}.get

    report = alice.push_to(serve(peer.create_peer_app(bob.sessions, bob.held, refresh)))

    assert len(calls) == 1
    assert len(report.accepted) == 4


# --- who a peer still serves (a removed member's reading window) ----------


def _manifest_refusal(address, device):
    """Ask for a manifest as this device, and return the refusal it gets."""
    with device.active():
        with pytest.raises(peer.PeerError) as caught:
            peer.RemotePeer(address, WORKSPACE, device.held).manifest(WORKSPACE)
    return caught.value


def test_a_revoked_device_is_refused_when_it_asks_to_read(alice, bob, serve):
    """Removed from the team, still holding a valid entitlement.

    This is the window the fix closes: the entitlement proves who bob was
    when it was issued, and only the roster says he has since been removed.
    Asking the control plane again changes nothing, because it agrees.
    """
    link(alice, bob)
    alice.sign_in(roster=a_roster(alice.issuer_key, [alice.device_id]))
    a_stored_artifact(alice.session, key=alice.signing_key())
    asked = []

    def refresh():
        asked.append(True)
        return {alice.device_id: alice.public_key}.get

    address = serve(peer.create_peer_app(alice.sessions, alice.held, refresh))

    refused = _manifest_refusal(address, bob)
    assert refused.status == 403
    assert refused.code == refusals.DEVICE_UNKNOWN, "the code did not cross the wire"
    assert "roster" in str(refused)
    assert asked == [True], "an unknown requester should cost exactly one refresh"

    report = bob.pull_from(address)
    assert report.accepted == []
    assert not report.ok


def test_a_requester_in_grace_is_refused_when_served(alice, bob, serve):
    """Grace is for reading what you hold, not for being handed more."""
    link(alice, bob)
    bob.sign_in(expires=-timedelta(hours=1))
    a_stored_artifact(alice.session, key=alice.signing_key())
    address = serve(peer.create_peer_app(alice.sessions, alice.held))

    refused = _manifest_refusal(address, bob)
    assert "current entitlement" in str(refused)
    assert "in_grace" in str(refused)


def test_a_roster_in_grace_serves_nobody_else(alice, bob, serve):
    """A roster a week old may list somebody removed six days ago."""
    link(alice, bob)
    alice.sign_in(
        roster=a_roster(
            alice.issuer_key, [alice.device_id, bob.device_id], expires=-timedelta(hours=1)
        )
    )
    address = serve(peer.create_peer_app(alice.sessions, alice.held))

    assert _manifest_refusal(address, bob).code == refusals.DEVICE_UNKNOWN


def test_a_new_teammate_is_served_after_one_roster_refresh(alice, bob, serve):
    """Bob joined after alice last renewed, so her roster has not heard of him."""
    link(alice, bob)
    alice.sign_in(roster=a_roster(alice.issuer_key, [alice.device_id]))
    artifact = a_stored_artifact(alice.session, key=alice.signing_key())
    renewals = []

    def refresh():
        """What `account.refresh` does: a new session, with a new roster."""
        renewals.append(True)
        alice.sign_in(
            device_keys={alice.device_id: alice.public_key, bob.device_id: bob.public_key}
        )
        return alice.held().resolve_device_key

    address = serve(peer.create_peer_app(alice.sessions, alice.held, refresh))
    report = bob.pull_from(address)

    assert report.ok, report.rejected
    assert report.accepted == [artifact.artifact_id]
    assert renewals == [True], "one refresh, then served from the new roster"


def test_a_server_whose_own_entitlement_lapsed_and_cannot_renew_serves_nobody(alice, bob, serve):
    """It may have been removed itself, and cannot find out."""
    link(alice, bob)
    alice.sign_in(expires=-timedelta(hours=1))
    a_stored_artifact(alice.session, key=alice.signing_key())

    def unreachable():
        raise ConnectionError("the control plane is down")

    address = serve(peer.create_peer_app(alice.sessions, alice.held, unreachable))

    refused = _manifest_refusal(address, bob)
    assert refused.status == 503
    assert "own entitlement" in str(refused)
    assert bob.pull_from(address).accepted == []


def test_a_server_renews_its_own_entitlement_before_refusing(alice, bob, serve):
    """A server left running overnight is not refused for that alone."""
    link(alice, bob)
    alice.sign_in(expires=-timedelta(hours=1))
    artifact = a_stored_artifact(alice.session, key=alice.signing_key())
    keys = {alice.device_id: alice.public_key, bob.device_id: bob.public_key}

    def renew():
        alice.sign_in(device_keys=keys)
        return alice.held().resolve_device_key

    address = serve(peer.create_peer_app(alice.sessions, alice.held, renew))
    report = bob.pull_from(address)

    assert report.ok, report.rejected
    assert report.accepted == [artifact.artifact_id]


def test_a_push_in_grace_renews_first_and_is_accepted(alice, bob, serve, monkeypatch):
    """`flanner peer push` renews before signing anything.

    Without it, a device that only ever synced lost push a day after its
    last `whoami --refresh`, because nothing else renewed an entitlement.
    """
    from types import SimpleNamespace

    from click.testing import CliRunner

    from flanner import account
    from flanner import cli as cli_module

    link(alice, bob)
    bob.sign_in(device_keys={alice.device_id: alice.public_key}, expires=-timedelta(hours=1))
    artifact = a_stored_artifact(bob.session, key=bob.signing_key())
    address = serve(peer.create_peer_app(alice.sessions, alice.held))

    renewed = a_session(
        bob.issuer_key,
        device_id=bob.device_id,
        roster=a_roster(bob.issuer_key, [alice.device_id, bob.device_id]),
    )
    asked = []

    def control_plane(endpoint, path, payload, *, repeatable=False):
        asked.append(path)
        if path == "/v1/devices/keyring":
            return {"devices": {alice.device_id: alice.public_key}}
        return renewed.to_dict()

    monkeypatch.setattr(account, "_post", control_plane)
    monkeypatch.setattr(cli_module, "_require_session", lambda: bob.session)
    monkeypatch.setattr(
        cli_module,
        "_resolve_project_or_cwd",
        lambda session, project: SimpleNamespace(workspace_id=WORKSPACE),
    )

    with bob.active():
        result = CliRunner().invoke(cli_module.cli, ["peer", "push", address])

    assert result.exit_code == 0, result.output
    assert asked[0] == "/v1/entitlements", "the push did not renew first"
    assert "/v1/devices/keyring" in asked, "the renewal did not refresh the device keys"
    assert get_artifact(alice.session, artifact.artifact_id) is not None


# --- a retired plan stops crossing the wire --------------------------------


def a_retired_plan(device, plan_file_id="plan_1"):
    """Store an artifact for a plan, then a tombstone claiming it retired."""
    body = "# secret plan\n"
    stone_payload = '{"restored": false}'
    # Hashed the way the receiver hashes it, or ingest refuses the payload
    # before any retirement rule gets a chance to run.
    artifact = artifacts.make_artifact(
        artifact_type=artifacts.REVIEW_PROPOSAL,
        workspace_id=WORKSPACE,
        content_hash=sync.payload_digest(artifacts.REVIEW_PROPOSAL, body.encode()),
        signing_key=device.signing_key(),
        actor_device_id=device.device_id,
        plan_file_id=plan_file_id,
    )
    stone = artifacts.make_artifact(
        artifact_type=artifacts.PLAN_TOMBSTONE,
        workspace_id=WORKSPACE,
        content_hash=sync.payload_digest(artifacts.PLAN_TOMBSTONE, stone_payload.encode()),
        signing_key=device.signing_key(),
        actor_device_id=device.device_id,
        plan_file_id=plan_file_id,
    )
    for made, payload in ((artifact, body), (stone, stone_payload)):
        save_artifact(
            device.session,
            artifact_id=made.artifact_id,
            artifact_type=made.artifact_type,
            workspace_id=made.workspace_id,
            content_hash=made.content_hash,
            actor_device_id=made.actor_device_id,
            created_at=made.created_at,
            signature=made.signature,
            plan_file_id=plan_file_id,
            payload=payload,
        )
    device.session.commit()
    return artifact, stone


def test_a_retired_plan_is_not_offered_or_handed_over(alice, bob, serve):
    """Hidden from the manifest *and* refused on fetch. Filtering only the
    manifest would make it advisory: a peer with a stale one would still
    get the content by asking for the id."""
    link(alice, bob)
    artifact, stone = a_retired_plan(alice)
    address = serve(peer.create_peer_app(alice.sessions, alice.held))

    report = bob.pull_from(address)

    assert artifact.artifact_id not in report.accepted
    assert get_artifact(bob.session, artifact.artifact_id) is None
    # Asking by id directly, as a peer holding a stale manifest would.
    # Inside Bob's home, because signing the request needs his device key.
    with bob.active():
        direct = peer.RemotePeer(address, WORKSPACE, bob.held).fetch([artifact.artifact_id])
    assert direct == []


def test_the_tombstone_itself_still_travels(alice, bob, serve):
    """Otherwise the claim stops at the device that made it, and nobody
    else ever learns the plan was retired."""
    link(alice, bob)
    _, stone = a_retired_plan(alice)
    address = serve(peer.create_peer_app(alice.sessions, alice.held))

    report = bob.pull_from(address)

    assert stone.artifact_id in report.accepted
    assert get_artifact(bob.session, stone.artifact_id) is not None


def test_a_plan_that_was_never_retired_is_unaffected(alice, bob, serve):
    link(alice, bob)
    ordinary = a_stored_artifact(alice.session, key=alice.signing_key())
    a_retired_plan(alice)
    address = serve(peer.create_peer_app(alice.sessions, alice.held))

    report = bob.pull_from(address)

    assert ordinary.artifact_id in report.accepted


def test_retiring_does_not_hide_a_plan_from_its_own_device(alice):
    """`hidden` is for what we send. Applying it to our own catalog would
    have us re-fetch our own artifacts from a peer."""
    alice.sign_in()
    artifact, _ = a_retired_plan(alice)
    mine = sync.build_manifest(alice.session, WORKSPACE)
    assert artifact.artifact_id in mine.artifact_ids


# --- replaying a captured request ------------------------------------------


def test_a_captured_request_cannot_be_answered_twice(alice, bob, serve):
    """The nonce, not the clock, is what refuses the second use.

    Before this the peer path had only the freshness window: inside it a
    captured request was perfectly valid, and every field including the
    signature travels to anyone who can see the connection. It also meant the
    window could not be widened for drifting clocks without widening this.
    """
    link(alice, bob)
    bob.sign_in(device_keys={alice.device_id: alice.public_key})
    a_stored_artifact(alice.session, key=alice.signing_key())

    # One request, sent twice — which is exactly what a replay is. `pull_from`
    # would mint a fresh nonce each time and so could never reproduce it.
    with bob.active():
        captured = sign_request(
            {
                "workspace_id": WORKSPACE,
                "public_key": bob.public_key,
                "entitlement": bob.held().entitlement,
            },
            device_id=bob.device_id,
            signing_key=bob.signing_key(),
        ).to_dict()

    first = peer.serve_request(peer.MANIFEST, captured, alice.sessions, alice.held)
    assert first is not None

    with pytest.raises(peer.PeerError, match="already been answered"):
        peer.serve_request(peer.MANIFEST, captured, alice.sessions, alice.held)


def test_spending_a_nonce_survives_a_failing_request(alice, bob):
    """A nonce stays spent even when the request it carried goes on to fail.

    Otherwise every error path is a replay window: capture something that
    fails late, and the nonce is still unspent for the retry.
    """
    from flanner import replay

    with alice.sessions() as session:
        assert replay.remember(session, device_id="dev_x", nonce="n1")
        assert not replay.remember(session, device_id="dev_x", nonce="n1")
        # A different device may use the same nonce value; they are scoped.
        assert replay.remember(session, device_id="dev_y", nonce="n1")


def test_old_nonces_are_pruned(alice):
    """The table holds minutes of requests, not a history."""
    from datetime import datetime, timedelta, timezone

    from flanner import replay
    from flanner.database import SeenNonceModel

    past = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=1)
    with alice.sessions() as session:
        replay.remember(session, device_id="dev_old", nonce="stale", now=past)
        assert session.query(SeenNonceModel).count() == 1
        replay.prune(session)
        session.commit()
        assert session.query(SeenNonceModel).count() == 0
