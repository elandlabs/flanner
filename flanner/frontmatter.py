"""
Frontmatter handling for Flanner

Provides YAML frontmatter generation and parsing for plan files.
"""

from datetime import datetime, timezone
from typing import Any
from uuid import UUID

import frontmatter
import yaml


def _utcnow() -> datetime:
    """Current UTC time, naive to match the DateTime columns."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def generate_frontmatter(
    project_id: UUID,
    project_name: str,
    plan_file_id: UUID,
    plan_name: str,
    version: int,
    created_by: str,
    created_at: datetime | None = None,
    artifact_id: str | None = None,
    parents: list[str] | tuple[str, ...] | None = None,
    workspace_id: str | None = None,
    actor_device_id: str | None = None,
    commit_anchor: str | None = None,
) -> str:
    """
    Generate YAML frontmatter for a plan file.

    Args:
        project_id: UUID of the project
        project_name: Name of the project
        plan_file_id: UUID of the plan file
        plan_name: Name of the plan
        version: Version number
        created_by: Who created this version (user, claude, codex)
        created_at: Creation timestamp (defaults to now)
        artifact_id: Id of the signed artifact for this version (PRD §12.4)
        parents: Parent artifact ids; ordering carries no meaning
        workspace_id: Workspace the version belongs to
        actor_device_id: Device that signed the artifact
        commit_anchor: Repo commit this version was written against

    Returns:
        YAML frontmatter string (including --- delimiters)

    The artifact fields are omitted when absent, so files written before
    artifacts existed keep their exact shape and stay valid.
    """
    if created_at is None:
        created_at = _utcnow()

    # Create frontmatter dictionary
    fm_data = {
        "mcp_plan_file": True,
        "plan_manager_version": "1.0",
        "project_id": str(project_id),  # Convert UUID to string
        "project_name": project_name,
        "plan_file_id": str(plan_file_id),  # Convert UUID to string
        "plan_name": plan_name,
        "version": version,
        "created_at": created_at.isoformat() + "Z",
        "created_by": created_by,
    }

    # Artifact identity, appended only when this version has been signed, so
    # that older files and their hashes are untouched.
    for key, value in (
        ("artifact_id", artifact_id),
        ("parents", list(parents) if parents else None),
        ("workspace_id", workspace_id),
        ("actor_device_id", actor_device_id),
        ("commit_anchor", commit_anchor),
    ):
        if value:
            fm_data[key] = value

    # Generate YAML
    yaml_str = yaml.dump(fm_data, default_flow_style=False, sort_keys=False)

    # Wrap in --- delimiters
    return f"---\n{yaml_str}---"


def parse_frontmatter(content: str) -> tuple[dict[str, Any], str]:
    """
    Parse frontmatter from markdown content.

    Args:
        content: Full markdown content (with frontmatter)

    Returns:
        Tuple of (frontmatter_dict, content_body)
        If no frontmatter, returns (empty dict, original content)
    """
    try:
        post = frontmatter.loads(content)
        return (dict(post.metadata), post.content)
    except (yaml.YAMLError, ValueError):
        # No frontmatter or invalid format
        return ({}, content)


def validate_frontmatter(fm_data: dict[str, Any]) -> bool:
    """
    Validate that frontmatter has required fields.

    Args:
        fm_data: Frontmatter dictionary

    Returns:
        True if valid, False otherwise
    """
    required_fields = [
        "mcp_plan_file",
        "project_id",
        "plan_file_id",
        "plan_name",
        "version",
        "created_by",
    ]

    for field in required_fields:
        if field not in fm_data:
            return False

    # Check that mcp_plan_file is True
    if fm_data.get("mcp_plan_file") is not True:
        return False

    return True


def is_mcp_plan_file(content: str) -> bool:
    """
    Check if content is an MCP plan file by checking frontmatter.

    Args:
        content: File content

    Returns:
        True if this is an MCP plan file
    """
    fm_data, _ = parse_frontmatter(content)
    return fm_data.get("mcp_plan_file") is True


def update_frontmatter(content: str, updates: dict[str, Any]) -> str:
    """
    Update frontmatter fields in content.

    Args:
        content: Original content with frontmatter
        updates: Dictionary of fields to update

    Returns:
        Updated content with modified frontmatter
    """
    fm_data, body = parse_frontmatter(content)

    # Update fields
    fm_data.update(updates)

    # Reconstruct
    post = frontmatter.Post(body, **fm_data)
    # str(): dumps() is untyped, so its return is Any and would silently
    # widen this function's contract to "anything at all".
    return str(frontmatter.dumps(post))


def create_plan_file_content(frontmatter_str: str, body: str) -> str:
    """
    Combine frontmatter and body into complete plan file content.

    Args:
        frontmatter_str: Frontmatter string (with --- delimiters)
        body: Markdown body content

    Returns:
        Complete file content
    """
    # Ensure frontmatter ends with newline
    if not frontmatter_str.endswith("\n"):
        frontmatter_str += "\n"

    # Combine
    return frontmatter_str + "\n" + body


def extract_metadata_from_frontmatter(fm_data: dict[str, Any]) -> dict[str, Any]:
    """
    Extract specific metadata fields from frontmatter.

    Args:
        fm_data: Frontmatter dictionary

    Returns:
        Dictionary with extracted metadata
    """
    return {
        "project_id": fm_data.get("project_id"),
        "project_name": fm_data.get("project_name"),
        "plan_file_id": fm_data.get("plan_file_id"),
        "plan_name": fm_data.get("plan_name"),
        "version": fm_data.get("version"),
        "created_by": fm_data.get("created_by"),
        "created_at": fm_data.get("created_at"),
        "plan_manager_version": fm_data.get("plan_manager_version", "1.0"),
    }


def increment_version_in_frontmatter(content: str) -> str:
    """
    Increment the version number in frontmatter.

    Args:
        content: Original content

    Returns:
        Content with incremented version
    """
    fm_data, body = parse_frontmatter(content)

    if "version" in fm_data:
        fm_data["version"] = fm_data["version"] + 1
    else:
        fm_data["version"] = 1

    # Update created_at
    fm_data["created_at"] = _utcnow().isoformat() + "Z"

    post = frontmatter.Post(body, **fm_data)
    # str(): dumps() is untyped, so its return is Any and would silently
    # widen this function's contract to "anything at all".
    return str(frontmatter.dumps(post))


_CLOSING_DELIMITER = "\n---\n"


def read_managed(text: str) -> tuple[dict[str, Any], str]:
    """Frontmatter metadata plus the body *exactly* as it was written.

    The frontmatter library strips surrounding whitespace from the body, but
    the write path hashes the unstripped body, so a parsed body can never
    reproduce the stored hash. Files are assembled as
    ``frontmatter + "---\\n" + "\\n" + body``, so the body is recovered by
    splitting on the closing delimiter and dropping the single separator
    line. Falls back to the parsed body for anything not in that shape.
    """
    fm_data, parsed = parse_frontmatter(text)
    if not fm_data or not text.startswith("---"):
        return fm_data, parsed
    end = text.find(_CLOSING_DELIMITER, 3)
    if end == -1:
        return fm_data, parsed
    after = text[end + len(_CLOSING_DELIMITER) :]
    return fm_data, after[1:] if after.startswith("\n") else after


# --- memory ------------------------------------------------------------------
#
# A second header format in the same module, because it is the same job and
# splitting it would mean two places that know how a flanner file starts.
# The marker differs (`flanner_memory` rather than `mcp_plan_file`), which
# is what lets one directory hold both and what `is_memory_file` reads.

#: Fields a memory header must carry to be adopted by a rebuild. Anything
#: else is optional and omitted when absent, so a header stays as short as
#: the memory it describes.
MEMORY_REQUIRED_FIELDS = (
    "flanner_memory",
    "id",
    "title",
    "scope",
    "category",
    "status",
    "confidence",
    "created_by",
    "created_at",
)


def generate_memory_frontmatter(
    memory_id: UUID,
    title: str,
    scope: str,
    category: str,
    created_by: str,
    *,
    project_id: UUID | None = None,
    workspace_id: str | None = None,
    status: str = "active",
    confidence: str = "confirmed",
    sensitivity: str = "normal",
    source_type: str = "explicit",
    source_refs: list[str] | tuple[str, ...] | None = None,
    supersedes: UUID | None = None,
    expires_at: datetime | None = None,
    created_at: datetime | None = None,
) -> str:
    """YAML frontmatter for a memory file.

    Ordered so that a person opening the file reads what it is before how
    it is filed. Optional fields are omitted rather than written as null,
    following the plan header, so that adding a field later does not change
    the bytes of every file that does not use it.
    """
    if created_at is None:
        created_at = _utcnow()

    fm_data: dict[str, Any] = {
        "flanner_memory": True,
        "id": str(memory_id),
        "title": title,
        "scope": scope,
        "category": category,
        "status": status,
        "confidence": confidence,
        "sensitivity": sensitivity,
        "source_type": source_type,
        "created_by": created_by,
        "created_at": created_at.isoformat() + "Z",
    }

    for key, value in (
        ("project_id", str(project_id) if project_id else None),
        ("workspace_id", workspace_id),
        ("source_refs", list(source_refs) if source_refs else None),
        ("supersedes", str(supersedes) if supersedes else None),
        ("expires_at", expires_at.isoformat() + "Z" if expires_at else None),
    ):
        if value:
            fm_data[key] = value

    yaml_str = yaml.dump(fm_data, default_flow_style=False, sort_keys=False)
    return f"---\n{yaml_str}---"


def validate_memory_frontmatter(fm_data: dict[str, Any]) -> bool:
    """Whether this header describes a memory a rebuild can adopt.

    Deliberately checks presence and the marker, not the vocabularies. A
    file whose category the current version does not recognise is still
    that person's memory; the database layer decides what it will accept,
    and reporting a bad value there says more than refusing to parse here.
    """
    if fm_data.get("flanner_memory") is not True:
        return False
    return all(field in fm_data for field in MEMORY_REQUIRED_FIELDS)


def is_memory_file(content: str) -> bool:
    """Whether this text is a flanner memory, without trusting its path."""
    try:
        fm_data, _ = parse_frontmatter(content)
    except Exception:  # noqa: BLE001 - a file that cannot be parsed is not one
        return False
    return fm_data.get("flanner_memory") is True
