"""Carrying peer requests over iroh, so neither device needs to be reachable.

:mod:`flanner.peer` speaks to whatever address it is given, which assumes a
route already exists between two machines. On a home connection behind NAT,
one usually does not. This supplies the route.

**Why this and not a mesh provider.** A VPN client presents a virtual network
adapter, which needs administrator rights on every operating system. That is
a separate installation the user must perform, and on a locked-down machine
cannot. iroh is a library: it opens a UDP socket, punches through NAT inside
the QUIC connection, and falls back to a relay when that fails. It arrives
with ``pip install`` and asks for nothing.

**The identity is the same key, not a second one.** An iroh endpoint id *is*
an Ed25519 public key, and so is a flanner device identity. The same 32
bytes serve as both, so this module introduces no new identity, no mapping
table, and nothing that can disagree with itself.

That is convenient, and it is also a trap worth naming. Connecting proves
the far side holds a private key. It does not prove that device may read
this workspace, which is a question only a signed entitlement answers. So
every request still goes through :func:`flanner.peer.serve_request`
unchanged, and this module decides nothing about access.

**Dialling needs the key, not the device id.** A device id is a hash of the
public key, and a hash cannot be dialled. The full key comes from the
organization keyring cached at login, via ``Session.resolve_device_key``, so
a device that was never in the directory cannot be dialled at all.
"""

from __future__ import annotations

import asyncio
import json
import os
import platform
import struct
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from . import identity, peer, refusals

#: Protocol name negotiated on the wire. Bump it when the framing changes,
#: never for a change inside the JSON, which the protocol version covers.
ALPN = b"flanner/peer/1"

#: Refuse a frame larger than this before allocating for it. A length
#: prefix arrives from an unauthenticated stranger, so believing it would
#: hand anyone a way to ask this process for an arbitrary allocation.
MAX_FRAME = 64 * 1024 * 1024

CONNECT_TIMEOUT = 45.0
_HEADER = struct.Struct(">I")

#: How a connection is actually travelling. Named here rather than borrowed
#: from :mod:`flanner.mesh`, which describes provisioning a private network.
#: A transport that imported the abstraction it replaced would be an odd
#: dependency to explain later.
DIRECT = "direct"
RELAY = "relay"
UNKNOWN = "unknown"

#: The UDP port on which the organization's relay answers QUIC address
#: discovery. Without it iroh never asks that relay, so a device relying on
#: it alone never learns its outside address and cannot punch through NAT.
#: iroh-relay's default; `0` turns discovery off for that relay.
RELAY_QUIC_PORT_ENV = "FLANNER_RELAY_QUIC_PORT"
DEFAULT_RELAY_QUIC_PORT = 7842


@dataclass(frozen=True)
class Route:
    """How this device is reaching one peer, right now.

    Worth reporting because both routes work and differ only in speed, so
    the difference is invisible until someone is waiting. ``relayed`` is
    the first thing to know about a sync that feels slow.
    """

    device_id: str
    connection: str = UNKNOWN
    address: str = ""
    rtt_ms: int = 0

    @property
    def relayed(self) -> bool:
        return self.connection == RELAY


@dataclass(frozen=True)
class LocalStatus:
    """This device's own presence on the network."""

    device_id: str
    dialable_id: str
    addresses: tuple[str, ...] = ()
    home_relay: str = ""
    configured_relay: str = ""


class _Loop:
    """One asyncio loop on a background thread, shared by the process.

    iroh's API is entirely coroutines; the CLI and ``sync.Peer`` are plain
    synchronous calls. Running ``asyncio.run`` per request would work and
    would also throw away the connection each time, so the loop outlives
    individual requests instead.
    """

    _instance: _Loop | None = None
    _guard = threading.Lock()

    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, name="flanner-iroh", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    @classmethod
    def shared(cls) -> _Loop:
        with cls._guard:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    def run(self, coro: Any, timeout: float) -> Any:
        return self.submit(coro).result(timeout)

    def submit(self, coro: Any) -> Any:
        return asyncio.run_coroutine_threadsafe(coro, self._loop)


#: The platforms ``pyproject.toml`` declares an iroh wheel for, as
#: ``(sys.platform, platform.machine())``. It has to stay in step with the
#: markers there, and with the platform list in the README.
_DECLARED_BUILDS = frozenset(
    {
        ("darwin", "arm64"),
        ("linux", "x86_64"),
        ("linux", "aarch64"),
        ("win32", "AMD64"),
    }
)


def _missing_transport() -> str:
    """Why the transport is not here, and what will actually fix it.

    Two refusals, because there are two causes with two different fixes. On
    a platform with no wheel the answer is an address, and it always will
    be. On one of the four we declare, an ImportError means the install is
    incomplete instead, and the answer is to finish it.

    Reporting the first for the second is the expensive mistake: it tells
    somebody their machine cannot do this, so they adopt the workaround
    permanently and never discover that one command would have fixed it.
    """
    fallback = (
        "Sync over an address instead: 'flanner peer serve --http' on the "
        "far side, then 'flanner peer pull <http-address>'."
    )
    if (sys.platform, platform.machine()) in _DECLARED_BUILDS:
        return (
            f"the iroh transport publishes a build for {platform.system()} "
            f"{platform.machine()} but it is not installed here, so direct "
            "peer sync is unavailable. Install it with 'pip install iroh'. " + fallback
        )
    return (
        f"direct peer sync is unavailable on {platform.system()} "
        f"{platform.machine()}, which the transport publishes no build for. " + fallback
    )


def _iroh() -> Any:
    """The transport library, or a refusal that says what to do instead.

    Absent on platforms it publishes no wheel for, which is why it is
    declared with markers rather than unconditionally: pip failing to
    install flanner at all would be a worse outcome than peer sync needing
    an address. Everything else in the client works without it.
    """
    try:
        import iroh
    except ImportError:  # pragma: no cover - depends on the platform
        raise peer.PeerError(_missing_transport(), status=503) from None
    return iroh


def available() -> bool:
    """Whether this machine can reach peers without being reachable."""
    try:
        _iroh()
    except peer.PeerError:
        return False
    return True


def _secret_bytes() -> bytes:
    from cryptography.hazmat.primitives import serialization

    return identity.load_or_create_device_key().private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )


def relay_mode(relay_url: str = "", token: str = "") -> Any:
    """Where to relay from, when a direct path cannot be punched through.

    An organization's own relay is *added* to the defaults rather than
    replacing them. Replacing would trade four relays spread across regions
    for one, which is a worse fallback than the one it replaced and puts a
    single machine behind every connection that could not go direct. The
    point of self-hosting here is not to leave the public network; it is to
    have somewhere to fall back to that we run ourselves.
    """
    iroh = _iroh()
    if not relay_url:
        return iroh.RelayMode.default_mode()
    relays = iroh.RelayMode.default_mode().relay_map()
    relays.insert(
        iroh.RelayConfig(url=relay_url, quic_port=relay_quic_port(), auth_token=token or None)
    )
    return iroh.RelayMode.custom(relays)


def relay_quic_port() -> int | None:
    """The configured relay's address-discovery port, or None to not ask it."""
    raw = os.environ.get(RELAY_QUIC_PORT_ENV, "").strip()
    try:
        port = int(raw) if raw else DEFAULT_RELAY_QUIC_PORT
    except ValueError:
        port = DEFAULT_RELAY_QUIC_PORT
    return port if 0 < port < 65536 else None


def _configured_relay() -> tuple[str, str]:
    """The relay this device should prefer, and any token for it.

    Read per bind rather than cached, and never required: a control plane
    that sends none, or a device not yet logged in, gets the defaults.
    """
    from . import session as cache

    url = os.environ.get("FLANNER_RELAY_URL", "")
    token = os.environ.get("FLANNER_RELAY_TOKEN", "")
    if url:
        return url, token
    current = cache.load()
    return (current.relay_url if current else ""), token


async def _bind() -> Any:
    """Bring this device's endpoint up.

    The n0 preset is three things at once, and the second is the one worth
    stating out loud. Its own description is "relays + discovery + crypto
    provider".

    *Discovery* is how a peer holding nothing but a device id finds a route
    to this machine. There is no address to exchange and none to configure
    because this endpoint publishes its own — its local candidate addresses
    and its home relay, signed under the same key the device id is a hash
    of — and a dialler resolves the id against that. Which is to say: taking
    the preset means addresses for this machine are published to
    infrastructure n0 runs. Not plan contents, which never leave the
    encrypted connection between two endpoints, and not anything a stranger
    could act on, since reaching this endpoint still proves nothing and
    grants nothing. But it is a default with a privacy consequence, and it
    arrives inside a call that does not look like it has one.

    ``relay_mode`` below then replaces the relay half with the defaults plus
    the organization's own, if it has one. Discovery is deliberately left as
    the preset set it: an organization running its own relay has somewhere
    of its own to fall back to, and still resolves ids through n0.
    """
    iroh = _iroh()
    url, token = _configured_relay()
    builder = iroh.EndpointBuilder()
    builder.apply_n0()
    builder.secret_key(_secret_bytes())
    builder.alpns([ALPN])
    # After the preset, not before: this is the half being overridden.
    builder.relay_mode(relay_mode(url, token))
    return await builder.bind()


async def _write(stream: Any, obj: dict[str, Any]) -> None:
    body = json.dumps(obj).encode("utf-8")
    if len(body) > MAX_FRAME:
        raise peer.PeerError("reply is too large to send", status=413)
    await stream.write_all(_HEADER.pack(len(body)) + body)
    await stream.finish()


async def _read(stream: Any) -> dict[str, Any]:
    (size,) = _HEADER.unpack(await stream.read_exact(_HEADER.size))
    if size > MAX_FRAME:
        raise peer.PeerError(f"frame of {size} bytes refused", status=413)
    loaded = json.loads(await stream.read_exact(size))
    if not isinstance(loaded, dict):
        raise peer.PeerError("peer sent something unusable")
    return loaded


class PeerEndpoint:
    """This device's presence on the iroh network.

    Binding is deferred until something needs it, so importing this module
    and running ``flanner --help`` open no sockets.
    """

    def __init__(self) -> None:
        self._endpoint: Any = None
        self._guard = threading.Lock()

    def ready(self, timeout: float = CONNECT_TIMEOUT) -> Any:
        with self._guard:
            if self._endpoint is None:
                self._endpoint = _Loop.shared().run(_bind(), timeout)
            return self._endpoint

    def dialable_id(self) -> str:
        """The id another device dials to reach this one.

        This is the device's public key in hex, which is also what the
        signature on every request is checked against.
        """
        return str(self.ready().id().to_bytes().hex())

    def serve(self, sessions: Any, held: Any, refresh_keys: Any = None) -> None:
        """Answer peer requests until the process ends. Blocks.

        Runs the accept loop on the shared background loop and waits, so a
        caller gets the same blocking behaviour as running an HTTP server.
        """
        endpoint = self.ready()
        loop = _Loop.shared()
        loop.run(endpoint.online(), CONNECT_TIMEOUT)
        loop.submit(_accept_forever(endpoint, sessions, held, refresh_keys)).result()

    def close(self) -> None:
        with self._guard:
            if self._endpoint is not None:
                _Loop.shared().run(self._endpoint.close(), CONNECT_TIMEOUT)
                self._endpoint = None


async def _accept_forever(
    endpoint: Any, sessions: Any, held: Any, refresh_keys: Any = None
) -> None:
    while True:
        incoming = await endpoint.accept_next()
        if incoming is None:
            return
        asyncio.create_task(_answer(incoming, sessions, held, refresh_keys))


async def _answer(incoming: Any, sessions: Any, held: Any, refresh_keys: Any = None) -> None:
    """One connection. Never raises: a bad caller must not stop the loop."""
    try:
        connection = await (await incoming.accept()).connect()
    except Exception:
        return
    try:
        while True:
            stream = await connection.accept_bi()
            request = await _read(stream.recv())
            reply = _dispatch(request, sessions, held, refresh_keys)
            await _write(stream.send(), reply)
    except Exception:
        # A closed connection is the normal end of this loop, and a caller
        # that sent nonsense has already been answered or has gone away.
        return


def _dispatch(
    request: dict[str, Any], sessions: Any, held: Any, refresh_keys: Any = None
) -> dict[str, Any]:
    operation = str(request.get("op") or "")
    payload = request.get("request")
    if not isinstance(payload, dict):
        return {"ok": False, "status": 400, "error": "no signed request"}
    try:
        return {
            "ok": True,
            "body": peer.serve_request(operation, payload, sessions, held, refresh_keys),
        }
    except peer.PeerError as e:
        return {"ok": False, "status": e.status, "error": str(e), "code": e.code}


def endpoint_id_for(device_id: str, held: Any) -> str:
    """The dialable id of another device, from the cached keyring.

    Raises rather than guessing. A device absent from the keyring is one
    this organization has not been told about, or one that was revoked, and
    inventing an id for it would turn that into a confusing timeout.
    """
    current = held()
    if current is None:
        raise peer.PeerError("this device is not logged in")
    encoded = current.resolve_device_key(device_id)
    if not encoded:
        raise peer.PeerError(
            f"{device_id} is not in this organization's device list; "
            "run 'flanner whoami --refresh'",
            status=404,
        )
    from cryptography.hazmat.primitives import serialization

    key = identity.load_public_key(encoded)
    return key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    ).hex()


class IrohTransport:
    """A :data:`flanner.peer.Transport` that reaches a device over iroh.

    The device is named by its flanner device id, not by an address. There
    is nothing to configure and nothing to forward: iroh finds the far side
    from its key, tries a direct path, and relays only if that fails.

    A class rather than a closure so it can remember ``last_route``. That
    matters because relayed and direct connections both work and differ
    only in speed, so a slow sync gives no hint which one it got. Reading
    it back off the connection just used costs nothing; asking afterwards
    would mean dialling a second time and might answer about a different
    path than the one the sync actually took.
    """

    def __init__(
        self,
        device_id: str,
        held: Any,
        *,
        endpoint: PeerEndpoint | None = None,
        timeout: float = peer.REQUEST_TIMEOUT,
    ) -> None:
        # Checked here rather than on first use. Constructing a transport
        # that can never carry anything would move the failure past the
        # point where callers are set up to report it, and turn "no build
        # for this platform" into a traceback mid-sync.
        _iroh()
        self.device_id = device_id
        self.last_route: Route | None = None
        self._local = endpoint or shared_endpoint()
        self._timeout = timeout
        self._remote_hex = endpoint_id_for(device_id, held)

    def __call__(self, operation: str, signed: dict[str, Any]) -> dict[str, Any]:
        iroh = _iroh()
        # Bound here, on the caller's thread. `ready` blocks on the shared
        # loop, so calling it from a coroutine already running there waits
        # on itself until the timeout: every dial from a fresh process did.
        bound = self._local.ready(self._timeout)

        async def exchange() -> tuple[dict[str, Any], Any]:
            await bound.online()
            address = iroh.EndpointAddr(iroh.EndpointId.from_string(self._remote_hex), None, [])
            connection = await bound.connect(address, ALPN)
            stream = await connection.open_bi()
            await _write(stream.send(), {"op": operation, "request": signed})
            return await _read(stream.recv()), connection

        try:
            reply, connection = _Loop.shared().run(exchange(), self._timeout + CONNECT_TIMEOUT)
        except peer.PeerError:
            raise
        except TimeoutError:
            raise peer.PeerError(f"{self.device_id} did not answer in time") from None
        except Exception as e:
            raise peer.PeerError(f"could not reach {self.device_id}: {e}") from None

        self.last_route = route_of(connection, self.device_id)

        if not reply.get("ok"):
            raise peer.PeerError(
                str(reply.get("error") or "peer refused"),
                status=int(reply.get("status") or 403),
                code=str(reply.get("code") or refusals.UNKNOWN),
            )
        body = reply.get("body")
        if not isinstance(body, dict):
            raise peer.PeerError("peer sent something unusable")
        return body


def transport(
    device_id: str,
    held: Any,
    *,
    endpoint: PeerEndpoint | None = None,
    timeout: float = peer.REQUEST_TIMEOUT,
) -> IrohTransport:
    """A transport reaching one device over iroh."""
    return IrohTransport(device_id, held, endpoint=endpoint, timeout=timeout)


def route_of(connection: Any, device_id: str) -> Route:
    """Which path a live connection settled on.

    iroh keeps several candidate paths and marks one selected; that one is
    the answer. Reading them can fail on a connection that has just closed,
    and an unknown route is not worth failing a sync that already
    succeeded, so this reports :data:`UNKNOWN` rather than raising.
    """
    try:
        paths = list(connection.paths())
    except Exception:
        return Route(device_id=device_id)

    chosen = next((p for p in paths if getattr(p, "is_selected", False)), None)
    if chosen is None:
        return Route(device_id=device_id)
    return Route(
        device_id=device_id,
        connection=RELAY if getattr(chosen, "is_relay", False) else DIRECT,
        address=str(getattr(chosen, "remote_addr", "") or ""),
        rtt_ms=int(getattr(chosen, "rtt_ms", 0) or 0),
    )


def local_status(held: Any, *, endpoint: PeerEndpoint | None = None) -> LocalStatus:
    """What this device looks like to a peer trying to reach it."""
    current = held()
    local = endpoint or shared_endpoint()
    bound = local.ready()
    _Loop.shared().run(bound.online(), CONNECT_TIMEOUT)
    address = bound.addr()
    return LocalStatus(
        device_id=current.device_id if current else identity.device_id(),
        dialable_id=str(bound.id().to_bytes().hex()),
        addresses=tuple(str(a) for a in address.direct_addresses()),
        home_relay=str(address.relay_url() or ""),
        configured_relay=_configured_relay()[0],
    )


def route_to(
    device_id: str,
    held: Any,
    *,
    endpoint: PeerEndpoint | None = None,
    timeout: float = peer.REQUEST_TIMEOUT,
) -> Route:
    """Reach a peer and report how the connection travelled.

    Deliberately opens a real connection rather than reading a cache. The
    question being asked is how this device reaches that one now, and a
    remembered answer from an hour ago on a different network would be a
    confident wrong one.
    """
    iroh = _iroh()
    local = endpoint or shared_endpoint()
    remote_hex = endpoint_id_for(device_id, held)

    bound = local.ready(timeout)  # on this thread; see IrohTransport.__call__

    async def dial() -> Any:
        await bound.online()
        address = iroh.EndpointAddr(iroh.EndpointId.from_string(remote_hex), None, [])
        return await bound.connect(address, ALPN)

    try:
        connection = _Loop.shared().run(dial(), timeout + CONNECT_TIMEOUT)
    except peer.PeerError:
        raise
    except Exception as e:
        raise peer.PeerError(f"could not reach {device_id}: {e}") from None
    return route_of(connection, device_id)


_shared: PeerEndpoint | None = None
_shared_guard = threading.Lock()


def shared_endpoint() -> PeerEndpoint:
    """The process's own endpoint, made once.

    One endpoint per device, not per peer: binding a second one would put
    the same key on the network twice and make discovery ambiguous.
    """
    global _shared
    with _shared_guard:
        if _shared is None:
            _shared = PeerEndpoint()
        return _shared


def remote_peer(device_id: str, workspace_id: str, held: Any) -> peer.RemotePeer:
    """A :class:`flanner.peer.RemotePeer` reached over iroh rather than HTTP."""
    return peer.RemotePeer(device_id, workspace_id, held, transport=transport(device_id, held))


def is_device_id(target: str) -> bool:
    """Whether a sync target names a device rather than an http address."""
    return target.startswith("dev_")


def peer_for(target: str, workspace_id: str, held: Any) -> peer.RemotePeer:
    """Pick a transport from how the peer was named.

    A device id goes over iroh, anything else is treated as an address and
    goes over HTTP. Keeping the choice in one function means the CLI, the
    daemon and the tests cannot disagree about what a target means.
    """
    if is_device_id(target):
        return remote_peer(target, workspace_id, held)
    return peer.RemotePeer(target, workspace_id, held)


Dialer = Callable[[str, str, Any], peer.RemotePeer]
