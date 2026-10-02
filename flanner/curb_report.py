"""Curb's reports for this machine, assembled once for the CLI and the web page alike.

Which launch contexts to assess (each agent found here, from a folder,
plus every scheduled job that runs one), and each one's assessment with
the credentials found and the agent's own version. Read-only.
"""

from __future__ import annotations

import os
import shutil
import sys
from dataclasses import replace
from pathlib import Path

from . import (
    agent_paths,
    curb_context,
    curb_credentials,
    curb_inventory,
    curb_reach,
    curb_settings,
    curb_store,
    curb_tester,
)
from .curb_context import LaunchContext


def contexts(
    directory: Path | None,
    agent: str | None,
    profile: str | None,
    launch: tuple[str, ...],
) -> tuple[Path, list[LaunchContext], list[str]]:
    """The launch contexts to report on, and the agents left out and why.

    Raises curb_context.LaunchError for a launch command Curb cannot read.
    """
    cwd = (directory or Path.cwd()).resolve()
    if launch:
        context = curb_context.parse(launch, cwd)
        if profile and context.agent == curb_context.CODEX:
            context = replace(context, profile=profile)
        return cwd, [context], []
    found: list[LaunchContext] = []
    skipped: list[str] = []
    jobs = curb_inventory.scheduled_jobs(Path.home())
    for name in [agent] if agent else list(curb_context.AGENTS):
        home = agent_paths.claude_config_dir() if name == "claude" else agent_paths.codex_home()
        job_contexts = [j.context for j in jobs if j.context and j.context.agent == name]
        if not (home.is_dir() or job_contexts or shutil.which(name)):
            skipped.append(f"{curb_context.LABELS[name]} was not found on this machine.")
            continue
        context = curb_context.default(name, cwd)
        if name == curb_context.CODEX and profile:
            context = replace(context, profile=profile)
        found += [context, *job_contexts]
    return cwd, found, skipped


def assess(launches: list[LaunchContext]) -> list[curb_reach.AgentReport]:
    """Each launch context's assessment."""
    home = Path.home()
    env = dict(os.environ)
    found: dict[str, list[curb_credentials.Credential]] = {}
    versions: dict[str, str | None] = {}
    reports = []
    for context in launches:
        key = str(context.cwd)
        if key not in found:
            found[key] = curb_credentials.find(home, context.cwd, env, sys.platform)
        if context.agent not in versions:
            versions[context.agent] = curb_inventory.version_of(context.agent)[1]
        reports.append(
            curb_reach.assess(
                context,
                curb_settings.resolve(context),
                found[key],
                platform=sys.platform,
                home=home,
                env=env,
                version=versions[context.agent],
            )
        )
    return reports


def with_proofs(reports: list[curb_reach.AgentReport]) -> list[curb_reach.AgentReport]:
    """Mark channels enforced where the tester proved them, in the same context."""
    if not (curb_store.curb_dir() / "proofs.json").is_file():
        return reports
    key = curb_store.digest_key()
    return [curb_tester.enforced(report, key, Path.home()) for report in reports]
