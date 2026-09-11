"""One answer to "is flanner set up here?", for a person and for an agent.

`flanner status` said whether the server ran and which agents had it
registered. The agent's `project_context` said which project it was in and
what memory kept. Neither said both, so checking a setup meant running two
things and reconciling them by hand. This builds the one answer both show.

Local reads only. Nothing here reaches the network, creates a device key,
or writes to the database.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from . import claude_integration, features, memory_ops, operations, skills_observe
from . import session as cache


def tools() -> list[str]:
    """The tools the MCP server advertises with the features switched on now."""
    on = features.integrations_enabled()
    return sorted({name for op in operations.OPERATIONS if not op.gated or on for name in op.mcp})


def agents(cwd: Path) -> dict[str, Any]:
    """Which agents will find the server, each checked where that agent looks."""
    desktop = claude_integration.check_server_status()
    return {
        "claude_desktop": bool(desktop["registered"] and desktop["config_valid"]),
        "claude_code": str(claude_integration.claude_code_registration(cwd) or "") or None,
        "codex": bool(claude_integration.codex_registration()),
    }


def watching(session: Session, project_root: str | None) -> list[dict[str, Any]]:
    """Which agents' skill use is recorded for this project."""
    if not project_root:
        return []
    here = Path(project_root).resolve()
    return [
        {"agent": scope["agent"], "observing": scope["observing"]}
        for scope in skills_observe.status(session)["scopes"]
        if Path(scope["project_root"]).resolve() == here
    ]


def peers() -> dict[str, Any]:
    """Who this machine is signed in as, and how many other devices it knows."""
    held = cache.load()
    if held is None:
        return {"signed_in": False, "user_id": None, "known_devices": 0, "entitlement": None}
    return {
        "signed_in": True,
        "user_id": held.user_id,
        "known_devices": len([d for d in held.device_keys if d != held.device_id]),
        "entitlement": held.status().status,
    }


def check(session: Session, cwd: Path | None = None) -> dict[str, Any]:
    """Everything a setup question needs, in one place."""
    here = cwd or Path.cwd()
    names = tools()
    result: dict[str, Any] = {
        "agents": agents(here),
        "tools": {
            "count": len(names),
            "names": names,
            "integrations": features.integrations_enabled(),
        },
        "project": None,
        "capture_mode": None,
        "watching": [],
        "peers": peers(),
    }
    project = memory_ops.resolve_project(session, str(here))
    if project is None:
        return result
    result["project"] = {"name": project.name, "root": project.project_root}
    try:
        result["capture_mode"] = memory_ops.policy_for(project).capture_mode
    except Exception as error:  # noqa: BLE001 - a bad policy file is reported, not raised
        result["capture_mode"] = f"unreadable: {error}"
    result["watching"] = watching(session, project.project_root)
    return result
