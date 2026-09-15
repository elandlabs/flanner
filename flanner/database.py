"""
Database layer for Flanner

Provides SQLAlchemy models and database operations.
"""

import json
import logging
import os
import uuid
from collections.abc import Callable, Collection
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol

from sqlalchemy import (
    Boolean,
    Connection,
    DateTime,
    Dialect,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    cast,
    create_engine,
    func,
    inspect,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError, SQLAlchemyError
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    mapped_column,
    relationship,
    sessionmaker,
)
from sqlalchemy.pool import NullPool
from sqlalchemy.types import CHAR, TypeDecorator, TypeEngine

from .exceptions import DatabaseError, DuplicateError, NotFoundError, ValidationError

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    """Current UTC time, naive to match the DateTime columns."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    """Declarative base for all flanner models."""


# Bump when the table layout changes incompatibly; stamped into SQLite's
# PRAGMA user_version so future releases can detect and migrate old files.
SCHEMA_VERSION = 4


class GUID(TypeDecorator[uuid.UUID]):
    """Platform-independent GUID type.

    Uses PostgreSQL's UUID type on PostgreSQL, otherwise uses
    CHAR(32), storing as stringified hex values.
    """

    impl = CHAR
    cache_ok = True

    def load_dialect_impl(self, dialect: Dialect) -> TypeEngine[Any]:
        if dialect.name == "postgresql":
            return dialect.type_descriptor(PG_UUID())
        else:
            return dialect.type_descriptor(CHAR(36))

    def process_bind_param(self, value: uuid.UUID | str | None, dialect: Dialect) -> str | None:
        if value is None:
            return value
        elif dialect.name == "postgresql":
            return str(value)
        else:
            if not isinstance(value, uuid.UUID):
                return str(uuid.UUID(value))
            else:
                return str(value)

    def process_result_value(self, value: Any, dialect: Dialect) -> uuid.UUID | None:
        if value is None:
            return None
        elif isinstance(value, uuid.UUID):
            return value
        else:
            return uuid.UUID(value)


class ProjectModel(Base):
    """Project model - represents a project with plan files"""

    __tablename__ = "projects"

    id: Mapped[uuid.UUID] = mapped_column(GUID, primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String, unique=True, nullable=False, index=True)
    description: Mapped[str | None] = mapped_column(Text)
    # Absolute path to project root (where .git is)
    project_root: Mapped[str | None] = mapped_column(String)
    # Relative path within project (nullable=True preserves the pre-2.0 column DDL;
    # the Python-side default always populates it for ORM-created rows)
    plan_directory: Mapped[str] = mapped_column(String, default=".plans", nullable=True)
    # Auto-update .gitignore
    auto_gitignore: Mapped[bool] = mapped_column(Boolean, default=True, nullable=True)
    # The control-plane workspace this project belongs to, once joined.
    # NULL means solo: artifacts get a local workspace id derived from the
    # project, and review authorization stays advisory (PRD §11.3).
    workspace_id: Mapped[str | None] = mapped_column(String)
    created_at: Mapped[datetime | None] = mapped_column(DateTime, default=_utcnow)
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime, default=_utcnow, onupdate=_utcnow
    )

    # Relationships
    plan_files: Mapped[list["PlanFileModel"]] = relationship(
        "PlanFileModel", back_populates="project", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<Project(id={self.id}, name='{self.name}')>"


class PlanFileModel(Base):
    """Plan file model - represents a plan file with multiple versions"""

    __tablename__ = "plan_files"

    id: Mapped[uuid.UUID] = mapped_column(GUID, primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("projects.id"), nullable=False, index=True
    )
    # Plan name (without .md extension)
    name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    current_version: Mapped[int] = mapped_column(Integer, default=1, nullable=True)
    # Auto-increment version on update
    auto_version: Mapped[bool] = mapped_column(Boolean, default=True, nullable=True)
    created_at: Mapped[datetime | None] = mapped_column(DateTime, default=_utcnow)
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime, default=_utcnow, onupdate=_utcnow
    )

    # Relationships
    project: Mapped["ProjectModel"] = relationship("ProjectModel", back_populates="plan_files")
    versions: Mapped[list["VersionModel"]] = relationship(
        "VersionModel", back_populates="plan_file", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<PlanFile(id={self.id}, name='{self.name}', version={self.current_version})>"


class VersionModel(Base):
    """Version model - represents a specific version of a plan file"""

    __tablename__ = "versions"

    id: Mapped[uuid.UUID] = mapped_column(GUID, primary_key=True, default=uuid.uuid4)
    plan_file_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("plan_files.id"), nullable=False, index=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    # Absolute path to the markdown file
    file_path: Mapped[str] = mapped_column(String, nullable=False)
    # SHA256 hash for change detection
    content_hash: Mapped[str | None] = mapped_column(String)
    # 'user', 'claude', 'codex', etc.
    created_by: Mapped[str] = mapped_column(String, default="user", nullable=True)
    created_at: Mapped[datetime | None] = mapped_column(DateTime, default=_utcnow)
    # Version notes/changelog
    notes: Mapped[str | None] = mapped_column(Text)
    # Id of the signed plan.version artifact this row records (PRD §12.4).
    # Null for versions written before artifacts existed.
    artifact_id: Mapped[str | None] = mapped_column(String, index=True)

    # Relationships
    plan_file: Mapped["PlanFileModel"] = relationship("PlanFileModel", back_populates="versions")

    def __repr__(self) -> str:
        return f"<Version(id={self.id}, version={self.version}, created_by='{self.created_by}')>"


class ArtifactModel(Base):
    """A signed immutable artifact (PRD §12).

    The row is an index over the envelope, not the authority: identity lives
    in the signature and the content hash, so a rebuilt catalog re-derives
    exactly the same artifacts from the files and payloads on disk.

    ``plan_file_id`` is deliberately not a foreign key. Artifacts arrive out
    of order during sync, so one may reference a plan this device has not
    received yet; holding it is correct, rejecting it is not.
    """

    __tablename__ = "artifacts"

    artifact_id: Mapped[str] = mapped_column(String, primary_key=True)
    artifact_type: Mapped[str] = mapped_column(String, nullable=False, index=True)
    protocol_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    organization_id: Mapped[str | None] = mapped_column(String)
    workspace_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    plan_file_id: Mapped[str | None] = mapped_column(String, index=True)
    # Which memory a `mem.*` artifact is about. Its own column rather than
    # reusing plan_file_id, because a query for one must never return the
    # other and a shared column would make that a matter of remembering.
    memory_id: Mapped[str | None] = mapped_column(String, index=True)
    # JSON array of parent artifact ids; ordering carries no meaning.
    parents: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    # Envelope timestamp, stored verbatim. Descriptive only, never ordering.
    created_at: Mapped[str] = mapped_column(String, nullable=False)
    actor_user_id: Mapped[str | None] = mapped_column(String)
    actor_device_id: Mapped[str] = mapped_column(String, nullable=False)
    content_hash: Mapped[str] = mapped_column(String, nullable=False)
    signature: Mapped[str] = mapped_column(String, nullable=False)
    # Event payloads live here; a plan.version's payload is its .md file.
    payload: Mapped[str | None] = mapped_column(Text)
    # When this device stored it. Local bookkeeping, never part of identity.
    received_at: Mapped[datetime | None] = mapped_column(DateTime, default=_utcnow)

    def __repr__(self) -> str:
        return f"<Artifact(id={self.artifact_id[:19]}..., type='{self.artifact_type}')>"


# --- memory -----------------------------------------------------------------
#
# A separate domain sharing one database. Plans and memories differ in every
# way that would make a shared table convenient: plans are browsed and
# versioned, memories are searched and corrected; a plan belongs to one
# project, a memory may be personal; a memory needs expiry, sensitivity and
# confidence that would be dead columns on every plan row. What they share
# is the project registry, the migration mechanism and one backup.

#: Scopes a memory can hold.
PERSONAL = "personal"
PROJECT = "project"
WORKSPACE = "workspace"
MEMORY_SCOPES = (PERSONAL, PROJECT, WORKSPACE)

#: The project id stored for personal memories.
#:
#: A sentinel rather than NULL, and this is the only reason why: SQLite
#: treats NULLs as distinct in a unique index, so two identical personal
#: memories would both insert and the deduplication index would do nothing
#: for exactly the scope with no project to fall back on. Uglier than NULL
#: and it is what makes the constraint real.
NO_PROJECT = uuid.UUID("00000000-0000-0000-0000-000000000000")

#: What a memory is about. The vocabulary is closed so that a policy file
#: can allowlist categories and mean something by it.
MEMORY_CATEGORIES = (
    "fact",
    "decision",
    "preference",
    "constraint",
    "lesson",
    "relationship",
    "task_context",
)

#: Where a memory is in its life. `proposed` exists from the first release
#: even though nothing proposes yet, because the alternative is a status
#: column that changes meaning in a later version.
MEMORY_STATUSES = ("active", "superseded", "expired", "forgotten", "proposed")

#: How much the author stood behind it. Read by ranking, and by the rule
#: that an inference may not silently replace something a person confirmed.
MEMORY_CONFIDENCES = ("confirmed", "inferred", "speculative")

#: How freely it may travel. Nothing enforces the difference between these
#: until sharing exists; they are recorded now so that memories written
#: before then do not have to be re-classified afterwards.
MEMORY_SENSITIVITIES = ("normal", "private", "restricted")

#: How it arrived.
MEMORY_SOURCE_TYPES = ("explicit", "agent_suggested", "tool_observation", "imported")


class MemoryModel(Base):
    """One durable fact, decision, preference, constraint or lesson.

    The row is an index over a Markdown file, not the record itself. `body`
    is stored so search and recall need not open every file, and
    `content_hash` is what detects the two drifting apart.
    """

    __tablename__ = "memories"

    id: Mapped[uuid.UUID] = mapped_column(GUID, primary_key=True, default=uuid.uuid4)
    scope: Mapped[str] = mapped_column(String, nullable=False, index=True)
    # NO_PROJECT for personal scope; see that constant for why not NULL.
    project_id: Mapped[uuid.UUID] = mapped_column(GUID, nullable=False, index=True)
    # Always NULL until memories can be shared.
    workspace_id: Mapped[str | None] = mapped_column(String)

    title: Mapped[str] = mapped_column(String, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(String, nullable=False, index=True)
    status: Mapped[str] = mapped_column(String, nullable=False, default="active", index=True)
    confidence: Mapped[str] = mapped_column(String, nullable=False, default="confirmed")
    sensitivity: Mapped[str] = mapped_column(String, nullable=False, default="normal")
    source_type: Mapped[str] = mapped_column(String, nullable=False, default="explicit")
    # A JSON list of strings: "plan:name_v4", "file:src/auth.py", a url.
    source_refs: Mapped[str] = mapped_column(Text, nullable=False, default="[]")

    supersedes_id: Mapped[uuid.UUID | None] = mapped_column(GUID, ForeignKey("memories.id"))
    content_hash: Mapped[str] = mapped_column(String, nullable=False)
    file_path: Mapped[str] = mapped_column(String, nullable=False)

    created_by: Mapped[str] = mapped_column(String, nullable=False)
    actor_device_id: Mapped[str | None] = mapped_column(String)
    created_at: Mapped[datetime | None] = mapped_column(DateTime, default=_utcnow)
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime, default=_utcnow, onupdate=_utcnow
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime)

    # Diagnostics. Deliberately not read by ranking: a memory recalled often
    # is not therefore more true, and letting use feed relevance is how a
    # search quietly stops surfacing anything new.
    last_recalled_at: Mapped[datetime | None] = mapped_column(DateTime)
    recall_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    events: Mapped[list["MemoryEventModel"]] = relationship(
        "MemoryEventModel", back_populates="memory", cascade="all, delete-orphan"
    )
    attachments: Mapped[list["MemoryAttachmentModel"]] = relationship(
        "MemoryAttachmentModel", back_populates="memory", cascade="all, delete-orphan"
    )

    __table_args__ = (
        # The whole of deduplication. `remember` called twice with the same
        # text returns the first memory rather than making a second, which
        # is also what makes a retried write safe when the reply was lost.
        UniqueConstraint("scope", "project_id", "content_hash", name="ux_memories_dedup"),
        Index("ix_memories_lookup", "scope", "project_id", "status"),
    )

    def __repr__(self) -> str:
        return f"<Memory(id={self.id}, title='{self.title[:32]}')>"


class MemoryEventModel(Base):
    """What happened to a memory, appended and never edited.

    Not canonical, and this is the one place in the memory design where the
    file is not the record: an event has no file, so `flanner mem rebuild`
    restores every memory and none of this. That is stated rather than
    hidden because somebody will eventually ask why a rebuilt catalog has
    no history.
    """

    __tablename__ = "memory_events"

    id: Mapped[uuid.UUID] = mapped_column(GUID, primary_key=True, default=uuid.uuid4)
    memory_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("memories.id"), nullable=False, index=True
    )
    action: Mapped[str] = mapped_column(String, nullable=False)
    actor: Mapped[str] = mapped_column(String, nullable=False)
    at: Mapped[datetime | None] = mapped_column(DateTime, default=_utcnow)
    detail: Mapped[str] = mapped_column(Text, nullable=False, default="{}")

    memory: Mapped["MemoryModel"] = relationship("MemoryModel", back_populates="events")

    def __repr__(self) -> str:
        return f"<MemoryEvent({self.action} on {self.memory_id})>"


class ActionModel(Base):
    """One thing somebody did, or asked for, through any surface.

    Appended by every write the service layer runs, and read by the command
    line, the web UI and the MCP server alike, so one action has one id and
    one person wherever it is looked at. `detail` holds ids and closed
    vocabularies only: never a body, a memory or a piece of evidence.
    """

    __tablename__ = "actions"

    id: Mapped[uuid.UUID] = mapped_column(GUID, primary_key=True, default=uuid.uuid4)
    at: Mapped[datetime | None] = mapped_column(DateTime, default=_utcnow, index=True)
    #: agent, cli or web.
    surface: Mapped[str] = mapped_column(String, nullable=False, index=True)
    #: Who it was on behalf of: the signed-in user, or this account.
    person: Mapped[str] = mapped_column(String, nullable=False)
    operation: Mapped[str] = mapped_column(String, nullable=False, index=True)
    subject: Mapped[str] = mapped_column(String, nullable=False, default="")
    #: done or failed; or pending, then applied, declined or stale.
    state: Mapped[str] = mapped_column(String, nullable=False, index=True)
    detail: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    message: Mapped[str] = mapped_column(Text, nullable=False, default="")
    decided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    decided_by: Mapped[str] = mapped_column(String, nullable=False, default="")
    decided_surface: Mapped[str] = mapped_column(String, nullable=False, default="")

    def __repr__(self) -> str:
        return f"<Action({self.operation} via {self.surface}, {self.state})>"


class MemoryAttachmentModel(Base):
    """One file attached to a memory.

    The row, never the bytes. `content_hash` addresses the file in the blob
    store, which is what lets the same screenshot attached to three
    memories be stored once and lets a backup of this database stay small
    enough to be worth taking.

    A memory may have several attachments and the same file may be attached
    to several memories, so deleting a row never deletes a blob. Collecting
    unreferenced blobs is a separate, deliberate step.
    """

    __tablename__ = "memory_attachments"

    id: Mapped[uuid.UUID] = mapped_column(GUID, primary_key=True, default=uuid.uuid4)
    memory_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("memories.id"), nullable=False, index=True
    )
    content_hash: Mapped[str] = mapped_column(String, nullable=False, index=True)
    mime_type: Mapped[str] = mapped_column(String, nullable=False)
    # For showing a person which file this was. Never used to build a path:
    # the digest decides where the blob lives.
    original_name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)

    # Whether anything has been read out of the file for searching, and
    # what. `unsupported` is an answer, not a failure: an image has no text
    # and saying so is different from having failed to find any.
    extraction_status: Mapped[str] = mapped_column(String, nullable=False, default="not_requested")
    extracted_text: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime | None] = mapped_column(DateTime, default=_utcnow)

    memory: Mapped["MemoryModel"] = relationship("MemoryModel", back_populates="attachments")

    __table_args__ = (
        # The same file attached to the same memory twice is one
        # attachment. Attaching it to a different memory is a second row
        # pointing at the same blob, which is the point of addressing by
        # content.
        UniqueConstraint("memory_id", "content_hash", name="ux_attachment_per_memory"),
    )

    def __repr__(self) -> str:
        return f"<MemoryAttachment({self.original_name} on {self.memory_id})>"


class JiraConfigModel(Base):
    """JIRA configuration model - stores JIRA settings per project"""

    __tablename__ = "jira_config"

    id: Mapped[uuid.UUID] = mapped_column(GUID, primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("projects.id"), unique=True, nullable=False, index=True
    )
    # Base URL (e.g., https://company.atlassian.net)
    jira_url: Mapped[str] = mapped_column(String, nullable=False)
    # Default JIRA project key (e.g., PROJ)
    jira_project_key: Mapped[str | None] = mapped_column(String)
    created_at: Mapped[datetime | None] = mapped_column(DateTime, default=_utcnow)
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime, default=_utcnow, onupdate=_utcnow
    )

    # Relationships
    project: Mapped["ProjectModel"] = relationship(
        "ProjectModel", backref="jira_config", uselist=False
    )

    def __repr__(self) -> str:
        return (
            f"<JiraConfig(id={self.id}, project_id={self.project_id}, jira_url='{self.jira_url}')>"
        )


class JiraLinkModel(Base):
    """JIRA link model - links plan files to JIRA issues"""

    __tablename__ = "jira_links"

    id: Mapped[uuid.UUID] = mapped_column(GUID, primary_key=True, default=uuid.uuid4)
    plan_file_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("plan_files.id"), nullable=False, index=True
    )
    # e.g., PROJ-123
    jira_issue_key: Mapped[str] = mapped_column(String, nullable=False)
    # Epic, Story, Task, Sub-task, etc.
    jira_issue_type: Mapped[str | None] = mapped_column(String)
    # User notes about the link
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime | None] = mapped_column(DateTime, default=_utcnow)
    # Who created the link
    created_by: Mapped[str] = mapped_column(String, default="user", nullable=True)

    # Relationships
    plan_file: Mapped["PlanFileModel"] = relationship("PlanFileModel", backref="jira_links")

    def __repr__(self) -> str:
        return (
            f"<JiraLink(id={self.id}, plan_file_id={self.plan_file_id}, "
            f"issue_key='{self.jira_issue_key}')>"
        )


class LinearConfigModel(Base):
    """Linear configuration model - stores the workspace slug per project"""

    __tablename__ = "linear_config"

    id: Mapped[uuid.UUID] = mapped_column(GUID, primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("projects.id"), unique=True, nullable=False, index=True
    )
    # Workspace URL slug (Linear's urlKey), e.g. "acme" in linear.app/acme/...
    workspace: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime | None] = mapped_column(DateTime, default=_utcnow)
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime, default=_utcnow, onupdate=_utcnow
    )

    # Relationships
    project: Mapped["ProjectModel"] = relationship(
        "ProjectModel", backref="linear_config", uselist=False
    )

    def __repr__(self) -> str:
        return (
            f"<LinearConfig(id={self.id}, project_id={self.project_id}, "
            f"workspace='{self.workspace}')>"
        )


class LinearLinkModel(Base):
    """Linear link model - links plan files to Linear issues"""

    __tablename__ = "linear_links"

    id: Mapped[uuid.UUID] = mapped_column(GUID, primary_key=True, default=uuid.uuid4)
    plan_file_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("plan_files.id"), nullable=False, index=True
    )
    # e.g., ENG-123
    linear_issue_id: Mapped[str] = mapped_column(String, nullable=False)
    # Cached from the Linear API when a key is configured (Tier 2); may be stale.
    issue_title: Mapped[str | None] = mapped_column(String)
    issue_state: Mapped[str | None] = mapped_column(String)
    # User notes about the link
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime | None] = mapped_column(DateTime, default=_utcnow)
    # Who created the link
    created_by: Mapped[str] = mapped_column(String, default="user", nullable=True)

    # Relationships
    plan_file: Mapped["PlanFileModel"] = relationship("PlanFileModel", backref="linear_links")

    def __repr__(self) -> str:
        return (
            f"<LinearLink(id={self.id}, plan_file_id={self.plan_file_id}, "
            f"issue_id='{self.linear_issue_id}')>"
        )


class SeenNonceModel(Base):
    """A signed peer request that has already been answered (PRD §21.1).

    The control plane has had one of these since revocation shipped. The peer
    path did not, so between two devices the timestamp window was the only
    thing standing between a captured request and a replay of it — and a
    window is a poor sole defence, because widening it for drifting clocks
    widens the attack with it.

    Rows live for minutes, not as history: past the window `device_auth`
    refuses the request on age anyway, so forgetting a nonce reopens nothing.
    """

    __tablename__ = "seen_nonces"
    __table_args__ = (UniqueConstraint("device_id", "nonce", name="uq_nonce_per_device"),)

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    device_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    nonce: Mapped[str] = mapped_column(String, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)

    def __repr__(self) -> str:
        return f"<SeenNonce(device_id={self.device_id}, expires_at={self.expires_at})>"


class SkillModel(Base):
    """One skill package, as seen at one path.

    An index over a directory somebody else owns, not the record itself.
    Plugin and user skills stay authoritative where they live; flanner
    reads them and never edits them in place, so a row going stale is a
    normal outcome that `scan` corrects rather than a corruption.

    Identity is (name, agent, directory). The same name at two paths is
    two rows on purpose: that duplication is the thing a reader most
    often needs told about, and merging them here would hide it.
    """

    __tablename__ = "skills"
    __table_args__ = (UniqueConstraint("name", "agent", "directory", name="uq_skill_per_path"),)

    id: Mapped[uuid.UUID] = mapped_column(GUID, primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String, nullable=False, index=True)
    agent: Mapped[str] = mapped_column(String, nullable=False, index=True)
    scope: Mapped[str] = mapped_column(String, nullable=False, index=True)
    directory: Mapped[str] = mapped_column(String, nullable=False)
    #: Where it came from: a plugin name, or the scope when it is not one.
    origin: Mapped[str] = mapped_column(String, nullable=False, default="")
    #: Whether this copy is the one the agent would load for the name.
    effective: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    #: For plugin copies: whether the agent's config lists this revision.
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)

    def __repr__(self) -> str:
        return f"<Skill(name={self.name}, scope={self.scope})>"


class SkillVersionModel(Base):
    """The bytes a package had when it was last seen changed.

    Appended rather than updated. "This skill changed and my agent started
    behaving differently" is only answerable if the earlier hash is still
    here, and a hash is small enough that keeping every one costs nothing.
    """

    __tablename__ = "skill_versions"

    id: Mapped[uuid.UUID] = mapped_column(GUID, primary_key=True, default=uuid.uuid4)
    skill_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("skills.id"), nullable=False, index=True
    )
    #: One digest over every file in the package, paths included.
    manifest_hash: Mapped[str] = mapped_column(String, nullable=False, index=True)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    file_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, index=True)

    def __repr__(self) -> str:
        return f"<SkillVersion(skill_id={self.skill_id}, hash={self.manifest_hash[:16]})>"


class SkillPolicyModel(Base):
    """Whether this machine watches an agent's skill use, and for how long.

    Off unless somebody turned it on, per agent and per project. There is
    no global switch: consent to being watched in one repository is not
    consent in another, and a single flag would make it one.
    """

    __tablename__ = "skill_policies"
    __table_args__ = (UniqueConstraint("agent", "project_id", name="uq_skill_policy_scope"),)

    id: Mapped[uuid.UUID] = mapped_column(GUID, primary_key=True, default=uuid.uuid4)
    agent: Mapped[str] = mapped_column(String, nullable=False, index=True)
    project_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("projects.id"), nullable=False, index=True
    )
    observing: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    #: How long a recorded use is kept. Days, because a window shorter than
    #: a working day answers nothing and one longer than a month is a
    #: liability nobody asked for.
    retention_days: Mapped[int] = mapped_column(Integer, nullable=False, default=30)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)

    def __repr__(self) -> str:
        return f"<SkillPolicy(agent={self.agent}, observing={self.observing})>"


class SkillObservationModel(Base):
    """One recorded use of a skill.

    What is stored is that a named skill was invoked, when, and by which
    session — never what was said to the agent or what it replied. A
    session reference is a local identifier, not a conversation.

    `skill_version_id` is nullable and stays null when the use cannot be
    tied to one package with confidence. An observation assigned to the
    wrong version is worse than one assigned to none: the first quietly
    corrupts every comparison drawn from it.
    """

    __tablename__ = "skill_observations"

    id: Mapped[uuid.UUID] = mapped_column(GUID, primary_key=True, default=uuid.uuid4)
    #: Same event delivered twice writes one row. A hook can fire again
    #: after a retry, and a doubled count reads as real usage.
    dedupe_key: Mapped[str] = mapped_column(String, nullable=False, unique=True, index=True)
    agent: Mapped[str] = mapped_column(String, nullable=False, index=True)
    project_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("projects.id"), nullable=False, index=True
    )
    #: A local session identifier from the harness. Not a conversation.
    session_ref: Mapped[str] = mapped_column(String, nullable=False, default="")
    skill_name: Mapped[str] = mapped_column(String, nullable=False, index=True)
    skill_version_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("skill_versions.id"), nullable=True, index=True
    )
    #: What happened: an explicit invocation, or a load we merely inferred.
    kind: Mapped[str] = mapped_column(String, nullable=False, default="invocation")
    #: Where the evidence came from, and how far it can be trusted. Both
    #: travel with every row so a report can say which is which.
    evidence: Mapped[str] = mapped_column(String, nullable=False, default="hook")
    certainty: Mapped[str] = mapped_column(String, nullable=False, default="observed")
    #: The model or harness build, when the harness said. Blank is common
    #: and groups as unknown rather than being folded into a real one.
    agent_version: Mapped[str] = mapped_column(String, nullable=False, default="")
    occurred_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, index=True)

    def __repr__(self) -> str:
        return f"<SkillObservation(skill={self.skill_name}, at={self.occurred_at})>"


class SkillCoverageWindowModel(Base):
    """A stretch of time this machine was actually watching.

    Without this, a report cannot tell "nobody used that skill" from "the
    hook was never installed". Both look like zero, and only one of them
    means anything.
    """

    __tablename__ = "skill_coverage_windows"

    id: Mapped[uuid.UUID] = mapped_column(GUID, primary_key=True, default=uuid.uuid4)
    agent: Mapped[str] = mapped_column(String, nullable=False, index=True)
    project_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("projects.id"), nullable=False, index=True
    )
    started_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, index=True)
    #: Null while the window is open.
    ended_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    #: What the adapter could do at the time, so an old window is not read
    #: as if it had today's abilities.
    capabilities: Mapped[str] = mapped_column(String, nullable=False, default="")
    #: Events that arrived and could not be stored. A gap that is known
    #: about is a different thing from a gap that is not.
    dropped_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    gap_reason: Mapped[str] = mapped_column(String, nullable=False, default="")

    def __repr__(self) -> str:
        return f"<SkillCoverageWindow(agent={self.agent}, started={self.started_at})>"


class SkillInstallationModel(Base):
    """A skill package flanner put somewhere, and what it replaced.

    The row is what makes an install reversible and a conflict detectable.
    `manifest_hash` is what was meant to be installed, `observed_hash` what
    was verified on disk afterwards, and `replaced_hash` names the snapshot
    of whatever was there before — so going back is looking up a hash, not
    hoping a backup was taken.

    A directory whose current bytes no longer match `observed_hash` has
    been edited by hand, and is treated as somebody else's from then on.
    """

    __tablename__ = "skill_installations"

    id: Mapped[uuid.UUID] = mapped_column(GUID, primary_key=True, default=uuid.uuid4)
    agent: Mapped[str] = mapped_column(String, nullable=False, index=True)
    #: Null for an install outside any project, such as into ~/.claude.
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("projects.id"), nullable=True, index=True
    )
    target_path: Mapped[str] = mapped_column(String, nullable=False, index=True)
    manifest_hash: Mapped[str] = mapped_column(String, nullable=False, index=True)
    observed_hash: Mapped[str] = mapped_column(String, nullable=False, default="")
    replaced_hash: Mapped[str] = mapped_column(String, nullable=False, default="")
    ownership: Mapped[str] = mapped_column(String, nullable=False, default="flanner")
    status: Mapped[str] = mapped_column(String, nullable=False, default="installed", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)

    def __repr__(self) -> str:
        return f"<SkillInstallation(target={self.target_path}, status={self.status})>"


class SkillEvidenceModel(Base):
    """A piece of work somebody explicitly handed over to learn from.

    Never harvested. Metadata observation cannot see repeated prompt
    content, and there is deliberately no fallback that reads conversation
    archives, so everything here arrived because a person submitted it or
    an agent reported it and said so.

    `expires_at` is not decoration. An excerpt is somebody's working
    material, and keeping it indefinitely to maybe write a skill one day
    is not a trade they agreed to.
    """

    __tablename__ = "skill_evidence"

    id: Mapped[uuid.UUID] = mapped_column(GUID, primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("projects.id"), nullable=False, index=True
    )
    #: The local session it came from. Not a conversation, just a name.
    session_ref: Mapped[str] = mapped_column(String, nullable=False, default="", index=True)
    #: Who says so: `user` submitted it, `agent` reported it. An agent's
    #: account of its own work is weaker evidence and is labelled as such
    #: wherever it is shown.
    source: Mapped[str] = mapped_column(String, nullable=False, default="user")
    #: What kind of knowledge this is (LRN-01). Only `procedure` is
    #: eligible to become a skill; a repeated fact is a memory.
    kind: Mapped[str] = mapped_column(String, nullable=False, default="procedure", index=True)
    summary: Mapped[str] = mapped_column(String, nullable=False, default="")
    body: Mapped[str] = mapped_column(Text, nullable=False, default="")
    #: What says this went well, and how strongly. "Nobody complained" is
    #: not success evidence and is recorded as `none`.
    outcome: Mapped[str] = mapped_column(String, nullable=False, default="unknown")
    outcome_detail: Mapped[str] = mapped_column(String, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, index=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)

    def __repr__(self) -> str:
        return f"<SkillEvidence(kind={self.kind}, source={self.source})>"


class SkillProposalModel(Base):
    """A suggested new skill, or a change to one, waiting on a person.

    Nothing here is ever activated by the thing that proposed it. The
    draft carries a hash, an approval names that exact hash, and an
    install checks the approval — so editing a draft after approval
    invalidates the approval rather than quietly shipping the edit.
    """

    __tablename__ = "skill_proposals"

    id: Mapped[uuid.UUID] = mapped_column(GUID, primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("projects.id"), nullable=False, index=True
    )
    #: create, update or merge.
    action: Mapped[str] = mapped_column(String, nullable=False, default="create")
    skill_name: Mapped[str] = mapped_column(String, nullable=False, index=True)
    #: What the draft would change, when it is not a new skill.
    base_hash: Mapped[str] = mapped_column(String, nullable=False, default="")
    draft_body: Mapped[str] = mapped_column(Text, nullable=False, default="")
    #: Over `draft_body`. What an approval binds to.
    draft_hash: Mapped[str] = mapped_column(String, nullable=False, index=True)
    #: Evidence ids, comma separated. A proposal that cannot point at what
    #: it came from is not reviewable.
    provenance: Mapped[str] = mapped_column(Text, nullable=False, default="")
    rationale: Mapped[str] = mapped_column(Text, nullable=False, default="")
    #: draft, approved, rejected, superseded.
    state: Mapped[str] = mapped_column(String, nullable=False, default="draft", index=True)
    created_by: Mapped[str] = mapped_column(String, nullable=False, default="user")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)

    def __repr__(self) -> str:
        return f"<SkillProposal(skill={self.skill_name}, state={self.state})>"


class SkillApprovalModel(Base):
    """One person, saying yes to one exact revision.

    Append-only. Withdrawing an approval writes a new row rather than
    deleting the old one: "who approved this and when" has to stay
    answerable after somebody changes their mind.
    """

    __tablename__ = "skill_approvals"

    id: Mapped[uuid.UUID] = mapped_column(GUID, primary_key=True, default=uuid.uuid4)
    proposal_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("skill_proposals.id"), nullable=False, index=True
    )
    #: The draft hash this approval covers, and nothing else.
    approved_hash: Mapped[str] = mapped_column(String, nullable=False, index=True)
    actor: Mapped[str] = mapped_column(String, nullable=False, default="")
    decision: Mapped[str] = mapped_column(String, nullable=False, default="approved")
    note: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, index=True)

    def __repr__(self) -> str:
        return f"<SkillApproval(decision={self.decision}, hash={self.approved_hash[:16]})>"


class SkillEvalCaseModel(Base):
    """One task a skill is meant to be good at, written down beforehand.

    Fixtures are hashed so a comparison can say which version of the task
    it ran. Changing the fixture and reusing the old results is the
    easiest way to produce a flattering number by accident.
    """

    __tablename__ = "skill_eval_cases"

    id: Mapped[uuid.UUID] = mapped_column(GUID, primary_key=True, default=uuid.uuid4)
    suite: Mapped[str] = mapped_column(String, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    prompt: Mapped[str] = mapped_column(Text, nullable=False, default="")
    rubric: Mapped[str] = mapped_column(Text, nullable=False, default="")
    fixture_hash: Mapped[str] = mapped_column(String, nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)

    def __repr__(self) -> str:
        return f"<SkillEvalCase(suite={self.suite}, name={self.name})>"


class SkillModelProfileModel(Base):
    """A model and harness a comparison ran against.

    A model is not an agent. Calling an endpoint directly does not show
    how a skill behaves inside Claude Code, so the harness and its version
    are separate fields and both appear in every report.
    """

    __tablename__ = "skill_model_profiles"

    id: Mapped[uuid.UUID] = mapped_column(GUID, primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String, nullable=False, unique=True, index=True)
    provider: Mapped[str] = mapped_column(String, nullable=False, default="")
    model: Mapped[str] = mapped_column(String, nullable=False, default="")
    #: Null when the provider does not publish one, which is the usual case.
    revision: Mapped[str | None] = mapped_column(String, nullable=True)
    harness: Mapped[str] = mapped_column(String, nullable=False, default="")
    harness_version: Mapped[str] = mapped_column(String, nullable=False, default="")
    settings_hash: Mapped[str] = mapped_column(String, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)

    def __repr__(self) -> str:
        return f"<SkillModelProfile(name={self.name})>"


class SkillTrialModel(Base):
    """One result: this fixture, this skill version, this profile.

    A cell of the comparison matrix. Cells with no row are reported as not
    run rather than as a zero — an empty cell and a bad score are
    different facts, and only one of them is evidence.
    """

    __tablename__ = "skill_trials"

    id: Mapped[uuid.UUID] = mapped_column(GUID, primary_key=True, default=uuid.uuid4)
    suite: Mapped[str] = mapped_column(String, nullable=False, index=True)
    case_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("skill_eval_cases.id"), nullable=False, index=True
    )
    profile_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("skill_model_profiles.id"), nullable=False, index=True
    )
    #: The skill version under test, or empty for the no-skill baseline.
    skill_hash: Mapped[str] = mapped_column(String, nullable=False, default="", index=True)
    #: Whether this row is the baseline the others are read against.
    baseline: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    #: passed, failed, error, skipped.
    result: Mapped[str] = mapped_column(String, nullable=False, default="skipped")
    score: Mapped[str] = mapped_column(String, nullable=False, default="")
    #: Where the number came from. A result with no stated source is not
    #: usable as evidence and is displayed as such.
    measured_by: Mapped[str] = mapped_column(String, nullable=False, default="")
    note: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, index=True)

    def __repr__(self) -> str:
        return f"<SkillTrial(suite={self.suite}, result={self.result})>"


class SkillTransferModel(Base):
    """A skill package that arrived from somewhere else, and what became of it.

    Receiving, verifying and installing are three states rather than one
    event, because they are three decisions. A package can be verified and
    still be something this machine never installs, and a row that
    collapsed the three would make "did I agree to this?" unanswerable.
    """

    __tablename__ = "skill_transfers"

    id: Mapped[uuid.UUID] = mapped_column(GUID, primary_key=True, default=uuid.uuid4)
    #: The signed envelope it travelled in.
    artifact_id: Mapped[str] = mapped_column(String, nullable=False, unique=True, index=True)
    workspace_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    skill_name: Mapped[str] = mapped_column(String, nullable=False, index=True)
    manifest_hash: Mapped[str] = mapped_column(String, nullable=False, index=True)
    #: The agent the package was built for. Installing it for a different
    #: one is refused rather than attempted.
    agent: Mapped[str] = mapped_column(String, nullable=False, default="claude-code")
    from_device: Mapped[str] = mapped_column(String, nullable=False, default="")
    #: received, verified, installed, rejected.
    state: Mapped[str] = mapped_column(String, nullable=False, default="received", index=True)
    #: Why, when it was rejected or could not be installed.
    detail: Mapped[str] = mapped_column(Text, nullable=False, default="")
    #: A one-time copy stays on the version that was sent. A subscription
    #: names the channel it came through; it still does not install.
    channel: Mapped[str] = mapped_column(String, nullable=False, default="")
    pinned: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)

    def __repr__(self) -> str:
        return f"<SkillTransfer(skill={self.skill_name}, state={self.state})>"


class SkillChannelModel(Base):
    """A subscription to somebody's updates for a named skill.

    Notify and review, never install. A channel that could install would
    hand whoever publishes it the ability to change what an agent on this
    machine reads, which is the thing the whole approval chain exists to
    stop.
    """

    __tablename__ = "skill_channels"
    __table_args__ = (UniqueConstraint("workspace_id", "name", name="uq_skill_channel"),)

    id: Mapped[uuid.UUID] = mapped_column(GUID, primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String, nullable=False, index=True)
    subscribed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    #: The newest version seen through this channel, installed or not.
    last_seen_hash: Mapped[str] = mapped_column(String, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)

    def __repr__(self) -> str:
        return f"<SkillChannel(name={self.name}, subscribed={self.subscribed})>"


# Database session management
_engine: Engine | None = None
_SessionLocal: sessionmaker[Session] | None = None


def _migration_1(conn: Connection) -> None:
    """0 -> 1: schema versioning introduced. The v1 layout matches the
    pre-versioning tables, so there is no DDL to apply."""


# Target version -> the step that upgrades from (target - 1) to target. Add an
# entry for every SCHEMA_VERSION bump; _apply_schema runs the pending ones in
# order. New whole tables are handled by create_all; use a migration here for
# in-place changes to existing tables (ADD COLUMN, backfills, index changes).
def _migration_2(conn: Connection) -> None:
    """1 -> 2: versions gain the id of their signed artifact (PRD §12.4).

    Existing rows keep NULL: they predate artifacts and are still valid
    plan versions, they simply carry no signature yet.
    """
    _add_column(conn, "versions", "artifact_id", "VARCHAR")
    conn.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_versions_artifact_id ON versions (artifact_id)"
    )


def _migration_3(conn: Connection) -> None:
    """2 -> 3: projects may name the control-plane workspace they joined.

    Existing rows keep NULL, which is the solo case and stays correct: the
    workspace id is derived locally and review remains advisory.
    """
    _add_column(conn, "projects", "workspace_id", "VARCHAR")


#: Whether this Python's SQLite was built with FTS5.
#:
#: Read rather than assumed. The module is standard in CPython's bundled
#: SQLite and absent from some distribution and conda builds, and the
#: failure without this flag is a crash on the first search rather than a
#: slower search. `flanner doctor` reports it; memory recall falls back to
#: LIKE; nothing about plans depends on it either way.
SEARCH_INDEX_AVAILABLE = True

#: The memory search index.
#:
#: Deliberately not a `Base` table and deliberately not a migration. It is
#: not a `Base` table because an FTS5 virtual table is not something
#: `create_all` can make. It is not a migration because `_apply_schema`
#: stamps a fresh database and returns before the ladder runs, so a
#: migration here would exist on every upgraded machine and on no new one --
#: which is the worst shape a schema bug can take, since it only appears for
#: people who have never used the product.
#:
#: `IF NOT EXISTS` makes it safe to run on every start, which is what keeps
#: it true after a rebuild drops and recreates it.
SEARCH_INDEX_DDL = """
CREATE VIRTUAL TABLE IF NOT EXISTS memory_search USING fts5(
    title,
    body,
    source_refs,
    category UNINDEXED,
    memory_id UNINDEXED,
    tokenize = 'unicode61 remove_diacritics 2'
)
"""


def _ensure_search_index(conn: Connection) -> None:
    """Create the memory search index, or record that this build cannot.

    Never raises. A machine without FTS5 must still get a working flanner:
    plans do not use it at all, and memory degrades to a slower search
    rather than refusing to start.
    """
    global SEARCH_INDEX_AVAILABLE
    try:
        conn.exec_driver_sql(SEARCH_INDEX_DDL)
        SEARCH_INDEX_AVAILABLE = True
    except DBAPIError as e:
        SEARCH_INDEX_AVAILABLE = False
        logger.warning("This Python's SQLite has no FTS5, so memory search will be slower: %s", e)


def _add_column(conn: Connection, table: str, column: str, kind: str) -> None:
    """Add a column unless the table already has it.

    Not defensiveness. `_apply_schema` runs `create_all` before the ladder,
    and `create_all` builds any *missing* table in its present-day shape --
    columns a later migration was written to add included. So a machine
    upgrading from a version that predates the table gets it complete, and
    the migration that adds a column to it then fails on a duplicate.

    `artifacts` is where that first bit: it arrives whole on any upgrade
    from v1, and `_migration_4` alters it. Every migration that adds a
    column should go through here, because whether its table might have
    been created by `create_all` is not a question worth re-deciding.
    """
    held = {row[1] for row in conn.exec_driver_sql(f"PRAGMA table_info({table})")}
    if column not in held:
        conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")


def _migration_4(conn: Connection) -> None:
    """Point an artifact at a memory.

    Memory artifacts need somewhere to say which memory they carry. The
    alternative was reusing `plan_file_id`, which would have made every
    query for one able to return the other, and correctness would then rest
    on every caller remembering to filter by type.

    Existing rows keep NULL, which is correct: every artifact written
    before this is about a plan.
    """
    _add_column(conn, "artifacts", "memory_id", "VARCHAR")
    conn.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_artifacts_memory_id ON artifacts (memory_id)"
    )


MIGRATIONS: dict[int, Callable[[Connection], None]] = {
    1: _migration_1,
    2: _migration_2,
    3: _migration_3,
    4: _migration_4,
}


def _apply_schema(engine: Engine) -> None:
    """Bring the database to SCHEMA_VERSION.

    Fresh file: create_all produces the current schema and we stamp it.
    Existing file: create_all adds any brand-new tables, then pending
    migrations run in order for in-place changes. A newer-than-supported
    database is refused rather than silently downgraded.
    """
    fresh = not inspect(engine).has_table("projects")

    # create_all never alters existing tables; it only creates missing ones.
    Base.metadata.create_all(engine)

    with engine.begin() as conn:
        # Before the fresh-database early return below, not after it, or a
        # new install would be the one machine without a search index.
        _ensure_search_index(conn)

        found = int(conn.exec_driver_sql("PRAGMA user_version").scalar() or 0)
        if found > SCHEMA_VERSION:
            raise DatabaseError(
                f"Database schema v{found} is newer than this flanner supports "
                f"(v{SCHEMA_VERSION}). Upgrade flanner."
            )
        if fresh:
            conn.exec_driver_sql(f"PRAGMA user_version = {SCHEMA_VERSION}")
            return
        for target in range(found + 1, SCHEMA_VERSION + 1):
            migrate = MIGRATIONS.get(target)
            if migrate is None:
                raise DatabaseError(f"No migration registered for schema v{target}")
            migrate(conn)
            conn.exec_driver_sql(f"PRAGMA user_version = {target}")


def init_database(db_path: str | None = None) -> None:
    """
    Initialize the database and create tables.

    Args:
        db_path: Path to SQLite database file. If None, uses FLANNER_DB_PATH,
            then FLANNER_HOME/data.db, then ~/.flanner/data.db
    """
    global _engine, _SessionLocal

    if db_path is None:
        db_path = os.environ.get("FLANNER_DB_PATH")
    if db_path is None:
        mcp_dir = Path(os.environ.get("FLANNER_HOME", Path.home() / ".flanner"))
        mcp_dir.mkdir(parents=True, exist_ok=True)
        db_path = str(mcp_dir / "data.db")
    else:
        # Ensure parent directory exists
        db_dir = Path(db_path).parent
        db_dir.mkdir(parents=True, exist_ok=True)

    # Create engine
    # NullPool: server tools create short-lived sessions without closing them;
    # a bounded QueuePool exhausts after ~15 rapid calls. Local SQLite connections
    # are cheap, so open/close per session is the safer default.
    # NullPool because callers do not close sessions explicitly, and a
    # bounded QueuePool would exhaust after ~15 rapid calls. Local SQLite
    # connections are cheap, so open/close per session is the safer default.
    #
    # This was carried as debt on the belief that sessions leak. They do
    # not: a session is dropped when the call that made it returns, and
    # refcounting closes it there. Measured, not assumed — 0 of 30 survive
    # a create-use-drop cycle, and `test_sessions_do_not_accumulate_across_
    # tool_calls` pins it against the real tool path. The one place a
    # session outlives a call is `peer.serve_request`, which already holds
    # it in a `with` block; `Session` is its own context manager.
    #
    # So the pool is a decision rather than a deferral. What would reopen
    # it is an interpreter without refcounting — none is supported, the
    # classifiers are CPython 3.10 to 3.13 — or a session stored somewhere
    # that outlives a call, which is what the test above would catch.
    _engine = create_engine(f"sqlite:///{db_path}", echo=False, poolclass=NullPool)

    # Create tables (fresh) or run pending migrations (existing), then stamp.
    _apply_schema(_engine)

    # Create session factory
    _SessionLocal = sessionmaker(bind=_engine, autocommit=False, autoflush=False)

    logger.info("Database initialized at: %s", db_path)


def store_open() -> bool:
    """Whether init_database has run, so a caller can decline rather than raise.

    The action recorder runs after every command, including ones that exit
    before a store exists; it asks this rather than catching the error.
    """
    return _SessionLocal is not None


def get_session() -> Session:
    """
    Get a database session.

    Returns:
        SQLAlchemy session

    Raises:
        RuntimeError: If database hasn't been initialized
    """
    if _SessionLocal is None:
        raise DatabaseError("Database not initialized. Call init_database() first.")

    return _SessionLocal()


def get_db_path() -> str | None:
    """Get the current database path"""
    if _engine is None:
        return None
    return str(_engine.url).replace("sqlite:///", "")


# CRUD Operations


def _commit(session: Session) -> None:
    """Commit, rolling back on failure so the session stays usable."""
    try:
        session.commit()
    except SQLAlchemyError as e:
        session.rollback()
        raise DatabaseError(f"Database write failed: {e}") from e


def create_project(
    session: Session,
    name: str,
    description: str = "",
    project_root: str | None = None,
    plan_directory: str = ".plans",
    auto_gitignore: bool = True,
) -> ProjectModel:
    """
    Create a new project.

    Args:
        session: Database session
        name: Project name (must be unique)
        description: Project description
        project_root: Absolute path to project root
        plan_directory: Relative path for plan files
        auto_gitignore: Whether to auto-update .gitignore

    Returns:
        Created project model

    Raises:
        ValueError: If project with same name already exists
    """
    # Check if project exists
    existing = session.query(ProjectModel).filter_by(name=name).first()
    if existing:
        raise DuplicateError(f"Project '{name}' already exists")

    project = ProjectModel(
        name=name,
        description=description,
        project_root=project_root,
        plan_directory=plan_directory,
        auto_gitignore=auto_gitignore,
    )
    session.add(project)
    _commit(session)
    session.refresh(project)

    return project


def get_project(session: Session, project_id: uuid.UUID) -> ProjectModel | None:
    """Get project by ID"""
    return session.query(ProjectModel).filter_by(id=project_id).first()


def get_project_by_name(session: Session, name: str) -> ProjectModel | None:
    """Get project by name"""
    return session.query(ProjectModel).filter_by(name=name).first()


def list_projects(
    session: Session, limit: int | None = None, offset: int = 0, sort: str = "updated"
) -> list[ProjectModel]:
    """List projects; optionally a page of them.

    ``sort`` orders before paging, so the control on the projects page sorts
    the whole collection rather than reshuffling whichever fifty rows the
    current page happens to hold.
    """
    order = (
        (ProjectModel.name.asc(), ProjectModel.id)
        if sort == "name"
        else (ProjectModel.updated_at.desc(), ProjectModel.created_at.desc(), ProjectModel.id)
    )
    query = session.query(ProjectModel).order_by(*order)
    if offset:
        query = query.offset(offset)
    if limit is not None:
        query = query.limit(limit)
    return query.all()


def count_projects(session: Session) -> int:
    """Total number of projects (SQL COUNT, no object loading)."""
    return session.query(func.count(ProjectModel.id)).scalar() or 0


def _without(query: Any, exclude: Collection[str]) -> Any:
    """Drop named plans from a plan-file query, if any were named.

    Ids arrive as strings because that is how they are stored on an
    artifact envelope; the column is a UUID, so the comparison is made
    against text to avoid a per-row cast.
    """
    if not exclude:
        return query
    return query.filter(~cast(PlanFileModel.id, String).in_([str(x) for x in exclude]))


def count_plan_files(
    session: Session,
    project_id: uuid.UUID | None = None,
    exclude: Collection[str] = (),
) -> int:
    """Total plan files, overall or for one project (SQL COUNT).

    ``exclude`` drops plans the caller is hiding. Taken as a parameter
    rather than worked out here, because deciding what is hidden means
    reading review artifacts and this layer sits below the module that
    defines them. Every listing and every count takes the same set, so a
    sidebar badge cannot disagree with the list it is counting.
    """
    query = session.query(func.count(PlanFileModel.id))
    if project_id is not None:
        query = query.filter(PlanFileModel.project_id == project_id)
    query = _without(query, exclude)
    return query.scalar() or 0


def plan_file_counts_by_project(
    session: Session, exclude: Collection[str] = ()
) -> dict[uuid.UUID, int]:
    """Plan-file count per project in one grouped query."""
    rows = (
        _without(session.query(PlanFileModel.project_id, func.count(PlanFileModel.id)), exclude)
        .group_by(PlanFileModel.project_id)
        .all()
    )
    return {project_id: count for project_id, count in rows}


def recent_plan_files(
    session: Session, limit: int = 10, exclude: Collection[str] = (), offset: int = 0
) -> list[PlanFileModel]:
    """Most recently updated plan files across all projects (SQL ORDER BY ... LIMIT).

    ``offset`` pages through them: the plans page asks for one page at a
    time rather than the two hundred it used to load and mostly not show.
    """
    rows: list[PlanFileModel] = (
        _without(session.query(PlanFileModel), exclude)
        .order_by(PlanFileModel.updated_at.desc(), PlanFileModel.id)
        .offset(offset)
        .limit(limit)
        .all()
    )
    return rows


def count_plan_files_recent(session: Session, days: int = 7, exclude: Collection[str] = ()) -> int:
    """Plan files updated within the last `days` (SQL COUNT).

    The cutoff is computed with the same naive-UTC convention as the columns
    (see _utcnow), so callers do not deal with timezones.
    """
    cutoff = _utcnow() - timedelta(days=days)
    return (
        _without(session.query(func.count(PlanFileModel.id)), exclude)
        .filter(PlanFileModel.updated_at >= cutoff)
        .scalar()
        or 0
    )


def update_project(
    session: Session,
    project_id: uuid.UUID,
    project_root: str | None = None,
    plan_directory: str | None = None,
    auto_gitignore: bool | None = None,
    description: str | None = None,
) -> ProjectModel | None:
    """Update project configuration"""
    project = session.query(ProjectModel).filter_by(id=project_id).first()
    if not project:
        return None

    if project_root is not None:
        project.project_root = project_root
    if plan_directory is not None:
        project.plan_directory = plan_directory
    if auto_gitignore is not None:
        project.auto_gitignore = auto_gitignore
    if description is not None:
        project.description = description

    project.updated_at = _utcnow()
    _commit(session)
    session.refresh(project)

    return project


def create_plan_file(
    session: Session,
    project_id: uuid.UUID,
    name: str,
    description: str = "",
    auto_version: bool = True,
    plan_file_id: uuid.UUID | None = None,
) -> PlanFileModel:
    """
    Create a new plan file.

    Args:
        session: Database session
        project_id: ID of the project
        name: Plan file name (without .md extension)
        description: Plan file description
        auto_version: Whether to auto-increment version on update
        plan_file_id: Explicit id, so a plan materialized from a peer keeps
            the identity it already has on the device that authored it

    Returns:
        Created plan file model

    Raises:
        ValueError: If project doesn't exist or plan file already exists
    """
    # Check if project exists
    project = session.query(ProjectModel).filter_by(id=project_id).first()
    if not project:
        raise NotFoundError(f"Project with ID {project_id} not found")

    # Check if plan file already exists
    existing = session.query(PlanFileModel).filter_by(project_id=project_id, name=name).first()
    if existing:
        raise DuplicateError(f"Plan file '{name}' already exists in project '{project.name}'")

    plan_file = PlanFileModel(
        project_id=project_id,
        name=name,
        description=description,
        current_version=1,
        auto_version=auto_version,
        **({"id": plan_file_id} if plan_file_id is not None else {}),
    )
    session.add(plan_file)
    _commit(session)
    session.refresh(plan_file)

    return plan_file


def get_plan_file(session: Session, plan_file_id: uuid.UUID) -> PlanFileModel | None:
    """Get plan file by ID"""
    return session.query(PlanFileModel).filter_by(id=plan_file_id).first()


def list_plan_files(
    session: Session,
    project_id: uuid.UUID,
    limit: int | None = None,
    offset: int = 0,
    exclude: Collection[str] = (),
) -> list[PlanFileModel]:
    """List plan files for a project, newest first; optionally a page of them."""
    query = _without(
        session.query(PlanFileModel).filter_by(project_id=project_id), exclude
    ).order_by(PlanFileModel.created_at.desc(), PlanFileModel.id)
    if offset:
        query = query.offset(offset)
    if limit is not None:
        query = query.limit(limit)
    rows: list[PlanFileModel] = query.all()
    return rows


def create_version(
    session: Session,
    plan_file_id: uuid.UUID,
    version: int,
    file_path: str,
    content_hash: str,
    created_by: str = "user",
    notes: str = "",
    artifact_id: str | None = None,
) -> VersionModel:
    """
    Create a new version of a plan file.

    Args:
        session: Database session
        plan_file_id: ID of the plan file
        version: Version number
        file_path: Absolute path to the markdown file
        content_hash: SHA256 hash of content
        created_by: Who created this version
        notes: Version notes
        artifact_id: Id of the signed artifact for this version

    Returns:
        Created version model
    """
    version_model = VersionModel(
        plan_file_id=plan_file_id,
        version=version,
        file_path=file_path,
        content_hash=content_hash,
        created_by=created_by,
        notes=notes,
        artifact_id=artifact_id,
    )
    session.add(version_model)
    _commit(session)
    session.refresh(version_model)

    return version_model


def get_version(
    session: Session, plan_file_id: uuid.UUID, version: int | None = None
) -> VersionModel | None:
    """
    Get a specific version or the latest version.

    Args:
        session: Database session
        plan_file_id: ID of the plan file
        version: Version number (if None, returns latest)

    Returns:
        Version model or None
    """
    query = session.query(VersionModel).filter_by(plan_file_id=plan_file_id)

    if version is not None:
        return query.filter_by(version=version).first()
    else:
        return query.order_by(VersionModel.version.desc()).first()


def list_versions(session: Session, plan_file_id: uuid.UUID) -> list[VersionModel]:
    """List all versions of a plan file"""
    return (
        session.query(VersionModel)
        .filter_by(plan_file_id=plan_file_id)
        .order_by(VersionModel.version.desc())
        .all()
    )


class SignedEnvelope(Protocol):
    """The shape `save_artifact` needs, without naming the class that has it.

    `artifacts.Artifact` satisfies this, but this module may not import that
    one: `database` is the bottom layer and the boundary test holds it to
    `exceptions` alone. That constraint is why this function used to take
    fourteen loose keyword arguments — every caller unpacked an Artifact
    field by field because the signature could not say "an Artifact".

    A structural type says it anyway. mypy checks the caller passes something
    with these fields; nothing is imported, so the layering is unchanged.
    """

    # Properties, not plain attributes: `Artifact` is a frozen dataclass, so
    # its fields are read-only, and a Protocol declaring them writable does
    # not match it. Read-only is also the truth about what this function
    # does with them — mypy refusing the frozen type was the check working.
    @property
    def artifact_id(self) -> str: ...
    @property
    def artifact_type(self) -> str: ...
    @property
    def workspace_id(self) -> str: ...
    @property
    def content_hash(self) -> str: ...
    @property
    def actor_device_id(self) -> str: ...
    @property
    def created_at(self) -> str: ...
    @property
    def signature(self) -> str: ...
    @property
    def protocol_version(self) -> int: ...
    @property
    def organization_id(self) -> str | None: ...
    @property
    def plan_file_id(self) -> str | None: ...
    @property
    def actor_user_id(self) -> str | None: ...
    @property
    def parents(self) -> tuple[str, ...]: ...


def save_envelope(
    session: Session,
    envelope: SignedEnvelope,
    *,
    plan_file_id: str | None = None,
    memory_id: str | None = None,
    payload: str | None = None,
) -> ArtifactModel:
    """Store a signed envelope, or return the one already held.

    The four-argument form of `save_artifact`. Callers hold an Artifact; this
    saves them restating its twelve fields at every call site, which is where
    a field gets forgotten.

    `plan_file_id` is separate because a caller sometimes knows the plan a
    payload belongs to when the envelope itself does not carry it.
    """
    return save_artifact(
        session,
        memory_id=memory_id or getattr(envelope, "memory_id", None),
        artifact_id=envelope.artifact_id,
        artifact_type=envelope.artifact_type,
        workspace_id=envelope.workspace_id,
        content_hash=envelope.content_hash,
        actor_device_id=envelope.actor_device_id,
        created_at=envelope.created_at,
        signature=envelope.signature,
        protocol_version=envelope.protocol_version,
        organization_id=envelope.organization_id,
        plan_file_id=plan_file_id if plan_file_id is not None else envelope.plan_file_id,
        parents=list(envelope.parents),
        actor_user_id=envelope.actor_user_id,
        payload=payload,
    )


def save_artifact(
    session: Session,
    *,
    artifact_id: str,
    artifact_type: str,
    workspace_id: str,
    content_hash: str,
    actor_device_id: str,
    created_at: str,
    signature: str,
    protocol_version: int = 1,
    organization_id: str | None = None,
    plan_file_id: str | None = None,
    memory_id: str | None = None,
    parents: list[str] | tuple[str, ...] = (),
    actor_user_id: str | None = None,
    payload: str | None = None,
) -> ArtifactModel:
    """Store an artifact, or return the one already held.

    Artifacts are immutable and content-addressed, so re-receiving one is
    normal during sync and must be a no-op rather than a conflict. The
    caller verifies the envelope before calling; storage does not re-judge it.
    """
    existing = session.get(ArtifactModel, artifact_id)
    if existing is not None:
        return existing

    artifact = ArtifactModel(
        artifact_id=artifact_id,
        artifact_type=artifact_type,
        protocol_version=protocol_version,
        organization_id=organization_id,
        workspace_id=workspace_id,
        plan_file_id=plan_file_id,
        memory_id=memory_id,
        parents=json.dumps(list(parents)),
        created_at=created_at,
        actor_user_id=actor_user_id,
        actor_device_id=actor_device_id,
        content_hash=content_hash,
        signature=signature,
        payload=payload,
    )
    session.add(artifact)
    _commit(session)
    return artifact


def get_artifact(session: Session, artifact_id: str) -> ArtifactModel | None:
    return session.get(ArtifactModel, artifact_id)


def list_artifacts(
    session: Session,
    plan_file_id: str | None = None,
    artifact_type: str | None = None,
) -> list[ArtifactModel]:
    """Artifacts, optionally narrowed to one plan or one type."""
    query = session.query(ArtifactModel)
    if plan_file_id is not None:
        query = query.filter_by(plan_file_id=plan_file_id)
    if artifact_type is not None:
        query = query.filter_by(artifact_type=artifact_type)
    return query.all()


def recent_arrivals(
    session: Session,
    *,
    exclude_device_id: str | None = None,
    limit: int = 20,
) -> list[ArtifactModel]:
    """What reached this device most recently, newest first.

    Ordered by ``received_at`` rather than the envelope's ``created_at``,
    which is the author's clock and is descriptive only. "What is new to me"
    is a local question and deserves the local answer.

    ``exclude_device_id`` drops this device's own work, which is stored
    through the same path and would otherwise crowd out everything a
    teammate sent.
    """
    query = session.query(ArtifactModel).filter(ArtifactModel.received_at.isnot(None))
    if exclude_device_id:
        query = query.filter(ArtifactModel.actor_device_id != exclude_device_id)
    return query.order_by(ArtifactModel.received_at.desc()).limit(limit).all()


def last_received_by_device(session: Session) -> dict[str, datetime]:
    """When something last arrived that each device had signed.

    Note what this does and does not say. It answers "when did I last get
    work of theirs", not "when did I last hear from them" — an artifact can
    reach us relayed through a third machine long after its author went
    offline. Presenting it as a liveness signal would be a lie the data
    cannot support.
    """
    rows = (
        session.query(
            ArtifactModel.actor_device_id,
            func.max(ArtifactModel.received_at),
        )
        .filter(ArtifactModel.received_at.isnot(None))
        .group_by(ArtifactModel.actor_device_id)
        .all()
    )
    return {device_id: stamp for device_id, stamp in rows if stamp is not None}


def artifact_parents(session: Session, plan_file_id: str) -> dict[str, tuple[str, ...]]:
    """The parent graph for one plan, in the form the lineage helpers take."""
    graph: dict[str, tuple[str, ...]] = {}
    for artifact in list_artifacts(session, plan_file_id=plan_file_id):
        try:
            parents = tuple(json.loads(artifact.parents))
        except (ValueError, TypeError):
            parents = ()
        graph[artifact.artifact_id] = parents
    return graph


def delete_project(session: Session, project_id: uuid.UUID) -> bool:
    """
    Delete a project and all associated plan files and versions.

    Args:
        session: Database session
        project_id: ID of the project to delete

    Returns:
        True if deleted, False if project not found
    """
    project = session.query(ProjectModel).filter_by(id=project_id).first()
    if not project:
        return False

    session.delete(project)
    _commit(session)

    return True


def get_project_by_root(session: Session, project_root: str) -> ProjectModel | None:
    """
    Get project by its project_root path.

    Args:
        session: Database session
        project_root: Absolute path to project root

    Returns:
        ProjectModel or None if not found
    """
    normalized_root = os.path.normpath(project_root)

    # Query all projects and compare normalized paths
    projects = session.query(ProjectModel).all()
    for project in projects:
        if project.project_root:
            if os.path.normpath(project.project_root) == normalized_root:
                return project

    return None


# JIRA Configuration Operations


def create_jira_config(
    session: Session, project_id: uuid.UUID, jira_url: str, jira_project_key: str | None = None
) -> JiraConfigModel:
    """
    Create or update JIRA configuration for a project.

    Args:
        session: Database session
        project_id: ID of the project
        jira_url: JIRA base URL (e.g., https://company.atlassian.net)
        jira_project_key: Default JIRA project key (e.g., PROJ)

    Returns:
        Created or updated JIRA config model

    Raises:
        ValueError: If project doesn't exist
    """
    # Check if project exists
    project = session.query(ProjectModel).filter_by(id=project_id).first()
    if not project:
        raise NotFoundError(f"Project with ID {project_id} not found")

    # Check if config already exists
    existing = session.query(JiraConfigModel).filter_by(project_id=project_id).first()
    if existing:
        # Update existing config
        existing.jira_url = jira_url
        if jira_project_key is not None:
            existing.jira_project_key = jira_project_key
        existing.updated_at = _utcnow()
        _commit(session)
        session.refresh(existing)
        return existing
    else:
        # Create new config
        jira_config = JiraConfigModel(
            project_id=project_id, jira_url=jira_url, jira_project_key=jira_project_key
        )
        session.add(jira_config)
        _commit(session)
        session.refresh(jira_config)
        return jira_config


def get_jira_config(session: Session, project_id: uuid.UUID) -> JiraConfigModel | None:
    """Get JIRA configuration for a project"""
    return session.query(JiraConfigModel).filter_by(project_id=project_id).first()


def delete_jira_config(session: Session, project_id: uuid.UUID) -> bool:
    """
    Delete JIRA configuration for a project.

    Args:
        session: Database session
        project_id: ID of the project

    Returns:
        True if deleted, False if config not found
    """
    config = session.query(JiraConfigModel).filter_by(project_id=project_id).first()
    if not config:
        return False

    session.delete(config)
    _commit(session)
    return True


# JIRA Link Operations


def create_jira_link(
    session: Session,
    plan_file_id: uuid.UUID,
    jira_issue_key: str,
    jira_issue_type: str | None = None,
    notes: str | None = None,
    created_by: str = "user",
) -> JiraLinkModel:
    """
    Create a JIRA link for a plan file.

    Args:
        session: Database session
        plan_file_id: ID of the plan file
        jira_issue_key: JIRA issue key (e.g., PROJ-123)
        jira_issue_type: Type of JIRA issue (Epic, Story, Task, etc.)
        notes: Notes about the link
        created_by: Who created the link

    Returns:
        Created JIRA link model

    Raises:
        ValueError: If plan file doesn't exist or link already exists
    """
    # Check if plan file exists
    plan_file = session.query(PlanFileModel).filter_by(id=plan_file_id).first()
    if not plan_file:
        raise NotFoundError(f"Plan file with ID {plan_file_id} not found")

    # Check if link already exists
    existing = (
        session.query(JiraLinkModel)
        .filter_by(plan_file_id=plan_file_id, jira_issue_key=jira_issue_key)
        .first()
    )
    if existing:
        raise DuplicateError(f"Plan file is already linked to {jira_issue_key}")

    jira_link = JiraLinkModel(
        plan_file_id=plan_file_id,
        jira_issue_key=jira_issue_key,
        jira_issue_type=jira_issue_type,
        notes=notes,
        created_by=created_by,
    )
    session.add(jira_link)
    _commit(session)
    session.refresh(jira_link)

    return jira_link


def get_jira_links(session: Session, plan_file_id: uuid.UUID) -> list[JiraLinkModel]:
    """Get all JIRA links for a plan file"""
    return (
        session.query(JiraLinkModel)
        .filter_by(plan_file_id=plan_file_id)
        .order_by(JiraLinkModel.created_at.desc())
        .all()
    )


def get_jira_link(session: Session, link_id: uuid.UUID) -> JiraLinkModel | None:
    """Get a specific JIRA link by ID"""
    return session.query(JiraLinkModel).filter_by(id=link_id).first()


def update_jira_link(
    session: Session,
    link_id: uuid.UUID,
    jira_issue_type: str | None = None,
    notes: str | None = None,
) -> JiraLinkModel | None:
    """
    Update a JIRA link.

    Args:
        session: Database session
        link_id: ID of the link to update
        jira_issue_type: New issue type
        notes: New notes

    Returns:
        Updated JIRA link model or None if not found
    """
    link = session.query(JiraLinkModel).filter_by(id=link_id).first()
    if not link:
        return None

    if jira_issue_type is not None:
        link.jira_issue_type = jira_issue_type
    if notes is not None:
        link.notes = notes

    _commit(session)
    session.refresh(link)
    return link


def delete_jira_link(session: Session, link_id: uuid.UUID) -> bool:
    """
    Delete a JIRA link.

    Args:
        session: Database session
        link_id: ID of the link to delete

    Returns:
        True if deleted, False if link not found
    """
    link = session.query(JiraLinkModel).filter_by(id=link_id).first()
    if not link:
        return False

    session.delete(link)
    _commit(session)
    return True


def delete_jira_link_by_key(
    session: Session, plan_file_id: uuid.UUID, jira_issue_key: str
) -> bool:
    """
    Delete a JIRA link by plan file ID and issue key.

    Args:
        session: Database session
        plan_file_id: ID of the plan file
        jira_issue_key: JIRA issue key to unlink

    Returns:
        True if deleted, False if link not found
    """
    link = (
        session.query(JiraLinkModel)
        .filter_by(plan_file_id=plan_file_id, jira_issue_key=jira_issue_key)
        .first()
    )
    if not link:
        return False

    session.delete(link)
    _commit(session)
    return True


def delete_all_jira_links(session: Session, plan_file_id: uuid.UUID) -> int:
    """
    Delete all JIRA links for a plan file.

    Args:
        session: Database session
        plan_file_id: ID of the plan file

    Returns:
        Number of links deleted
    """
    links = session.query(JiraLinkModel).filter_by(plan_file_id=plan_file_id).all()
    count = len(links)

    for link in links:
        session.delete(link)

    _commit(session)
    return count


def list_all_jira_links(session: Session, project_id: uuid.UUID) -> list[dict[str, Any]]:
    """
    List all JIRA links for all plan files in a project.

    Args:
        session: Database session
        project_id: ID of the project

    Returns:
        List of dicts with plan file and JIRA link information
    """
    # Get all plan files for the project
    plan_files = session.query(PlanFileModel).filter_by(project_id=project_id).all()

    results: list[dict[str, Any]] = []
    for plan_file in plan_files:
        links = session.query(JiraLinkModel).filter_by(plan_file_id=plan_file.id).all()
        for link in links:
            results.append(
                {
                    "plan_file_id": plan_file.id,
                    "plan_file_name": plan_file.name,
                    "jira_link_id": link.id,
                    "jira_issue_key": link.jira_issue_key,
                    "jira_issue_type": link.jira_issue_type,
                    "notes": link.notes,
                    "created_at": link.created_at,
                    "created_by": link.created_by,
                }
            )

    return results


# Linear Configuration Operations


def create_linear_config(
    session: Session, project_id: uuid.UUID, workspace: str
) -> LinearConfigModel:
    """
    Create or update Linear configuration (workspace slug) for a project.

    Raises:
        NotFoundError: If the project doesn't exist.
    """
    project = session.query(ProjectModel).filter_by(id=project_id).first()
    if not project:
        raise NotFoundError(f"Project with ID {project_id} not found")

    existing = session.query(LinearConfigModel).filter_by(project_id=project_id).first()
    if existing:
        existing.workspace = workspace
        existing.updated_at = _utcnow()
        _commit(session)
        session.refresh(existing)
        return existing

    config = LinearConfigModel(project_id=project_id, workspace=workspace)
    session.add(config)
    _commit(session)
    session.refresh(config)
    return config


def get_linear_config(session: Session, project_id: uuid.UUID) -> LinearConfigModel | None:
    """Get Linear configuration for a project."""
    return session.query(LinearConfigModel).filter_by(project_id=project_id).first()


# Linear Link Operations


def create_linear_link(
    session: Session,
    plan_file_id: uuid.UUID,
    linear_issue_id: str,
    issue_title: str | None = None,
    issue_state: str | None = None,
    notes: str | None = None,
    created_by: str = "user",
) -> LinearLinkModel:
    """
    Create a Linear link for a plan file.

    Raises:
        NotFoundError: If the plan file doesn't exist.
        DuplicateError: If the plan file is already linked to this issue.
    """
    plan_file = session.query(PlanFileModel).filter_by(id=plan_file_id).first()
    if not plan_file:
        raise NotFoundError(f"Plan file with ID {plan_file_id} not found")

    existing = (
        session.query(LinearLinkModel)
        .filter_by(plan_file_id=plan_file_id, linear_issue_id=linear_issue_id)
        .first()
    )
    if existing:
        raise DuplicateError(f"Plan file is already linked to {linear_issue_id}")

    link = LinearLinkModel(
        plan_file_id=plan_file_id,
        linear_issue_id=linear_issue_id,
        issue_title=issue_title,
        issue_state=issue_state,
        notes=notes,
        created_by=created_by,
    )
    session.add(link)
    _commit(session)
    session.refresh(link)
    return link


def get_linear_links(session: Session, plan_file_id: uuid.UUID) -> list[LinearLinkModel]:
    """Get all Linear links for a plan file, newest first."""
    return (
        session.query(LinearLinkModel)
        .filter_by(plan_file_id=plan_file_id)
        .order_by(LinearLinkModel.created_at.desc())
        .all()
    )


def update_linear_link_cache(
    session: Session, link_id: uuid.UUID, issue_title: str | None, issue_state: str | None
) -> LinearLinkModel | None:
    """Refresh the cached title/state on a link (used by the API refresh path)."""
    link = session.query(LinearLinkModel).filter_by(id=link_id).first()
    if not link:
        return None
    link.issue_title = issue_title
    link.issue_state = issue_state
    _commit(session)
    session.refresh(link)
    return link


def delete_linear_link_by_id(
    session: Session, plan_file_id: uuid.UUID, linear_issue_id: str
) -> bool:
    """Delete a single Linear link by plan file + issue identifier."""
    link = (
        session.query(LinearLinkModel)
        .filter_by(plan_file_id=plan_file_id, linear_issue_id=linear_issue_id)
        .first()
    )
    if not link:
        return False
    session.delete(link)
    _commit(session)
    return True


def delete_all_linear_links(session: Session, plan_file_id: uuid.UUID) -> int:
    """Delete all Linear links for a plan file. Returns the count removed."""
    links = session.query(LinearLinkModel).filter_by(plan_file_id=plan_file_id).all()
    count = len(links)
    for link in links:
        session.delete(link)
    _commit(session)
    return count


def list_all_linear_links(session: Session, project_id: uuid.UUID) -> list[dict[str, Any]]:
    """List all Linear links for every plan file in a project."""
    plan_files = session.query(PlanFileModel).filter_by(project_id=project_id).all()

    results: list[dict[str, Any]] = []
    for plan_file in plan_files:
        links = session.query(LinearLinkModel).filter_by(plan_file_id=plan_file.id).all()
        for link in links:
            results.append(
                {
                    "plan_file_id": plan_file.id,
                    "plan_file_name": plan_file.name,
                    "linear_link_id": link.id,
                    "linear_issue_id": link.linear_issue_id,
                    "issue_title": link.issue_title,
                    "issue_state": link.issue_state,
                    "notes": link.notes,
                    "created_at": link.created_at,
                    "created_by": link.created_by,
                }
            )

    return results


# --- memory ------------------------------------------------------------------


def create_memory(
    session: Session,
    *,
    memory_id: uuid.UUID | None = None,
    scope: str,
    project_id: uuid.UUID,
    title: str,
    body: str,
    category: str,
    content_hash: str,
    file_path: str,
    created_by: str,
    confidence: str = "confirmed",
    sensitivity: str = "normal",
    source_type: str = "explicit",
    source_refs: str = "[]",
    status: str = "active",
    supersedes_id: uuid.UUID | None = None,
    expires_at: datetime | None = None,
) -> MemoryModel:
    """Insert one memory row. The file is written by `memory_ops`, first.

    Validates the closed vocabularies here rather than at each caller, so a
    typo in a scope or a category cannot reach the database and quietly
    make a memory invisible to every filtered query.
    """
    for value, allowed, field in (
        (scope, MEMORY_SCOPES, "scope"),
        (category, MEMORY_CATEGORIES, "category"),
        (status, MEMORY_STATUSES, "status"),
        (confidence, MEMORY_CONFIDENCES, "confidence"),
        (sensitivity, MEMORY_SENSITIVITIES, "sensitivity"),
        (source_type, MEMORY_SOURCE_TYPES, "source_type"),
    ):
        if value not in allowed:
            raise ValidationError(f"{field} must be one of {', '.join(allowed)}, not {value!r}")

    memory = MemoryModel(
        id=memory_id or uuid.uuid4(),
        scope=scope,
        project_id=project_id,
        title=title,
        body=body,
        category=category,
        status=status,
        confidence=confidence,
        sensitivity=sensitivity,
        source_type=source_type,
        source_refs=source_refs,
        supersedes_id=supersedes_id,
        content_hash=content_hash,
        file_path=file_path,
        created_by=created_by,
        expires_at=expires_at,
    )
    session.add(memory)
    _commit(session)
    session.refresh(memory)
    return memory


def get_memory(session: Session, memory_id: uuid.UUID) -> MemoryModel | None:
    """One memory by id, whatever its status."""
    return session.query(MemoryModel).filter_by(id=memory_id).first()


def find_memory_by_hash(
    session: Session, *, scope: str, project_id: uuid.UUID, content_hash: str
) -> MemoryModel | None:
    """The memory this text already is, if it is one.

    Matches the unique constraint exactly, so a caller that checks this
    first and a caller that does not both end up in the same place.
    """
    return (
        session.query(MemoryModel)
        .filter_by(scope=scope, project_id=project_id, content_hash=content_hash)
        .first()
    )


def list_memories(
    session: Session,
    *,
    scope: str | None = None,
    project_id: uuid.UUID | None = None,
    category: str | None = None,
    status: str | None = "active",
    limit: int | None = None,
    offset: int = 0,
    order: str = "newest",
) -> list[MemoryModel]:
    """Memories matching every filter given, newest first; optionally a page.

    `status` defaults to active rather than to everything, because the
    common question is "what do I believe now" and the uncommon one should
    be the one that has to ask.
    """
    query = session.query(MemoryModel)
    if scope is not None:
        query = query.filter_by(scope=scope)
    if project_id is not None:
        query = query.filter_by(project_id=project_id)
    if category is not None:
        query = query.filter_by(category=category)
    if status is not None:
        query = query.filter_by(status=status)
    if order == "oldest":
        query = query.order_by(MemoryModel.created_at.asc(), MemoryModel.id)
    elif order == "title":
        query = query.order_by(MemoryModel.title.asc(), MemoryModel.id)
    else:
        query = query.order_by(MemoryModel.created_at.desc(), MemoryModel.id)
    if offset:
        query = query.offset(offset)
    if limit is not None:
        query = query.limit(limit)
    return list(query.all())


def record_memory_event(
    session: Session,
    *,
    memory_id: uuid.UUID,
    action: str,
    actor: str,
    detail: str = "{}",
) -> MemoryEventModel:
    """Append one event. Never updates, never deletes."""
    event = MemoryEventModel(memory_id=memory_id, action=action, actor=actor, detail=detail)
    session.add(event)
    _commit(session)
    return event


def list_memory_events(session: Session, memory_id: uuid.UUID) -> list[MemoryEventModel]:
    """Everything that happened to one memory, oldest first."""
    return list(
        session.query(MemoryEventModel)
        .filter_by(memory_id=memory_id)
        .order_by(MemoryEventModel.at.asc())
        .all()
    )


def count_memories(
    session: Session,
    *,
    status: str | None = "active",
    category: str | None = None,
    scope: str | None = None,
) -> int:
    """How many memories match, for the nav badge and for paging a filtered list."""
    query = session.query(MemoryModel)
    if status is not None:
        query = query.filter_by(status=status)
    if category is not None:
        query = query.filter_by(category=category)
    if scope is not None:
        query = query.filter_by(scope=scope)
    return int(query.count())


def delete_memory(session: Session, memory_id: uuid.UUID) -> bool:
    """Remove a memory and its events entirely.

    The destructive half of forgetting. Kept separate from status changes
    so that nothing can purge by accident: a caller has to name this.
    """
    memory = get_memory(session, memory_id)
    if memory is None:
        return False
    session.delete(memory)
    _commit(session)
    return True


def add_attachment(
    session: Session,
    *,
    memory_id: uuid.UUID,
    content_hash: str,
    mime_type: str,
    original_name: str,
    size_bytes: int,
    description: str = "",
    extraction_status: str = "not_requested",
    extracted_text: str | None = None,
) -> MemoryAttachmentModel:
    """Record one file against one memory."""
    attachment = MemoryAttachmentModel(
        memory_id=memory_id,
        content_hash=content_hash,
        mime_type=mime_type,
        original_name=original_name,
        size_bytes=size_bytes,
        description=description or None,
        extraction_status=extraction_status,
        extracted_text=extracted_text,
    )
    session.add(attachment)
    _commit(session)
    session.refresh(attachment)
    return attachment


def get_attachment(session: Session, attachment_id: uuid.UUID) -> MemoryAttachmentModel | None:
    return session.query(MemoryAttachmentModel).filter_by(id=attachment_id).first()


def list_attachments(session: Session, memory_id: uuid.UUID) -> list[MemoryAttachmentModel]:
    """Everything attached to one memory, oldest first."""
    return list(
        session.query(MemoryAttachmentModel)
        .filter_by(memory_id=memory_id)
        .order_by(MemoryAttachmentModel.created_at.asc())
        .all()
    )


def delete_attachment(session: Session, attachment_id: uuid.UUID) -> bool:
    """Remove the reference. Never the file: another memory may hold it."""
    attachment = get_attachment(session, attachment_id)
    if attachment is None:
        return False
    session.delete(attachment)
    _commit(session)
    return True


def referenced_digests(session: Session) -> set[str]:
    """Every blob some attachment still points at.

    What the collector keeps. Read from the database rather than tracked as
    a count, because a count that drifts deletes somebody's evidence.
    """
    return {row[0] for row in session.query(MemoryAttachmentModel.content_hash).all()}


def attached_bytes(session: Session, memory_id: uuid.UUID) -> int:
    """How much one memory's attachments come to, for the per-memory cap."""
    return sum(a.size_bytes for a in list_attachments(session, memory_id))
