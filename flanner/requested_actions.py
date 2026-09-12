"""Actions an agent may ask for but not take: share, install, roll back, restore.

Each one changes what an agent loads, what a team receives, or undoes a
decision somebody made. So an agent's request stores a preview and does
nothing else. A person reads the preview in the command line or the web
UI and applies it or declines it there.

The preview carries a fingerprint of the state it describes: the bytes at
the target, the transfer's state, the memory's status. Applying checks the
fingerprint again, and a preview that no longer describes what is on disk
is marked stale rather than applied. Otherwise a person approves one
change and gets another.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from . import actions, memory_ops, skills_manage, skills_mesh, skills_ops
from . import session as cache
from .database import (
    SkillInstallationModel,
    SkillTransferModel,
    get_memory,
    get_project_by_root,
)
from .entitlements import approval_matches
from .exceptions import FlannerError
from .frontmatter import parse_frontmatter

SKILLS_INSTALL = "skills_install"
SKILLS_ROLLBACK = "skills_rollback"
SKILLS_SHARE = "skills_share"
SKILLS_IMPORT = "skills_import"
MEMORY_RESTORE = "memory_restore"
OPERATIONS = (SKILLS_INSTALL, SKILLS_ROLLBACK, SKILLS_SHARE, SKILLS_IMPORT, MEMORY_RESTORE)


@dataclass(frozen=True)
class Preview:
    """What applying would do, and a fingerprint of the state that says so."""

    summary: str
    changes: list[str] = field(default_factory=list)
    fingerprint: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"summary": self.summary, "changes": self.changes, "fingerprint": self.fingerprint}


def _fingerprint(*parts: object) -> str:
    return hashlib.sha256("|".join(str(p) for p in parts).encode("utf-8")).hexdigest()


def _on_disk(target: Path) -> str:
    return skills_ops.manifest_hash(target)[0] if target.exists() else "absent"


def _skill_name(manifest_hash: str) -> str:
    manifest = skills_manage.snapshot_path(manifest_hash) / "SKILL.md"
    try:
        meta, _ = parse_frontmatter(manifest.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return ""
    return str((meta or {}).get("name") or "").strip()


def _root(arguments: dict[str, Any]) -> Path:
    root = str(arguments.get("project_root") or "")
    if not root:
        raise ValueError("not inside a flanner project, so there is nowhere to act")
    return Path(root)


def preview(session: Session, operation: str, arguments: dict[str, Any]) -> Preview:
    """Describe an action without taking it. Raises ValueError if it cannot be taken."""
    if operation == SKILLS_INSTALL:
        return _preview_install(session, arguments)
    if operation == SKILLS_ROLLBACK:
        return _preview_rollback(session, arguments)
    if operation == SKILLS_SHARE:
        return _preview_share(session, arguments)
    if operation == SKILLS_IMPORT:
        return _preview_import(session, arguments)
    if operation == MEMORY_RESTORE:
        return _preview_restore(session, arguments)
    raise ValueError(f"{operation} is not one of {', '.join(OPERATIONS)}")


def _preview_install(session: Session, arguments: dict[str, Any]) -> Preview:
    wanted = str(arguments.get("manifest_hash") or "")
    if not skills_manage.verify(wanted):
        raise ValueError(f"no verified snapshot for {wanted}")
    name = str(arguments.get("name") or "") or _skill_name(wanted)
    if not name:
        raise ValueError("could not read a skill name out of that snapshot; pass name")
    target = _root(arguments) / ".claude" / "skills" / name
    current = _on_disk(target)
    held = skills_manage.ownership(session, target) if target.exists() else "absent"
    changes = [f"Writes {name} at {target}."]
    if current == wanted:
        changes = [f"{target} already holds exactly these bytes. Nothing would change."]
    elif current != "absent":
        changes.append(f"Replaces what is there now ({current[:19]}…), keeping a copy.")
        if held == skills_manage.EXTERNAL:
            changes.append("That directory was not installed by flanner, or was edited since.")
    return Preview(
        summary=f"Install {name} for {arguments.get('agent') or 'claude-code'}",
        changes=changes,
        fingerprint=_fingerprint(wanted, target, current, held),
    )


def _installation(session: Session, installation_id: str) -> SkillInstallationModel:
    try:
        wanted = uuid.UUID(installation_id)
    except ValueError:
        raise ValueError(f"no installation with id {installation_id}") from None
    row = session.query(SkillInstallationModel).filter_by(id=wanted).one_or_none()
    if row is None:
        raise ValueError(f"no installation with id {installation_id}")
    return row


def _preview_rollback(session: Session, arguments: dict[str, Any]) -> Preview:
    row = _installation(session, str(arguments.get("installation_id") or ""))
    wanted = str(arguments.get("to_hash") or "") or (row.replaced_hash or "")
    if not wanted:
        raise ValueError("that install replaced nothing, so there is nothing to go back to")
    target = Path(row.target_path)
    current = _on_disk(target)
    return Preview(
        summary=f"Roll back {target.name}",
        changes=[f"Puts {wanted[:19]}… back at {target}, replacing {current[:19]}…."],
        fingerprint=_fingerprint(row.id, wanted, target, current),
    )


def _preview_share(session: Session, arguments: dict[str, Any]) -> Preview:
    wanted = str(arguments.get("manifest_hash") or "")
    if not skills_manage.verify(wanted):
        raise ValueError(f"no verified snapshot for {wanted}")
    name = str(arguments.get("name") or "") or _skill_name(wanted)
    project = get_project_by_root(session, str(_root(arguments)))
    workspace = str(getattr(project, "workspace_id", "") or "")
    if not workspace:
        raise ValueError("this project has not joined a workspace, so there is nobody to send to")
    size = len(skills_mesh.bundle(wanted, name, str(arguments.get("agent") or "claude-code")))
    return Preview(
        summary=f"Send {name} to workspace {workspace}",
        changes=[
            f"Signs {size} bytes of package files for everyone in {workspace}.",
            "Sends no evidence, uses or session references.",
            "Each teammate decides whether to install it.",
        ],
        fingerprint=_fingerprint(wanted, name, workspace),
    )


def _preview_import(session: Session, arguments: dict[str, Any]) -> Preview:
    transfer_id = str(arguments.get("transfer_id") or "")
    try:
        wanted = uuid.UUID(transfer_id)
    except ValueError:
        raise ValueError(f"no transfer with id {transfer_id}") from None
    row = session.query(SkillTransferModel).filter_by(id=wanted).one_or_none()
    if row is None:
        raise ValueError(f"no transfer with id {transfer_id}")
    target = _root(arguments) / ".claude" / "skills" / row.skill_name
    current = _on_disk(target)
    changes = [f"Installs {row.skill_name} from device {row.from_device} at {target}."]
    if current != "absent":
        changes.append(f"Replaces what is there now ({current[:19]}…), keeping a copy.")
    return Preview(
        summary=f"Install {row.skill_name}, sent by a teammate",
        changes=changes,
        fingerprint=_fingerprint(row.id, row.state, row.manifest_hash, target, current),
    )


def _preview_restore(session: Session, arguments: dict[str, Any]) -> Preview:
    memory_id = str(arguments.get("memory_id") or "")
    try:
        memory = get_memory(session, uuid.UUID(memory_id))
    except ValueError:
        memory = None
    if memory is None:
        raise ValueError(f"no memory with id {memory_id}")
    return Preview(
        summary=f"Bring back the memory: {memory.title}",
        changes=[f"It is {memory.status} now, and would be recalled again."],
        fingerprint=_fingerprint(memory.id, memory.status),
    )


def request(session: Session, operation: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Store a pending action with its preview. Takes nothing."""
    shown = preview(session, operation, arguments)
    row = actions.record(
        session,
        surface=actions.AGENT,
        operation=operation,
        arguments=arguments,
        state=actions.PENDING,
        subject=shown.summary,
        extra={"preview": shown.as_dict(), "request": arguments},
    )
    return actions.view(row)


def decide(
    session: Session,
    action_id: str,
    *,
    approve: bool,
    surface: str,
    confirmation: str | None = None,
    at_a_terminal: bool = False,
) -> dict[str, Any]:
    """Apply or decline a pending action, for a person.

    An agent that asked for something can usually also type the command that
    applies it, so applying asks for more than the command. Signed in, the
    person confirms in the console, where an agent on this machine holds no
    session, and the confirmation is bound to this action and its preview.
    Not signed in, nothing here can prove a person. The command line then
    refuses inside a shell an agent host started, and refuses unless
    somebody typed the action's code at a terminal. A determined agent can
    unset a variable and allocate a terminal, so this is a hurdle rather
    than proof, and the only proof on offer is an account.

    The web UI never applies. It cannot reach the console, and a request to
    it from this machine looks the same whoever made it.

    Declining grants nothing, so it needs none of this.
    """
    if surface not in actions.PERSON_SURFACES:
        raise PermissionError("only a person decides a requested action, in the CLI or web UI")
    row = actions.get(session, action_id)
    if row is None:
        raise ValueError(f"no action with id {action_id}")
    if row.state != actions.PENDING:
        raise ValueError(f"that action is {row.state}, not pending")
    if approve:
        _require_a_person(row, surface, confirmation, at_a_terminal)

    detail = json.loads(row.detail or "{}")
    arguments = dict(detail.get("request") or {})
    row.decided_at = actions.utcnow()
    row.decided_by = actions.person()
    row.decided_surface = surface

    if not approve:
        row.state = actions.DECLINED
        row.message = "declined; nothing was changed"
    else:
        try:
            now = preview(session, row.operation, arguments)
        except ValueError as error:
            now = None
            row.state, row.message = actions.STALE, f"can no longer be taken: {error}"
        if now is not None and now.fingerprint != (detail.get("preview") or {}).get("fingerprint"):
            row.state = actions.STALE
            row.message = "what the preview described has changed; nothing was changed"
        elif now is not None:
            try:
                _apply(session, row.operation, arguments)
                row.state, row.message = actions.APPLIED, now.summary
            except (ValueError, OSError, FlannerError, skills_manage.ConflictError) as error:
                row.state, row.message = actions.FAILED, str(error)
    session.commit()
    actions.touched(row)
    return actions.view(row)


def _require_a_person(
    row: Any, surface: str, confirmation: str | None, at_a_terminal: bool
) -> None:
    """Refuse an apply nothing shows a person made. See `decide`."""
    if surface != actions.CLI:
        raise PermissionError(
            "applying an agent's request happens in your terminal: "
            f"run flanner actions apply {str(row.id)[:8]}"
        )
    held = cache.load()
    if held is not None and held.status().usable:
        fingerprint = str(
            (json.loads(row.detail or "{}").get("preview") or {}).get("fingerprint", "")
        )
        if not approval_matches(
            confirmation or "",
            held.keyring,
            workspace_id="",
            user_id=held.user_id,
            device_id=held.device_id,
            proposal_id=str(row.id),
            target_artifact_id=fingerprint,
        ):
            raise PermissionError("this apply was not confirmed in the console")
        return
    shell = actions.agent_shell()
    if shell:
        raise PermissionError(
            f"this looks like an agent's shell ({shell} is set). "
            "Apply it from your own terminal, or sign in and confirm in the console."
        )
    if not at_a_terminal:
        raise PermissionError(
            "applying needs somebody at the keyboard: run it in a terminal and type the "
            "code it shows, or sign in so the console can confirm it"
        )


def _apply(session: Session, operation: str, arguments: dict[str, Any]) -> None:
    root = _root(arguments) if operation != MEMORY_RESTORE else None
    agent = str(arguments.get("agent") or "claude-code")
    force = bool(arguments.get("force"))
    if operation == SKILLS_INSTALL:
        wanted = str(arguments["manifest_hash"])
        name = str(arguments.get("name") or "") or _skill_name(wanted)
        project = get_project_by_root(session, str(root))
        target = Path(str(root)) / ".claude" / "skills" / name
        skills_manage.install(session, wanted, target, agent, project, force=force)
    elif operation == SKILLS_ROLLBACK:
        skills_manage.rollback(
            session, str(arguments["installation_id"]), arguments.get("to_hash") or None
        )
    elif operation == SKILLS_SHARE:
        wanted = str(arguments["manifest_hash"])
        name = str(arguments.get("name") or "") or _skill_name(wanted)
        project = get_project_by_root(session, str(root))
        if project is None or not project.workspace_id:
            raise ValueError(
                "this project has not joined a workspace, so there is nobody to send to"
            )
        skills_mesh.share(session, wanted, name, str(project.workspace_id), agent=agent)
    elif operation == SKILLS_IMPORT:
        skills_mesh.install_transfer(
            session,
            str(arguments["transfer_id"]),
            Path(str(root)),
            agent=agent,
            project=get_project_by_root(session, str(root)),
            force=force,
        )
    elif operation == MEMORY_RESTORE:
        memory_ops.restore(
            session, memory_id=uuid.UUID(str(arguments["memory_id"])), created_by=actions.person()
        )
