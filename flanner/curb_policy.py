"""Org policy on a device: verify it, apply what only tightens, hold the rest (Curb PRD §10.8).

The control plane sends the newest signed policy at each check-in. It is
trusted only when it verifies against a current key in the policy
authority list, which is itself signed by the entitlement issuer key this
device already trusts (`docs/curb-wire-contract.md`). Then:

    higher version, valid             the apply step
    same version, same hash           nothing: a normal check-in
    same version, different hash      integrity error: keep the current one, alert
    lower version                     rollback: refuse it, keep the current one, alert
    bad signature, another org, an    refuse it, keep the current one, alert
    unknown or revoked key, expired

The apply step compiles the policy into each agent's user settings
(`curb_compile`) and runs the tighten-only test on each file. Under the
delegation, the one standing approval, a change that only tightens is
written at once; anything else waits for the person to approve that exact
change. An expired policy stays in force, flagged, and so does a policy
whose signing key was revoked later: a device never weakens on its own.

Nothing here reaches the network. `curb_team` passes in what the control
plane said.
"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import (
    agent_paths,
    curb_compile,
    curb_fix,
    curb_reach,
    curb_store,
    curb_tighten,
    curb_wire,
)
from .curb_context import CLAUDE, CODEX, LABELS
from .curb_credentials import Credential
from .curb_fix import Edit
from .curb_reach import AgentReport
from .curb_settings import ClaudeSettings, CodexSettings

ACTIVE, RETIRING, REVOKED = "active", "retiring", "revoked"
APPLY, UNCHANGED, NONE = "apply", "unchanged", "none"
INTEGRITY, ROLLBACK, REJECTED = "integrity", "rollback", "rejected"


def _stamp(text: Any) -> datetime:
    moment = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class Policy:
    token: str
    fields: Mapping[str, Any]

    @property
    def version(self) -> int:
        return int(self.fields["version"])

    @property
    def hash(self) -> str:
        return curb_wire.doc_hash(dict(self.fields))

    @property
    def key_id(self) -> str:
        return str(self.fields.get("key_id") or "")

    def expired(self, now: datetime) -> bool:
        return now > _stamp(self.fields["expires_at"])

    def rules(self) -> curb_compile.Rules:
        """Raises ValueError for rules this flanner cannot read."""
        raw = self.fields.get("rules")
        return curb_compile.parse(raw if isinstance(raw, Mapping) else {})

    @property
    def export(self) -> Mapping[str, Any] | None:
        value = self.fields.get("audit_export")
        return value if isinstance(value, Mapping) and value.get("url") else None


# --- the policy authority list ---------------------------------------------------------


def authority(issuer_keyring: dict[str, str]) -> dict[str, Any] | None:
    """The cached authority list, if it still verifies against the issuer keyring."""
    token = curb_store.read_state("authority").get("token")
    return curb_wire.read(str(token), issuer_keyring, curb_wire.AUTHORITY) if token else None


def accept_authority(
    token: str, issuer_keyring: dict[str, str]
) -> tuple[dict[str, Any] | None, str]:
    """Cache a fetched list when it verifies and is the newest.

    Returns the list in force, and why a fetched one was refused.
    """
    held = authority(issuer_keyring)
    data = curb_wire.read(token, issuer_keyring, curb_wire.AUTHORITY)
    if data is None:
        return held, "a policy authority list that does not verify was refused"
    try:
        version = int(data["version"])
        _stamp(data["expires_at"])
    except (KeyError, TypeError, ValueError):
        return held, "an unreadable policy authority list was refused"
    if held is not None:
        if version < int(held["version"]):
            return held, "an older policy authority list was refused"
        if version == int(held["version"]) and curb_wire.doc_hash(data) != curb_wire.doc_hash(
            held
        ):
            return held, "a policy authority list changed without a new version and was refused"
    curb_store.write_state("authority", {"token": token})
    return data, ""


def authority_keys(listing: Mapping[str, Any], now: datetime) -> tuple[dict[str, str], set[str]]:
    """The keys that may sign a policy now, and the ids of revoked ones."""
    accepted: dict[str, str] = {}
    revoked: set[str] = set()
    for key in listing.get("keys") or []:
        try:
            key_id, status = str(key["key_id"]), str(key["status"])
            if status == REVOKED:
                revoked.add(key_id)
            elif status in (ACTIVE, RETIRING) and (
                _stamp(key["not_before"]) <= now <= _stamp(key["not_after"])
            ):
                accepted[key_id] = str(key["public_key"])
        except (KeyError, TypeError, ValueError):
            continue
    return accepted, revoked


# --- check-in ---------------------------------------------------------------------------------


def judge(
    token: str,
    *,
    listing: Mapping[str, Any] | None,
    organization_id: str,
    received: Policy | None,
    now: datetime,
) -> tuple[str, Policy | None, str]:
    """What to do with a policy the control plane sent: the outcome, the policy, and why."""
    if listing is None:
        return REJECTED, None, "no policy authority list verifies, so no policy can be trusted"
    if now > _stamp(listing["expires_at"]):
        return (
            REJECTED,
            None,
            "the policy authority list has expired, so no new policy is accepted",
        )
    accepted, revoked = authority_keys(listing, now)
    unverified = curb_wire.fields_of(token) or {}
    if str(unverified.get("key_id") or "") in revoked:
        return REJECTED, None, "the policy was signed by a revoked key"
    fields = curb_wire.read(token, accepted, curb_wire.POLICY)
    if fields is None:
        return REJECTED, None, "the policy does not verify against a current authority key"
    policy = Policy(token, fields)
    try:
        if str(fields["organization_id"]) != organization_id:
            return REJECTED, None, "the policy is for another organization"
        if policy.expired(now):
            return REJECTED, None, "the policy has expired"
        version = policy.version
    except (KeyError, TypeError, ValueError):
        return REJECTED, None, "the policy is missing a required field"
    if received is not None:
        if version < received.version:
            return ROLLBACK, None, f"policy version {version} is older than {received.version}"
        if version == received.version:
            if policy.hash == received.hash:
                return UNCHANGED, received, ""
            return INTEGRITY, None, f"policy version {version} arrived with different contents"
        if version == received.version + 1 and fields.get("previous_hash") != received.hash:
            return INTEGRITY, None, f"policy version {version} does not follow the one it replaces"
    return APPLY, policy, ""


@dataclass
class State:
    received: Policy | None = None
    applied: dict[str, Any] | None = None
    pending: dict[str, Any] | None = None
    rejected: dict[str, Any] | None = None
    delegated_at: float | None = None
    unmet: dict[str, list[str]] = field(default_factory=dict)
    checked_at: float | None = None


def load() -> State:
    data = curb_store.read_state("policy")
    held = data.get("received")
    received = None
    if isinstance(held, Mapping) and held.get("token"):
        fields = curb_wire.fields_of(str(held["token"]))
        received = Policy(str(held["token"]), fields) if fields else None
    return State(
        received=received,
        applied=data.get("applied"),
        pending=data.get("pending"),
        rejected=data.get("rejected"),
        delegated_at=data.get("delegated_at"),
        unmet=dict(data.get("unmet") or {}),
        checked_at=data.get("checked_at"),
    )


def save(state: State) -> None:
    curb_store.write_state(
        "policy",
        {
            "received": {"token": state.received.token} if state.received else None,
            "applied": state.applied,
            "pending": state.pending,
            "rejected": state.rejected,
            "delegated_at": state.delegated_at,
            "unmet": state.unmet,
            "checked_at": state.checked_at,
        },
    )


def receive(
    response: Mapping[str, Any],
    *,
    issuer_keyring: dict[str, str],
    organization_id: str,
    now: datetime,
) -> tuple[str, str]:
    """Take a check-in answer. Returns the outcome and, for a refusal, why.

    A refused policy changes nothing but the record of the refusal; the
    policy in force stays in force.
    """
    status = str(response.get("status") or "")
    if status in (UNCHANGED, NONE):
        return status, ""
    state = load()
    outcome, policy, reason = judge(
        str(response.get("policy") or ""),
        listing=authority(issuer_keyring),
        organization_id=organization_id,
        received=state.received,
        now=now,
    )
    if outcome == APPLY and policy is not None:
        state.received = policy
        state.rejected = None
        token = response.get("audit_export_token")
        if isinstance(token, str) and token:
            curb_store.keep_secret("export", token)
    elif outcome in (INTEGRITY, ROLLBACK, REJECTED):
        unverified = curb_wire.fields_of(str(response.get("policy") or "")) or {}
        state.rejected = {
            "version": unverified.get("version"),
            "outcome": outcome,
            "reason": reason,
            "at": now.timestamp(),
        }
    save(state)
    return outcome, reason


#: What each flag means, for a person. The codes go to the control plane.
FLAGS = {
    "unverified": "the stored policy does not verify: check in again",
    "expired": "expired: still in force until a newer policy arrives",
    "revoked_key": "signed by a revoked key: still in force until a re-signed version arrives",
    "authority_expired": "the policy authority list has expired: no new policy can be accepted",
}


def flags(state: State, listing: Mapping[str, Any] | None, now: datetime) -> list[str]:
    """What a person and the fleet view should know about the policy in force, as codes."""
    out = []
    policy = state.received
    if policy is not None and listing is not None:
        keys = {str(k.get("key_id")): str(k.get("public_key")) for k in listing.get("keys") or []}
        if curb_wire.read(policy.token, keys, curb_wire.POLICY) is None:
            out.append("unverified")
        if policy.key_id in authority_keys(listing, now)[1]:
            out.append("revoked_key")
    if policy is not None:
        try:
            if policy.expired(now):
                out.append("expired")
        except (KeyError, ValueError):
            out.append("unverified")
    if listing is not None and now > _stamp(listing["expires_at"]):
        out.append("authority_expired")
    return sorted(set(out))


# --- the delegation ----------------------------------------------------------------------------


def delegate(on: bool, *, now: float | None = None) -> None:
    """Turn the delegation on (after the person's approval) or off (any time)."""
    state = load()
    state.delegated_at = (now or time.time()) if on else None
    save(state)


# --- the apply step --------------------------------------------------------------------------


def _expand(path: str, home: Path) -> Path:
    return home / path[2:] if path.startswith("~/") else Path(path)


def probes(rules: curb_compile.Rules, home: Path) -> list[Credential]:
    """One stand-in credential per denied path: the path itself and a file under it."""
    return [
        Credential(
            "policy",
            "policy",
            f"Denied path {number}",
            (_expand(p, home), _expand(p, home) / ".curb-policy-probe"),
        )
        for number, p in enumerate(rules.deny_read, start=1)
    ]


@dataclass
class Change:
    """What the apply step would write for a policy."""

    ready: list[Edit] = field(default_factory=list)
    held: list[Edit] = field(default_factory=list)
    #: Written under the delegation by `apply_received`.
    written: list[Edit] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    guided: list[str] = field(default_factory=list)

    @property
    def edits(self) -> list[Edit]:
        return self.ready + self.held

    def summary(self) -> str:
        return curb_fix.Plan(self.edits).summary()


def _edit(
    rules: curb_compile.Rules, report: AgentReport, platform: str
) -> tuple[Edit | None, list[str]]:
    if report.context.agent == CLAUDE:
        path = agent_paths.claude_config_dir() / "settings.json"
        before = curb_tighten.load(path)
        after, actions, guided = curb_compile.claude_user(rules, before, platform=platform)
        if after == before:
            return None, guided
        return Edit(
            CLAUDE, path, before, after, json.dumps(after, indent=2) + "\n", tuple(actions)
        ), guided
    path = agent_paths.codex_home() / "config.toml"
    before = curb_tighten.load(path)
    original = path.read_text(encoding="utf-8-sig") if path.is_file() else ""
    after, text, actions, guided = curb_compile.codex_user(rules, before, original)
    if text is None:
        return None, guided
    return Edit(CODEX, path, before, after, text, tuple(actions)), guided


def plan(
    policy: Policy,
    reports: Sequence[AgentReport],
    *,
    home: Path,
    platform: str,
    env: Mapping[str, str],
) -> Change:
    """Each agent's user-settings change for the policy, judged by the tighten-only test."""
    change = Change()
    rules = policy.rules()
    for report in reports:
        if report.context.source != "default":
            continue
        where = f"{LABELS[report.context.agent]} user settings"
        try:
            edit, guided = _edit(rules, report, platform)
        except (OSError, ValueError):
            change.guided.append(f"{where} cannot be read, so Curb changes nothing there")
            continue
        change.guided += guided
        if edit is None:
            continue
        if not report.supported:
            # The test uses each agent version's own rules; an untested
            # version has none, so the change's effect is not established.
            change.held.append(edit)
            change.reasons.append(
                f"{where}: Curb's rules are not tested for this version, "
                "so it cannot establish what the change does"
            )
            continue
        verdict = curb_tighten.check(
            report.context,
            edit.path,
            edit.after,
            probes=[*curb_fix._probes(report, home), *probes(rules, home)],
            platform=platform,
            home=home,
            env=env,
        )
        if verdict.tighten_only:
            change.ready.append(edit)
        else:
            change.held.append(edit)
            change.reasons += [f"{where}: {r}" for r in verdict.broader + verdict.unproven]
    return change


def apply_received(
    reports: Sequence[AgentReport],
    *,
    home: Path,
    platform: str,
    env: Mapping[str, str],
    now: float | None = None,
) -> Change | None:
    """The apply step for the newest received policy. None when there is nothing to do.

    Under the delegation, changes that only tighten are written now. The
    rest, or everything without the delegation, waits for `approve`.
    """
    state = load()
    policy = state.received
    if policy is None or (state.applied or {}).get("hash") == policy.hash:
        return None
    stamp = now or time.time()
    change = plan(policy, reports, home=home, platform=platform, env=env)
    if state.delegated_at is not None and change.ready:
        curb_fix.write(change.ready)
        change.written, change.ready = change.ready, []
    if change.edits:
        state.pending = {
            "version": policy.version,
            "hash": policy.hash,
            "summary": change.summary(),
            "reasons": change.reasons
            or (["the delegation is off, so every change waits for you"] if change.ready else []),
            "at": stamp,
        }
    else:
        by = "delegation" if change.written else "already in place"
        state.applied = {"version": policy.version, "hash": policy.hash, "at": stamp, "by": by}
        state.pending = None
    save(state)
    return change


def approved(*, now: float | None = None) -> None:
    """Record that the person approved and Curb wrote the pending change."""
    state = load()
    if state.received is not None:
        state.applied = {
            "version": state.received.version,
            "hash": state.received.hash,
            "at": now or time.time(),
            "by": "person",
        }
    state.pending = None
    save(state)


# --- compliance and drift -------------------------------------------------------------------


def _sandboxed(report: AgentReport, platform: str) -> bool:
    settings = report.settings
    if isinstance(settings, ClaudeSettings):
        return platform != "win32" and settings.sandbox_enabled and not settings.allow_unsandboxed
    return settings.sandbox not in ("danger-full-access", "none", "unknown")


def _mcp_outside(rules: curb_compile.Rules, settings: ClaudeSettings | CodexSettings) -> int:
    allowed = rules.mcp or ()
    outside = 0
    for server in settings.mcp:
        by_name = any(not (s.url or s.command) and s.name == server.name for s in allowed)
        if isinstance(settings, ClaudeSettings):
            ok = by_name or any(
                (s.url and server.url == s.url) or (s.command and server.command == s.command)
                for s in allowed
            )
        else:
            ok = by_name or any(
                (s.url and server.url == s.url)
                or (s.command and server.command[:1] == s.command[:1])
                for s in allowed
            )
        outside += not ok
    return outside


def _web_off(report: AgentReport) -> bool:
    # Codex's cached search still answers from the web, so only "disabled" is off.
    if isinstance(report.settings, CodexSettings):
        return report.settings.web_search == "disabled"
    return _state(report, curb_reach.WEB) in (curb_reach.ABSENT, curb_reach.CONTROLLED)


def _state(report: AgentReport, key: str) -> str:
    return next((c.state for c in report.channels if c.key == key), curb_reach.ABSENT)


def unmet(
    rules: curb_compile.Rules,
    reports: Sequence[AgentReport],
    *,
    home: Path,
    env: Mapping[str, str],
    platform: str,
) -> dict[str, list[str]]:
    """Per agent, each policy rule the effective settings do not meet. No locations named."""
    out: dict[str, list[str]] = {}
    for report in reports:
        if report.context.source != "default":
            continue
        missing: list[str] = []
        checked = curb_reach.assess(
            report.context,
            report.settings,
            probes(rules, home),
            platform=platform,
            home=home,
            env=env,
            version=report.version,
        )
        for reach in checked.readable:
            channels = ", ".join(sorted(curb_reach.LABELS[c].lower() for c in reach.via))
            missing.append(f"{reach.credential.label.lower()} is readable through {channels}")
        if rules.needs_sandbox and not _sandboxed(report, platform):
            missing.append("the sandbox is off or can be bypassed")
        if rules.allowed_domains is not None and _state(report, curb_reach.SHELL_NETWORK) not in (
            curb_reach.ABSENT,
            curb_reach.CONTROLLED,
        ):
            missing.append("sandboxed commands can reach domains outside the policy")
        if rules.web_off and not _web_off(report):
            missing.append("web fetch or web search is on")
        if rules.mcp is not None:
            outside = _mcp_outside(rules, report.settings)
            if outside:
                missing.append(f"{outside} MCP server(s) outside the policy can load")
        if rules.unknown:
            missing.append(
                f"this flanner does not know rule(s) {', '.join(rules.unknown)}: update flanner"
            )
        out[report.context.agent] = missing
    return out


def compliance_hash(policy: Policy, missing: Mapping[str, Sequence[str]]) -> str:
    """The hash the fleet view compares: the same on every device that meets the policy."""
    unmet_rules = {agent: sorted(rules) for agent, rules in missing.items() if rules}
    return curb_wire.doc_hash({"policy": policy.hash, "unmet": unmet_rules})


def reconcile(
    reports: Sequence[AgentReport],
    *,
    home: Path,
    env: Mapping[str, str],
    platform: str,
    now: float | None = None,
) -> dict[str, list[str]] | None:
    """Re-read effective settings against the policy in force. None when there is no policy."""
    state = load()
    if state.received is None:
        return None
    try:
        rules = state.received.rules()
    except ValueError as e:
        missing = {"policy": [f"the policy's rules cannot be read: {e}"]}
    else:
        missing = unmet(rules, reports, home=home, env=env, platform=platform)
    state.unmet = missing
    state.checked_at = now or time.time()
    save(state)
    return missing


def summary(state: State, flagged: Sequence[str] = ()) -> dict[str, Any]:
    """The policy state the control plane and the fleet view are told: no paths, no names."""
    policy = state.received
    pending = state.pending or {}
    return {
        "flags": list(flagged),
        "received": policy.version if policy else None,
        "hash": policy.hash if policy else None,
        "applied": (state.applied or {}).get("version"),
        "approved_by": (state.applied or {}).get("by"),
        "pending": pending.get("version"),
        "rejected": (state.rejected or {}).get("version"),
        "rejected_outcome": (state.rejected or {}).get("outcome"),
        "delegation": state.delegated_at is not None,
        "compliance_hash": compliance_hash(policy, state.unmet) if policy else None,
        "drift": any(state.unmet.values()),
    }
