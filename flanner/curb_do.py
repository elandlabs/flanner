"""What each button in the web UI's Curb section does (Curb PRD §11.3).

A change from the page takes the path its command takes: work out the exact
change, ask the operating system for a yes to that change, write it, and
say what happened. Each function returns that last part, in words for the
page. Nothing here prints, and nothing here reaches the network.
"""

from __future__ import annotations

import hashlib
import os
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import (
    agent_paths,
    curb_approval,
    curb_attribution,
    curb_ci,
    curb_fix,
    curb_kingfisher,
    curb_live,
    curb_observe,
    curb_ops,
    curb_page,
    curb_policy,
    curb_reveal,
    curb_scrub,
    curb_store,
    curb_sweep,
    curb_tester,
    curb_tighten,
)
from .curb_context import LABELS
from .curb_page import count, sentence
from .curb_reach import AgentReport

BUSY = "Nothing changed. Another approval is already waiting on your screen."
MOVED = "Nothing changed. The settings are not as Curb last read them, so it is checking again."


@dataclass(frozen=True)
class Done:
    """What a button did, for the page to say."""

    ok: bool
    said: str


def presence() -> curb_approval.Presence | None:
    """The way this machine asks a person, looked up at most once a minute."""
    found: curb_approval.Presence | None = curb_live.kept(
        "presence", curb_approval.method, seconds=60
    )
    return found


def asker() -> str:
    """Who asks, for a button: `Ask Windows`."""
    return {"win32": "Windows", "darwin": "macOS"}.get(sys.platform, "your desktop")


def scanner_missing() -> str | None:
    """Why the leak scan cannot run here, or None. A yes is kept; a no is asked again."""
    try:
        curb_live.kept("scanner", _scanner_here, seconds=3600)
    except LookupError as missing:
        return str(missing)
    return None


def _scanner_here() -> bool:
    reason = curb_kingfisher.unavailable()
    if reason:
        raise LookupError(reason)  # not kept: installing it should show at the next look
    return True


def _granted(summary: str, change: Any) -> tuple[curb_approval.Broker, curb_approval.Grant] | Done:
    """Ask the operating system for a yes to one exact change."""
    method = presence()
    if method is None:
        return Done(
            False, "Nothing changed. This machine has no way to ask you, so Curb is read-only."
        )
    if not curb_live.APPROVAL.acquire(blocking=False):
        return Done(False, BUSY)
    try:
        broker = curb_approval.Broker(method)
        try:
            grant = broker.request(summary, curb_approval.change_hash(change))
        except curb_approval.Paused as stop:
            return Done(False, sentence(f"Not asked: {stop}"))
    finally:
        curb_live.APPROVAL.release()
    if grant is None:
        return Done(False, "Not approved, so nothing changed.")
    return broker, grant


def _unmoved(edits: Sequence[curb_fix.Edit]) -> bool:
    """Whether each file still holds what its edit was planned from."""
    try:
        return all(curb_tighten.load(edit.path) == edit.before for edit in edits)
    except (OSError, ValueError):
        return False


def _write(plan: curb_fix.Plan, done: str) -> Done:
    """Ask for, and write, a plan the page showed. Refuses a plan the files have left behind."""
    if not _unmoved(plan.edits):
        curb_live.changed()
        return Done(False, MOVED)
    held = _granted(plan.summary(), plan.change())
    if isinstance(held, Done):
        return held
    if not _unmoved(plan.edits):
        curb_live.changed()
        return Done(False, MOVED)
    try:
        curb_fix.apply(plan, *held)
    except (curb_fix.FixFailed, curb_approval.NoGrant) as failure:
        return Done(False, sentence(f"Nothing changed: {failure}"))
    curb_live.changed()
    return Done(True, done)


# --- names and locations --------------------------------------------------------------


def reveal(browser: str) -> Done:
    """Show names in one browser for five minutes, after the system's yes to its code."""
    if not curb_live.APPROVAL.acquire(blocking=False):
        return Done(False, BUSY)
    try:
        curb_reveal.REVEALS.show(browser, curb_approval.Broker(presence()))
    except curb_reveal.NotShown as why:
        return Done(False, sentence(f"Names stay hidden: {why}"))
    finally:
        curb_live.APPROVAL.release()
    return Done(True, "Names and locations show on this page for 5 minutes.")


# --- leaks ------------------------------------------------------------------------------


def scan(folders: Sequence[Path], *, validate: bool) -> Done:
    """Start the leak scan in the background. The page shows its progress."""
    reason = scanner_missing()
    if reason:
        return Done(False, sentence(reason))

    def work(job: curb_live.Job) -> curb_page.Scan:
        # Not at lower priority, as the command runs: that would slow every page.
        report = curb_ops.sweep(folders, validate=validate, progress=job.step)
        curb_store.save_report("sweep", curb_sweep.stored(report))
        return curb_page.keep(report)

    if not curb_live.SCAN.start(work, label="Reading agent files for secrets"):
        return Done(False, "A scan is already running.")
    return Done(True, "")


def removal(scan_held: curb_page.Scan | None, file: str) -> curb_scrub.Scrub | Done:
    """What removing the secrets from one scanned file would change."""
    leak = next((x for x in scan_held.leaks if x.file == file), None) if scan_held else None
    if leak is None:
        return Done(False, "Scan again first: Curb no longer holds where that secret is.")
    reason = scanner_missing()
    if reason:
        return Done(False, sentence(reason))
    try:
        planned = curb_scrub.plan(Path(leak.path), curb_kingfisher.Detector())
    except (curb_scrub.ScrubFailed, OSError) as failure:
        return Done(False, sentence(f"Not removed: {failure}. The file is unchanged"))
    if not planned.secrets:
        return Done(False, "No secret is in that file now.")
    return planned


def remove(scan_held: curb_page.Scan | None, file: str) -> Done:
    """Replace the secrets in one file with placeholders. No backup, so no undo."""
    planned = removal(scan_held, file)
    if isinstance(planned, Done):
        return planned
    held = _granted(
        f"Replace {planned.secrets} secret(s) in {planned.path.name}, with no backup",
        planned.change(),
    )
    if isinstance(held, Done):
        return held
    try:
        curb_scrub.apply(planned, *held)
    except (curb_scrub.ScrubFailed, curb_approval.NoGrant, OSError) as failure:
        return Done(False, sentence(f"Not removed: {failure}. The file is unchanged"))
    if scan_held is not None:
        scan_held.leaks = [x for x in scan_held.leaks if x.file != file]
    return Done(
        True,
        f"Replaced {count(planned.secrets, 'secret')} with placeholders. No copy was kept. "
        "Scan again to bring the counts up to date.",
    )


# --- fixes ------------------------------------------------------------------------------


def apply_fixes(plan: curb_fix.Plan) -> Done:
    if not plan.edits:
        return Done(False, "There is nothing Curb can change for you.")
    fixes = sum(len(edit.actions) for edit in plan.edits)
    return _write(
        plan,
        f"Applied {count(fixes, 'fix', 'fixes')}. Each file is backed up for 7 days. "
        "Curb is checking what your agents can reach now.",
    )


def undo(backup: str) -> Done:
    """Put back the files one fix changed, where they still hold what Curb wrote."""
    if backup not in {row["id"] for row in curb_page.applied()}:
        return Done(False, "That backup is gone. Curb keeps each one for 7 days.")
    folder = curb_fix.backups_dir() / backup
    held = _granted("Put back the agent settings a fix changed", curb_fix.undo_change(folder))
    if isinstance(held, Done):
        return held
    try:
        restored, kept = curb_fix.undo(*held, folder)
    except (curb_approval.NoGrant, OSError, ValueError) as failure:
        return Done(False, sentence(f"Nothing changed: {failure}"))
    curb_live.changed()
    said = f"Put back {count(len(restored), 'file')}." if restored else "Nothing was put back."
    if kept:
        said += f" Left {count(len(kept), 'file')} alone, because it changed after the fix."
    return Done(bool(restored), said)


# --- tests ------------------------------------------------------------------------------


def run_tests(reports: Sequence[AgentReport]) -> Done:
    """Ask, then test each block against the agent itself, in the background."""
    curb_tester.remove_expired()
    todo = [b for b in curb_page.blocks(reports) if b.testable]
    if not todo:
        return Done(False, "No block here is one a test can try, so there is nothing to run.")
    decoys = sum(1 for b in todo if b.target is not None)
    change = {
        "test": sorted({curb_tester.context_key(b.report.context) for b in todo}),
        "sessions": len(todo),
        "decoys": decoys,
    }
    held = _granted(f"Run {len(todo)} test session(s) and plant {decoys} decoy(s)", change)
    if isinstance(held, Done):
        return held
    broker, grant = held
    try:
        broker.redeem(grant, curb_approval.change_hash(change))
    except curb_approval.NoGrant as refusal:
        return Done(False, sentence(f"Nothing ran: {refusal}"))

    def work(job: curb_live.Job) -> dict[str, dict[str, Any]]:
        key, env = curb_store.digest_key(), dict(os.environ)
        fresh: dict[str, dict[str, Any]] = dict(job.result or {})
        results: dict[int, tuple[AgentReport, list[curb_tester.Result]]] = {}
        for number, block in enumerate(todo):
            job.step(number, len(todo), f"{block.report.context.label}: {block.label}")
            context = block.report.context
            if block.target is not None:
                result = curb_tester.test_target(context, block.target, key=key)
            elif block.probe is not None:
                result = curb_tester.test_probe(context, block.probe, env=env)
            else:  # pragma: no cover - a block is one or the other
                continue
            results.setdefault(id(block.report), (block.report, []))[1].append(result)
            fresh[block.id] = {
                "outcomes": dict(result.outcomes),
                "scratch": result.scratch,
                "at": time.time(),
            }
        for report, found in results.values():
            curb_tester.record(found, report, key)
        job.step(len(todo), len(todo))
        return fresh

    if not curb_live.TESTS.start(work, label="Starting the tests"):
        return Done(False, "A test run is already going.")
    return Done(True, "")


def decoys(action: str) -> Done:
    curb_tester.remove_expired()
    if action == "renew":
        return Done(
            True, f"Kept {count(curb_tester.renew(), 'fake credential')} for 30 more days."
        )
    if action == "remove":
        gone = curb_tester.remove(curb_tester.inventory())
        return Done(True, f"Removed {count(gone, 'fake credential')}.")
    return Done(False, "Nothing changed.")


# --- activity ---------------------------------------------------------------------------


def logging(agent: str, on: bool) -> Done:
    """Add or remove Curb's logging hooks in one agent's settings."""
    if agent not in LABELS:
        return Done(False, "Nothing changed.")
    plan = curb_observe.hook_plan([agent], enable=on)
    if not plan.edits:
        said = "; ".join(plan.guided) or f"logging is already {'on' if on else 'off'}"
        return Done(False, sentence(said))
    label = LABELS[agent]
    return _write(
        plan,
        f"Logging is on for {label}. Records start with its next session."
        if on
        else f"Logging is off for {label}. The records already made are kept for 30 days.",
    )


# --- projects ---------------------------------------------------------------------------


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def ci_fix(name: str, root: Path) -> Done:
    """Make the one-line workflow fixes that are safe without a person's judgement."""
    steps, _ = curb_ci.check(root)
    files = [root / w for w in sorted({s.workflow for s in steps})]
    planned = {path: curb_ci.fix(path, write=False) for path in files}
    planned = {path: done for path, done in planned.items() if done}
    if not planned:
        return Done(False, f"No workflow in {name} has a fix that is safe to make blind.")
    fixes = sum(len(done) for done in planned.values())
    change = {"ci-fix": {str(path): _sha(path) for path in planned}}
    held = _granted(
        f"Make {fixes} one-line fix(es) in {len(planned)} workflow file(s) of {name}", change
    )
    if isinstance(held, Done):
        return held
    try:
        held[0].redeem(held[1], curb_approval.change_hash(change))
    except curb_approval.NoGrant as refusal:
        return Done(False, sentence(f"Nothing changed: {refusal}"))
    if any(
        _sha(path) != was for path, was in zip(planned, change["ci-fix"].values(), strict=True)
    ):
        return Done(False, "Nothing changed. A workflow changed while you were asked.")
    made = sum(len(curb_ci.fix(path)) for path in planned)
    curb_live.changed()
    return Done(
        True,
        f"Made {count(made, 'fix', 'fixes')} in {count(len(planned), 'workflow file')} of {name}. "
        "Read the changes, then open a pull request with them.",
    )


def _installed() -> list[str]:
    found = []
    if agent_paths.claude_config_dir().exists():
        found.append("claude")
    if agent_paths.codex_home().exists():
        found.append("codex")
    return found


def _real(edits: Sequence[curb_fix.Edit]) -> list[curb_fix.Edit]:
    """The edits that change their file. The signing plan also lists ones that would not."""
    return [edit for edit in edits if edit.before != edit.after]


def signing_pending() -> list[str]:
    """Agents here whose commits are not yet signed with a key of their own.

    A key alone is not enough: a refused approval leaves the key made and
    the settings as they were.
    """
    signer = curb_attribution.signer_path()
    held = curb_attribution.keys()["keys"]
    return [
        agent
        for agent in _installed()
        if signer is None
        or agent not in held
        or _real(curb_attribution.setup_plan([agent], signer).edits)
    ]


def signing_setup() -> Done:
    """Give each agent a signing key of its own, and route its commits through it."""
    agents = _installed()
    if not agents:
        return Done(False, "Neither Claude Code nor Codex is set up on this machine.")
    signer = curb_attribution.signer_path()
    if signer is None:
        return Done(False, "The flanner-curb-sign program is missing. Reinstall flanner.")
    try:
        for agent in agents:
            if agent not in curb_attribution.keys()["keys"]:
                curb_attribution.create(agent)
    except curb_attribution.NoKeychain as failure:
        return Done(False, sentence(str(failure)))
    plan = curb_attribution.setup_plan(agents, signer)
    plan.edits = _real(plan.edits)
    done = "Each agent now signs its commits with its own key."
    if not plan.edits:
        return Done(True, sentence("; ".join(plan.guided)) if plan.guided else done)
    return _write(plan, done)


def signing_replace(agent: str) -> Done:
    """Retire one agent's key and make its replacement."""
    entry = curb_attribution.keys()["keys"].get(agent)
    if entry is None:
        return Done(False, "That agent has no signing key yet.")
    change = {"rotate": agent, "fingerprint": entry["fingerprint"]}
    label = LABELS.get(agent, agent)
    held = _granted(f"Replace the {label} signing key; the old one can sign nothing new", change)
    if isinstance(held, Done):
        return held
    try:
        held[0].redeem(held[1], curb_approval.change_hash(change))
        curb_attribution.rotate(agent)
    except (curb_approval.NoGrant, curb_attribution.NoKeychain, KeyError) as failure:
        return Done(False, sentence(f"Nothing changed: {failure}"))
    return Done(
        True,
        f"Replaced the {label} key. Commits the old key signed stay valid, and it can sign "
        "nothing new.",
    )


# --- team -------------------------------------------------------------------------------


def approve_policy(reports: Sequence[AgentReport]) -> Done:
    """Apply the org policy change that waits for this person's own yes."""
    state = curb_policy.load()
    if not state.pending or state.received is None:
        return Done(False, "Nothing waits for your approval.")
    change = curb_policy.plan(
        state.received, list(reports), home=Path.home(), platform=sys.platform, env=os.environ
    )
    plan = curb_fix.Plan(change.edits)
    version = state.received.version
    if not plan.edits:
        curb_policy.approved()
        return Done(True, f"Policy version {version} is already in place.")
    done = _write(plan, f"Applied policy version {version}. Each file is backed up for 7 days.")
    if done.ok:
        curb_policy.approved()
    return done


def delegation(on: bool) -> Done:
    """Let signed org policy make changes that only tighten, or stop that."""
    if not on:
        curb_policy.delegate(False)
        return Done(True, "Off. Every later policy change waits for your yes.")
    plan = curb_observe.hook_plan(["claude", "codex"], enable=True, session=True)
    consent = {"delegation": "tighten-only org policy", "edits": plan.change()}
    held = _granted(
        "Let your organization's signed policy make changes that only tighten your agents' "
        "settings, and re-check it at each session start",
        consent,
    )
    if isinstance(held, Done):
        return held
    try:
        held[0].redeem(held[1], curb_approval.change_hash(consent))
        if plan.edits:
            curb_fix.write(plan.edits)
    except (curb_fix.FixFailed, curb_approval.NoGrant) as failure:
        return Done(False, sentence(f"Nothing changed: {failure}"))
    curb_policy.delegate(True)
    curb_live.changed()
    return Done(True, "On. Policy changes that only make things stricter now apply on their own.")


# --- everything -------------------------------------------------------------------------


def forget(*, backups: bool) -> Done:
    """Delete what Curb keeps on this machine, after the system's yes."""
    change: Mapping[str, Any] = {"forget": "reports, decoys, digest key", "backups": backups}
    held = _granted(
        "Delete Curb's stored reports, action log, fake credentials and digest key"
        + (", and its settings backups" if backups else ""),
        change,
    )
    if isinstance(held, Done):
        return held
    try:
        held[0].redeem(held[1], curb_approval.change_hash(change))
    except curb_approval.NoGrant as refusal:
        return Done(False, sentence(f"Nothing deleted: {refusal}"))
    removed = curb_store.forget()
    gone = curb_tester.remove(curb_tester.inventory())
    if gone:
        removed.append(f"{gone} fake credential(s)")
    if backups and curb_fix.count():
        removed.append(f"{curb_fix.count()} settings backup(s)")
        curb_fix.forget_backups()
    curb_live.SCAN.result = None
    curb_live.TESTS.result = None
    curb_live.changed()
    return Done(
        True, sentence("Deleted " + "; ".join(removed)) if removed else "Curb held nothing."
    )
