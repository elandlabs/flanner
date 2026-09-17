"""Peer sync carried over iroh instead of a reachable http address.

Most of this needs no network: the framing, the refusal shapes and the
choice of transport are all decidable locally. One test is different and
deliberately so. ``test_a_plan_crosses_between_two_devices_over_iroh``
dials the real network, because the entire reason this module exists is a
claim about reachability, and a fake cannot disagree with that claim.

That test skips when there is no route out, rather than failing. An
offline laptop is not a broken build.

The same courtesy is owed to a machine with no transport at all. `iroh`
publishes no wheel for some platforms and may simply be missing from an
incomplete install, and a contributor there should see three skips with a
reason, not three failures that read as their change having broken sync.
"""

import asyncio
import base64
import threading

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from flanner import peer, peer_iroh
from tests.test_peer import WORKSPACE, Device, a_stored_artifact, link

# Defined here rather than imported. Importing a fixture by name binds it as
# a module global and every test that takes it as an argument then reads as
# a redefinition, which buries real warnings under noise.


#: Anything that builds a real transport needs the library present.
needs_iroh = pytest.mark.skipif(
    not peer_iroh.available(),
    reason="iroh is not installed here; peer sync falls back to an http address",
)


@pytest.fixture
def issuer_key():
    return Ed25519PrivateKey.generate()


@pytest.fixture
def team():
    return []


@pytest.fixture
def alice(tmp_path, issuer_key, team):
    return Device(tmp_path / "alice", issuer_key, team)


@pytest.fixture
def bob(tmp_path, issuer_key, team):
    return Device(tmp_path / "bob", issuer_key, team)


class _FakeStream:
    """Enough of an iroh stream to exercise the framing."""

    def __init__(self, data=b""):
        self.buffer = bytearray(data)
        self.sent = bytearray()
        self.finished = False

    async def write_all(self, data):
        self.sent.extend(data)

    async def finish(self):
        self.finished = True

    async def read_exact(self, size):
        if len(self.buffer) < size:
            raise EOFError("stream ended")
        out = bytes(self.buffer[:size])
        del self.buffer[:size]
        return out


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------- framing


def test_a_frame_survives_a_round_trip():
    stream = _FakeStream()
    run(peer_iroh._write(stream, {"op": "manifest", "n": 1}))
    assert stream.finished
    assert run(peer_iroh._read(_FakeStream(stream.sent))) == {"op": "manifest", "n": 1}


def test_an_oversized_length_prefix_is_refused_before_allocating():
    """The prefix comes from a stranger, so it is checked, not believed."""
    header = peer_iroh._HEADER.pack(peer_iroh.MAX_FRAME + 1)
    with pytest.raises(peer.PeerError) as caught:
        run(peer_iroh._read(_FakeStream(header + b"x")))
    assert caught.value.status == 413


def test_a_frame_that_is_not_an_object_is_refused():
    body = b"[1, 2, 3]"
    with pytest.raises(peer.PeerError):
        run(peer_iroh._read(_FakeStream(peer_iroh._HEADER.pack(len(body)) + body)))


# --------------------------------------------------------------- dispatch


def test_a_refusal_carries_its_status_into_the_reply():
    """The same refusal must read the same over either transport."""
    reply = peer_iroh._dispatch(
        {"op": "manifest", "request": {"body": {"workspace_id": WORKSPACE}}},
        sessions=None,
        held=lambda: None,
    )
    assert reply["ok"] is False
    assert reply["status"] == 503
    assert "not part of a team" in reply["error"]


def test_a_request_with_no_signed_payload_is_refused():
    reply = peer_iroh._dispatch({"op": "manifest"}, sessions=None, held=lambda: None)
    assert reply == {"ok": False, "status": 400, "error": "no signed request"}


def test_an_unknown_operation_is_refused_by_the_shared_handler():
    """Not by this module: the transport decides nothing about access."""
    reply = peer_iroh._dispatch(
        {"op": "delete_everything", "request": {"body": {"workspace_id": WORKSPACE}}},
        sessions=None,
        held=lambda: None,
    )
    assert reply["ok"] is False


# --------------------------------------------------------------- dialling


def test_a_device_absent_from_the_keyring_cannot_be_dialled(alice, bob):
    """A revoked or unknown device is named as such, not left to time out."""
    alice.sign_in(device_keys={alice.device_id: alice.public_key})
    with pytest.raises(peer.PeerError) as caught:
        peer_iroh.endpoint_id_for(bob.device_id, alice.held)
    assert caught.value.status == 404
    assert bob.device_id in str(caught.value)


def test_dialling_without_a_session_is_refused(alice, bob):
    with pytest.raises(peer.PeerError):
        peer_iroh.endpoint_id_for(bob.device_id, lambda: None)


def test_the_dialable_id_is_the_devices_own_public_key(alice, bob):
    """No second identity: the id dialled is the key signatures are checked against."""
    link(alice, bob)
    resolved = peer_iroh.endpoint_id_for(bob.device_id, alice.held)
    assert resolved == base64.b64decode(bob.public_key).hex()


def test_a_device_id_routes_over_iroh_and_an_address_over_http(alice, bob, monkeypatch):
    link(alice, bob)
    assert peer_iroh.is_device_id(bob.device_id)
    assert not peer_iroh.is_device_id("http://127.0.0.1:8776")

    built = {}
    monkeypatch.setattr(peer_iroh, "transport", lambda d, h, **kw: built.setdefault("device", d))
    with alice.active():
        over_http = peer_iroh.peer_for("http://127.0.0.1:8776", WORKSPACE, alice.held)
    assert "device" not in built
    assert isinstance(over_http, peer.RemotePeer)

    with alice.active():
        peer_iroh.peer_for(bob.device_id, WORKSPACE, alice.held)
    assert built["device"] == bob.device_id


# ------------------------------------------------------------- the network


@pytest.fixture
def endpoints():
    """Real iroh endpoints, closed when the test ends."""
    made = []

    def start(device):
        with device.active():
            endpoint = peer_iroh.PeerEndpoint()
            try:
                endpoint.ready(timeout=60)
            except Exception as exc:  # pragma: no cover - depends on the network
                pytest.skip(f"no iroh endpoint available here: {exc}")
        made.append(endpoint)
        return endpoint

    yield start

    for endpoint in made:
        try:
            endpoint.close()
        except Exception:  # noqa: S110 - teardown is best effort
            pass


def test_a_plan_crosses_between_two_devices_over_iroh(alice, bob, endpoints):
    """The claim this module exists for: no address, no port, no admin rights.

    Alice never learns Bob's address and Bob opens no listening port. The
    only thing Alice is given is Bob's device id.
    """
    link(alice, bob)
    a_stored_artifact(bob.session, key=bob.signing_key())

    serving = endpoints(bob)
    threading.Thread(target=lambda: serving.serve(bob.sessions, bob.held), daemon=True).start()

    dialing = endpoints(alice)
    with alice.active():
        try:
            carry = peer_iroh.transport(bob.device_id, alice.held, endpoint=dialing, timeout=60)
            remote = peer.RemotePeer(bob.device_id, WORKSPACE, alice.held, transport=carry)
            manifest = remote.manifest(WORKSPACE)
        except peer.PeerError as exc:  # pragma: no cover - depends on the network
            pytest.skip(f"no route between two iroh endpoints here: {exc}")

        assert manifest.workspace_id == WORKSPACE
        assert manifest.artifact_ids, "Bob served an empty manifest over iroh"
        wanted = sorted(manifest.artifact_ids)
        fetched = remote.fetch(wanted)

    assert sorted(envelope["artifact_id"] for envelope, _ in fetched) == wanted


# ----------------------------------------------------------------- relaying


@needs_iroh
def test_an_organizations_relay_is_added_to_the_defaults_not_swapped_for_them():
    """Replacing four regional relays with one machine is a worse fallback."""
    defaults = set(peer_iroh.relay_mode().relay_map().urls())
    assert len(defaults) > 1

    with_ours = set(peer_iroh.relay_mode("https://relay.example.com").relay_map().urls())
    assert defaults < with_ours
    assert any("relay.example.com" in url for url in with_ours)


@needs_iroh
def test_no_configured_relay_means_the_transport_defaults():
    assert peer_iroh.relay_mode().relay_map().urls() == (
        peer_iroh.relay_mode("").relay_map().urls()
    )


def test_the_relay_url_survives_a_session_round_trip(tmp_path, monkeypatch):
    """Delivered with the entitlement, so a renewal teaches a device where to relay."""
    from flanner import session as cache

    monkeypatch.setenv("FLANNER_HOME", str(tmp_path / "home"))
    cache.save(
        cache.Session(
            endpoint="https://api.example.com",
            device_id="dev_1",
            organization_id="org_1",
            user_id="maria",
            entitlement="tok",
            relay_url="https://relay.example.com",
        )
    )
    assert cache.load().relay_url == "https://relay.example.com"


def test_a_session_from_an_older_control_plane_has_no_relay_and_still_loads(tmp_path, monkeypatch):
    """A control plane that sends none must not break a client that expects one."""
    import json

    from flanner import session as cache

    monkeypatch.setenv("FLANNER_HOME", str(tmp_path / "home"))
    path = cache.session_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "endpoint": "https://api.example.com",
                "device_id": "dev_1",
                "organization_id": "org_1",
                "user_id": "maria",
                "entitlement": "tok",
            }
        ),
        encoding="utf-8",
    )
    loaded = cache.load()
    assert loaded is not None
    assert loaded.relay_url == ""


def test_the_environment_overrides_what_the_control_plane_sent(tmp_path, monkeypatch):
    """An operator debugging a relay should not have to re-issue an entitlement."""
    from flanner import session as cache

    monkeypatch.setenv("FLANNER_HOME", str(tmp_path / "home"))
    cache.save(
        cache.Session(
            endpoint="https://api.example.com",
            device_id="dev_1",
            organization_id="org_1",
            user_id="maria",
            entitlement="tok",
            relay_url="https://from-session.example.com",
        )
    )
    assert peer_iroh._configured_relay()[0] == "https://from-session.example.com"

    monkeypatch.setenv("FLANNER_RELAY_URL", "https://from-env.example.com")
    assert peer_iroh._configured_relay()[0] == "https://from-env.example.com"


# --------------------------------------------------------------- the route


class _Path:
    def __init__(self, **fields):
        for name, value in fields.items():
            setattr(self, name, value)


class _Connection:
    def __init__(self, *paths):
        self._paths = paths

    def paths(self):
        return list(self._paths)


def test_the_selected_path_is_the_answer_not_the_first_one():
    """iroh keeps candidates; only the selected one is carrying traffic."""
    route = peer_iroh.route_of(
        _Connection(
            _Path(is_selected=False, is_relay=True, remote_addr="https://relay/", rtt_ms=200),
            _Path(is_selected=True, is_relay=False, remote_addr="1.2.3.4:5", rtt_ms=12),
        ),
        "dev_1",
    )
    assert route.connection == peer_iroh.DIRECT
    assert route.relayed is False
    assert route.address == "1.2.3.4:5"
    assert route.rtt_ms == 12


def test_a_relayed_path_says_so():
    route = peer_iroh.route_of(
        _Connection(
            _Path(is_selected=True, is_relay=True, remote_addr="https://relay/", rtt_ms=90)
        ),
        "dev_1",
    )
    assert route.connection == peer_iroh.RELAY
    assert route.relayed is True


class _Unbound:
    """An endpoint nobody bound in advance, as in a fresh `flanner` process.

    The real endpoint's `ready` blocks on the shared loop, so calling it from
    a coroutine already on that loop waits on itself. This one refuses to be
    asked from there, which is the bug the fixture's pre-binding hid.
    """

    def __init__(self):
        self.asked_from = []

    def ready(self, timeout=0):
        self.asked_from.append(threading.current_thread().name)
        assert threading.current_thread().name != "flanner-iroh", "ready() called on the loop"
        return self

    async def online(self):
        return None

    async def connect(self, address, alpn):
        return _Connection(_Path(is_selected=True, is_relay=False, remote_addr="x", rtt_ms=1))


class _FakeIroh:
    """Just enough of the iroh module to build a dial address."""

    class EndpointId:
        @staticmethod
        def from_string(value):
            return value

    @staticmethod
    def EndpointAddr(*parts):  # noqa: N802 - mirrors the binding's name
        return parts


def test_a_fresh_process_can_dial_without_deadlocking(monkeypatch):
    monkeypatch.setattr(peer_iroh, "_iroh", lambda: _FakeIroh)
    monkeypatch.setattr(peer_iroh, "endpoint_id_for", lambda device, held: "ab" * 32)
    endpoint = _Unbound()

    route = peer_iroh.route_to("dev_1", lambda: None, endpoint=endpoint, timeout=5)

    assert route.connection == peer_iroh.DIRECT
    assert endpoint.asked_from and "flanner-iroh" not in endpoint.asked_from


def test_a_connection_that_will_not_answer_gives_unknown_not_an_exception():
    """A sync that already succeeded must not fail on a cosmetic question."""

    class Closed:
        def paths(self):
            raise RuntimeError("connection closed")

    assert peer_iroh.route_of(Closed(), "dev_1").connection == peer_iroh.UNKNOWN


def test_no_selected_path_is_unknown_rather_than_a_guess():
    route = peer_iroh.route_of(
        _Connection(_Path(is_selected=False, is_relay=False, remote_addr="1.2.3.4:5", rtt_ms=1)),
        "dev_1",
    )
    assert route.connection == peer_iroh.UNKNOWN
    assert route.relayed is False


@needs_iroh
def test_a_fresh_transport_has_not_taken_a_route_yet(alice, bob):
    link(alice, bob)
    with alice.active():
        carry = peer_iroh.transport(bob.device_id, alice.held)
    assert carry.last_route is None
    assert carry.device_id == bob.device_id


def test_the_transport_records_the_route_it_used(alice, bob, endpoints):
    """Read off the connection the sync used, not a second one dialled after."""
    link(alice, bob)
    a_stored_artifact(bob.session, key=bob.signing_key())

    serving = endpoints(bob)
    threading.Thread(target=lambda: serving.serve(bob.sessions, bob.held), daemon=True).start()

    dialing = endpoints(alice)
    with alice.active():
        try:
            carry = peer_iroh.transport(bob.device_id, alice.held, endpoint=dialing, timeout=60)
            remote = peer.RemotePeer(bob.device_id, WORKSPACE, alice.held, transport=carry)
            remote.manifest(WORKSPACE)
        except peer.PeerError as exc:  # pragma: no cover - depends on the network
            pytest.skip(f"no route between two iroh endpoints here: {exc}")

    assert carry.last_route is not None
    assert carry.last_route.device_id == bob.device_id
    assert carry.last_route.connection in {peer_iroh.DIRECT, peer_iroh.RELAY}


def test_local_status_reports_this_devices_own_identity(alice, endpoints):
    """What a peer sees. The dialable id must be this device's own key."""
    import base64

    alice.sign_in()
    bound = endpoints(alice)
    with alice.active():
        status = peer_iroh.local_status(alice.held, endpoint=bound)

    assert status.device_id == alice.device_id
    assert status.dialable_id == base64.b64decode(alice.public_key).hex()


# ------------------------------------------------ platforms with no build
#
# The transport publishes wheels for four platforms and no sdist at all, so
# on an Intel Mac, Alpine, or Windows-on-ARM it simply is not there. That is
# declared with markers in pyproject, so `pip install flanner` still works;
# these check the other half, that the client is usable when it is absent.


@pytest.fixture
def without_iroh(monkeypatch):
    """As if the wheel had never existed on this machine."""
    import builtins

    real = builtins.__import__

    def refuse(name, *args, **kwargs):
        if name == "iroh":
            raise ImportError("No module named 'iroh'")
        return real(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", refuse)


def test_the_client_reports_the_transport_as_unavailable(without_iroh):
    assert peer_iroh.available() is False


def test_the_refusal_names_the_platform_and_the_way_round_it(without_iroh):
    """A missing wheel is not a bug the reader can fix, so say what to do."""
    with pytest.raises(peer.PeerError) as caught:
        peer_iroh._iroh()
    message = str(caught.value)
    assert "peer serve --http" in message
    assert "peer pull" in message
    assert caught.value.status == 503


def test_an_http_peer_still_works_with_no_transport_installed(without_iroh, alice, bob):
    """The fallback has to be reachable without touching the missing module."""
    link(alice, bob)
    with alice.active():
        remote = peer_iroh.peer_for("http://127.0.0.1:8776", WORKSPACE, alice.held)
    assert isinstance(remote, peer.RemotePeer)


def test_asking_for_a_device_id_refuses_rather_than_crashing(without_iroh, alice, bob):
    link(alice, bob)
    with alice.active(), pytest.raises(peer.PeerError):
        peer_iroh.peer_for(bob.device_id, WORKSPACE, alice.held)


def test_every_declared_iroh_marker_matches_a_published_wheel():
    """The dependency must never become unconditional again by accident.

    Guards the packaging decision, not the code: without markers, pip on a
    platform with no wheel fails to install flanner at all, and the error
    reads as a missing package rather than an unsupported machine.
    """
    from pathlib import Path

    import tomllib
    from packaging.markers import Marker

    root = Path(__file__).resolve().parent.parent
    deps = [
        d
        for d in tomllib.loads((root / "pyproject.toml").read_text())["project"]["dependencies"]
        if d.startswith("iroh")
    ]
    assert deps, "iroh is no longer declared"
    assert all(";" in d for d in deps), "iroh must stay conditional: no sdist exists"

    def required(sys_platform, machine):
        return any(
            Marker(d.split(";", 1)[1]).evaluate(
                {"sys_platform": sys_platform, "platform_machine": machine}
            )
            for d in deps
        )

    # Wheels published as of 1.1.0.
    assert required("darwin", "arm64")
    assert required("linux", "x86_64")
    assert required("linux", "aarch64")
    assert required("win32", "AMD64")
    # No wheel, and no sdist to fall back to.
    assert not required("darwin", "x86_64")
    assert not required("win32", "ARM64")
    assert not required("linux", "armv7l")

    # `_missing_transport` branches on this set to tell a broken install
    # apart from an unsupported machine. A platform added to the markers and
    # not here would go back to being told its machine has no build.
    for declared in peer_iroh._DECLARED_BUILDS:
        assert required(*declared), f"{declared} is declared in code but has no marker"
    for unsupported in (("darwin", "x86_64"), ("win32", "ARM64"), ("linux", "armv7l")):
        assert unsupported not in peer_iroh._DECLARED_BUILDS


def test_a_platform_we_ship_a_wheel_for_is_told_to_install_it(monkeypatch):
    """An ImportError here means the install is incomplete, not the platform.

    This is the failure the message used to get wrong, and getting it wrong
    is expensive: it tells somebody their machine cannot do direct sync, so
    they adopt the address fallback for good rather than running one
    command.
    """
    monkeypatch.setattr(peer_iroh.sys, "platform", "win32")
    monkeypatch.setattr(peer_iroh.platform, "machine", lambda: "AMD64")

    message = peer_iroh._missing_transport()

    assert "pip install iroh" in message
    assert "publishes no build for" not in message


def test_a_platform_with_no_wheel_is_still_told_to_use_an_address(monkeypatch):
    """The other half. Here the fallback really is the answer, forever."""
    monkeypatch.setattr(peer_iroh.sys, "platform", "linux")
    monkeypatch.setattr(peer_iroh.platform, "machine", lambda: "armv7l")

    message = peer_iroh._missing_transport()

    assert "publishes no build for" in message
    assert "pip install iroh" not in message
