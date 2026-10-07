"""Scrubbing: replace a secret's exact bytes in a file, after rotation (Curb PRD §10.3, R3).

Opt-in, one file and one approval at a time:

- each occurrence of each secret Kingfisher finds is replaced by a
  placeholder of the same length, so offsets and line lengths hold;
- every changed line must still parse the way it did (JSON lines, a whole
  JSON, TOML or YAML file, `.env` assignments), no occurrence may remain,
  even escaped, and the file must not have changed since it was read;
- the new file is swapped in with one rename.

If any check fails, the file is left exactly as it was. No backup is kept,
because a backup would be another copy of the secret: this is the one write
Curb cannot undo (rule 8). Scrubbing hides a secret here; it cannot unsend
one, so the person rotates it first.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from . import curb_approval
from .curb_approval import Broker, Grant
from .curb_kingfisher import Match

PREFIX = b"CURB-SCRUBBED"
_ENV_LINE = re.compile(rb"^\s*(#.*|(export\s+)?[A-Za-z_][A-Za-z0-9_]*\s*=.*)?\s*$")


class ScrubFailed(RuntimeError):
    """A check failed, so the file was left as it was."""


@dataclass(frozen=True)
class Scrub:
    path: Path
    original: str  # sha256 of the file as read
    new: bytes = b""
    secrets: int = 0
    lines: int = 0

    def change(self) -> dict[str, object]:
        """What a grant binds to: this file, as it was read, and how much changes."""
        return {"scrub": str(self.path), "was": self.original, "secrets": self.secrets}


def placeholder(length: int) -> bytes:
    """Same length as the secret: CURB-SCRUBBED padded with *, or just * when shorter."""
    if length < len(PREFIX):
        return b"*" * length
    return PREFIX + b"*" * (length - len(PREFIX))


def plan(path: Path, detector: Callable[[Path], list[Match]]) -> Scrub:
    """Work out the scrubbed file in memory. Raises ScrubFailed where it cannot be done safely."""
    data = path.read_bytes()
    found = sorted({m.secret for m in detector(path)}, key=len, reverse=True)
    if not found:
        return Scrub(path, _sha(data))
    new = data
    for secret in found:
        raw = secret.encode("utf-8")
        new = new.replace(raw, placeholder(len(raw)))
    remaining = [s for s in found if _occurs(new, s)]
    if remaining:
        raise ScrubFailed(f"{len(remaining)} secret(s) also appear in an escaped form")
    changed = [
        n
        for n, (a, b) in enumerate(zip(data.split(b"\n"), new.split(b"\n"), strict=True))
        if a != b
    ]
    _check(path, data, new, changed)
    return Scrub(path, _sha(data), new, len(found), len(changed))


def _occurs(data: bytes, secret: str) -> bool:
    forms = {secret, json.dumps(secret)[1:-1], secret.replace("/", "\\/")}
    return any(form.encode("utf-8") in data for form in forms)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _check(path: Path, before: bytes, after: bytes, changed: list[int]) -> None:
    """Every changed line still parses as it did, or ScrubFailed."""
    suffix = path.suffix.lower()
    name = path.name.lower()
    try:
        after.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ScrubFailed("the file would no longer be UTF-8") from error
    if suffix == ".jsonl":
        old_lines, new_lines = before.split(b"\n"), after.split(b"\n")
        for number in changed:
            if _parses(json.loads, old_lines[number]) and not _parses(
                json.loads, new_lines[number]
            ):
                raise ScrubFailed(f"line {number + 1} would no longer be JSON")
    elif suffix == ".json":
        _whole(json.loads, before, after, "JSON")
    elif suffix == ".toml":
        from .curb_settings import _toml_loads

        loads = _toml_loads()
        if loads is not None:
            _whole(loads, before, after, "TOML")
    elif suffix in (".yaml", ".yml"):
        import yaml

        _whole(yaml.safe_load, before, after, "YAML")
    elif name == ".env" or name.startswith(".env."):
        new_lines = after.split(b"\n")
        for number in changed:
            if not _ENV_LINE.match(new_lines[number]):
                raise ScrubFailed(f"line {number + 1} would no longer be an assignment")


def _parses(loader: Callable[[str], object], data: bytes) -> bool:
    try:
        loader(data.decode("utf-8"))
    except Exception:  # noqa: BLE001 - JSON, TOML and YAML each raise their own error
        return False
    return True


def _whole(loader: Callable[[str], object], before: bytes, after: bytes, kind: str) -> None:
    if _parses(loader, before) and not _parses(loader, after):
        raise ScrubFailed(f"the file would no longer be {kind}")


def apply(scrub: Scrub, broker: Broker, grant: Grant | None) -> None:
    """Swap the scrubbed file in, with a grant for exactly this file, or leave it as it was."""
    broker.redeem(grant, curb_approval.change_hash(scrub.change()))
    if not scrub.secrets:
        return
    if _sha(scrub.path.read_bytes()) != scrub.original:
        raise ScrubFailed("the file changed since it was read")
    temporary = scrub.path.with_name(f".{scrub.path.name}.curb-scrub")
    try:
        fd = os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0), 0o600
        )
        with os.fdopen(fd, "wb") as handle:
            handle.write(scrub.new)
            handle.flush()
            os.fsync(handle.fileno())
        with contextlib.suppress(OSError):
            shutil.copymode(scrub.path, temporary)
        os.replace(temporary, scrub.path)
    finally:
        temporary.unlink(missing_ok=True)
    if scrub.path.read_bytes() != scrub.new:  # pragma: no cover - a rename does not half-happen
        raise ScrubFailed("the file did not read back as written")
