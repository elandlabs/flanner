"""Verifying signed entitlements and workspace capabilities (PRD §11.3, §18.3).

This module only ever *verifies*. Issuing requires the control plane's
private signing key and deliberately lives elsewhere, so a client can
confirm what it was granted without being able to grant anything.

An entitlement says who a device belongs to, what the subscription allows,
and which role its owner holds in each workspace. Peers check it locally
rather than calling the control plane on every operation (§18.3), which is
also what lets flanner keep working while the network or the control plane
is down.

Two properties matter more than the format:

- **Expiry is graded, not binary.** A device that has been offline for a
  weekend is not a security problem, so an expired entitlement stays usable
  through a defined grace window and reports that it is doing so (§11.4).
  A caller that wants strictness can still refuse anything not ``VALID``.
- **Key rotation needs overlap.** Verification takes a keyring rather than
  one key, so a new signing key can be introduced before the old one is
  retired without invalidating every entitlement in flight (§18.3).
"""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from . import identity
from .artifacts import canonical_bytes

ISSUER = "flanner.io"
TOKEN_SEPARATOR = "."  # noqa: S105 - delimiter between claims and signature

# Features an entitlement may carry. Named here rather than at each caller
# so that a plan change and the check that enforces it cannot drift apart.
TEAM_SYNC = "team_sync"
LIVE_COLLABORATION = "live_collaboration"
#: Carrying memory between devices, as distinct from carrying plans.
#: A separate flag so a plan-only tier stays possible, and so a
#: device that predates memory sharing simply does not have it
#: rather than being told it does and then refusing.
MEM_SYNC = "mem_sync"
#: Carrying a skill package between devices. Separate from `MEM_SYNC` for
#: the same reason that one is separate from plans: they are different
#: things to be paying for, and an organization may reasonably want its
#: procedures shared while its durable context stays put.
SKILL_SYNC = "skill_sync"

# How long an expired entitlement keeps working offline before team features
# stop. Local plan work never depends on this (§18.4).
DEFAULT_GRACE = timedelta(days=7)

# Verification outcomes, in the order a caller should care about them.
VALID = "valid"
IN_GRACE = "in_grace"
EXPIRED = "expired"
UNTRUSTED_KEY = "untrusted_key"
BAD_SIGNATURE = "bad_signature"
MALFORMED = "malformed"


@dataclass(frozen=True)
class WorkspaceCapability:
    """One workspace, and the role its holder has there."""

    workspace_id: str
    role: str

    def to_dict(self) -> dict[str, str]:
        return {"workspace_id": self.workspace_id, "role": self.role}


@dataclass(frozen=True)
class Claims:
    """What the control plane asserts about a device."""

    organization_id: str
    user_id: str
    device_id: str
    key_id: str
    issued_at: str
    expires_at: str
    plan: str = "managed_mesh"
    issuer: str = ISSUER
    features: tuple[str, ...] = ()
    workspace_capabilities: tuple[WorkspaceCapability, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "issuer": self.issuer,
            "organization_id": self.organization_id,
            "user_id": self.user_id,
            "device_id": self.device_id,
            "plan": self.plan,
            "features": list(self.features),
            "workspace_capabilities": [c.to_dict() for c in self.workspace_capabilities],
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
            "key_id": self.key_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Claims:
        """Rebuild claims from their wire form; raises ValueError if malformed."""
        try:
            capabilities = tuple(
                WorkspaceCapability(
                    workspace_id=str(entry["workspace_id"]), role=str(entry["role"])
                )
                for entry in (data.get("workspace_capabilities") or [])
            )
            return cls(
                issuer=str(data.get("issuer", ISSUER)),
                organization_id=str(data["organization_id"]),
                user_id=str(data["user_id"]),
                device_id=str(data["device_id"]),
                plan=str(data.get("plan", "managed_mesh")),
                features=tuple(data.get("features") or ()),
                workspace_capabilities=capabilities,
                issued_at=str(data["issued_at"]),
                expires_at=str(data["expires_at"]),
                key_id=str(data["key_id"]),
            )
        except (KeyError, TypeError, ValueError) as e:
            raise ValueError(f"Malformed entitlement claims: {e}") from None

    def role_in(self, workspace_id: str) -> str | None:
        for capability in self.workspace_capabilities:
            if capability.workspace_id == workspace_id:
                return capability.role
        return None

    def has_feature(self, feature: str) -> bool:
        return feature in self.features


@dataclass(frozen=True)
class Verdict:
    """Whether an entitlement may be used, and why."""

    status: str
    claims: Claims | None = None
    reason: str = ""

    @property
    def usable(self) -> bool:
        """True while the entitlement still authorizes team features.

        Grace counts as usable on purpose: refusing a device because its
        owner spent a week offline would punish the disconnection this
        design exists to tolerate.
        """
        return self.status in (VALID, IN_GRACE)

    def __bool__(self) -> bool:
        return self.usable


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64url(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def encode_token(claims: Claims, signature_b64: str) -> str:
    """Pack claims and their signature into one self-contained string."""
    return _b64url(canonical_bytes(claims.to_dict())) + TOKEN_SEPARATOR + signature_b64


def signing_payload(claims: Claims) -> bytes:
    """Exactly the bytes an issuer signs and a verifier checks."""
    return canonical_bytes(claims.to_dict())


def _parse(token: str) -> tuple[Claims, str]:
    body, _, signature = token.partition(TOKEN_SEPARATOR)
    if not body or not signature:
        raise ValueError("token is not <claims>.<signature>")
    try:
        raw = _unb64url(body)
    except (binascii.Error, ValueError) as e:
        raise ValueError(f"claims are not valid base64url: {e}") from None
    import json

    try:
        data = json.loads(raw)
    except ValueError as e:
        raise ValueError(f"claims are not valid JSON: {e}") from None
    if not isinstance(data, dict):
        raise ValueError("claims are not an object")
    return Claims.from_dict(data), signature


def verify(
    token: str,
    keyring: dict[str, str],
    *,
    now: datetime | None = None,
    grace: timedelta = DEFAULT_GRACE,
) -> Verdict:
    """Check an entitlement against the issuer's public keys.

    ``keyring`` maps key id to public key, so several signing keys can be
    trusted at once during a rotation. Never raises: a malformed or hostile
    token comes back as a Verdict carrying its reason, because verification
    sits on the path every peer connection takes.
    """
    try:
        claims, signature = _parse(token)
    except ValueError as e:
        return Verdict(MALFORMED, reason=str(e))

    public_key = keyring.get(claims.key_id)
    if not public_key:
        return Verdict(
            UNTRUSTED_KEY,
            claims=claims,
            reason=f"no trusted key with id {claims.key_id}",
        )

    if not identity.verify(public_key, signing_payload(claims), signature):
        return Verdict(BAD_SIGNATURE, claims=claims, reason="signature does not verify")

    moment = now or datetime.now(timezone.utc)
    try:
        expires = _parse_stamp(claims.expires_at)
    except ValueError as e:
        return Verdict(MALFORMED, claims=claims, reason=f"unreadable expires_at: {e}")

    if moment <= expires:
        return Verdict(VALID, claims=claims)
    if moment <= expires + grace:
        overdue = moment - expires
        return Verdict(
            IN_GRACE,
            claims=claims,
            reason=f"expired {overdue.days}d ago; refresh before grace runs out",
        )
    return Verdict(EXPIRED, claims=claims, reason="expired beyond the offline grace window")


def _parse_stamp(raw: str) -> datetime:
    stamp = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


#: What a signed document other than an entitlement is for. An entitlement
#: carries no kind, and each of these lacks a field an entitlement needs,
#: so none of the three can be passed off as another.
ROSTER = "roster"
APPROVAL = "approval"


@dataclass(frozen=True)
class Member:
    """One person in a workspace roster, and the devices that sign for them."""

    user_id: str
    role: str
    devices: tuple[str, ...] = ()


@dataclass(frozen=True)
class Roster:
    """Every member of this device's workspaces, as the control plane lists them.

    An entitlement names one person's role, which is all a device needs to
    act. Judging a teammate's proposal needs theirs too, and a device used
    to have nothing to read it from, so every teammate's review was dropped.
    """

    organization_id: str
    workspaces: dict[str, tuple[Member, ...]]

    def members(self, workspace_id: str) -> tuple[Member, ...]:
        return self.workspaces.get(workspace_id, ())


def read_signed(token: str, keyring: dict[str, str], kind: str) -> dict[str, Any] | None:
    """The fields of a signed document of this kind, or None if it does not verify."""
    body, _, signature = token.partition(TOKEN_SEPARATOR)
    if not body or not signature:
        return None
    try:
        import json

        data = json.loads(_unb64url(body))
    except (binascii.Error, ValueError):
        return None
    if not isinstance(data, dict) or data.get("kind") != kind:
        return None
    public_key = keyring.get(str(data.get("key_id") or ""))
    if not public_key or not identity.verify(public_key, canonical_bytes(data), signature):
        return None
    return data


def verify_roster(
    token: str,
    keyring: dict[str, str],
    *,
    now: datetime | None = None,
    grace: timedelta = DEFAULT_GRACE,
) -> Roster | None:
    """A roster that verifies and has not lapsed past the grace window."""
    data = read_signed(token, keyring, ROSTER)
    if data is None:
        return None
    try:
        if (now or datetime.now(timezone.utc)) > _parse_stamp(str(data["expires_at"])) + grace:
            return None
        return Roster(
            organization_id=str(data["organization_id"]),
            workspaces={
                str(workspace_id): tuple(
                    Member(
                        user_id=str(m["user_id"]),
                        role=str(m["role"]),
                        devices=tuple(str(d) for d in m.get("devices") or ()),
                    )
                    for m in members
                )
                for workspace_id, members in dict(data["workspaces"]).items()
            },
        )
    except (KeyError, TypeError, ValueError):
        return None


def approval_subject(proposal_id: str, target_artifact_id: str) -> str:
    """What a confirmation is about, in a form the control plane may hold.

    The control plane keeps no artifact ids (PRD §17.2), so it is asked to
    confirm a digest of them. A device holding the proposal computes the
    same digest; the digest itself names neither id.
    """
    import hashlib

    fields = {"proposal_id": proposal_id, "target_artifact_id": target_artifact_id}
    return hashlib.sha256(canonical_bytes(fields)).hexdigest()


def approval_matches(
    token: str,
    keyring: dict[str, str],
    *,
    workspace_id: str,
    user_id: str,
    device_id: str,
    proposal_id: str,
    target_artifact_id: str,
) -> bool:
    """Whether a person confirmed exactly this approval in the console.

    Every field is bound, so a confirmation cannot be moved to another
    proposal, another version, another person or another machine. It never
    expires: it is part of the review history, and history does not lapse.
    """
    data = read_signed(token, keyring, APPROVAL)
    return data is not None and (
        data.get("workspace_id"),
        data.get("user_id"),
        data.get("device_id"),
        data.get("subject"),
    ) == (workspace_id, user_id, device_id, approval_subject(proposal_id, target_artifact_id))


def roles_from_entitlement(
    claims: Claims, workspace_id: str, actor: str | None = None
) -> dict[str, str]:
    """The role map for one workspace, in the form the workflow consumes.

    This is the bridge that turns a signed capability into an authorization
    decision: a role granted here comes from the control plane and cannot be
    self-assigned, unlike the local placeholder it replaces.
    """
    role = claims.role_in(workspace_id)
    if role is None:
        return {}
    return {actor or claims.user_id: role}


@dataclass
class EntitlementStore:
    """The entitlement this device is currently holding.

    Cached rather than fetched per operation, so a control-plane outage does
    not interrupt work already authorized (§18.3).
    """

    token: str | None = None
    keyring: dict[str, str] = field(default_factory=dict)

    def current(self, *, now: datetime | None = None) -> Verdict:
        if not self.token:
            return Verdict(MALFORMED, reason="no entitlement held on this device")
        return verify(self.token, self.keyring, now=now)
