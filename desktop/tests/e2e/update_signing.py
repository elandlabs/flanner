"""A throwaway updater key for the update tests, and a minisign signer.

Tauri's updater verifies downloads with minisign: an Ed25519 key, and a
signature over the BLAKE2b-512 hash of the file (algorithm "ED"), plus a
second signature over that one and its trusted comment. This produces the
same text `tauri signer` does, so the tests need no tauri-cli.

The key is derived from a fixed seed and published in this file on
purpose. It is only ever built into test builds of the app, never into a
release, so knowing it signs nothing anyone installs.

    python desktop/tests/e2e/update_signing.py

prints the TAURI_CONFIG a test build is compiled with: this key, and an
update address on this machine. Plain http is allowed only there.
"""

from __future__ import annotations

import base64
import hashlib
import json
import time

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

SEED = hashlib.sha256(b"flanner desktop update tests, never a release").digest()
KEY_ID = bytes.fromhex("f1a22e25d35e0001")
PORT = 38421
URL = f"http://127.0.0.1:{PORT}/latest.json"

_key = Ed25519PrivateKey.from_private_bytes(SEED)


def _public_bytes() -> bytes:
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    return _key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)


def public_key() -> str:
    """The key as Tauri's config wants it: base64 of the minisign public key file."""
    text = (
        f"untrusted comment: minisign public key {KEY_ID.hex().upper()}\n"
        + base64.b64encode(b"Ed" + KEY_ID + _public_bytes()).decode()
        + "\n"
    )
    return base64.b64encode(text.encode()).decode()


def sign(data: bytes, name: str) -> str:
    """A signature as latest.json carries it: base64 of the minisign signature file."""
    signature = _key.sign(hashlib.blake2b(data, digest_size=64).digest())
    trusted = f"timestamp:{int(time.time())}\tfile:{name}"
    global_signature = _key.sign(signature + trusted.encode())
    text = (
        "untrusted comment: signature from the flanner test key\n"
        + base64.b64encode(b"ED" + KEY_ID + signature).decode()
        + "\n"
        + f"trusted comment: {trusted}\n"
        + base64.b64encode(global_signature).decode()
        + "\n"
    )
    return base64.b64encode(text.encode()).decode()


def tauri_config() -> str:
    return json.dumps(
        {
            "plugins": {
                "updater": {
                    "pubkey": public_key(),
                    "endpoints": [URL],
                    "dangerousInsecureTransportProtocol": True,
                }
            }
        }
    )


if __name__ == "__main__":
    print(tauri_config())
