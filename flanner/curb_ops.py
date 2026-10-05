"""What Curb's surfaces share: this device, its assessments, and whole passes.

The CLI and the web UI both assess the home folder, run the leak sweep and
read this device's team identity. Each of those is written once here, and
prints nothing, so neither surface has to import the other.

Nothing here reaches the network. A team pass is handed its client by the
surface that runs it, and only the CLI builds one.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from . import (
    curb_inventory,
    curb_kingfisher,
    curb_observe,
    curb_policy,
    curb_report,
    curb_settings,
    curb_store,
    curb_sweep,
    curb_team,
)
from .curb_context import LaunchContext
from .curb_reach import AgentReport


def device() -> curb_team.Device | None:
    """This device as the team pass sees it, or None when it is not signed in."""
    from . import __version__, identity
    from . import session as cache

    held = cache.load()
    if held is None:
        return None
    return curb_team.Device(
        device_id=held.device_id,
        organization_id=held.organization_id,
        issuer_keyring=dict(held.keyring),
        claims=held.status().claims,
        offered=tuple(held.curb_capabilities),
        sign=identity.sign,
        version=__version__,
    )


def default_reports(folder: Path) -> list[AgentReport]:
    """Each agent's default launch from one folder."""
    _, contexts, _ = curb_report.contexts(folder, None, None, ())
    return curb_report.assess([c for c in contexts if c.source == "default"])


def home_reports() -> list[AgentReport]:
    """Each agent's default launch from the home folder: the team view of this device.

    Not the current folder, so the fleet view and the policy's drift do not
    change with whichever project a session last started in.
    """
    return default_reports(Path.home())


def team_pass(asking: curb_team.Device, client: curb_team.Client) -> curb_team.Outcome:
    """One team pass for a signed-in device: policy, alerts, reports."""
    return curb_team.cycle(client, asking, home_reports, home=Path.home(), platform=sys.platform)


def enrolled() -> bool:
    """Whether the person turned Curb's team checks on (`flanner curb policy --enrol`)."""
    return curb_policy.load().delegated_at is not None or any(
        curb_observe.hooks_on(agent, session=True) for agent in ("claude", "codex")
    )


def sweep(
    folders: Sequence[Path],
    *,
    validate: bool,
    progress: Callable[[int, int], None] | None = None,
) -> curb_sweep.SweepReport:
    """Run the leak sweep over the agents' own files and each project folder given.

    The caller checked Kingfisher is here and, for `validate`, asked the
    person: validation sends each secret to its own issuer.
    """
    contexts: list[LaunchContext] = []
    seen: set[str] = set()
    for folder in folders:
        for context in curb_report.contexts(folder, None, None, ())[1]:
            key = f"{context.agent}|{context.cwd}|{context.describe()}"
            if key not in seen:  # a scheduled job is the same launch in every folder
                seen.add(key)
                contexts.append(context)
    agents = dict.fromkeys(c.agent for c in contexts)
    versions = {name: curb_inventory.version_of(name)[1] for name in agents}
    launches = [(c, curb_settings.resolve(c), versions[c.agent]) for c in contexts]
    first, *rest = [Path(f).resolve() for f in folders]
    report = curb_sweep.run(
        first,
        curb_kingfisher.Detector(),
        launches,
        home=Path.home(),
        env=dict(os.environ),
        platform=sys.platform,
        key=curb_store.digest_key(),
        also=rest,
        progress=progress,
    )
    if validate:
        curb_sweep.validate(report, curb_kingfisher.validate)
    return report
