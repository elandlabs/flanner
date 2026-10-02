"""What Curb keeps on this device: a digest key and redacted reports (Curb PRD §7).

A fingerprint is HMAC-SHA256 under a per-device key, truncated to 16 bytes
(§7.2), so a found secret can be matched later without being kept. The key
is made on first use, kept in the OS keychain where there is one and in a
user-only file where there is not, and never leaves the device. Losing it
costs only matching against older reports, so unlike the device's signing
key it is simply made again.

Reports keep categories, classes, rule ids and keyed digests, never a value
or a location, and are deleted after 30 days. `forget` removes all of it.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import os
import secrets
import shutil
import time
from pathlib import Path
from typing import Any

from . import identity

KEYCHAIN_SERVICE = "flanner-curb-digest"
REPORT_DAYS = 30


def curb_dir() -> Path:
    return identity.flanner_home() / "curb"


def _key_file() -> Path:
    return curb_dir() / "digest.key"


def _account() -> str:
    # One key per flanner home, as the device key does it.
    home = os.path.normcase(os.path.abspath(str(identity.flanner_home())))
    return hashlib.sha256(home.encode("utf-8")).hexdigest()[:16]


def _write_user_only(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)


def digest_key() -> bytes:
    """This device's digest key, made on first use."""
    # The keychain the device key uses, found the same way.
    store = identity._keychain()
    held = None
    if store is not None:
        # A locked or broken keychain reads as absent.
        with contextlib.suppress(Exception):
            held = store.get_password(KEYCHAIN_SERVICE, _account())
    if held:
        return bytes.fromhex(held)
    path = _key_file()
    if path.is_file():
        return bytes.fromhex(path.read_text(encoding="utf-8").strip())
    key = secrets.token_bytes(32)
    if store is not None:
        # A refused write, or one that does not read back, means the file.
        with contextlib.suppress(Exception):
            store.set_password(KEYCHAIN_SERVICE, _account(), key.hex())
            if store.get_password(KEYCHAIN_SERVICE, _account()) == key.hex():
                return key
    _write_user_only(path, key.hex())
    return key


def read_state(name: str) -> dict[str, Any]:
    """One of Curb's JSON state files, {} when absent or unreadable."""
    try:
        data = json.loads((curb_dir() / f"{name}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def write_state(name: str, data: dict[str, Any]) -> None:
    """Replace a state file in one step, user-only."""
    path = curb_dir() / f"{name}.json"
    temporary = path.with_name(f".{path.name}.tmp")
    _write_user_only(temporary, json.dumps(data, indent=1, sort_keys=True))
    os.replace(temporary, path)


def keep_secret(name: str, value: str) -> None:
    """Hold a secret in the OS keychain, or a user-only file where there is none."""
    store = identity._keychain()
    if store is not None:
        with contextlib.suppress(Exception):
            store.set_password(f"flanner-curb-{name}", _account(), value)
            if store.get_password(f"flanner-curb-{name}", _account()) == value:
                (curb_dir() / f"{name}.secret").unlink(missing_ok=True)
                return
    _write_user_only(curb_dir() / f"{name}.secret", value)


def read_secret(name: str) -> str | None:
    store = identity._keychain()
    if store is not None:
        with contextlib.suppress(Exception):
            held = store.get_password(f"flanner-curb-{name}", _account())
            if held:
                return str(held)
    path = curb_dir() / f"{name}.secret"
    return path.read_text(encoding="utf-8").strip() or None if path.is_file() else None


def digest(value: str | bytes, key: bytes) -> str:
    """The keyed fingerprint of a secret or a path: matchable here, not reversible."""
    data = value.encode("utf-8") if isinstance(value, str) else value
    return hmac.new(key, data, hashlib.sha256).digest()[:16].hex()


def save_report(kind: str, report: dict[str, Any], *, now: float | None = None) -> Path:
    """Keep a redacted report, and drop any older than 30 days."""
    folder = curb_dir() / "reports"
    stamp = now if now is not None else time.time()
    prune(folder, now=stamp)
    path = folder / f"{kind}-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime(stamp))}.json"
    _write_user_only(path, json.dumps({"kind": kind, "created": stamp, **report}, indent=1))
    return path


def prune(folder: Path, *, now: float | None = None) -> None:
    limit = (now if now is not None else time.time()) - REPORT_DAYS * 86400
    for path in folder.glob("*.json") if folder.is_dir() else []:
        with contextlib.suppress(OSError):
            if path.stat().st_mtime < limit:
                path.unlink()


def forget() -> list[str]:
    """Delete everything Curb keeps here. Returns what was removed, in words."""
    removed = []
    reports = curb_dir() / "reports"
    if reports.is_dir():
        count = sum(1 for _ in reports.glob("*.json"))
        shutil.rmtree(reports)
        removed.append(f"{count} stored report(s)")
    proofs = curb_dir() / "proofs.json"
    if proofs.is_file():
        proofs.unlink()
        removed.append("the tester's proofs")
    actions = curb_dir() / "actions.jsonl"
    if actions.is_file():
        actions.unlink()
        removed.append("the action log")
    store = identity._keychain()
    gone = False
    if store is not None:
        with contextlib.suppress(Exception):
            if store.get_password(KEYCHAIN_SERVICE, _account()):
                store.delete_password(KEYCHAIN_SERVICE, _account())
                gone = True
    if _key_file().is_file():
        _key_file().unlink()
        gone = True
    if gone:
        removed.append("the digest key, so older fingerprints can no longer be matched")
    return removed
