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
import logging
import os
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from . import operations
from . import session as cache
from .database import ActionModel, get_session, store_open

logger = logging.getLogger(__name__)

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
    touched(row)
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


#: Commands an agent's own hooks run on every file write and skill use.
#: Recording them would bury every decision a person made under noise.
_UNRECORDED = frozenset({"hook guard-write", "hook skill-use"})

#: Set by agent hosts in the shells they run commands in. A person's own
#: terminal carries none of them. Deleting one is easy, so finding one is
#: evidence of an agent and not finding one proves nothing.
AGENT_SHELL_MARKERS = ("CLAUDECODE", "AI_AGENT", "CODEX_SANDBOX", "CODEX_SANDBOX_NETWORK_DISABLED")

_scope: ContextVar[list[str] | None] = ContextVar("flanner_action_scope", default=None)


def agent_shell() -> str:
    """The first agent-host marker in this environment, or an empty string."""
    return next((marker for marker in AGENT_SHELL_MARKERS if os.environ.get(marker)), "")


@contextmanager
def watching() -> Iterator[list[str]]:
    """Collect what is recorded while one command or one request runs.

    A write through dispatch records itself, with its arguments. The command
    line and the web UI also record writes they make directly, and this is
    how they tell whether dispatch already did, so nothing is recorded twice.
    """
    seen: list[str] = []
    token = _scope.set(seen)
    try:
        yield seen
    finally:
        _scope.reset(token)


def touched(row: ActionModel) -> None:
    """Note that this command or request already has its entry."""
    seen = _scope.get()
    if seen is not None:
        seen.append(str(row.id))


def failed(message: str) -> None:
    """Mark the write in progress as refused, for a surface that answers with a page."""
    seen = _scope.get()
    if seen is not None:
        seen.append("failed:" + message)


def written_by(surface: str, name: str) -> operations.Operation | None:
    """The registry operation a CLI command or web route performs, if it writes."""
    if name in _UNRECORDED:
        return None
    for op in operations.OPERATIONS:
        if name in (op.cli if surface == CLI else op.web) and op.access != "read":
            return op
    return None


def record_unless_recorded(
    seen: list[str],
    *,
    surface: str,
    name: str,
    ok: bool,
    arguments: dict[str, Any] | None = None,
) -> None:
    """Record a direct write, unless it wrote through dispatch or wrote nothing."""
    op = written_by(surface, name)
    if op is None or any(not entry.startswith("failed:") for entry in seen):
        return
    refusals = [entry.removeprefix("failed:") for entry in seen if entry.startswith("failed:")]
    if not store_open():
        # No store is open. A command that exited before opening one -- a
        # `--help`, `mesh status` on a machine that has not run `init` --
        # has no history to write into, and saying so with a stack trace
        # under its output read as the tool being broken.
        logger.debug("no store open; %s not recorded in the action history", name)
        return
    try:
        record(
            get_session(),
            surface=surface,
            operation=name,
            arguments=arguments or {},
            state=DONE if ok and not refusals else FAILED,
            message=refusals[0] if refusals else "",
            subject=op.action,
        )
    except Exception:  # noqa: BLE001 - a history that cannot be written must not fail the write
        logger.warning("could not record %s in the action history", name, exc_info=True)
