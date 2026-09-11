"""Application service layer.

The write operations, in one place, shared by every surface. MCP tools
(``server``), the local web UI (``web``), and the daemon IPC endpoint all
call these functions, so a plan can only be created or revised one way
(PRD Phase 1 §9.5 steps 1-2).

Composition roots must not import each other, so this module is where the
shared implementations live: ``server`` and ``web`` both depend on it, and
neither depends on the other. Each function returns the same plain dict the
MCP tool contract promises, which is also what travels over IPC.

``dispatch`` is the single-writer entry point: when a local daemon is
running the call is forwarded to it, otherwise it executes in-process
(still safe, because the write path takes the cross-process plan lock).
"""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any, NamedTuple, cast
from uuid import UUID

from sqlalchemy.orm import Session

from . import ipc
from .database import create_project as db_create_project
from .database import delete_project as db_delete_project
from .database import (
    get_memory,
    get_plan_file,
    get_project,
    get_session,
    get_version,
    init_database,
    update_project,
)
from .exceptions import DatabaseError, NotFoundError
from .git_integration import find_git_root, update_gitignore, validate_git_repo
from .plan_ops import create_plan, record_new_version
from .storage import ensure_plan_directory_exists
from .utils import hash_content


def ensure_database() -> None:
    """Ensure the database singleton is initialized."""
    try:
        get_session()
    except DatabaseError:
        # Not initialized; path resolution is env-aware in init_database
        init_database()


def create_project(
    name: str,
    description: str = "",
    project_root: str | None = None,
    plan_directory: str = ".plans",
) -> dict[str, Any]:
    """Create a new project with git integration."""
    session = get_session()

    # Auto-detect project root if not provided
    if not project_root:
        project_root = find_git_root(os.getcwd())
        if not project_root:
            return {
                "error": True,
                "message": (
                    "Could not find git repository. Please specify project_root "
                    "or run from within a git repository."
                ),
            }

    # Validate it's a git repository
    if not validate_git_repo(project_root):
        return {"error": True, "message": f"{project_root} is not a valid git repository"}

    # Create project in database
    try:
        project = db_create_project(
            session,
            name=name,
            description=description,
            project_root=project_root,
            plan_directory=plan_directory,
            auto_gitignore=True,
        )
    except ValueError as e:
        return {"error": True, "message": str(e)}

    # Create plan directory
    full_plan_path = ensure_plan_directory_exists(project_root, plan_directory)

    # Update .gitignore
    pattern = plan_directory.rstrip("/") + "/"
    gitignore_updated = update_gitignore(project_root, pattern, comment="MCP Plan Manager")

    return {
        "id": str(project.id),  # Convert UUID to string
        "name": project.name,
        "description": project.description,
        "project_root": project_root,
        "plan_directory": plan_directory,
        "full_plan_path": full_plan_path,
        "gitignore_updated": gitignore_updated,
        "message": f"Project '{name}' created successfully",
    }


def initialize_project(
    project_root: str | None = None,
    name: str | None = None,
    plan_directory: str = ".plans",
) -> dict[str, Any]:
    """Adopt a repository into flanner so its plan documents are tracked."""
    from .agent_hooks import wire_agent_integration
    from .database import get_project_by_root

    ensure_database()
    session = get_session()

    root = project_root or find_git_root(os.getcwd())
    if not root:
        return {
            "error": True,
            "message": "Not inside a git repository; pass project_root or run from a git repo.",
        }

    existing = get_project_by_root(session, root)
    created = existing is None
    if created:
        result: dict[str, Any] = create_project(
            name=name or os.path.basename(os.path.normpath(root)),
            project_root=root,
            plan_directory=plan_directory,
        )
        if result.get("error"):
            return result

    project = get_project_by_root(session, root)
    if not project:
        return {"error": True, "message": "Failed to load project after creation"}

    wiring = wire_agent_integration(root, project)
    return {
        "project_id": str(project.id),
        "project_name": project.name,
        "project_root": project.project_root,
        "plan_directory": project.plan_directory,
        "created": created,
        "installed": wiring.installed,
        "skipped": wiring.skipped,
        "message": (
            f"{'Adopted' if created else 'Re-synced'} project '{project.name}'. "
            f"Create plans with create_plan_file_tool(project_id='{project.id}', name=..., "
            f"content=...)."
        ),
    }


def configure_project(
    project_id: str,
    project_root: str | None = None,
    plan_directory: str | None = None,
    auto_gitignore: bool | None = None,
    description: str | None = None,
) -> dict[str, Any]:
    """Update project configuration."""
    session = get_session()

    # Convert to UUID
    try:
        project_uuid = UUID(project_id)
    except ValueError:
        return {"error": True, "message": f"Invalid UUID: {project_id}"}

    # Get current project
    project = get_project(session, project_uuid)
    if not project:
        return {"error": True, "message": f"Project with ID {project_id} not found"}

    old_plan_dir = project.plan_directory

    # Update project (returns the same identity-mapped instance as `project`, refreshed)
    update_project(
        session,
        project_uuid,
        project_root=project_root,
        plan_directory=plan_directory,
        auto_gitignore=auto_gitignore,
        description=description,
    )

    # If plan directory changed and auto_gitignore is enabled, update .gitignore
    if plan_directory and plan_directory != old_plan_dir and project.auto_gitignore:
        if project.project_root:
            from .git_integration import update_plan_directory_in_gitignore

            old_pattern = old_plan_dir.rstrip("/") + "/"
            new_pattern = plan_directory.rstrip("/") + "/"
            update_plan_directory_in_gitignore(project.project_root, old_pattern, new_pattern)

    return {
        "id": str(project.id),  # Convert UUID to string
        "name": project.name,
        "description": project.description,
        "project_root": project.project_root,
        "plan_directory": project.plan_directory,
        "auto_gitignore": project.auto_gitignore,
        "message": "Project configuration updated successfully",
    }


def delete_project(project_id: str) -> dict[str, Any]:
    """Delete a project and all associated plan files and versions from the database."""
    session = get_session()

    # Convert to UUID
    try:
        project_uuid = UUID(project_id)
    except ValueError:
        return {"error": True, "message": f"Invalid UUID: {project_id}"}

    # Get project info before deletion
    project = get_project(session, project_uuid)
    if not project:
        return {"error": True, "message": f"Project with ID {project_id} not found"}

    project_name = project.name
    plan_files_count = len(project.plan_files)
    plan_directory_path = (
        f"{project.project_root}/{project.plan_directory}" if project.project_root else None
    )

    # Delete project (cascade deletes plan files and versions)
    if db_delete_project(session, project_uuid):
        message = (
            f"Project '{project_name}' deleted successfully. "
            f"Removed {plan_files_count} plan file(s) from database."
        )
        if plan_directory_path:
            message += f" Note: Files on disk at {plan_directory_path} were NOT deleted."

        return {
            "success": True,
            "project_name": project_name,
            "plan_files_deleted": plan_files_count,
            "plan_directory_path": plan_directory_path,
            "message": message,
        }
    else:
        return {"error": True, "message": "Failed to delete project"}


def create_plan_file(
    project_id: str, name: str, content: str, description: str = "", created_by: str = "claude"
) -> dict[str, Any]:
    """Create a new plan file (v1) with proper frontmatter."""
    session = get_session()

    # Convert to UUID
    try:
        project_uuid = UUID(project_id)
    except ValueError:
        return {"error": True, "message": f"Invalid UUID: {project_id}"}

    # Get project
    project = get_project(session, project_uuid)
    if not project:
        return {"error": True, "message": f"Project with ID {project_id} not found"}

    if not project.project_root:
        # Previously crashed with TypeError; surface the config problem instead
        return {
            "error": True,
            "message": f"Project '{project.name}' has no project_root configured",
        }

    # A name may address a subdirectory (e.g. "auth/login-flow"). Normalize it
    # here so the stored name, frontmatter, and file path all agree, and reject
    # path traversal.
    from .utils import sanitize_plan_path

    try:
        name = sanitize_plan_path(name)
    except ValueError as e:
        return {"error": True, "message": str(e)}

    # Create plan record and initial version through the shared write path
    try:
        plan_file, version = create_plan(
            session,
            project=project,
            name=name,
            content=content,
            description=description,
            created_by=created_by,
        )
    except ValueError as e:
        return {"error": True, "message": str(e)}

    return {
        "id": str(plan_file.id),  # Convert UUID to string
        "name": plan_file.name,
        "version": 1,
        "file_path": version.file_path,
        "message": f"Plan file created successfully at {version.file_path}",
    }


def update_plan_file(
    plan_file_id: str, content: str, notes: str = "", created_by: str = "claude"
) -> dict[str, Any] | None:
    """Update a plan file (creates new version if content changed)."""
    session = get_session()

    # Convert to UUID
    try:
        plan_file_uuid = UUID(plan_file_id)
    except ValueError:
        return {"error": True, "message": f"Invalid UUID: {plan_file_id}"}

    # Get plan file
    plan_file = get_plan_file(session, plan_file_uuid)
    if not plan_file:
        return {"error": True, "message": f"Plan file with ID {plan_file_id} not found"}

    # Get project
    project = get_project(session, plan_file.project_id)
    if not project:
        return {"error": True, "message": "Project not found"}

    if not project.project_root:
        # Previously crashed with TypeError; surface the config problem instead
        return {
            "error": True,
            "message": f"Project '{project.name}' has no project_root configured",
        }

    # Get latest version
    latest_version = get_version(session, plan_file_uuid)
    if not latest_version:
        return {"error": True, "message": "No versions found for this plan file"}

    # Check if content actually changed
    new_hash = hash_content(content)
    if latest_version.content_hash == new_hash:
        return {
            "message": "No changes detected (content is identical)",
            "version": latest_version.version,
            "file_path": latest_version.file_path,
        }

    # Create new version (locked: refreshes, picks the next free number, commits)
    if plan_file.auto_version:
        version = record_new_version(
            session,
            project=project,
            plan_file=plan_file,
            content=content,
            created_by=created_by,
            notes=notes,
        )

        return {
            "id": str(version.id),  # Convert UUID to string
            "version": version.version,
            "file_path": version.file_path,
            "content_hash": new_hash,
            "created_by": created_by,
            "message": f"Created version {version.version} at {version.file_path}",
        }

    # auto_version disabled: preserve historical behavior of returning None
    return None


def configure_jira(
    project_id: str, jira_url: str, jira_project_key: str | None = None
) -> dict[str, Any]:
    """Configure JIRA integration for a project."""
    from .database import create_jira_config
    from .jira_utils import is_valid_jira_url, normalize_jira_url

    ensure_database()
    session = get_session()

    # Validate URL
    if not is_valid_jira_url(jira_url):
        return {
            "error": True,
            "message": f"Invalid JIRA URL format: {jira_url}. Expected format: https://company.atlassian.net",
        }

    try:
        project_uuid = UUID(project_id)
    except ValueError:
        return {"error": True, "message": f"Invalid UUID: {project_id}"}

    # Check if project exists
    project = get_project(session, project_uuid)
    if not project:
        return {"error": True, "message": f"Project with ID {project_id} not found"}

    # Create or update JIRA config
    try:
        normalized_url = normalize_jira_url(jira_url)
        jira_config = create_jira_config(session, project_uuid, normalized_url, jira_project_key)

        return {
            "id": str(jira_config.id),
            "project_id": str(jira_config.project_id),
            "jira_url": jira_config.jira_url,
            "jira_project_key": jira_config.jira_project_key,
            "message": f"JIRA configuration updated for project '{project.name}'",
        }
    except Exception as e:
        return {"error": True, "message": str(e)}


def link_plan_to_jira(
    plan_file_id: str,
    jira_issue_key: str,
    issue_type: str | None = None,
    notes: str | None = None,
    created_by: str = "claude",
) -> dict[str, Any]:
    """Link a plan file to a JIRA issue."""
    from .database import create_jira_link, get_jira_config
    from .jira_utils import format_jira_issue_key, generate_jira_issue_url, is_valid_jira_issue_key

    ensure_database()
    session = get_session()

    # Validate and format issue key
    formatted_issue = format_jira_issue_key(jira_issue_key)
    if not is_valid_jira_issue_key(formatted_issue):
        return {
            "error": True,
            "message": (
                f"Invalid JIRA issue key format: {jira_issue_key}. Expected format: PROJECT-123"
            ),
        }

    try:
        plan_file_uuid = UUID(plan_file_id)
    except ValueError:
        return {"error": True, "message": f"Invalid UUID: {plan_file_id}"}

    # Check if plan file exists
    plan_file = get_plan_file(session, plan_file_uuid)
    if not plan_file:
        return {"error": True, "message": f"Plan file with ID {plan_file_id} not found"}

    # Create link
    try:
        jira_link = create_jira_link(
            session, plan_file_uuid, formatted_issue, issue_type, notes, created_by=created_by
        )

        # Get JIRA config for URL generation
        jira_config = get_jira_config(session, plan_file.project_id)
        jira_url = None
        if jira_config:
            jira_url = generate_jira_issue_url(jira_config.jira_url, formatted_issue)

        return {
            "id": str(jira_link.id),
            "plan_file_id": str(jira_link.plan_file_id),
            "jira_issue_key": jira_link.jira_issue_key,
            "jira_issue_type": jira_link.jira_issue_type,
            "notes": jira_link.notes,
            "jira_url": jira_url,
            "created_at": jira_link.created_at.isoformat() if jira_link.created_at else None,
            "message": f"Linked '{plan_file.name}' to {formatted_issue}",
        }
    except ValueError as e:
        return {"error": True, "message": str(e)}
    except Exception as e:
        return {"error": True, "message": f"Failed to create link: {str(e)}"}


def unlink_jira_issue(plan_file_id: str, jira_issue_key: str | None = None) -> dict[str, Any]:
    """Unlink a JIRA issue from a plan file."""
    from .database import delete_all_jira_links, delete_jira_link_by_key
    from .jira_utils import format_jira_issue_key

    ensure_database()
    session = get_session()

    try:
        plan_file_uuid = UUID(plan_file_id)
    except ValueError:
        return {"error": True, "message": f"Invalid UUID: {plan_file_id}"}

    # Check if plan file exists
    plan_file = get_plan_file(session, plan_file_uuid)
    if not plan_file:
        return {"error": True, "message": f"Plan file with ID {plan_file_id} not found"}

    # Unlink
    try:
        if jira_issue_key:
            formatted_issue = format_jira_issue_key(jira_issue_key)
            deleted = delete_jira_link_by_key(session, plan_file_uuid, formatted_issue)
            if deleted:
                return {
                    "success": True,
                    "message": f"Unlinked '{plan_file.name}' from {formatted_issue}",
                }
            else:
                return {"error": True, "message": f"Link to {formatted_issue} not found"}
        else:
            count = delete_all_jira_links(session, plan_file_uuid)
            return {
                "success": True,
                "message": f"Unlinked {count} JIRA issue(s) from '{plan_file.name}'",
                "count": count,
            }
    except Exception as e:
        return {"error": True, "message": f"Failed to unlink: {str(e)}"}


def configure_linear(project_id: str, workspace: str) -> dict[str, Any]:
    """Configure Linear integration for a project."""
    from .database import create_linear_config
    from .linear_utils import is_valid_linear_workspace, normalize_linear_workspace

    ensure_database()
    session = get_session()

    if not is_valid_linear_workspace(workspace):
        return {
            "error": True,
            "message": f"Invalid Linear workspace: {workspace}. Expected a slug like 'acme'.",
        }

    try:
        project_uuid = UUID(project_id)
    except ValueError:
        return {"error": True, "message": f"Invalid UUID: {project_id}"}

    project = get_project(session, project_uuid)
    if not project:
        return {"error": True, "message": f"Project with ID {project_id} not found"}

    try:
        slug = normalize_linear_workspace(workspace)
        config = create_linear_config(session, project_uuid, slug)
        return {
            "id": str(config.id),
            "project_id": str(config.project_id),
            "workspace": config.workspace,
            "message": f"Linear configuration updated for project '{project.name}'",
        }
    except Exception as e:
        return {"error": True, "message": str(e)}


class _IssueMeta(NamedTuple):
    """What asking Linear about an issue told us, if we asked at all.

    `error` is separate from `warning` on purpose. An issue that does not
    exist is a mistake worth refusing; Linear being unreachable is not, and
    linking without verification is still useful.
    """

    title: str | None = None
    state: str | None = None
    warning: str | None = None
    error: dict[str, Any] | None = None


def _verify_issue(issue_id: str, plan_name: str, attach_url: str | None) -> _IssueMeta:
    """Confirm the issue exists and cache its title and state.

    Needs LINEAR_API_KEY; without one this is link-only, which is the
    documented behaviour rather than a failure.
    """
    from .exceptions import LinearError
    from .linear_api import attach_url_to_issue, fetch_issue_by_identifier, get_api_key

    api_key = get_api_key()
    if not api_key:
        return _IssueMeta()

    try:
        issue = fetch_issue_by_identifier(issue_id, api_key)
    except LinearError as e:
        return _IssueMeta(warning=f"Linked without verification: {e}")

    if issue is None:
        return _IssueMeta(error={"error": True, "message": f"Linear issue {issue_id} not found"})
    try:
        if attach_url and issue.get("id"):
            attach_url_to_issue(issue["id"], attach_url, plan_name, api_key)
    except LinearError as e:
        return _IssueMeta(issue["title"], issue["state"], f"Linked without verification: {e}")

    return _IssueMeta(issue["title"], issue["state"])


def link_plan_to_linear(
    plan_file_id: str,
    linear_issue_id: str,
    notes: str | None = None,
    verify: bool = True,
    attach_url: str | None = None,
    created_by: str = "claude",
) -> dict[str, Any]:
    """Link a plan file to a Linear issue."""
    from .database import create_linear_link, get_linear_config
    from .linear_utils import (
        format_linear_issue_id,
        generate_linear_issue_url,
        is_valid_linear_issue_id,
    )

    ensure_database()
    session = get_session()

    issue_id = format_linear_issue_id(linear_issue_id)
    if not is_valid_linear_issue_id(issue_id):
        return {
            "error": True,
            "message": f"Invalid Linear issue id: {linear_issue_id}. Expected format: ENG-123",
        }

    try:
        plan_file_uuid = UUID(plan_file_id)
    except ValueError:
        return {"error": True, "message": f"Invalid UUID: {plan_file_id}"}

    plan_file = get_plan_file(session, plan_file_uuid)
    if not plan_file:
        return {"error": True, "message": f"Plan file with ID {plan_file_id} not found"}

    config = get_linear_config(session, plan_file.project_id)
    meta = _verify_issue(issue_id, plan_file.name, attach_url) if verify else _IssueMeta()
    if meta.error is not None:
        return meta.error

    try:
        link = create_linear_link(
            session,
            plan_file_uuid,
            issue_id,
            issue_title=meta.title,
            issue_state=meta.state,
            notes=notes,
            created_by=created_by,
        )
    except ValueError as e:
        return {"error": True, "message": str(e)}
    except Exception as e:
        return {"error": True, "message": f"Failed to create link: {str(e)}"}

    result: dict[str, Any] = {
        "id": str(link.id),
        "plan_file_id": str(link.plan_file_id),
        "linear_issue_id": link.linear_issue_id,
        "issue_title": link.issue_title,
        "issue_state": link.issue_state,
        "notes": link.notes,
        "linear_url": generate_linear_issue_url(config.workspace, issue_id) if config else None,
        "created_at": link.created_at.isoformat() if link.created_at else None,
        "message": f"Linked '{plan_file.name}' to {issue_id}",
    }
    warning = meta.warning
    if warning:
        result["warning"] = warning
    return result


def unlink_linear_issue(plan_file_id: str, linear_issue_id: str | None = None) -> dict[str, Any]:
    """Unlink a Linear issue from a plan file."""
    from .database import delete_all_linear_links, delete_linear_link_by_id
    from .linear_utils import format_linear_issue_id

    ensure_database()
    session = get_session()

    try:
        plan_file_uuid = UUID(plan_file_id)
    except ValueError:
        return {"error": True, "message": f"Invalid UUID: {plan_file_id}"}

    plan_file = get_plan_file(session, plan_file_uuid)
    if not plan_file:
        return {"error": True, "message": f"Plan file with ID {plan_file_id} not found"}

    try:
        if linear_issue_id:
            issue_id = format_linear_issue_id(linear_issue_id)
            deleted = delete_linear_link_by_id(session, plan_file_uuid, issue_id)
            if deleted:
                return {"success": True, "message": f"Unlinked '{plan_file.name}' from {issue_id}"}
            return {"error": True, "message": f"Link to {issue_id} not found"}
        count = delete_all_linear_links(session, plan_file_uuid)
        return {
            "success": True,
            "message": f"Unlinked {count} Linear issue(s) from '{plan_file.name}'",
            "count": count,
        }
    except Exception as e:
        return {"error": True, "message": f"Failed to unlink: {str(e)}"}


def propose_plan_revision(
    plan_file_id: str,
    artifact_id: str | None = None,
    message: str = "",
    actor: str = "claude",
) -> dict[str, Any]:
    """Offer a plan version for review."""
    from . import review

    # Accepted for compatibility and never used as anybody's authority: the
    # proposer is whoever this device is signed in as. See authz.resolve.
    del actor

    ensure_database()
    session = get_session()
    try:
        plan_uuid = UUID(plan_file_id)
    except ValueError:
        return {"error": True, "message": f"Invalid UUID: {plan_file_id}"}
    plan_file = get_plan_file(session, plan_uuid)
    if not plan_file:
        return {"error": True, "message": f"Plan file with ID {plan_file_id} not found"}
    project = get_project(session, plan_file.project_id)
    if not project:
        return {"error": True, "message": "Project not found"}

    try:
        result = review.propose(
            session,
            project=project,
            plan_file=plan_file,
            artifact_id=artifact_id,
            message=message,
            actor=None,
        )
    except (ValueError, PermissionError) as e:
        return {"error": True, "message": str(e)}
    return {
        "proposal_id": result.event.event_id,
        "target_artifact_id": result.event.payload["target_artifact_id"],
        "plan_name": plan_file.name,
        "message": f"Proposed {plan_file.name} for review",
    }


def record_plan_review_decision(
    plan_file_id: str,
    proposal_id: str,
    decision: str,
    actor: str = "claude",
) -> dict[str, Any]:
    """Approve, reject, request changes on, or withdraw a proposal."""
    from . import review

    # As above: identity is resolved, not supplied.
    del actor

    ensure_database()
    session = get_session()
    try:
        plan_uuid = UUID(plan_file_id)
    except ValueError:
        return {"error": True, "message": f"Invalid UUID: {plan_file_id}"}
    plan_file = get_plan_file(session, plan_uuid)
    if not plan_file:
        return {"error": True, "message": f"Plan file with ID {plan_file_id} not found"}
    project = get_project(session, plan_file.project_id)
    if not project:
        return {"error": True, "message": "Project not found"}

    try:
        result = review.decide(
            session,
            project=project,
            plan_file=plan_file,
            proposal_id=proposal_id,
            action=decision,
            actor=None,
            # Every call reaching this seam came through the MCP server.
            surface=review.AGENT,
        )
    except (ValueError, PermissionError) as e:
        return {"error": True, "message": str(e)}
    return {
        "decision_id": result.event.event_id,
        "decision": decision,
        "advanced_baseline": result.advanced_baseline,
        "accepted_event_id": result.accepted.event_id if result.accepted else None,
        "message": result.reason,
    }


# Every write operation, by the name used on the wire and in the registry.
# --- memory -----------------------------------------------------------------
#
# Same seam as plans: a dict in, a dict out, and never an exception across
# it. The MCP tools are thin wrappers over these, so an agent that gets a
# refusal gets a sentence rather than a traceback.


def _memory_scope(session: Session, scope: str, project_id: str | None) -> tuple[str, Any]:
    """Resolve the scope a memory call is operating in.

    A project id names one explicitly; without one, project scope means the
    repository the caller is standing in. Personal scope never needs a
    project and must not silently acquire one.
    """
    from . import memory_ops

    if scope == "personal":
        return scope, None
    project = None
    if project_id:
        project = get_project(session, UUID(project_id))
        if project is None:
            raise NotFoundError(f"Project with ID {project_id} not found")
    else:
        project = memory_ops.resolve_project(session)
    return scope, project


def memory_remember(
    content: str,
    category: str,
    scope: str = "project",
    project_id: str | None = None,
    title: str | None = None,
    confidence: str = "confirmed",
    sensitivity: str = "normal",
    source_type: str = "explicit",
    source_refs: list[str] | None = None,
    created_by: str = "claude",
    expires_at: str | None = None,
) -> dict[str, Any]:
    """Store one memory."""
    from . import memory_ops

    try:
        ensure_database()
        session = get_session()
        resolved_scope, project = _memory_scope(session, scope, project_id)
        if resolved_scope == "project" and project is None:
            return {
                "error": True,
                "message": (
                    "no flanner project here, so there is nothing to scope this to. "
                    "Run `flanner init` in the repository, or use scope='personal'."
                ),
            }
        memory, created = memory_ops.remember(
            session,
            content=content,
            category=category,
            scope=resolved_scope,
            project=project,
            title=title,
            confidence=confidence,
            sensitivity=sensitivity,
            source_type=source_type,
            source_refs=source_refs,
            created_by=created_by,
            expires_at=_memory_time(expires_at),
        )
    except Exception as e:  # noqa: BLE001 - the seam returns, never raises
        return {"error": True, "message": str(e)}

    return {
        "id": str(memory.id),
        "title": memory.title,
        "scope": memory.scope,
        "category": memory.category,
        "path": memory.file_path,
        "created": created,
        "message": (
            f"Remembered: {memory.title}"
            if created
            else f"Already remembered, on {memory.created_at}: {memory.title}"
        ),
    }


def memory_supersede(
    memory_id: str,
    content: str,
    reason: str = "",
    created_by: str = "claude",
    title: str | None = None,
) -> dict[str, Any]:
    """Replace a memory with a corrected one."""
    from . import memory_ops

    try:
        ensure_database()
        session = get_session()
        old = get_memory(session, UUID(memory_id))
        if old is None:
            return {"error": True, "message": f"No memory with id {memory_id}"}
        project = get_project(session, old.project_id) if old.scope == "project" else None
        replacement = memory_ops.supersede(
            session,
            memory_id=UUID(memory_id),
            content=content,
            project=project,
            reason=reason,
            created_by=created_by,
            title=title,
        )
    except Exception as e:  # noqa: BLE001
        return {"error": True, "message": str(e)}

    return {
        "id": str(replacement.id),
        "supersedes": memory_id,
        "title": replacement.title,
        "path": replacement.file_path,
        "message": f"Superseded. The new memory is {replacement.id}.",
    }


def memory_forget(
    memory_id: str, reason: str = "", purge: bool = False, created_by: str = "claude"
) -> dict[str, Any]:
    """Take a memory out of recall, or remove it from the machine."""
    from . import memory_ops

    try:
        ensure_database()
        session = get_session()
        outcome = memory_ops.forget(
            session,
            memory_id=UUID(memory_id),
            reason=reason,
            purge=purge,
            created_by=created_by,
        )
    except Exception as e:  # noqa: BLE001
        return {"error": True, "message": str(e)}

    outcome["message"] = (
        "Purged. The file and every trace of it are gone from this machine."
        if outcome["purged"]
        else "Forgotten. It will not be recalled; `flanner mem restore` brings it back."
    )
    return outcome


def memory_restore(memory_id: str, created_by: str = "claude") -> dict[str, Any]:
    """Bring a forgotten or expired memory back."""
    from . import memory_ops

    try:
        ensure_database()
        session = get_session()
        memory = memory_ops.restore(session, memory_id=UUID(memory_id), created_by=created_by)
    except Exception as e:  # noqa: BLE001
        return {"error": True, "message": str(e)}

    return {"id": str(memory.id), "status": memory.status, "message": f"Restored: {memory.title}"}


def memory_rebuild() -> dict[str, Any]:
    """Restore the memory catalog and search index from the files."""
    from . import memory_ops

    try:
        ensure_database()
        session = get_session()
        outcome = memory_ops.rebuild(session)
    except Exception as e:  # noqa: BLE001
        return {"error": True, "message": str(e)}

    return {
        "adopted": outcome.adopted,
        "updated": outcome.updated,
        "skipped": outcome.skipped,
        "failed": outcome.failed,
        "message": (
            f"{outcome.total} memories in the catalog. "
            "Event history is not restored: events have no file."
        ),
    }


def memory_consider(
    candidates: list[dict[str, Any]],
    scope: str = "project",
    project_id: str | None = None,
    created_by: str = "claude",
) -> dict[str, Any]:
    """Offer candidates for capture and report what happened to each."""
    from . import memory_ops

    try:
        ensure_database()
        session = get_session()
        resolved_scope, project = _memory_scope(session, scope, project_id)
        policy = memory_ops.policy_for(project)
        offered = [
            memory_ops.Candidate(
                content=str(item.get("content") or ""),
                category=str(item.get("category") or ""),
                confidence=str(item.get("confidence") or "confirmed"),
                why_durable=str(item.get("why_durable") or ""),
                source_refs=tuple(item.get("source_refs") or ()),
                explicit=bool(item.get("explicit")),
                sensitivity=str(item.get("sensitivity") or "normal"),
                source_type=str(item.get("source_type") or "agent_suggested"),
            )
            for item in candidates
        ]
        outcomes = memory_ops.consider(
            session,
            offered,
            policy=policy,
            project=project,
            scope=resolved_scope,
            created_by=created_by,
        )
    except Exception as e:  # noqa: BLE001 - the seam returns, never raises
        return {"error": True, "message": str(e)}

    kept = sum(1 for o in outcomes if o["outcome"] in ("committed", "proposed"))
    return {
        "capture_mode": policy.capture_mode,
        "outcomes": outcomes,
        "message": (
            f"{kept} of {len(outcomes)} kept. "
            + (
                "Nothing is in recall until it is approved."
                if any(o["outcome"] == "proposed" for o in outcomes)
                else "Nothing is waiting on you."
            )
        ),
    }


def memory_decide(
    memory_id: str,
    decision: str,
    content: str | None = None,
    supersede_conflict: bool = False,
    created_by: str = "claude",
    surface: str = "agent",
) -> dict[str, Any]:
    """Approve, edit or reject one proposal.

    `surface` defaults to the agent, the most restricted, so a caller that
    forgets to say where it is gets the rule that refuses.
    """
    from . import memory_ops

    try:
        ensure_database()
        session = get_session()
        outcome = memory_ops.decide(
            session,
            memory_id=UUID(memory_id),
            decision=decision,
            content=content,
            supersede_conflict=supersede_conflict,
            created_by=created_by,
            surface=surface,
        )
    except Exception as e:  # noqa: BLE001
        return {"error": True, "message": str(e)}

    outcome["message"] = {
        "approved": "Approved. It will be recalled from now on.",
        "rejected": "Rejected and removed. It was never in recall.",
    }.get(outcome["outcome"], "Done.")
    return outcome


def memory_attach(
    memory_id: str, path: str, description: str = "", created_by: str = "claude"
) -> dict[str, Any]:
    """Attach a local file to a memory."""
    from . import memory_ops

    try:
        ensure_database()
        session = get_session()
        return memory_ops.attach(
            session,
            memory_id=UUID(memory_id),
            source=path,
            description=description,
            created_by=created_by,
        )
    except Exception as e:  # noqa: BLE001 - the seam returns, never raises
        return {"error": True, "message": str(e)}


def memory_detach(attachment_id: str, created_by: str = "claude") -> dict[str, Any]:
    """Remove one attachment's reference to a file."""
    from . import memory_ops

    try:
        ensure_database()
        session = get_session()
        return memory_ops.detach(session, attachment_id=UUID(attachment_id), created_by=created_by)
    except Exception as e:  # noqa: BLE001
        return {"error": True, "message": str(e)}


def memory_gc() -> dict[str, Any]:
    """Delete stored files nothing points at."""
    from . import memory_ops

    try:
        ensure_database()
        session = get_session()
        outcome = memory_ops.collect_blobs(session)
    except Exception as e:  # noqa: BLE001
        return {"error": True, "message": str(e)}

    outcome["message"] = (
        f"{outcome['removed']} file(s) removed, {outcome['freed_bytes'] // 1024} KB freed."
    )
    return outcome


def memory_share(
    memory_id: str, workspace_id: str = "", created_by: str = "claude"
) -> dict[str, Any]:
    """Promote one memory to the workspace its project has joined."""
    from . import memory_ops

    try:
        ensure_database()
        session = get_session()
        memory = get_memory(session, UUID(memory_id))
        if memory is None:
            return {"error": True, "message": f"No memory with id {memory_id}"}
        target: str = workspace_id
        if not target:
            project = get_project(session, memory.project_id)
            target = (project.workspace_id or "") if project else ""
        if not target:
            return {
                "error": True,
                "message": (
                    "this project has not joined a workspace, so there is nobody "
                    "to share with. Run `flanner join <workspace-id>` first."
                ),
            }
        return memory_ops.promote(
            session, memory_id=UUID(memory_id), workspace_id=target, created_by=created_by
        )
    except Exception as e:  # noqa: BLE001 - the seam returns, never raises
        return {"error": True, "message": str(e)}


def memory_withdraw(
    memory_id: str, reason: str = "", created_by: str = "claude"
) -> dict[str, Any]:
    """Ask peers to stop recalling a shared memory."""
    from . import memory_ops

    try:
        ensure_database()
        session = get_session()
        return memory_ops.withdraw(
            session, memory_id=UUID(memory_id), reason=reason, created_by=created_by
        )
    except Exception as e:  # noqa: BLE001
        return {"error": True, "message": str(e)}


def _memory_time(value: str | None) -> Any:
    """An ISO timestamp from a caller, or nothing."""
    if not value:
        return None
    from datetime import datetime

    return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)


REGISTRY: dict[str, Callable[..., Any]] = {
    "create_project": create_project,
    "initialize_project": initialize_project,
    "configure_project": configure_project,
    "delete_project": delete_project,
    "create_plan_file": create_plan_file,
    "update_plan_file": update_plan_file,
    "configure_jira": configure_jira,
    "link_plan_to_jira": link_plan_to_jira,
    "unlink_jira_issue": unlink_jira_issue,
    "configure_linear": configure_linear,
    "link_plan_to_linear": link_plan_to_linear,
    "unlink_linear_issue": unlink_linear_issue,
    "propose_plan_revision": propose_plan_revision,
    "record_plan_review_decision": record_plan_review_decision,
    "memory_remember": memory_remember,
    "memory_supersede": memory_supersede,
    "memory_forget": memory_forget,
    "memory_restore": memory_restore,
    "memory_rebuild": memory_rebuild,
    "memory_consider": memory_consider,
    "memory_decide": memory_decide,
    "memory_attach": memory_attach,
    "memory_detach": memory_detach,
    "memory_gc": memory_gc,
    "memory_share": memory_share,
    "memory_withdraw": memory_withdraw,
}


def _run(op: str, args: dict[str, Any]) -> Any:
    """Run a write operation on the daemon when one is up, else in-process.

    A daemon that refuses the call (no token, unknown op, bad auth) has not
    executed anything, and ``call_daemon`` reports that as ``None``, so the
    local fallback can never double-apply a write.
    """
    forwarded = ipc.call_daemon("/ipc/call", {"op": op, "args": args})
    if forwarded is not None and "result" in forwarded:
        return forwarded["result"]
    return REGISTRY[op](**args)


def dispatch(op: str, args: dict[str, Any]) -> dict[str, Any]:
    """Run an operation that always reports a result dict."""
    return cast("dict[str, Any]", _run(op, args))


def dispatch_optional(op: str, args: dict[str, Any]) -> dict[str, Any] | None:
    """Run an operation that may report nothing (update with auto-version off)."""
    return cast("dict[str, Any] | None", _run(op, args))
