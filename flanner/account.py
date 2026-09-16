"""Fetching and renewing an entitlement (PRD §11.4).

Everything in this module can reach the network; nothing else below the
composition roots may import it. ``session`` holds the cache and knows
nothing of HTTP, so a command that only reads authorization cannot
accidentally acquire a network call by depending on the wrong module.

Two behaviours are deliberate.

**Refresh failure is not an error.** A cached entitlement stays usable
through its grace window, so being unable to reach the control plane
degrades team features later rather than interrupting work now. Local plan
work never depends on any of this (§18.4).

**The transport is stdlib.** Two JSON POSTs do not justify a dependency in
an MIT client that most users install only for local use.
"""

from __future__ import annotations

import json
import os
import platform
import random
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from . import device_auth, identity, refusals
from . import session as cache
from .entitlements import VALID, approval_subject
from .session import DEFAULT_ENDPOINT, Session

REQUEST_TIMEOUT = 15.0

#: Three tries, matching the control plane's own retry budget. Past this the
#: thing is down rather than blipping, and somebody is watching a prompt.
ATTEMPTS = 3

#: 0.2s then 0.4s, with jitter. Under a second in total, which is the most a
#: person waiting on a command should spend on a problem that fixed itself.
BASE_DELAY = 0.2

#: The longest a `Retry-After` may make us wait before the decision is handed
#: back to the person. A server asking for thirty seconds is telling them
#: something they should hear, not something a CLI should sit through.
MAX_HONOURED_WAIT = 5.0


class SessionError(Exception):
    """Enrolment or renewal was refused, or could not be attempted.

    Carries the control plane's machine-readable `code` where it sent one,
    so a caller can tell "your subscription lapsed" from "your clock is
    wrong" without matching on English. See `flanner.refusals`.

    `code` is `refusals.UNKNOWN` when the refusal carried none — an older
    control plane, or a failure that never reached one at all, such as the
    network being down. Callers must handle that rather than assume a code
    is always meaningful.
    """

    def __init__(self, message: str, *, code: str = refusals.UNKNOWN) -> None:
        super().__init__(message)
        self.code = code

    @property
    def retryable(self) -> bool:
        """Whether the same request could work again without changing it."""
        return refusals.is_retryable(self.code)


def _learn_peers(session: Session) -> Session:
    """Fetch the organization's device keys, right after enrolling or renewing.

    Without these, `flanner peer pull dev_...` cannot turn a teammate's id
    into the public key it needs to dial them — so a freshly enrolled device
    could not reach a single colleague, which is the whole advertised
    workflow. They were only ever fetchable through an internal helper that
    no command called.

    Best-effort on purpose. Enrolment or renewal has already succeeded by the
    time this runs, and failing it because a second request did not land
    would trade a working install for a missing convenience. The keys held
    before are kept, and `whoami --refresh` retries and says what happened.
    """
    try:
        fetch_device_keys()
    except SessionError:
        return session
    refreshed = cache.load()
    return refreshed if refreshed is not None else session


def login(code: str, *, endpoint: str = DEFAULT_ENDPOINT, label: str | None = None) -> Session:
    """Redeem an enrolment code and cache the entitlement it returns."""
    if not code.strip():
        raise SessionError("an enrolment code is required")
    body = _post(
        endpoint,
        "/v1/devices/enroll",
        {
            "enrollment_code": code.strip(),
            "public_key": identity.device_public_key_b64(),
            "label": label or _default_label(),
            "platform": platform.system() or os.name,
        },
    )
    session = _session_from(endpoint, body)
    cache.save(session)
    return _learn_peers(session)


def refresh(session: Session | None = None) -> Session:
    """Renew the entitlement by proving possession of the device key."""
    current = session or cache.load()
    if current is None:
        raise SessionError("this device is not logged in")
    # Signed per attempt, not once: a nonce is spent on use, so a retried
    # envelope would be refused as a replay rather than renewing anything.
    body = _post(
        current.endpoint,
        "/v1/entitlements",
        lambda: device_auth.sign_request({}, device_id=current.device_id).to_dict(),
        repeatable=True,
    )
    renewed = _session_from(current.endpoint, body)
    # The roster arrives with the entitlement; the device keys do not, and
    # copying the old ones forward left a removed teammate's key trusted
    # until somebody thought to run `whoami --refresh`. So a renewal fetches
    # them too. The old keys are kept only for when that fetch fails:
    # dropping them would break artifact verification offline.
    renewed.device_keys = renewed.device_keys or current.device_keys
    # A control plane that predates the field sends none; keep what we knew.
    renewed.org_role = renewed.org_role or current.org_role
    cache.save(renewed)
    return _learn_peers(renewed)


def ensure_fresh(*, now: datetime | None = None) -> Session | None:
    """The best entitlement available, renewing only when one is needed.

    A valid entitlement is used as-is. Anything else is worth a renewal
    attempt, and a failed attempt falls back to what is cached rather than
    raising: an unreachable control plane should cost team features at the
    end of the grace window, not this operation.

    Called on use — by pull, push and `peer serve` — and never on a timer.
    The control plane records a renewal as the device being used, so a
    renewal nobody asked for would make an idle device look active.
    """
    current = cache.load()
    # A session cached before rosters existed renews once, or teammates'
    # review would stay invisible until the entitlement next expired.
    if current is None or (current.status(now=now).status == VALID and current.roster):
        return current
    try:
        return refresh(current)
    except SessionError:
        return current


def _session_from(endpoint: str, body: dict[str, Any]) -> Session:
    """Build a session from an enrolment or renewal response.

    Every optional field the control plane sends has to be read here or it
    is silently dropped, and two advertised features were broken by exactly
    that: `relay_url` was never stored, so the relay fallback never engaged,
    and `device_keys` had nowhere to land even if the server sent them.

    So absent fields are defaulted rather than ignored, and anything new the
    control plane starts sending has one obvious place to be picked up.
    """
    try:
        return Session(
            endpoint=endpoint,
            device_id=str(body["device_id"]),
            organization_id=str(body["organization_id"]),
            user_id=str(body["user_id"]),
            entitlement=str(body["entitlement"]),
            keyring=dict(body.get("keyring") or {}),
            # Where two devices meet when they cannot reach each other
            # directly. Deployment configuration rather than a secret, sent
            # with every entitlement, and until now thrown away on arrival.
            relay_url=str(body.get("relay_url") or ""),
            # Sent by no control plane today; read anyway, so the day one
            # does the client already works rather than needing a release.
            device_keys=dict(body.get("device_keys") or {}),
            roster=str(body.get("roster") or ""),
            org_role=str(body.get("org_role") or ""),
        )
    except (KeyError, TypeError, ValueError) as e:
        raise SessionError(f"the control plane returned something unusable: {e}") from None


def _default_label() -> str:
    import socket

    try:
        return socket.gethostname()
    except OSError:
        return "unnamed device"


def _post(
    endpoint: str,
    path: str,
    payload: dict[str, Any] | Callable[[], dict[str, Any]],
    *,
    repeatable: bool = False,
) -> dict[str, Any]:
    """One call to the control plane, retried where a repeat cannot do harm.

    Args:
        payload: the body, or a function returning one. **A signed request
            must pass the function**, because a nonce is spent on use: the
            same envelope sent twice is refused as a replay, so each attempt
            has to be signed afresh.
        repeatable: the caller's promise that doing this twice is harmless.
            Defaults to False so the promise is made deliberately. Reads and
            idempotent writes qualify; anything that spends a one-shot
            secret or sends an email does not.

    Retries only when the request never completed — a refused connection, a
    DNS failure, a timeout before the first byte. A response with a status,
    including a 5xx, is never repeated: the far side received it. The one
    exception is a 429, where the server has explicitly said to come back
    and nothing was applied.
    """
    url = endpoint.rstrip("/") + path
    if not url.startswith(("http://", "https://")):
        raise SessionError(f"{endpoint} is not an http endpoint")

    for attempt in range(ATTEMPTS):
        body = payload() if callable(payload) else payload
        request = urllib.request.Request(  # noqa: S310 - scheme checked above
            url,
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:  # noqa: S310 - scheme checked before the call
                return dict(json.loads(response.read().decode("utf-8")))
        except urllib.error.HTTPError as e:
            wait = _asked_wait(e)
            if wait is not None and attempt < ATTEMPTS - 1:
                time.sleep(wait)
                continue
            message, code = _refusal(e)
            raise SessionError(message, code=code) from None
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            if not repeatable or attempt == ATTEMPTS - 1:
                raise SessionError(f"could not reach {endpoint}: {e}") from None
            _pause(attempt)
        except ValueError as e:
            raise SessionError(f"the control plane returned something unusable: {e}") from None

    raise AssertionError("unreachable")  # pragma: no cover


def _asked_wait(error: urllib.error.HTTPError) -> float | None:
    """How long the server said to wait, if waiting that long is reasonable.

    Only for 429, where the refusal means nothing was applied — so trying
    again is safe whatever the request was. Anything past `MAX_HONOURED_WAIT`
    is reported instead: a person watching a prompt should be told to come
    back later, not left staring at it.
    """
    if error.code != 429 or not error.headers:
        return None
    try:
        wait = float(error.headers.get("Retry-After") or "")
    except ValueError:
        return None
    return wait if 0 < wait <= MAX_HONOURED_WAIT else None


def _pause(attempt: int) -> None:
    """Back off, with jitter.

    Jitter matters even for one client: several machines whose entitlements
    expire together renew together, and without it they retry together too.
    """
    time.sleep(random.uniform(0, BASE_DELAY * (2**attempt)))  # noqa: S311 - jitter, not crypto


def _refusal(error: urllib.error.HTTPError) -> tuple[str, str]:
    """The server's own explanation, and its code for what happened.

    The sentence is for a person and may change between releases; the code
    is for us and may not. Reading both means a caller can act on the code
    and still show the wording somebody wrote for the situation.
    """
    body: dict[str, Any] = {}
    try:
        body = dict(json.loads(error.read().decode("utf-8")))
    except (ValueError, OSError, TypeError):
        body = {}

    detail = body.get("detail")
    code = str(body.get("code") or "") or refusals.UNKNOWN
    message = str(detail) if detail else f"the control plane refused this request ({error.code})"

    # A refusal that says when to come back is far more useful than one that
    # does not, and the server already worked the number out.
    retry_after = error.headers.get("Retry-After") if error.headers else None
    if retry_after:
        message += f" (try again in {retry_after}s)"
    return message, code


# --- the console: what a member can do for themselves ----------------------
# Each of these signs the request with this device's key, so the machine the
# member already trusts is what authorises the next one. No operator, no
# password, no API token to leak.


def accept_invitation(
    token: str, *, user_id: str, endpoint: str = DEFAULT_ENDPOINT, label: str | None = None
) -> Session:
    """Join an organization and enrol this machine in one step.

    The invitation is spent here. There is no separate "now enrol" step,
    because that would need a second secret to carry the new member between
    the two, and emailing one credential is already one more than ideal.
    """
    if not token.strip():
        raise SessionError("an invitation token is required")
    if not user_id.strip():
        raise SessionError("choose a user id for yourself")
    body = _post(
        endpoint,
        "/v1/invitations/accept",
        {
            "token": token.strip(),
            "user_id": user_id.strip(),
            "public_key": identity.device_public_key_b64(),
            "label": label or _default_label(),
            "platform": platform.system() or os.name,
        },
    )
    session = _session_from(endpoint, body)
    cache.save(session)
    return _learn_peers(session)


def request_enrollment_code() -> tuple[str, str]:
    """Mint a code to type on another machine. Returns the code and expiry."""
    # Not repeatable: this mints a code. A retry after an outcome we
    # never learned leaves a second one valid and unaccounted for.
    body = _signed("/v1/devices/codes", {})
    return str(body["enrollment_code"]), str(body["expires_at"])


def request_approval(
    *, workspace_id: str, proposal_id: str, target_artifact_id: str
) -> dict[str, Any]:
    """Ask the console to confirm an approval. Returns its id, link and code.

    Sends a digest of the proposal and version, never the ids themselves.
    """
    # Not repeatable: each call opens a request somebody has to answer.
    return _signed(
        "/v1/approvals",
        {
            "workspace_id": workspace_id,
            "subject": approval_subject(proposal_id, target_artifact_id),
        },
    )


def approval_status(request_id: str) -> dict[str, Any]:
    """Whether a person has answered, and the signed confirmation if they agreed."""
    return _signed("/v1/approvals/status", {"request_id": request_id}, repeatable=True)


def fetch_device_keys() -> dict[str, str]:
    """Refresh the cached public keys of this organization's devices.

    Kept beside the entitlement rather than fetched per sync: verifying a
    peer's artifacts has to work while the control plane is unreachable,
    which is the whole point of holding signed artifacts in the first place.
    """
    current = cache.load()
    if current is None:
        raise SessionError("this device is not logged in")
    body = _signed("/v1/devices/keyring", {}, repeatable=True)
    current.device_keys = dict(body.get("devices") or {})
    cache.save(current)
    return current.device_keys


def list_devices() -> list[dict[str, Any]]:
    """Every machine enrolled under this member."""
    return list(_signed("/v1/devices/list", {}, repeatable=True).get("devices") or [])


def revoke_device(device_id: str) -> None:
    """Retire a machine. Yours always; anyone's if you are an admin."""
    _signed("/v1/devices/revoke", {"device_id": device_id}, repeatable=True)


def invite_member(email: str, *, admin: bool = False) -> str:
    """Invite someone to the organization. Returns the invitation token."""
    # Not repeatable: this sends an email. Two invitations to the same
    # person is a worse outcome than one failure they can see and redo.
    body = _signed("/v1/members/invite", {"email": email, "role": "admin" if admin else "member"})
    return str(body["token"])


def list_members() -> dict[str, Any]:
    """Who is in the organization, and the seat count that implies."""
    return _signed("/v1/members/list", {}, repeatable=True)


def remove_member(membership_id: str) -> None:
    _signed("/v1/members/remove", {"membership_id": membership_id}, repeatable=True)


def _signed(path: str, body: dict[str, Any], *, repeatable: bool = False) -> dict[str, Any]:
    """Call an endpoint that authenticates by device signature.

    `repeatable` is the caller's promise that doing this twice is harmless,
    and it is passed through rather than assumed: a read is safe, minting a
    code or sending an invitation is not.
    """
    current = cache.load()
    if current is None:
        raise SessionError("this device is not logged in")
    return _post(
        current.endpoint,
        path,
        # Freshly signed each attempt. The nonce is single-use.
        lambda: device_auth.sign_request(body, device_id=current.device_id).to_dict(),
        repeatable=repeatable,
    )


def mesh_credential() -> tuple[str, str, datetime] | None:
    """A fresh credential for joining this organization's private network.

    Deliberately not stored. The control plane issues one with every
    entitlement, so asking again is cheap, and a single-use join key sitting
    in a cache is a secret at rest bought for nothing.

    Returns the credential, the management URL, and when it stops working,
    or None when the deployment has no mesh provider. None is an ordinary
    answer: devices still sync over any network they already share.
    """
    current = cache.load()
    if current is None:
        raise SessionError("this device is not logged in")
    request = device_auth.sign_request({}, device_id=current.device_id)
    body = _post(current.endpoint, "/v1/entitlements", request.to_dict())

    mesh = body.get("mesh") or {}
    credential = str(mesh.get("credential") or "")
    if not credential:
        return None

    # Carried through rather than invented. A join key that has already
    # expired should fail saying so, not fail looking like a bad key.
    raw = str(mesh.get("expires_at") or "")
    try:
        expires_at = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        expires_at = datetime.now(timezone.utc)
    return credential, str(mesh.get("management_url") or ""), expires_at
