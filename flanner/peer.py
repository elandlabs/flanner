"""Syncing with another device over the network (PRD §14).

``sync`` defines the protocol and left the transport out on purpose. This
fills it in: a handler that answers for this device's catalog, and a
:class:`Peer` implementation that talks to another device's.

Both halves live here because they are two ends of one wire format. Putting
them in separate modules is how the two drift.

**Carriers are interchangeable, access rules are not.** HTTP is built in
here because it needs nothing beyond the standard library;
:mod:`flanner.peer_iroh` adds one that works when neither device is
reachable. Both hand every request to :func:`serve_request`, so there is
exactly one place that decides who may read what.

**What authorises a peer.** Three checks, and none normally needs the
control plane at the time of the request:

1. *You hold this device's key.* A device id is the hash of its public key,
   so a caller that sends a public key which hashes to the id it claims has
   proved the two belong together, and the signature over the request
   proves it holds the private half. No key registry is needed for this.
2. *That device may read this workspace.* The caller sends its entitlement,
   which the control plane signed and which names both the device id and
   the workspace role. The ids in the two proofs must match, so an
   entitlement is useless to anyone who did not receive it.
3. *That device is still on the team.* The signed roster this device
   holds must list the caller's device under the caller's user. An
   entitlement says who someone was when it was issued; the roster is how a
   removal reaches a peer.

The entitlement must also carry the team-sync feature. A role alone says
which workspace, not whether syncing was paid for, and checking only the
role would leave a lapsed subscription syncing exactly as before.

Serving others needs everything current: the caller's entitlement, the
roster, and this device's own entitlement. Grace is for a device reading
what it already holds. When this device's own entitlement or roster is not
enough, it renews once, rate-limited, before refusing.

Together those mean a peer can authorise a stranger while offline, for as
long as its own entitlement and roster are current, which is the property
that makes a mesh worth having.

**What is still checked afterwards.** Authorisation is not verification.
Every artifact received is checked against its *author's* key, which is
usually a third device, using the organization keyring cached at login.
A peer you trust to talk to is not a peer you trust to be honest about
what someone else wrote (§14.4).

**What this is not.** There is no private addressing here. This speaks to
whatever address it is given, so it works over a LAN, a tunnel, or a mesh
provider's network without changing. Providing a route is a transport's
job, and deliberately not this module's.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from . import artifacts, entitlements, identity, refusals, replay, sync
from . import push as push_rules
from .assurance import retired_plan_ids
from .device_auth import SignedRequest, sign_request, verify_request
from .sync import Manifest

DEFAULT_PORT = 8776
REQUEST_TIMEOUT = 30.0

#: Where an http refusal carries its :mod:`flanner.refusals` code. A header
#: rather than the body, so the body stays the plain detail older peers read.
REFUSAL_HEADER = "X-Flanner-Refusal"

#: Carries one signed request to a peer and returns its reply.
Transport = Callable[[str, dict[str, Any]], dict[str, Any]]


class PeerError(Exception):
    """A peer could not be reached, or refused.

    ``status`` exists so the answer does not depend on the carrier. HTTP
    turns it into a response code; the iroh transport puts it in the reply
    body. Refusals then read the same either way, which is what keeps the
    two transports from drifting into different behaviour.

    ``code`` is the same vocabulary the control plane uses
    (:mod:`flanner.refusals`), set where a refusal has a meaning a program
    could act on, and ``refusals.UNKNOWN`` elsewhere.
    """

    def __init__(self, message: str, *, status: int = 403, code: str = refusals.UNKNOWN) -> None:
        super().__init__(message)
        self.status = status
        self.code = code


@dataclass(frozen=True)
class PeerIdentity:
    """A caller that has proved who it is and what it may read."""

    device_id: str
    user_id: str
    organization_id: str
    role: str
    # Carried so the serving path can spend it. Authorising proves the
    # request was signed by this device; spending the nonce proves it is not
    # the same request twice.
    nonce: str = ""
    # Whether this caller's entitlement covers memory as well as plans. A
    # separate flag rather than a second authorisation call, because both
    # answers come from the same verified claims and asking twice is how
    # they come to disagree.
    may_sync_memory: bool = False
    #: And whether it covers skill packages. Read alongside the memory flag
    #: from the same verified claims, so the two cannot disagree about what
    #: one entitlement said.
    may_sync_skills: bool = False


def authorize(
    payload: dict[str, Any],
    workspace_id: str,
    issuer_keyring: dict[str, str],
    *,
    now: Any = None,
    strict: bool = False,
) -> PeerIdentity:
    """Decide whether a caller may reach this workspace. Raises if not.

    ``strict`` refuses an entitlement that is merely inside its grace
    window. The serving path always sets it, for reads as well as writes.

    Never trusts a claimed device id. The public key travels with the
    request and must hash to the id it claims, which is what lets this work
    without a registry lookup.
    """
    try:
        request = SignedRequest.from_dict(payload)
    except ValueError as e:
        raise PeerError(f"malformed request: {e}") from None

    public_key = str(request.body.get("public_key") or "")
    if not public_key:
        raise PeerError("request carries no public key")
    try:
        derived = identity.device_id_for(identity.load_public_key(public_key))
    except ValueError as e:
        raise PeerError(f"unusable public key: {e}") from None
    if derived != request.device_id:
        raise PeerError("public key does not match the device id claimed")

    result = verify_request(request, public_key, now=now)
    if not result:
        raise PeerError(result.reason)

    verdict = entitlements.verify(
        str(request.body.get("entitlement") or ""), issuer_keyring, now=now
    )
    if not verdict.usable or verdict.claims is None:
        raise PeerError(f"entitlement is {verdict.status}")
    # A device asking a peer for anything is online, so it cannot claim it
    # was unable to renew. Grace is for a device's own reads of what it
    # already holds, which never come through here. Refusing it when serving
    # is what keeps a revoked device's window down to one entitlement
    # lifetime instead of a lifetime plus the grace period.
    if strict and verdict.status != entitlements.VALID:
        raise PeerError(
            f"being served needs a current entitlement, and this one is {verdict.status}; "
            "run 'flanner whoami --refresh'"
        )
    if verdict.claims.device_id != request.device_id:
        raise PeerError("that entitlement was issued to a different device")

    # A role says which workspace; the feature says whether syncing is
    # bought at all. Checking only the role would leave team sync working
    # after a subscription lapsed, which would make billing decorative.
    if not verdict.claims.has_feature(entitlements.TEAM_SYNC):
        raise PeerError("this entitlement does not include team sync")

    role = verdict.claims.role_in(workspace_id)
    if role is None:
        raise PeerError("no access to this workspace")

    return PeerIdentity(
        nonce=request.nonce,
        device_id=request.device_id,
        user_id=verdict.claims.user_id,
        organization_id=verdict.claims.organization_id,
        role=role,
        may_sync_memory=verdict.claims.has_feature(entitlements.MEM_SYNC),
        may_sync_skills=verdict.claims.has_feature(entitlements.SKILL_SYNC),
    )


MANIFEST = "manifest"
FETCH = "fetch"
#: A sender naming what it has, so the receiver can say what it lacks. Two
#: round trips rather than one on purpose: a sender that ships everything
#: blindly wastes exactly the bandwidth this design exists to save, and it
#: is also what makes an echo — B pushing back what A just sent — cost one
#: small request instead of a re-upload.
OFFER = "offer"
PUSH = "push"

#: Operations that write. Held apart from the read ones because the
#: entitlement rule differs and that difference must be impossible to miss.
WRITES = frozenset({OFFER, PUSH})


def accepting_pushes() -> bool:
    """Whether this device takes artifacts it did not ask for.

    On by default: every peer in a mesh is already an authorised teammate,
    and accepting cannot overwrite anything. A laptop on a metered
    connection can opt out with `flanner peer pushes off`, the web UI, or
    ``FLANNER_ACCEPT_PUSHES=0``. Saying no is a plain answer rather than an
    error: a device that refuses pushes still serves every read.
    """
    from .identity import pushes_preference

    return pushes_preference()[0]


#: Rate limiting is per process and per device. Module level so it survives
#: between requests, which is the only way a window means anything.
_pushes = push_rules.RateLimiter()

#: Shared by every peer, because the keyring it guards is per organisation.
_keyring_cooldown = push_rules.Cooldown()


def _requested(operation: str, body: dict[str, Any]) -> tuple[list[str], list[dict[str, Any]]]:
    """What this operation asks for, bounded before anyone is authorised.

    Batch limits belong here rather than after the signature check: an
    oversized request is refusable without knowing who sent it, and parsing a
    large body only to reject it is work an unauthenticated caller can ask
    for. Refusing early keeps that cheap.

    Returns the ids asked for and the artifacts offered. Only one is ever
    populated; which depends on the operation.
    """
    wanted: list[str] = []
    items: list[dict[str, Any]] = []

    if operation == FETCH:
        wanted = [str(x) for x in (body.get("artifact_ids") or [])]
        if len(wanted) > sync.MAX_FETCH_BATCH:
            raise PeerError(f"at most {sync.MAX_FETCH_BATCH} artifacts per request", status=413)
        return wanted, items

    if operation == MANIFEST:
        return wanted, items

    if operation not in WRITES:
        raise PeerError(f"unknown operation: {operation}", status=404)

    # Answered before authorising, because "I do not accept pushes" is not a
    # secret and making a sender prove itself only to be told no wastes both
    # sides' time. It is a 200-level fact expressed as a refusal, not a
    # failure: the device is healthy and still serves every read.
    if not accepting_pushes():
        raise PeerError("this device is not accepting pushes", status=403)

    if operation == OFFER:
        wanted = [str(x) for x in (body.get("artifact_ids") or [])]
        if len(wanted) > sync.MAX_PUSH_BATCH:
            raise PeerError(f"at most {sync.MAX_PUSH_BATCH} artifacts per offer", status=413)
        return wanted, items

    items = [x for x in (body.get("artifacts") or []) if isinstance(x, dict)]
    too_big = push_rules.check_batch(items)
    if too_big:
        raise PeerError(too_big, status=413)
    return wanted, items


def _serve_fetch(
    session: Any,
    *,
    wanted: list[str],
    workspace_id: str,
    hidden: Any,
    memory: bool = True,
    skills: bool = True,
) -> dict[str, Any]:
    """Hand over what was asked for, minus what this caller may not have.

    Both filters are withholding rules, not display rules, which is why they
    run here and not in the manifest alone: a caller can ask for an id it was
    never offered.
    """
    out = []
    for envelope, blob in sync.LocalPeer(session).fetch(wanted):
        # The caller proved access to one workspace, so anything belonging to
        # another is withheld even if asked for by id. Ids are guessable in
        # principle; access is not.
        if envelope.get("workspace_id") != workspace_id:
            continue
        # Asked for despite not being offered, which an older peer or a stale
        # manifest will do. Withheld here too, or the manifest filter would be
        # advisory.
        retired = (
            envelope.get("plan_file_id") in hidden
            and envelope.get("artifact_type") != artifacts.PLAN_TOMBSTONE
        )
        if retired:
            continue
        # Withheld for the same reason a push of one is refused: an
        # organization that switched memory sharing off, or a subscription
        # that lapsed, has to mean the memory does not travel either way.
        if not memory and envelope.get("artifact_type") in push_rules.MEMORY_TYPES:
            continue
        if not skills and envelope.get("artifact_type") in push_rules.SKILL_TYPES:
            continue
        payload = blob.decode("utf-8", errors="replace") if blob is not None else None
        out.append({"envelope": envelope, "payload": payload})
    return {"artifacts": out}


def serve_request(
    operation: str,
    payload: dict[str, Any],
    sessions: Any,
    held: Any,
    refresh_keys: Any = None,
) -> dict[str, Any]:
    """Answer one peer request, whatever carried it here.

    The two proofs, the workspace checks and the withholding rule all live
    in this one function on purpose. A transport that reimplemented any of
    them would be a second place for the access rules to be wrong, and the
    two would diverge the first time one was changed.

    ``held`` is a callable read per request rather than captured, so a
    renewed entitlement or a refreshed keyring takes effect without a
    restart.

    Every outcome is recorded, because `peer serve` runs unattended: the
    person whose machine refused a colleague is not watching when it
    happens, and a refusal that leaves no trace cannot be explained later.
    """
    from . import observe

    started = time.perf_counter()
    device = str(payload.get("device_id") or "")
    try:
        answer = _serve_request(operation, payload, sessions, held, refresh_keys)
    except PeerError as e:
        observe.served(
            operation,
            ms=(time.perf_counter() - started) * 1000,
            ok=False,
            peer=device,
            reason=str(e),
        )
        raise
    observe.served(operation, ms=(time.perf_counter() - started) * 1000, ok=True, peer=device)
    return answer


def _serve_request(
    operation: str,
    payload: dict[str, Any],
    sessions: Any,
    held: Any,
    refresh_keys: Any = None,
) -> dict[str, Any]:
    """The work itself. Wrapped above so every path is recorded once."""
    current = held()
    if current is None:
        # Not logged in: this device has no way to check anyone's
        # entitlement, so it serves nobody rather than serving everyone.
        raise PeerError("this device is not part of a team", status=503)

    body = payload.get("body") or {}
    workspace_id = str(body.get("workspace_id") or "")
    if not workspace_id:
        raise PeerError("workspace_id is required", status=400)

    wanted, items = _requested(operation, body)

    caller = authorize(payload, workspace_id, dict(current.keyring), strict=True)

    # A device whose own entitlement has lapsed may have been removed, and a
    # removed device must not hand out the team's plans. Renewed once before
    # refusing, so a server that ran overnight is not refused for that alone.
    # After authorising, so only a proven, entitled caller can cause a call
    # to the control plane.
    if not _current(current):
        current = _relearned(held, refresh_keys) or current
    if not _current(current):
        raise PeerError(
            f"this device's own entitlement is {current.status().status}, "
            "so it serves nobody until it renews",
            status=503,
        )

    # The entitlement proves who the caller was when it was issued; the
    # roster says whether they still belong. A caller this device has not
    # heard of may be a teammate who joined since the roster was fetched,
    # so it is fetched once more, rate-limited, before refusing.
    if not _on_roster(current, caller, workspace_id):
        current = _relearned(held, refresh_keys) or current
        if not _on_roster(current, caller, workspace_id):
            raise PeerError(
                "that device is not on this workspace's current roster",
                code=refusals.DEVICE_UNKNOWN,
            )

    # After authorisation, so the limit is keyed to a device id that was
    # actually proved rather than one a caller asserted.
    if operation == PUSH and not _pushes.allow(caller.device_id):
        raise PeerError("too many pushes; try again shortly", status=429)

    with sessions() as session:
        # Spend the nonce, after the signature and never before: recording it
        # first would let any caller fill the table, or burn a nonce for a
        # request they had not authored.
        #
        # This is what makes the freshness window a clock tolerance rather
        # than the sole replay defence. Without it, widening the window for
        # machines whose clocks drift would widen the replay window by the
        # same amount, and there would be no way to have one without the
        # other.
        if not replay.remember(session, device_id=caller.device_id, nonce=caller.nonce):
            raise PeerError("this request has already been answered", status=409)

        # Plans this device has been asked to stop showing. Computed once
        # per request and used by both read paths, so a retired plan is
        # neither advertised nor handed over. The tombstones themselves
        # always travel; they are how the claim reaches anybody else.
        hidden = retired_plan_ids(session)

        if operation == MANIFEST:
            return dict(sync.build_manifest(session, workspace_id, hidden=hidden).to_dict())

        if operation in WRITES:
            return _serve_write(
                operation,
                session,
                caller,
                workspace_id,
                wanted,
                items,
                current.resolve_device_key,
                refresh_keys,
            )

        return _serve_fetch(
            session,
            wanted=wanted,
            workspace_id=workspace_id,
            hidden=hidden,
            memory=caller.may_sync_memory,
            skills=caller.may_sync_skills,
        )


def _current(current: Any) -> bool:
    """Whether a session's own entitlement is current, not merely usable."""
    return bool(current.status().status == entitlements.VALID)


def _on_roster(current: Any, caller: PeerIdentity, workspace_id: str) -> bool:
    """Whether the signed roster this device holds lists the caller's device.

    Under the caller's own user, in this workspace, and only while the
    roster is current. A roster in grace is as old as an entitlement in
    grace, and serving others on either is what gave a removed member a
    week of reading. The device keyring is not used: it is unsigned.
    """
    roster = entitlements.verify_roster(current.roster, current.keyring, grace=timedelta(0))
    return roster is not None and any(
        member.user_id == caller.user_id and caller.device_id in member.devices
        for member in roster.members(workspace_id)
    )


def _relearned(held: Any, refresh_keys: Any) -> Any:
    """Renew once if the shared cooldown allows, and read the session back.

    The same cooldown as the unknown-author refresh, because it is the same
    call: a renewal brings the entitlement, the roster and the keys at once.
    None when nothing was renewed, so the caller keeps what it had.
    """
    if push_rules.relearn(refresh_keys, _keyring_cooldown) is None:
        return None
    return held()


def _serve_write(
    operation: str,
    session: Any,
    caller: PeerIdentity,
    workspace_id: str,
    wanted: list[str],
    items: list[dict[str, Any]],
    resolve_key: Any,
    refresh_keys: Any = None,
) -> dict[str, Any]:
    """The receiving half of a push: say what is missing, then take it in."""
    if operation == OFFER:
        # Only the subset we lack, so the sender uploads nothing we already
        # hold. Ids offered for another workspace are simply not asked for.
        held = sync.build_manifest(session, workspace_id).artifact_ids
        return {"wanted": sorted(set(wanted) - set(held))}

    report = push_rules.accept(
        session,
        items,
        workspace_id=workspace_id,
        role=caller.role,
        resolve_key=resolve_key,
        refresh_keys=refresh_keys,
        cooldown=_keyring_cooldown,
        memory_sync=caller.may_sync_memory,
        skill_sync=caller.may_sync_skills,
    )
    return {
        "accepted": report.accepted,
        "already_held": report.already_held,
        "rejected": [{"artifact_id": a, "reason": r} for a, r in report.rejected],
    }


def create_peer_app(sessions: Any, held: Any, refresh_keys: Any = None) -> Any:
    """An HTTP app serving this device's catalog to authorised peers.

    ``held`` is a callable returning the current cached session, read per
    request rather than captured, so a renewed entitlement or a refreshed
    keyring takes effect without a restart.
    """
    from fastapi import FastAPI, HTTPException

    app = FastAPI(title="Flanner peer", version=str(sync.PROTOCOL_VERSION))

    @app.middleware("http")
    async def refuse_oversized(request: Any, call_next: Any) -> Any:
        """Bound the request before FastAPI parses it into a dict.

        Every other limit here runs after the body has been read and decoded,
        which bounds what a peer may store rather than what it may make this
        device allocate. An unauthenticated caller could send a gigabyte and
        have it parsed before a signature was so much as looked at.

        A body with no declared length is refused rather than counted. ASGI
        servers deliver exactly the declared number of bytes for a framed
        body, so the header is a real bound; a chunked body has no bound to
        check, and this protocol's own clients always declare one.
        """
        from starlette.responses import PlainTextResponse

        declared = request.headers.get("content-length")
        if request.method == "POST" and (declared is None or not declared.isdigit()):
            return PlainTextResponse("Refused: declare a content-length.", status_code=411)
        if declared is not None and declared.isdigit() and int(declared) > sync.MAX_REQUEST_BYTES:
            return PlainTextResponse(
                f"Refused: at most {sync.MAX_REQUEST_BYTES} bytes per request.", status_code=413
            )
        return await call_next(request)

    def _serve(operation: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            return serve_request(operation, payload, sessions, held, refresh_keys)
        except PeerError as e:
            raise HTTPException(e.status, str(e), headers={REFUSAL_HEADER: e.code}) from None

    @app.post("/peer/manifest")
    def manifest(payload: dict[str, Any]) -> dict[str, Any]:
        """What this device holds for a workspace the caller may read."""
        return _serve(MANIFEST, payload)

    @app.post("/peer/fetch")
    def fetch(payload: dict[str, Any]) -> dict[str, Any]:
        """Hand over specific artifacts, and only from the named workspace."""
        return _serve(FETCH, payload)

    @app.post("/peer/offer")
    def offer(payload: dict[str, Any]) -> dict[str, Any]:
        """Which of these artifacts this device does not already hold."""
        return _serve(OFFER, payload)

    @app.post("/peer/push")
    def receive(payload: dict[str, Any]) -> dict[str, Any]:
        """Take in artifacts a peer sent, each verified against its author."""
        return _serve(PUSH, payload)

    return app


def sign_body(body: dict[str, Any], held: Any) -> dict[str, Any]:
    """Sign one request body as this device. Shared by every transport.

    One read of the key, and the id derived from it, so the public key and
    the device id cannot disagree. Taking the id from the cached session
    instead would produce a request the far side rejects as a key mismatch,
    which blames the wrong thing entirely.
    """
    current = held()
    if current is None:
        raise PeerError("this device is not logged in")

    key = identity.load_or_create_device_key()
    device_id = identity.device_id_for(key.public_key())
    if device_id != current.device_id:
        raise PeerError(
            "the cached session belongs to a different device; run 'flanner login' again"
        )

    # The public key travels with every request so the far side can
    # check it against the device id without a registry.
    signed = sign_request(
        {
            **body,
            "public_key": identity.public_key_b64(key.public_key()),
            "entitlement": current.entitlement,
        },
        device_id=device_id,
        signing_key=key,
    )
    return dict(signed.to_dict())


def http_transport(address: str, *, timeout: float = REQUEST_TIMEOUT) -> Transport:
    """Carry requests to a peer at an http address.

    This is the original transport and remains the one that needs no
    dependency beyond the standard library. It requires the far side to be
    reachable, which is exactly the constraint :mod:`flanner.peer_iroh`
    exists to remove.
    """
    import json
    import urllib.error
    import urllib.request

    base = address.rstrip("/")

    def send(operation: str, signed: dict[str, Any]) -> dict[str, Any]:
        url = f"{base}/peer/{operation}"
        if not url.startswith(("http://", "https://")):
            raise PeerError(f"{base} is not an http address")
        request = urllib.request.Request(  # noqa: S310 - scheme checked above
            url,
            data=json.dumps(signed).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - scheme checked before the call
                return dict(json.loads(response.read().decode("utf-8")))
        except urllib.error.HTTPError as e:
            code = (e.headers.get(REFUSAL_HEADER) if e.headers else None) or refusals.UNKNOWN
            raise PeerError(_detail(e), status=e.code, code=code) from None
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise PeerError(f"could not reach {base}: {e}") from None
        except ValueError as e:
            raise PeerError(f"peer sent something unusable: {e}") from None

    return send


class RemotePeer:
    """Another device. Implements :class:`sync.Peer`.

    How the request travels is the ``transport``'s business; what it says
    and what proves it is this class's. Defaults to HTTP so existing
    callers that pass an address keep working unchanged.
    """

    def __init__(
        self,
        address: str,
        workspace_id: str,
        held: Any,
        *,
        timeout: float = REQUEST_TIMEOUT,
        transport: Transport | None = None,
    ) -> None:
        self._address = address.rstrip("/")
        # Held on the instance because sync.Peer.fetch takes only ids: the
        # workspace is a property of the connection, not of each request.
        self._workspace = workspace_id
        self._held = held
        self._timeout = timeout
        self._send = transport or http_transport(address, timeout=timeout)

    @property
    def transport(self) -> Transport:
        """The carrier in use, for a caller that wants to ask it something.

        Exposed because a transport may know things worth reporting that
        the protocol does not carry, such as whether the connection was
        relayed. Reading it is optional; nothing here depends on it.
        """
        return self._send

    def _post(self, operation: str, body: dict[str, Any]) -> dict[str, Any]:
        return self._send(operation, sign_body(body, self._held))

    def manifest(self, workspace_id: str) -> Manifest:
        return Manifest.from_dict(self._post(MANIFEST, {"workspace_id": workspace_id}))

    def fetch(self, artifact_ids: list[str]) -> list[tuple[dict[str, Any], bytes | None]]:
        """Fetch in batches, so one large sync is not one huge request."""
        out: list[tuple[dict[str, Any], bytes | None]] = []
        for start in range(0, len(artifact_ids), sync.MAX_FETCH_BATCH):
            batch = artifact_ids[start : start + sync.MAX_FETCH_BATCH]
            body = self._post(FETCH, {"workspace_id": self._workspace, "artifact_ids": batch})
            for item in body.get("artifacts") or []:
                envelope = item.get("envelope")
                if not isinstance(envelope, dict):
                    continue
                blob = item.get("payload")
                out.append((envelope, blob.encode("utf-8") if blob is not None else None))
        return out

    def offer(self, artifact_ids: list[str]) -> list[str]:
        """Ask which of these the peer lacks. Never sends the artifacts."""
        out: list[str] = []
        for start in range(0, len(artifact_ids), sync.MAX_PUSH_BATCH):
            batch = artifact_ids[start : start + sync.MAX_PUSH_BATCH]
            body = self._post(OFFER, {"workspace_id": self._workspace, "artifact_ids": batch})
            out.extend(str(x) for x in (body.get("wanted") or []))
        return out

    def push(self, items: list[dict[str, Any]]) -> dict[str, Any]:
        """Upload artifacts the peer said it wanted."""
        return self._post(PUSH, {"workspace_id": self._workspace, "artifacts": items})


def pull(
    session: Any,
    address: str,
    workspace_id: str,
    held: Any,
    *,
    remote: Any = None,
    project: Any = None,
) -> sync.SyncReport:
    """Pull everything a peer holds for a workspace that this device lacks.

    ``project`` is where pulled plans should be written when the workspace
    alone cannot say — a team with several repositories has several local
    projects in one workspace. The CLI passes the one the person named or
    ran from; the background catch-up passes nothing and lets the sync
    apply its own rule.

    Artifacts are verified against their author's key from the cached
    organization keyring, not against the peer that handed them over.

    ``remote`` lets a caller supply a peer reached some other way. It exists
    because choosing a transport means knowing about every transport, and
    this module deliberately knows about one. The composition root chooses;
    defaulting to HTTP keeps every existing caller working.
    """
    current = held()
    if current is None:
        report = sync.SyncReport()
        report.rejected.append(("<peer>", "this device is not logged in"))
        return report

    peer = remote or RemotePeer(address, workspace_id, held)
    return sync.sync_from_peer(
        session, peer, workspace_id, current.resolve_device_key, project=project
    )


def _batches(items: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Split a push into requests the receiver will accept.

    Bounded by count and by bytes, because either alone lets the other
    through: fifty tiny comments are fine, and two large plan versions are
    also fine, but fifty large ones are not.

    An item too big to travel even alone is still emitted as its own batch.
    Silently dropping it here would report success for something that was
    never sent; letting the receiver refuse it puts the reason in the
    report where somebody can read it.
    """
    out: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    size = 0
    for item in items:
        weight = len(str(item.get("payload") or ""))
        if current and (
            len(current) >= sync.MAX_PUSH_BATCH or size + weight > sync.MAX_PUSH_BYTES
        ):
            out.append(current)
            current, size = [], 0
        current.append(item)
        size += weight
    if current:
        out.append(current)
    return out


def push(
    session: Any,
    address: str,
    workspace_id: str,
    held: Any,
    *,
    remote: Any = None,
) -> sync.SyncReport:
    """Send a peer everything it lacks for a workspace.

    The mirror of :func:`pull`, and deliberately the same shape: offer ids,
    upload only what was asked for, report what happened. The receiver
    decides what it will take, verifies every artifact against its author's
    key, and may refuse the whole thing — none of which is this side's
    business beyond reporting it.

    Nothing is queued. A peer that is offline is simply not pushed to, and
    picks the work up on its next catch-up pull. A disk queue would be a
    second copy of the artifact store with its own retry semantics, to solve
    a problem the pull path already solves.
    """
    current = held()
    report = sync.SyncReport()
    if current is None:
        report.rejected.append(("<peer>", "this device is not logged in"))
        return report

    mine = sorted(sync.build_manifest(session, workspace_id).artifact_ids)
    if not mine:
        return report

    peer = remote or RemotePeer(address, workspace_id, held)
    try:
        wanted = peer.offer(mine)
    except Exception as e:
        report.rejected.append(("<offer>", f"peer would not take an offer: {e}"))
        return report
    if not wanted:
        report.already_held.extend(mine)
        return report

    local = sync.LocalPeer(session)
    items = [
        {
            "envelope": envelope,
            "payload": blob.decode("utf-8", errors="replace") if blob is not None else None,
        }
        for envelope, blob in local.fetch(wanted)
    ]

    for batch in _batches(items):
        try:
            answer = peer.push(batch)
        except Exception as e:
            for item in batch:
                envelope = item.get("envelope") or {}
                report.rejected.append((str(envelope.get("artifact_id", "<unknown>")), str(e)))
            continue
        report.accepted.extend(str(x) for x in (answer.get("accepted") or []))
        report.already_held.extend(str(x) for x in (answer.get("already_held") or []))
        for refusal in answer.get("rejected") or []:
            if isinstance(refusal, dict):
                report.rejected.append(
                    (str(refusal.get("artifact_id", "<unknown>")), str(refusal.get("reason", "")))
                )
    return report


def catch_up(
    session: Any,
    workspace_ids: list[str],
    held: Any,
    *,
    dial: Any = None,
    on_result: Any = None,
) -> dict[str, sync.SyncReport]:
    """Pull from every teammate device this one knows about.

    The offline half of the signal. A push tells a device that was running;
    this is what a device that was asleep does instead, and between the two
    nobody needs a notification service to learn that a plan moved.

    Peers come from the cached session's device keyring, which is already
    the list of machines whose signatures this device would accept — so
    there is no second notion of "known peer" to keep in step with it.

    Every failure is a result, not an exception. Most peers are expected to
    be unreachable at any given moment; a laptop that is shut cannot be a
    reason for this device not to start.

    ``dial`` maps a device id to a peer. Required in practice, because
    choosing a transport means knowing about every transport and this
    module knows about one.
    """
    reports: dict[str, sync.SyncReport] = {}
    current = held()
    if current is None:
        return reports

    for device_id in sorted(current.device_keys):
        if device_id == current.device_id:
            continue
        for workspace_id in workspace_ids:
            key = f"{device_id}/{workspace_id}"
            try:
                remote = dial(device_id, workspace_id) if dial else None
                report = pull(session, device_id, workspace_id, held, remote=remote)
            except Exception as e:
                # Including whatever the transport raises while dialling.
                # A catch-up that can crash a daemon start is worse than no
                # catch-up at all.
                report = sync.SyncReport()
                report.rejected.append(("<peer>", str(e)))
            reports[key] = report
            if on_result is not None:
                on_result(device_id, workspace_id, report)
    return reports


def _detail(error: Any) -> str:
    import json

    try:
        detail = json.loads(error.read().decode("utf-8")).get("detail")
    except (ValueError, OSError):
        detail = None
    return str(detail) if detail else f"peer refused the request ({error.code})"


__all__ = [
    "DEFAULT_PORT",
    "PeerError",
    "PeerIdentity",
    "RemotePeer",
    "authorize",
    "catch_up",
    "create_peer_app",
    "pull",
    "push",
]
