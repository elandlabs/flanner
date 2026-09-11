"""One history of what was done, through any surface, and on whose behalf.

Every write that passes through `services.dispatch` is recorded here with
an id, the surface it came through, and the person behind it. The command
line, the web UI and the MCP server all read this one table, so something
an agent did shows up in each of them under the same id and the same
person.

The person is who this machine is signed in as, or the account at the
keyboard when it is not signed in. An agent acts for that person, so the
record says both: the person, and that the agent was the surface.

What is kept is an allowlist of arguments. A plan body, a memory or a
piece of evidence is somebody's content, and a history that copied it
would be a second store of it nobody asked for.
"""

from __future__ import annotations

import getpass
import json
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from . import session as cache
from .database import ActionModel

#: Where an action came from.
AGENT = "agent"
CLI = "cli"
WEB = "web"
SURFACES = (AGENT, CLI, WEB)
#: The surfaces a person operates. An agent may ask; only these decide.
PERSON_SURFACES = (CLI, WEB)

#: What became of it.
DONE = "done"
FAILED = "failed"
PENDING = "pending"
APPLIED = "applied"
DECLINED = "declined"
STALE = "stale"

#: Arguments worth keeping: ids and closed vocabularies, never content.
_KEPT = (
    "agent",
    "category",
    "decision",
    "installation_id",
    "kind",
    "manifest_hash",
    "memory_id",
    "name",
    "operation",
    "plan_file_id",
    "project_id",
    "proposal_id",
    "scope",
    "skill_name",
    "to_hash",
    "transfer_id",
    "workspace_id",
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def person() -> str:
    """Who an action is on behalf of: the signed-in user, else this account."""
    held = cache.load()
    if held is not None and held.user_id:
        return held.user_id
    try:
        return getpass.getuser()
    except Exception:  # noqa: BLE001 - a nameless account is still somebody
        return "you"


def kept(arguments: dict[str, Any]) -> dict[str, Any]:
    """The arguments safe to write down."""
    return {key: arguments[key] for key in _KEPT if arguments.get(key) not in (None, "")}


def record(
    session: Session,
    *,
    surface: str,
    operation: str,
    arguments: dict[str, Any],
    state: str,
    message: str = "",
    subject: str = "",
    extra: dict[str, Any] | None = None,
) -> ActionModel:
    """Append one action."""
    row = ActionModel(
        id=uuid.uuid4(),
        surface=surface if surface in SURFACES else "unknown",
        person=person(),
        operation=operation,
        subject=subject[:200],
        state=state,
        detail=json.dumps({"arguments": kept(arguments), **(extra or {})}, sort_keys=True),
        message=message[:500],
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def get(session: Session, action_id: str) -> ActionModel | None:
    try:
        wanted = uuid.UUID(str(action_id))
    except ValueError:
        return None
    return session.query(ActionModel).filter_by(id=wanted).one_or_none()


def recent(session: Session, *, limit: int = 50, state: str | None = None) -> list[ActionModel]:
    """Newest first."""
    query = session.query(ActionModel)
    if state:
        query = query.filter_by(state=state)
    rows = query.order_by(ActionModel.at.desc())
    return list(rows.limit(limit) if limit else rows)


def view(row: ActionModel) -> dict[str, Any]:
    """An action as every surface shows it."""
    detail = json.loads(row.detail or "{}")
    return {
        "id": str(row.id),
        "at": row.at.isoformat() + "Z" if row.at else None,
        "surface": row.surface,
        "person": row.person,
        "operation": row.operation,
        "subject": row.subject,
        "state": row.state,
        "message": row.message,
        "arguments": detail.get("arguments", {}),
        "preview": detail.get("preview"),
        "decided_at": row.decided_at.isoformat() + "Z" if row.decided_at else None,
        "decided_by": row.decided_by or None,
        "decided_surface": row.decided_surface or None,
    }
