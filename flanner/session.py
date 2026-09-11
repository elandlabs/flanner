"""The entitlement this device is holding, as cached on disk.

Deliberately free of any network code. Read commands - review status, an
assurance verdict - resolve authorization through here, and an import
boundary is a better guarantee than a promise that nobody will call out.
Fetching and renewing live in ``account``, which imports this module rather
than the other way round.

Nothing cached here is a secret worth stealing. An entitlement is a signed
statement about one device, bound to a device id no other machine can sign
for, so copying it to another laptop gains nothing: that laptop cannot
produce the signature the next renewal needs.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from . import identity
from .entitlements import EntitlementStore, Verdict

SESSION_FILENAME = "session.json"
DEFAULT_ENDPOINT = "https://api.flanner.io"


@dataclass
class Session:
    """What this device knows about its own account."""

    endpoint: str
    device_id: str
    organization_id: str
    user_id: str
    entitlement: str
    # Signing keys of the control plane, for checking the entitlement.
    keyring: dict[str, str] = field(default_factory=dict)
    # Public keys of this organization's devices, for checking artifacts a
    # peer relays. Cached because verification must work offline, and
    # because the author of an artifact may be a machine this device has
    # never connected to (PRD §14.4).
    device_keys: dict[str, str] = field(default_factory=dict)
    # Relay this organization prefers when two devices cannot reach each
    # other directly. Empty means the transport's own defaults, which is
    # also what an older control plane that never sends one produces.
    relay_url: str = ""
    # Every member of this device's workspaces, signed by the control plane.
    # Empty from a control plane that does not send one.
    roster: str = ""

    def store(self) -> EntitlementStore:
        return EntitlementStore(token=self.entitlement, keyring=self.keyring)

    def status(self, *, now: datetime | None = None) -> Verdict:
        return self.store().current(now=now)

    def resolve_device_key(self, device_id: str) -> str | None:
        """The public key of a device, for verifying what it signed.

        The shape ``sync.ingest_artifact`` expects. Returning None for an
        unknown device is what makes an artifact from a revoked or
        unrecognised machine fail verification rather than be trusted.
        """
        return self.device_keys.get(device_id)

    def to_dict(self) -> dict[str, Any]:
        return {
            "endpoint": self.endpoint,
            "device_id": self.device_id,
            "organization_id": self.organization_id,
            "user_id": self.user_id,
            "entitlement": self.entitlement,
            "keyring": self.keyring,
            "device_keys": self.device_keys,
            "relay_url": self.relay_url,
            "roster": self.roster,
        }


def session_path() -> Path:
    return identity.flanner_home() / SESSION_FILENAME


def load() -> Session | None:
    """The cached session, or None if this device has never logged in."""
    path = session_path()
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return Session(
            endpoint=str(data.get("endpoint") or DEFAULT_ENDPOINT),
            device_id=str(data["device_id"]),
            organization_id=str(data["organization_id"]),
            user_id=str(data["user_id"]),
            entitlement=str(data["entitlement"]),
            keyring=dict(data.get("keyring") or {}),
            device_keys=dict(data.get("device_keys") or {}),
            relay_url=str(data.get("relay_url") or ""),
            roster=str(data.get("roster") or ""),
        )
    except (OSError, ValueError, KeyError, TypeError):
        # A corrupt cache is indistinguishable from never having logged in,
        # and both are fixed the same way. Refusing to start would be worse.
        return None


def save(session: Session) -> Path:
    """Cache a session owner-readable only, atomically."""
    path = session_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(session.to_dict(), f, indent=2, sort_keys=True)
    os.replace(temp, path)
    return path


def clear() -> bool:
    """Forget this device's session. The device key is left alone.

    Logging out is not un-enrolling: the key is this device's identity, and
    discarding it would make the same machine look like a new one. Removing
    the device's access is the control plane's job, not the client's.
    """
    path = session_path()
    if not path.exists():
        return False
    path.unlink()
    return True
