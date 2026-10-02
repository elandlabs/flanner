"""SSH signatures in OpenSSH's SSHSIG format, the form git's SSH commit signing uses.

Ed25519 only. The format is OpenSSH's PROTOCOL.sshsig: a blob holding the
signer's public key, a namespace ("git" for commits), the hash algorithm
and the signature over

    "SSHSIG" || string(namespace) || string(reserved) || string(hash) || string(H(message))

armored between `-----BEGIN SSH SIGNATURE-----` lines. A key's fingerprint
is OpenSSH's: `SHA256:` and the unpadded base64 of the SHA-256 of its wire
form, as `ssh-keygen -l` and GitHub show it.
"""

from __future__ import annotations

import base64
import hashlib
import struct

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

MAGIC = b"SSHSIG"
KEY_TYPE = b"ssh-ed25519"
BEGIN, END = "-----BEGIN SSH SIGNATURE-----", "-----END SSH SIGNATURE-----"


def _string(data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + data


def _read(blob: bytes, at: int) -> tuple[bytes, int]:
    if at + 4 > len(blob):
        raise ValueError("truncated")
    (size,) = struct.unpack(">I", blob[at : at + 4])
    end = at + 4 + size
    if end > len(blob):
        raise ValueError("truncated")
    return blob[at + 4 : end], end


def raw_public(key: Ed25519PublicKey) -> bytes:
    return key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def wire(key: Ed25519PublicKey) -> bytes:
    """The public key as SSH puts it on the wire."""
    return _string(KEY_TYPE) + _string(raw_public(key))


def openssh_line(key: Ed25519PublicKey, comment: str = "") -> str:
    """`ssh-ed25519 AAAA... comment`, as an authorized_keys or allowed_signers line holds it."""
    line = "ssh-ed25519 " + base64.b64encode(wire(key)).decode("ascii")
    return f"{line} {comment}" if comment else line


def from_openssh_line(line: str) -> Ed25519PublicKey:
    """The key from `ssh-ed25519 AAAA...`. Raises ValueError for anything else."""
    parts = line.strip().removeprefix("key::").split()
    if len(parts) < 2 or parts[0] != "ssh-ed25519":
        raise ValueError("not an ssh-ed25519 public key")
    blob = base64.b64decode(parts[1], validate=True)
    kind, at = _read(blob, 0)
    raw, _ = _read(blob, at)
    if kind != KEY_TYPE or len(raw) != 32:
        raise ValueError("not an ssh-ed25519 public key")
    return Ed25519PublicKey.from_public_bytes(raw)


def fingerprint(key: Ed25519PublicKey) -> str:
    digest = hashlib.sha256(wire(key)).digest()
    return "SHA256:" + base64.b64encode(digest).decode("ascii").rstrip("=")


def _signed_data(message: bytes, namespace: str, hash_name: str) -> bytes:
    digest = hashlib.new(hash_name, message).digest()
    return (
        MAGIC
        + _string(namespace.encode("utf-8"))
        + _string(b"")
        + _string(hash_name.encode("ascii"))
        + _string(digest)
    )


def sign(key: Ed25519PrivateKey, message: bytes, namespace: str = "git") -> str:
    """An armored SSHSIG over the message."""
    signature = key.sign(_signed_data(message, namespace, "sha512"))
    blob = (
        MAGIC
        + struct.pack(">I", 1)
        + _string(wire(key.public_key()))
        + _string(namespace.encode("utf-8"))
        + _string(b"")
        + _string(b"sha512")
        + _string(_string(KEY_TYPE) + _string(signature))
    )
    text = base64.b64encode(blob).decode("ascii")
    lines = [text[i : i + 70] for i in range(0, len(text), 70)]
    return "\n".join([BEGIN, *lines, END]) + "\n"


def verify(armored: str, message: bytes, namespace: str = "git") -> Ed25519PublicKey | None:
    """The signer's public key when the signature is good for this message, else None."""
    try:
        body = armored.strip()
        if not (body.startswith(BEGIN) and body.endswith(END)):
            return None
        blob = base64.b64decode("".join(body[len(BEGIN) : -len(END)].split()), validate=True)
        if blob[:6] != MAGIC or struct.unpack(">I", blob[6:10])[0] != 1:
            return None
        public, at = _read(blob, 10)
        signed_namespace, at = _read(blob, at)
        _, at = _read(blob, at)  # reserved
        hash_name, at = _read(blob, at)
        signature, _ = _read(blob, at)
        kind, inner = _read(public, 0)
        raw, _ = _read(public, inner)
        sig_kind, sig_at = _read(signature, 0)
        sig, _ = _read(signature, sig_at)
        if kind != KEY_TYPE or sig_kind != KEY_TYPE or signed_namespace != namespace.encode():
            return None
        if hash_name not in (b"sha256", b"sha512"):
            return None
        key = Ed25519PublicKey.from_public_bytes(raw)
        key.verify(sig, _signed_data(message, namespace, hash_name.decode("ascii")))
    except (ValueError, InvalidSignature, struct.error, UnicodeError):
        return None
    return key
