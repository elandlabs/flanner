"""The Curb team wire format: what devices and the control plane exchange (Curb PRD §12.2, R5).

`docs/curb-wire-contract.md` is the contract, and this module is the
client's half of it. The control plane imports none of this: it holds the
same names as string literals and checks the contract's test vectors, which
`tests/test_curb_wire.py` recomputes from here so the two cannot drift.

Signed documents use the form rosters already use: the document's canonical
bytes, base64url without padding, a dot, then the signer's Ed25519
signature over those bytes, in standard base64. The document names the key
that signed it in `key_id`. A document's hash is the SHA-256 of its
canonical bytes, written `sha256:<hex>`.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from collections.abc import Callable, Sequence
from typing import Any

from .artifacts import canonical_bytes
from .entitlements import (
    CURB_ALERTS,
    CURB_FLEET,
    CURB_POLICY,
    Claims,
    read_signed,
)

#: What this client speaks. A server lists what it offers in every
#: entitlement response, as `curb_capabilities`.
POLICY_V1 = "curb-policy/1"
FLEET_V1 = "curb-fleet/1"
ALERTS_V1 = "curb-alerts/1"
CAPABILITIES = (POLICY_V1, FLEET_V1, ALERTS_V1)
FOR_FEATURE = {CURB_POLICY: POLICY_V1, CURB_FLEET: FLEET_V1, CURB_ALERTS: ALERTS_V1}

VERSION_HEADER = "Flanner-Client-Version"
CAPABILITIES_HEADER = "Flanner-Curb-Capabilities"
#: Sent by the relay with each webhook delivery, so a receiver can drop repeats.
EVENT_HEADER = "Flanner-Event-Id"

#: The `kind` of each signed document.
AUTHORITY = "curb_authority"
POLICY = "curb_policy"
REPORT = "curb_report"

PATHS = {
    "authority": "/v1/curb/authority",
    "policy": "/v1/curb/policy",
    "state": "/v1/curb/policy/state",
    "reports": "/v1/curb/reports",
    "alerts": "/v1/curb/alerts",
    "fleet": "/v1/curb/fleet",
}


def headers(version: str, capabilities: Sequence[str] = CAPABILITIES) -> dict[str, str]:
    """The headers every Curb request carries."""
    return {VERSION_HEADER: version, CAPABILITIES_HEADER: ", ".join(capabilities)}


def usable(feature: str, claims: Claims | None, offered: Sequence[str]) -> bool:
    """Whether both the entitlement and the server offer a Curb team feature."""
    return claims is not None and claims.has_feature(feature) and FOR_FEATURE[feature] in offered


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def encode(fields: dict[str, Any], sign: Callable[[bytes], str]) -> str:
    """A signed document: its canonical bytes and the signature over them."""
    body = canonical_bytes(fields)
    return _b64url(body) + "." + sign(body)


def read(token: str, keyring: dict[str, str], kind: str) -> dict[str, Any] | None:
    """A document's fields when its signature verifies with a key in `keyring`."""
    return read_signed(token, keyring, kind)


def fields_of(token: str) -> dict[str, Any] | None:
    """A document's fields WITHOUT checking the signature: for hashing and explaining."""
    body = token.partition(".")[0]
    try:
        data = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    except (binascii.Error, ValueError):
        return None
    return data if isinstance(data, dict) else None


def doc_hash(fields: dict[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(canonical_bytes(fields)).hexdigest()


def event_id(device_id: str, finding: str, sequence: int) -> str:
    """An alert's stable id: the same device, finding and change always give the same one."""
    data = {"device_id": device_id, "finding": finding, "sequence": sequence}
    return "evt_" + hashlib.sha256(canonical_bytes(data)).hexdigest()[:32]
