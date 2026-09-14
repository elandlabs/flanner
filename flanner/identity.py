"""Device identity.

Every installation owns an Ed25519 key pair (PRD §11.2). It signs the
artifacts this device produces, so a peer can verify who wrote a plan
version without trusting the transport that carried it, and without asking
Flanner Mesh. The private key never leaves the machine.

The device id is derived from the public key rather than assigned, so an
offline installation has a stable identity from first use and the control
plane can later map it to an account without ever minting it.

Nothing here talks to the network or the database: this module is the
foundation the artifact layer signs with.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import os
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from .exceptions import IdentityUnavailableError

KEY_FILENAME = "device_key"

#: Written once the key is proven readable from the keychain, and only then.
#: Its presence is the record that this home *has* an identity even though no
#: key file remains, which is what lets a keychain that cannot be read be
#: told apart from a machine that has never had a key.
KEYCHAIN_MARKER = "device_key.in-keychain"
KEYCHAIN_SERVICE = "flanner"
DEVICE_ID_PREFIX = "dev_"
_DEVICE_ID_HEX_CHARS = 16


def flanner_home() -> Path:
    """Flanner data directory (override with FLANNER_HOME)."""
    return Path(os.environ.get("FLANNER_HOME", Path.home() / ".flanner"))


#: Set by an operator to decide push acceptance for this machine outright.
ACCEPT_PUSHES_ENV = "FLANNER_ACCEPT_PUSHES"
#: Present when somebody switched pushes off from the CLI or the web UI.
REFUSE_PUSHES_MARKER = "refuse-pushes"
_NO = {"0", "no", "false", "off"}


def pushes_preference() -> tuple[bool, str]:
    """Whether this device takes work pushed to it, and what decided that.

    The environment variable wins, so a machine its operator configured
    stays configured whatever a page says. Without it, a marker file that
    `flanner peer pushes` and the web UI both write. On by default: every
    peer is an authorised teammate, and accepting overwrites nothing.
    """
    raw = os.environ.get(ACCEPT_PUSHES_ENV, "").strip().lower()
    if raw:
        return raw not in _NO, ACCEPT_PUSHES_ENV
    if (flanner_home() / REFUSE_PUSHES_MARKER).exists():
        return False, "setting"
    return True, "default"


def set_accepting_pushes(accept: bool) -> None:
    """Record the choice. `pushes_preference` says whether it is in force."""
    marker = flanner_home() / REFUSE_PUSHES_MARKER
    if accept:
        marker.unlink(missing_ok=True)
        return
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.touch()


def device_key_path() -> Path:
    return flanner_home() / KEY_FILENAME


def keychain_marker_path() -> Path:
    return flanner_home() / KEYCHAIN_MARKER


def _pem(key: Ed25519PrivateKey) -> bytes:
    """The one serialisation, so the keychain and the file agree byte for byte."""
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


def _write_private_key(path: Path, key: Ed25519PrivateKey) -> None:
    """Write the key owner-readable only, without a world-readable window."""
    path.parent.mkdir(parents=True, exist_ok=True)
    # Create with 0600 from the outset; opening then chmod-ing would leave the
    # key briefly readable by others.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(_pem(key))


def _keychain() -> Any | None:
    """The OS keychain, or None when this machine has no usable one.

    Headless Linux with no Secret Service, containers, and CI all land here,
    as does anyone who sets ``FLANNER_NO_KEYCHAIN``. None is not a degraded
    mode: it selects the 0600 file this module has always used, which stays
    a supported configuration rather than becoming a consolation prize.

    Every failure is swallowed on purpose. A keychain that cannot be reached
    must cost a fallback, never the ability to sign, because signing is how
    this device exists at all.
    """
    if os.environ.get("FLANNER_NO_KEYCHAIN"):
        return None
    try:
        import keyring
        from keyring.backends.fail import Keyring as Unusable

        backend = keyring.get_keyring()
    except Exception:  # noqa: BLE001 - any import or platform failure means "no keychain"
        return None
    return None if isinstance(backend, Unusable) else keyring


def _keychain_account() -> str:
    """One keychain entry per flanner home, so two homes stay two devices.

    A single shared entry would make every FLANNER_HOME on a machine the
    same device. Tests rely on that isolation, and so does anyone running a
    second account beside their own.
    """
    home = os.path.normcase(os.path.abspath(str(flanner_home())))
    return hashlib.sha256(home.encode("utf-8")).hexdigest()[:16]


def _read_keychain(store: Any) -> Ed25519PrivateKey | None:
    """The stored key, or None for absent, unreadable, or the wrong type."""
    try:
        pem = store.get_password(KEYCHAIN_SERVICE, _keychain_account())
    except Exception:  # noqa: BLE001 - a locked or broken keychain reads as absent
        return None
    if not pem:
        return None
    try:
        loaded = serialization.load_pem_private_key(pem.encode("utf-8"), password=None)
    except Exception:  # noqa: BLE001 - a corrupt entry must not be fatal
        return None
    return loaded if isinstance(loaded, Ed25519PrivateKey) else None


def _store_keychain(store: Any, key: Ed25519PrivateKey) -> bool:
    """Write the key to the keychain and prove it reads back identical.

    The read-back is the point. A backend that accepts a write and returns
    nothing afterwards would cost this device its identity, and the caller
    only deletes the file once this has returned True.
    """
    pem = _pem(key)
    try:
        store.set_password(KEYCHAIN_SERVICE, _keychain_account(), pem.decode("ascii"))
    except Exception:  # noqa: BLE001 - a refused write means keep the file
        return False
    written = _read_keychain(store)
    return written is not None and _pem(written) == pem


def load_or_create_device_key() -> Ed25519PrivateKey:
    """This device's signing key, generating it on first use.

    Keychain, then file, then generate. The file is still read when it is
    there, because an install predating the keychain must not wake up as a
    different device: the id is derived from the key, so a new key is a new
    machine as far as every peer and the control plane are concerned.

    Generating is the step that has to be earned. Once the key has moved to
    the keychain the file is deleted, so a keychain that is locked, that has
    had its entry removed, or that resolves to a different backend than the
    one written to, looks exactly like a machine that has never run flanner.
    Falling through to `generate` there would silently make this a *new*
    device: peers reject its signatures, its entitlement no longer matches,
    and the plans it authored are stranded under an id nothing can produce
    again. Refusing and saying so is the only safe answer.
    """
    store = _keychain()
    if store is not None:
        held = _read_keychain(store)
        if held is not None:
            # Written on every successful read, not only on the migration
            # that first stored it. An install that moved its key before
            # this existed has no marker, and would still be one locked
            # keychain away from becoming a different device; the first
            # reading it does get right is the one that fixes that.
            if not keychain_marker_path().exists():
                _mark_in_keychain(held)
            return held

    path = device_key_path()
    if path.exists():
        loaded = serialization.load_pem_private_key(path.read_bytes(), password=None)
        if not isinstance(loaded, Ed25519PrivateKey):
            raise TypeError(f"{path} does not hold an Ed25519 private key")
        if store is not None and _store_keychain(store, loaded):
            # Only now, with the key proven readable from the keychain, is
            # removing the file safe. Leaving it would mean the move bought
            # nothing, so this is the step the whole change exists for.
            path.unlink(missing_ok=True)
            _mark_in_keychain(loaded)
        return loaded

    marker = keychain_marker_path()
    if marker.exists():
        raise IdentityUnavailableError(_locked_out(marker))

    key = Ed25519PrivateKey.generate()
    if store is None or not _store_keychain(store, key):
        _write_private_key(path, key)
    else:
        _mark_in_keychain(key)
    return key


def _mark_in_keychain(key: Ed25519PrivateKey) -> None:
    """Record that this home's identity lives in the keychain.

    The device id goes in the file so the refusal can name the device the
    machine is meant to be, which is the difference between a message
    somebody can act on and one that only says something is wrong.
    """
    with contextlib.suppress(OSError):
        marker = keychain_marker_path()
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(device_id_for(key.public_key()), encoding="utf-8")


def _locked_out(marker: Path) -> str:
    known = ""
    with contextlib.suppress(OSError):
        known = marker.read_text(encoding="utf-8").strip()
    which = f" This device is {known}." if known else ""
    return (
        "This machine's signing key is in the system keychain and could not be "
        f"read.{which} Unlock the keychain and try again. Generating a new key "
        "would make this a different device: peers would reject its signatures "
        "and any plans it has authored would be stranded. "
        "If the key is genuinely gone, 'flanner login' again to enrol as a new "
        f"device, after deleting {marker}."
    )


def public_key_b64(key: Ed25519PublicKey) -> str:
    raw = key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return base64.b64encode(raw).decode("ascii")


def load_public_key(encoded: str) -> Ed25519PublicKey:
    """Rebuild a public key from its base64 form; raises ValueError if malformed."""
    try:
        raw = base64.b64decode(encoded, validate=True)
    except Exception as e:
        raise ValueError(f"Malformed public key: {e}") from None
    if len(raw) != 32:
        raise ValueError(f"Ed25519 public keys are 32 bytes, got {len(raw)}")
    return Ed25519PublicKey.from_public_bytes(raw)


def device_id_for(public_key: Ed25519PublicKey) -> str:
    """Stable id derived from a public key, so identity needs no server."""
    digest = hashes.Hash(hashes.SHA256())
    digest.update(
        public_key.public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
    )
    return DEVICE_ID_PREFIX + digest.finalize().hex()[:_DEVICE_ID_HEX_CHARS]


def device_id() -> str:
    """This device's id."""
    return device_id_for(load_or_create_device_key().public_key())


def device_public_key_b64() -> str:
    """This device's public key, in the form the control plane records."""
    return public_key_b64(load_or_create_device_key().public_key())


def sign(payload: bytes, key: Ed25519PrivateKey | None = None) -> str:
    """Sign bytes with this device's key; returns a base64 signature."""
    signer = key if key is not None else load_or_create_device_key()
    return base64.b64encode(signer.sign(payload)).decode("ascii")


def verify(public_key_encoded: str, payload: bytes, signature_b64: str) -> bool:
    """True when the signature is this key's over exactly these bytes.

    Returns False for every failure, including malformed input, so callers
    can treat verification as a single predicate and never have to guess
    which exception a hostile peer might provoke.
    """
    try:
        key = load_public_key(public_key_encoded)
        key.verify(base64.b64decode(signature_b64, validate=True), payload)
    except (InvalidSignature, ValueError, TypeError):
        return False
    return True
