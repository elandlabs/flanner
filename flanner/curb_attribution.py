"""Agent commit attribution: per-agent keys, a signing broker, a signed registry (PRD §10.15).

A signature proves who held a key, not who wrote a commit, so this is
attribution. Each agent on each device has its own random Ed25519 key,
never derived from the device's identity, whose private half lives only
in the OS credential store: an agent can read files, so there is no file
fallback. git signs agent commits through `flanner-curb-sign`
(`curb_signer`), which signs only inside an agent session the hooks
recorded.

Each key is registered with the control plane with two signatures: the
device's, on the request, and the key's own, over the device id, the
public key, a fresh nonce and any key it replaces. The organization's
registry, signed by the issuer key, lists every key's owner and status. A
device accepts a registry only when it verifies, names this organization,
is not older than the one held, matches it exactly at the same version,
and keeps every revocation and every owner of the one before. Revocations
seen once are kept for good.

`state_of` gives a commit one of five states: attributed, attributed with
a retired key, untrusted because revoked, key status unknown (no fresh
registry, and no known revocation), or unattributed.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import subprocess
import sys
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from . import (
    agent_paths,
    curb_fix,
    curb_observe,
    curb_sshsig,
    curb_store,
    curb_tighten,
    curb_wire,
    identity,
)
from .artifacts import canonical_bytes
from .curb_context import CLAUDE, CODEX, LABELS

SERVICE = "flanner-curb-attribution"
ROTATE_DAYS = 90
PROOF = "curb_attribution_proof"
REGISTRY = curb_wire.REGISTRY
ACTIVE, RETIRED_STATUS, REVOKED_STATUS = "active", "retired", "revoked"
ATTRIBUTED = "attributed"
RETIRED = "attributed, retired key"
REVOKED = "untrusted: revoked"
UNKNOWN = "key status unknown"
UNATTRIBUTED = "unattributed"
SIGNER = "flanner-curb-sign"


class NoKeychain(RuntimeError):
    """This machine has no OS credential store, so it can hold no attribution key."""


# --- keys ---------------------------------------------------------------------------------


def _store() -> Any:
    store = identity._keychain()
    if store is None:
        raise NoKeychain(
            "attribution keys live only in the OS credential store, and this machine has none"
        )
    return store


def _account(agent: str) -> str:
    return f"{curb_store._account()}:{agent}"


def keys() -> dict[str, Any]:
    """What this device knows about its attribution keys: public halves only."""
    data = curb_store.read_state("attribution")
    return {"keys": dict(data.get("keys") or {}), "retired": list(data.get("retired") or [])}


def _save(data: Mapping[str, Any]) -> None:
    curb_store.write_state("attribution", {**curb_store.read_state("attribution"), **data})


def create(agent: str, *, now: float | None = None) -> Ed25519PublicKey:
    """A new key for an agent, its private half in the OS credential store only."""
    store = _store()
    key = Ed25519PrivateKey.generate()
    seed = key.private_bytes(
        serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption()
    ).hex()
    store.set_password(SERVICE, _account(agent), seed)
    if store.get_password(SERVICE, _account(agent)) != seed:
        raise NoKeychain("the OS credential store did not keep the key")
    public = key.public_key()
    held = keys()
    held["keys"][agent] = {
        "public": curb_sshsig.openssh_line(public),
        "fingerprint": curb_sshsig.fingerprint(public),
        "created": time.time() if now is None else now,
        "registered": False,
    }
    _save({"keys": held["keys"]})
    return public


def private_key(agent: str) -> Ed25519PrivateKey | None:
    try:
        seed = _store().get_password(SERVICE, _account(agent))
    except NoKeychain:
        return None
    if not seed:
        return None
    key = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(seed))
    current = keys()["keys"].get(agent) or {}
    # A key the metadata does not name is not this agent's current key.
    if curb_sshsig.fingerprint(key.public_key()) != current.get("fingerprint"):
        return None
    return key


def agent_for(public_line: str) -> str | None:
    """Which agent a configured public key belongs to, current or retired."""
    try:
        wanted = curb_sshsig.fingerprint(curb_sshsig.from_openssh_line(public_line))
    except ValueError:
        return None
    held = keys()
    for agent, entry in held["keys"].items():
        if entry.get("fingerprint") == wanted:
            return str(agent)
    for entry in held["retired"]:
        if entry.get("fingerprint") == wanted:
            return str(entry.get("agent"))
    return None


def rotate(agent: str, *, now: float | None = None) -> tuple[str, Ed25519PublicKey]:
    """Retire an agent's key and make its replacement. Returns the retired fingerprint.

    The retired private half is gone once the new one is stored: it can
    sign nothing new, and the commits it signed stay valid.
    """
    held = keys()
    old = held["keys"].get(agent)
    if old is None:
        raise KeyError(f"{agent} has no attribution key")
    retired = [
        *held["retired"],
        {**old, "agent": agent, "retired_at": time.time() if now is None else now},
    ]
    _save({"retired": retired})
    public = create(agent, now=now)
    held = keys()
    held["keys"][agent]["replaces"] = str(old["fingerprint"])  # named when it is registered
    _save({"keys": held["keys"]})
    return str(old["fingerprint"]), public


def due(*, now: float | None = None) -> list[str]:
    """Agents whose key is older than 90 days."""
    moment = time.time() if now is None else now
    return [
        agent
        for agent, entry in keys()["keys"].items()
        if moment - float(entry.get("created") or 0) > ROTATE_DAYS * 86400
    ]


# --- registration ------------------------------------------------------------------------


def proof_bytes(device_id: str, public_key: str, nonce: str, replaces: str) -> bytes:
    """Exactly what the key signs to show this device holds it."""
    return canonical_bytes(
        {
            "kind": PROOF,
            "device_id": device_id,
            "public_key": public_key,
            "nonce": nonce,
            "replaces": replaces,
        }
    )


def registration(agent: str, device_id: str) -> dict[str, str]:
    """The body of a registration request, with the key's proof of possession."""
    key = private_key(agent)
    if key is None:
        raise KeyError(f"{agent} has no attribution key on this device")
    replaces = str(keys()["keys"][agent].get("replaces") or "")
    public = base64.b64encode(curb_sshsig.raw_public(key.public_key())).decode("ascii")
    nonce = secrets.token_hex(16)
    proof = key.sign(proof_bytes(device_id, public, nonce, replaces))
    return {
        "agent": agent,
        "public_key": public,
        "nonce": nonce,
        "replaces": replaces,
        "proof": base64.b64encode(proof).decode("ascii"),
    }


def mark_registered(agent: str) -> None:
    held = keys()
    if agent in held["keys"]:
        held["keys"][agent]["registered"] = True
        _save({"keys": held["keys"]})


# --- the registry --------------------------------------------------------------------------


def _stamp(text: Any) -> datetime:
    moment = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def registry(issuer_keyring: dict[str, str]) -> dict[str, Any] | None:
    token = curb_store.read_state("attribution-registry").get("token")
    return curb_wire.read(str(token), issuer_keyring, REGISTRY) if token else None


def revoked() -> set[str]:
    """Every fingerprint any accepted registry has revoked. Never undone."""
    return set(curb_store.read_state("attribution-registry").get("revoked") or [])


def _entries(listing: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    return {
        str(entry["fingerprint"]): entry
        for entry in listing.get("keys") or []
        if isinstance(entry, Mapping) and entry.get("fingerprint")
    }


def accept_registry(
    token: str, issuer_keyring: dict[str, str], organization_id: str
) -> tuple[dict[str, Any] | None, str]:
    """Cache a fetched registry when it may replace the held one.

    Returns the registry in force, and why a fetched one was refused.
    """
    held = registry(issuer_keyring)
    data = curb_wire.read(token, issuer_keyring, REGISTRY)
    if data is None:
        return held, "an attribution registry that does not verify was refused"
    try:
        version = int(data["version"])
        _stamp(data["expires_at"])
        if str(data["organization_id"]) != organization_id:
            return held, "an attribution registry for another organization was refused"
    except (KeyError, TypeError, ValueError):
        return held, "an unreadable attribution registry was refused"
    new = _entries(data)
    known = revoked()
    if held is not None:
        old = _entries(held)
        if version < int(held["version"]):
            return held, "an older attribution registry was refused"
        if version == int(held["version"]) and new != old:
            return held, "an attribution registry changed without a new version and was refused"
        moved = [f for f in old if f in new and new[f].get("device_id") != old[f].get("device_id")]
        if moved:
            return held, "an attribution registry that moves a key to another device was refused"
    dropped = [f for f in known if f in new and new[f].get("status") != REVOKED_STATUS]
    dropped += [f for f in known if f not in new]
    if dropped:
        return held, "an attribution registry that drops a known revocation was refused"
    known |= {f for f, entry in new.items() if entry.get("status") == REVOKED_STATUS}
    curb_store.write_state("attribution-registry", {"token": token, "revoked": sorted(known)})
    return data, ""


def fresh(listing: Mapping[str, Any] | None, now: datetime) -> bool:
    return listing is not None and now <= _stamp(listing["expires_at"])


# --- commits ---------------------------------------------------------------------------------


def split_signature(raw: bytes) -> tuple[bytes, str | None]:
    """A commit's signed payload, and its SSH signature if it has one."""
    head, sep, message = raw.partition(b"\n\n")
    lines = head.split(b"\n")
    kept: list[bytes] = []
    signature: list[bytes] = []
    inside = False
    for line in lines:
        if line.startswith(b"gpgsig "):
            inside = True
            signature.append(line[len(b"gpgsig ") :])
        elif inside and line.startswith(b" "):
            signature.append(line[1:])
        else:
            inside = False
            kept.append(line)
    payload = b"\n".join(kept) + sep + message
    armored = b"\n".join(signature).decode("utf-8", "replace") if signature else None
    return payload, armored


def with_signature(payload: bytes, armored: str) -> bytes:
    """The commit object git writes once it has the signature (git's sign_with_header)."""
    end = payload.find(b"\n\n")
    at = len(payload) if end < 0 else end + 1
    lines = armored.encode("utf-8").splitlines(keepends=True)
    header = b"gpgsig" + b"".join(b" " + line for line in lines)
    if not header.endswith(b"\n"):
        header += b"\n"
    return payload[:at] + header + payload[at:]


def commit_id(content: bytes) -> str:
    return hashlib.sha1(b"commit %d\0" % len(content) + content, usedforsecurity=False).hexdigest()


def _git(cwd: Path, *args: str) -> bytes:
    done = subprocess.run(  # noqa: S603 - git with fixed arguments and revisions given by the person
        ["git", "-C", str(cwd), *args],  # noqa: S607 - git from PATH, as every git tool finds it
        capture_output=True,
        check=False,
        timeout=60,
    )
    if done.returncode != 0:
        raise ValueError(done.stderr.decode("utf-8", "replace").strip() or "git failed")
    return done.stdout


def commits(revision: str, cwd: Path) -> list[str]:
    """The commits a revision or a range names, newest first."""
    if ".." in revision:
        listed = _git(cwd, "rev-list", revision, "--")
    else:
        listed = _git(cwd, "rev-list", "--max-count=1", revision, "--")
    return listed.decode("ascii").split()


def recent(cwd: Path, limit: int) -> list[str]:
    """The newest commits on the branch checked out, newest first."""
    return _git(cwd, "rev-list", f"--max-count={limit}", "HEAD", "--").decode("ascii").split()


def raw_commit(sha: str, cwd: Path) -> bytes:
    return _git(cwd, "cat-file", "commit", sha)


@dataclass(frozen=True)
class Verdict:
    state: str
    fingerprint: str | None = None
    agent: str | None = None
    device_id: str | None = None


def state_of(
    raw: bytes, listing: Mapping[str, Any] | None, known_revoked: set[str], now: datetime
) -> Verdict:
    """One commit's attribution state (PRD §10.15)."""
    payload, armored = split_signature(raw)
    signer = curb_sshsig.verify(armored, payload) if armored else None
    if signer is None:
        return Verdict(UNATTRIBUTED)
    print_ = curb_sshsig.fingerprint(signer)
    if print_ in known_revoked:
        return Verdict(REVOKED, print_)
    if not fresh(listing, now):
        return Verdict(UNKNOWN, print_)
    entry = _entries(listing or {}).get(print_)
    if entry is None:
        return Verdict(UNATTRIBUTED, print_)
    status = entry.get("status")
    owner = (str(entry.get("agent") or ""), str(entry.get("device_id") or ""))
    if status == ACTIVE:
        return Verdict(ATTRIBUTED, print_, *owner)
    if status == RETIRED_STATUS:
        return Verdict(RETIRED, print_, *owner)
    return Verdict(REVOKED, print_, *owner)


# --- setting agents up -------------------------------------------------------------------------


def signer_path() -> Path | None:
    """The `flanner-curb-sign` program installed beside this Python."""
    name = SIGNER + (".exe" if sys.platform == "win32" else "")
    beside = Path(sys.executable).with_name(name)
    if beside.is_file():
        return beside
    import shutil

    found = shutil.which(SIGNER)
    return Path(found) if found else None


def git_config(agent: str, signer: Path) -> dict[str, str]:
    """Environment variables that make git sign this agent's commits through the broker."""
    entry = keys()["keys"][agent]
    settings = (
        ("gpg.format", "ssh"),
        ("gpg.ssh.program", str(signer)),
        ("user.signingkey", f"key::{entry['public']}"),
        ("commit.gpgsign", "true"),
    )
    env = {"GIT_CONFIG_COUNT": str(len(settings))}
    for number, (key, value) in enumerate(settings):
        env[f"GIT_CONFIG_KEY_{number}"] = key
        env[f"GIT_CONFIG_VALUE_{number}"] = value
    return env


def setup_plan(agents: list[str], signer: Path) -> curb_fix.Plan:
    """The settings edits that route each agent's commits through the broker.

    Hooks come too: the broker signs only inside a session they recorded.
    Each agent's settings file is edited once, with both changes.
    """
    plan = curb_observe.hook_plan(agents, enable=True)
    by_path = {edit.path: edit for edit in plan.edits}
    for agent in agents:
        env = git_config(agent, signer)
        if agent == CLAUDE:
            path = agent_paths.claude_config_dir() / "settings.json"
            held = by_path.get(path)
            before = held.before if held else curb_tighten.load(path)
            after = json.loads(json.dumps(held.after if held else before))
            current = after.get("env") if isinstance(after.get("env"), Mapping) else {}
            if current.get("GIT_CONFIG_COUNT") not in (None, env["GIT_CONFIG_COUNT"]):
                plan.guided.append(f"{LABELS[agent]} already sets GIT_CONFIG_COUNT; left alone")
                continue
            after["env"] = {**current, **env}
            actions = (*(held.actions if held else ()), "sign commits with its attribution key")
            text = json.dumps(after, indent=2) + "\n"
            by_path[path] = curb_fix.Edit(agent, path, before, after, text, actions)
        elif agent == CODEX:
            path = agent_paths.codex_home() / "config.toml"
            try:
                before = curb_tighten.load(path)
                original = path.read_text(encoding="utf-8-sig") if path.is_file() else ""
            except (OSError, ValueError):  # unreadable, or Python 3.10 with no TOML reader
                plan.guided.append(
                    f"{LABELS[agent]}'s config.toml cannot be read, so it is left alone"
                )
                continue
            after = json.loads(json.dumps(before))
            edits = []
            for key, value in env.items():
                if curb_fix._set(after, ("shell_environment_policy", "set", key), value):
                    edits.append((("shell_environment_policy", "set", key), value))
            if not edits:
                continue
            edited = curb_fix._toml_edit(original, edits)
            if edited is None or curb_fix._toml_parse(edited) != after:
                plan.guided.append(f"{LABELS[agent]}'s config.toml could not be edited safely")
                continue
            by_path[path] = curb_fix.Edit(
                agent, path, before, after, edited, ("sign commits with its attribution key",)
            )
    plan.edits = list(by_path.values())
    return plan
